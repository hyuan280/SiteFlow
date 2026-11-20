"""Short-lived process-local grants, separate from durable job capabilities."""

import hashlib
import secrets
import threading
import time

from app.services.secret_providers import SecretDenied


class AssistantPluginAuthorization:
    def __init__(self):
        self._grants = {}
        self._lock = threading.RLock()

    def register(self, execution, scope, step, runtimes):
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self._lock:
            now = time.monotonic()
            self._grants = {k: v for k, v in self._grants.items() if v[0] > now}
            if len(self._grants) >= 1024:
                raise SecretDenied()
            self._grants[digest] = (min(scope.deadline, now + 300), execution, scope, step,
                                    {key: value["generation"] for key, value in runtimes.items()})
        return token

    def revoke(self, token):
        with self._lock:
            self._grants.pop(hashlib.sha256(token.encode()).hexdigest(), None)

    def authorize(self, token, session_id, operation_id, plugin_id, generation, capability):
        with self._lock:
            grant = self._grants.get(hashlib.sha256(token.encode()).hexdigest())
            if grant is None:
                raise SecretDenied()
            deadline, execution, scope, step, generations = grant
            if (deadline <= time.monotonic() or not execution._reserved
                    or execution.session_id != session_id or execution.operation_id != operation_id
                    or generations.get(plugin_id) != generation):
                raise SecretDenied()
            feature = step["feature_type"]
            method = step["config"].get("captcha_method", "")
            expected = set()
            if feature.startswith(f"plugin.{plugin_id}."):
                expected.add("features")
            if method.startswith(f"plugin.{plugin_id}."):
                expected.add("captcha_methods")
            if capability not in expected:
                raise SecretDenied()
            try:
                execution._authorize(scope, feature)
            except Exception:
                raise SecretDenied() from None

    def authorize_resign(self, token, job_id, site_id, step_key, db):
        from app.models.execution import Run, SiteJob

        with self._lock:
            grant = self._grants.get(hashlib.sha256(token.encode()).hexdigest())
            if grant is None:
                raise SecretDenied()
            deadline, execution, scope, step, _ = grant
            if (deadline <= time.monotonic() or not execution._reserved
                    or execution._job_id != job_id or step["step_key"] != step_key):
                raise SecretDenied()
            job = db.get(SiteJob, job_id, populate_existing=True)
            run = db.get(Run, job.run_id, populate_existing=True) if job else None
            if (job is None or job.site_id != site_id or job.status != "running"
                    or run is None or run.status != "running" or run.cancel_requested):
                raise SecretDenied()
            try:
                execution._authorize(scope, step["feature_type"], db=db)
            except Exception:
                raise SecretDenied() from None
            return job, step["feature_type"]


assistant_plugin_authorization = AssistantPluginAuthorization()
