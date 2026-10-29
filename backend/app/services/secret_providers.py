from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
import threading
import time

import httpx
import grpc
import json
from siteflow_proto import plugin_pb2, plugin_pb2_grpc
from app.models.plugin import PluginRegistration
from app.services.plugin_runtime import PluginRuntimeService, validate_plugin_endpoint

from app.models.execution import Run, SiteJob
from app.models.workflow import Site
from app.services.bitwarden import BitwardenService
from app.services.credentials import CredentialService
from app.services.integrations import IntegrationService
from app.services.plugin_audit import record

logger = logging.getLogger("siteflow.secrets")


class SecretDenied(ValueError):
    pass


def credential_provider(site):
    return getattr(site, "credential_provider_id", None) or ("builtin.bitwarden" if site.bitwarden_enabled else "builtin.manual")


def step_provider(site, step):
    return credential_provider(site) if step.feature_type == "auth.form_login" else step.config.get("totp_provider_id") or "builtin.twofauth"


class SecretProviders:
    """Process-local, fail-closed capabilities; no secret values are cached."""

    def __init__(self, session_factory, settings, crypto):
        self.session_factory = session_factory
        self.settings = settings
        self.crypto = crypto
        self.lock = threading.Lock()
        self.jobs = {}
        self.leases = {}
        self.plugin_jobs = {}

    def register_plugins(self, job, runtimes):
        token = secrets.token_urlsafe(32)
        with self.lock:
            self.plugin_jobs[job.id] = (self.digest(token), job.attempt, time.monotonic() + self.settings.execution_timeout,
                                       {key: (value["generation"], value["permission_epoch"]) for key, value in runtimes.items()})
        return token

    def authorize_plugin(self, token, job_id, plugin_id, generation, capability=None):
        with self.lock:
            grant = self.plugin_jobs.get(job_id)
        if not grant or not secrets.compare_digest(grant[0], self.digest(token)) or grant[2] <= time.monotonic() or plugin_id not in grant[3] or grant[3][plugin_id][0] != generation:
            raise SecretDenied()
        with self.session_factory() as session:
            job = session.get(SiteJob, job_id)
            run = session.get(Run, job.run_id) if job else None
            if not job or not run or run.cancel_requested or job.attempt != grant[1] or job.status not in {"queued", "running"}:
                raise SecretDenied()
            try:
                runtime = PluginRuntimeService(session, self.settings).runtime_for_plugin(plugin_id)
            except Exception:
                raise SecretDenied() from None
            if (runtime["generation"], runtime["permission_epoch"]) != grant[3][plugin_id] or (capability and capability not in runtime["approved_capabilities"]):
                raise SecretDenied()
            report = PluginRuntimeService(session, self.settings).references(plugin_id)
            if any(item["owner_type"] == "site" and item["owner_id"] == job.site_id and item["enabled"] and item["errors"] for item in report["items"]):
                raise SecretDenied()

    @staticmethod
    def digest(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def register(self, job, site, steps):
        token = secrets.token_urlsafe(32)
        allowed = {}
        for step in steps:
            if step.enabled and step.feature_type in {"auth.form_login", "challenge.2fa"}:
                provider = step_provider(site, step)
                options = dict(step.config)
                options.update(base_url=site.base_url, bitwarden_match_mode=site.bitwarden_match_mode, bitwarden_custom_match=site.bitwarden_custom_match)
                if provider.startswith("plugin."):
                    with self.session_factory() as session:
                        row = session.get(PluginRegistration, provider[7:])
                        options["plugin_scope"] = (row.generation, row.permission_epoch) if row else None
                allowed[step.step_key] = (step.feature_type, provider, options)
        with self.lock:
            self.leases = {key: value for key, value in self.leases.items() if value[0] != job.id}
            self.jobs[job.id] = (self.digest(token), site.id, job.attempt, time.monotonic() + self.settings.execution_timeout, allowed)
        return token

    def revoke(self, job_id):
        with self.lock:
            self.jobs.pop(job_id, None)
            self.plugin_jobs.pop(job_id, None)
            self.leases = {key: value for key, value in self.leases.items() if value[0] != job_id}

    def clear(self):
        with self.lock:
            self.jobs.clear()
            self.plugin_jobs.clear()
            self.leases.clear()

    async def request(self, token, job_id, site_id, step_key, purpose, provider, lease=None):
        try:
            result = await self._request(token, job_id, site_id, step_key, purpose, provider, lease)
        except SecretDenied:
            # Rejected caller-controlled scope is deliberately not persisted.
            with self.session_factory() as session:
                record(session, "lease_reject", "denied", "scope_denied")
                session.commit()
            raise
        with self.session_factory() as session:
            scope = dict(job_id=job_id, site_id=site_id, step_key=step_key, provider_id=provider,
                         plugin_id=provider[7:] if provider.startswith("plugin.") else None)
            if lease is None:
                record(session, "lease_issue", **scope)
            if lease is not None:
                record(session, "provider_resolve", **scope)
            session.commit()
        return result

    async def _request(self, token, job_id, site_id, step_key, purpose, provider, lease=None):
        now = time.monotonic()
        scope = (job_id, site_id, step_key, purpose, provider)
        with self.lock:
            self.leases = {key: value for key, value in self.leases.items() if value[-1] > now}
            grant = self.jobs.get(job_id)
            if not grant or not secrets.compare_digest(grant[0], self.digest(token)) or grant[1] != site_id or grant[3] <= now:
                raise SecretDenied()
            step = grant[4].get(step_key)
            if not step or step[:2] != (purpose, provider):
                raise SecretDenied()
            if lease is not None:
                key = self.digest(lease)
                entry = self.leases.get(key)
                if not entry or entry[:-2] != scope or entry[-2] != grant[0]:
                    raise SecretDenied()
                del self.leases[key]
        with self.session_factory() as session:
            job = session.get(SiteJob, job_id)
            run = session.get(Run, job.run_id) if job else None
            site = session.get(Site, site_id)
            if not job or not run or not site or run.cancel_requested or job.site_id != site_id or job.attempt != grant[2] or job.status not in {"queued", "running"}:
                raise SecretDenied()
            if provider.startswith("plugin."):
                try:
                    runtime = PluginRuntimeService(session, self.settings).runtime_for_plugin(provider[7:])
                    capability = "secret.credentials" if purpose == "auth.form_login" else "secret.totp"
                    if capability not in runtime["approved_capabilities"] or (runtime["generation"], runtime["permission_epoch"]) != step[2].get("plugin_scope"):
                        raise SecretDenied()
                except Exception:
                    raise SecretDenied() from None
            if lease is None:
                value = secrets.token_urlsafe(32)
                with self.lock:
                    if self.jobs.get(job_id) is not grant:
                        raise SecretDenied()
                    self.leases = {key: entry for key, entry in self.leases.items() if entry[:-2] != scope}
                    self.leases[self.digest(value)] = (*scope, grant[0], now + 30)
                return {"lease": value, "expires_in": 30}
            record(session, "lease_consume", job_id=job_id, site_id=site_id, step_key=step_key, provider_id=provider, plugin_id=provider[7:] if provider.startswith("plugin.") else None)
            session.commit()
            try:
                if provider == "builtin.manual":
                    secret = CredentialService(session, self.crypto).resolve(site_id)
                    result = {"username": secret.username, "password": secret.password}
                elif provider == "builtin.bitwarden":
                    secret = await asyncio.to_thread(self._bitwarden, step[2])
                    result = {"username": secret.username, "password": secret.password}
                elif provider == "builtin.twofauth":
                    integrations = IntegrationService(session, self.settings, self.crypto)
                    url = integrations.resolve_twofauth_url().value
                    api_token = integrations.resolve_twofauth_token().value
                    hint = str(step[2].get("service_hint") or "").strip().casefold()
                    if not url or not api_token or not hint:
                        raise SecretDenied()
                    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
                        headers = {"Authorization": f"Bearer {api_token}"}
                        response = await client.get(f"{url.rstrip('/')}/api/v1/twofaccounts", headers=headers)
                        response.raise_for_status()
                        body = response.json()
                        accounts = body if isinstance(body, list) else body.get("data", [])
                        username = str(step[2].get("username_hint") or "").strip().casefold()
                        if username:
                            accounts = [a for a in accounts if str(a.get("account") or "").strip().casefold() == username]
                        exact = [a for a in accounts if str(a.get("service") or "").strip().casefold() == hint]
                        matches = exact if len(exact) == 1 else [a for a in accounts if hint in str(a.get("service") or "").casefold() or hint in str(a.get("account") or "").casefold()]
                        if len(matches) != 1:
                            raise SecretDenied()
                        response = await client.get(f"{url.rstrip('/')}/api/v1/twofaccounts/{int(matches[0]['id'])}/otp", headers=headers)
                        response.raise_for_status()
                        body = response.json()
                        code = str(body.get("password") or body.get("otp") or "")
                        if not code.isdigit() or not 4 <= len(code) <= 10:
                            raise SecretDenied()
                        result = {"code": code}
                else:
                    if purpose == "challenge.2fa" and not str(step[2].get("service_hint") or "").strip():
                        raise SecretDenied()
                    result = await asyncio.to_thread(self._remote, provider, purpose, job_id, site_id, step_key, site.base_url, step[2])
            except Exception:
                record(session, "provider_resolve", "failed", "provider_failed", job_id=job_id, site_id=site_id, step_key=step_key, provider_id=provider, plugin_id=provider[7:] if provider.startswith("plugin.") else None)
                session.commit()
                logger.warning("secret resolve job=%s site=%s step=%s provider=%s outcome=failed", job_id, site_id, step_key, provider)
                raise SecretDenied() from None
        with self.lock:
            if self.jobs.get(job_id) is not grant:
                raise SecretDenied()
        with self.session_factory() as session:
            job = session.get(SiteJob, job_id)
            run = session.get(Run, job.run_id) if job else None
            if not job or not run or run.cancel_requested or job.status not in {"queued", "running"} or job.attempt != grant[2]:
                raise SecretDenied()
            if provider.startswith("plugin."):
                try:
                    runtime = PluginRuntimeService(session, self.settings).runtime_for_plugin(provider[7:])
                    if (runtime["generation"], runtime["permission_epoch"]) != step[2].get("plugin_scope"):
                        raise SecretDenied()
                except Exception:
                    raise SecretDenied() from None
        logger.info("secret resolve job=%s site=%s step=%s provider=%s outcome=success", job_id, site_id, step_key, provider)
        return result

    def _bitwarden(self, options):
        from types import SimpleNamespace
        with self.session_factory() as session:
            return BitwardenService(session, self.settings, self.crypto).resolve_for_site(SimpleNamespace(**options))

    def _remote(self, provider, purpose, job_id, site_id, step_key, origin, options):
        if not provider.startswith("plugin."):
            raise SecretDenied()
        plugin_id = provider[len("plugin."):]
        capability = "secret.credentials" if purpose == "auth.form_login" else "secret.totp"
        with self.session_factory() as session:
            row = session.get(PluginRegistration, plugin_id)
            if not row or capability not in json.loads(row.capabilities_json):
                raise SecretDenied()
            runtime = PluginRuntimeService(session, self.settings).runtime_for_plugin(plugin_id)
            generation = row.generation
            epoch = row.permission_epoch
            endpoint = validate_plugin_endpoint(runtime["endpoint"], self.settings.plugin_endpoint_allowlist_values(), resolve=self.settings.plugin_resolve_endpoint)
            with grpc.insecure_channel(endpoint, options=(("grpc.enable_http_proxy", 0),)) as channel:
                stub = plugin_pb2_grpc.SecretProviderStub(channel)
                rpc = stub.ResolveCredential if capability == "secret.credentials" else stub.GenerateTotp
                call = rpc.future(plugin_pb2.SecretProviderRequest(
                    job_id=job_id, site_id=site_id, step_key=step_key, site_origin=origin,
                    match_mode=options.get("bitwarden_match_mode") or "domain",
                    custom_match=options.get("bitwarden_custom_match") or "",
                    service_hint=options.get("service_hint") or "",
                    username_hint=options.get("username_hint") or ""),
                             timeout=20, metadata=(("x-plugin-token", runtime["token"]),))
                try:
                    while True:
                        with self.lock:
                            granted = job_id in self.jobs
                        session.expire_all()
                        job = session.get(SiteJob, job_id)
                        run = session.get(Run, job.run_id) if job else None
                        current = PluginRuntimeService(session, self.settings).runtime_for_plugin(plugin_id)
                        if not granted or not job or not run or run.cancel_requested or job.status not in {"queued", "running"} or (current["generation"], current["permission_epoch"]) != (generation, epoch):
                            raise SecretDenied()
                        try:
                            value = call.result(timeout=0.1)
                            break
                        except grpc.FutureTimeoutError:
                            continue
                finally:
                    call.cancel()
            # Recheck approval after the remote call before releasing a secret.
            session.expire_all()
            PluginRuntimeService(session, self.settings).runtime_for_plugin(plugin_id)
            if row.generation != generation or row.permission_epoch != epoch or capability not in json.loads(row.approved_capabilities_json):
                raise SecretDenied()
            if capability == "secret.credentials":
                if not value.username or not value.password or max(len(value.username), len(value.password)) > 4096:
                    raise SecretDenied()
                return {"username": value.username, "password": value.password}
            if not value.code.isdigit() or not 4 <= len(value.code) <= 10:
                raise SecretDenied()
            return {"code": value.code}
