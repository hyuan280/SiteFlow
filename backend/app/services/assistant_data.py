"""Read-only admin data tools; the caller must authorize the session owner first."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import math
import re
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import func, select

from app.models.execution import Run, SiteJob
from app.models.integration import IntegrationConfig
from app.models.setting import AppSetting
from app.models.workflow import (
    Site, SiteCredential, Template, WorkflowGroup, WorkflowSignal, WorkflowStep,
)
from app.services.bitwarden import BitwardenService
from app.services.feature_settings import FeatureSettingsService
from app.services.lanes import LANE_DEFINITIONS, LaneService
from app.services.workflows import WorkflowError, WorkflowNotFoundError, WorkflowService


OUTPUT_LIMIT = 128 * 1024
REDACTED = "[redacted]"
# Consume plain scheme-like runs too, so a failed URL match cannot retry each suffix.
_URL = re.compile(r"(?:[a-z][a-z0-9+.-]*:)?//[^\s<>\"'`]+|[a-z][a-z0-9+.-]*", re.I)
_SENSITIVE = re.compile(
    r"password|passwd|username|credential|cookie|token|apikey|secret|authorization|"
    r"authentication|basicauth|bearer|privatekey|clientkey|accesskey|headers?|llmprompt|"
    r"inline|staticfields|fieldvalues|formfields|^fields$|^values?$|^raw$|^body$|^payload$", re.I,
)
_SITE_FIELDS = (
    "id", "history_key", "name", "base_url", "enabled", "template_id", "lane_policy",
    "support_resign", "resign_max_days", "bitwarden_enabled", "credential_provider_id",
    "checkin_after", "timezone", "config_revision", "created_at", "updated_at",
)
_TEMPLATE_FIELDS = (
    "id", "history_key", "name", "parent_id", "enabled", "version", "created_at", "updated_at",
)
_RUN_FIELDS = ("id", "mode", "trigger", "status", "created_at", "started_at", "finished_at")
_JOB_FIELDS = (
    "id", "run_id", "site_id", "site_name", "lane", "attempt", "status",
    "started_at", "finished_at",
)


def _safe_url(match: re.Match) -> str:
    if "//" not in match.group():
        return match.group()
    try:
        parsed = urlsplit(match.group())
        if parsed.scheme.lower() not in {"", "http", "https", "socks5", "socks5h", "ws", "wss"}:
            return REDACTED
        host = parsed.hostname
        if not host or "\\" in parsed.netloc:
            return REDACTED
        host = f"[{host}]" if ":" in host else host
        if parsed.port is not None:
            host += f":{parsed.port}"
        return urlunsplit((parsed.scheme, host, parsed.path, "", ""))
    except ValueError:
        return REDACTED


def _safe_value(value, *, config=False):
    def walk(item, depth):
        if depth > 40:
            raise ValueError("Configuration nesting exceeds the output policy")
        if isinstance(item, dict):
            result = {}
            for key, child in item.items():
                if not isinstance(key, str):
                    continue
                lowered = key.lower()
                normalized = re.sub(r"[^a-z0-9]", "", lowered)
                if config:
                    # Only reviewed builtin names may bypass the secret-name denylist.
                    selector = lowered in {
                        "username_selector", "password_selector", "token_selector",
                    }
                    metadata = (
                        selector or lowered in {"username_field", "password_field"}
                    ) and (isinstance(child, str) or (
                        selector and isinstance(child, list)
                        and all(isinstance(selector, str) for selector in child)
                    ))
                    if lowered in {"credential_provider_id", "totp_provider_id"}:
                        builtins = ("manual|bitwarden" if lowered == "credential_provider_id"
                                    else "twofauth")
                        if not isinstance(child, str) or not re.fullmatch(
                            rf"(?:builtin\.(?:{builtins})|plugin\.[a-z][a-z0-9-]{{0,62}})", child,
                        ):
                            continue
                    elif _SENSITIVE.search(normalized) and not metadata:
                        continue
                result[_URL.sub(_safe_url, key)] = walk(child, depth + 1)
            return result
        if isinstance(item, (list, tuple)):
            return [walk(child, depth + 1) for child in item]
        if isinstance(item, str):
            # Signed relative URLs are also unsafe, even without an authority.
            if item.startswith("/") and not item.startswith("//"):
                return item.split("?", 1)[0].split("#", 1)[0]
            return _URL.sub(_safe_url, item)
        if isinstance(item, datetime):
            return item.replace(tzinfo=item.tzinfo or timezone.utc).isoformat()
        if item is None or type(item) in {bool, int}:
            return item
        if type(item) is float and math.isfinite(item):
            return item
        return None

    return walk(value, 0)


def redact_config(value):
    """Structural redaction, not generic DLP for arbitrary user-authored prose.

    Drop all literal fill/static-field values, even when their field names look
    harmless. Scrub URL authorities and suffixes in every string, including prose.
    Retain reviewed selector/field-name metadata and valid provider IDs. Prompts remain
    omitted conservatively; this does not classify all non-secret free text.
    """
    return _safe_value(value, config=True)


class _ReadWorkflowService(WorkflowService):
    """Use the workflow resolver with Core rows, never credential-aware views."""

    def _get_site(self, site_id):
        row = self.session.execute(select(
            *(getattr(Site, field) for field in _SITE_FIELDS),
        ).where(Site.id == site_id)).first()
        if row is None:
            raise WorkflowNotFoundError("Site not found")
        return row

    def _get_template(self, template_id):
        row = self.session.execute(select(
            *(getattr(Template, field) for field in _TEMPLATE_FIELDS),
        ).where(Template.id == template_id)).first()
        if row is None:
            raise WorkflowNotFoundError("Template not found")
        return row

    def _owned_rows(self, model, owner_type, owner_id):
        return self.session.execute(select(model.__table__).where(
            model.owner_type == owner_type, model.owner_id == owner_id,
        ).order_by(model.id)).all()

    def _steps(self, owner_type, owner_id):
        return self._owned_rows(WorkflowStep, owner_type, owner_id)

    def _signals(self, owner_type, owner_id):
        return self._owned_rows(WorkflowSignal, owner_type, owner_id)

    def _groups(self, owner_type, owner_id):
        return self._owned_rows(WorkflowGroup, owner_type, owner_id)

    def _feature_settings_service(self):
        if self._feature_settings is None:
            self._feature_settings = _ReadFeatureSettings(self.session)
        return self._feature_settings


class _ReadFeatureSettings(FeatureSettingsService):
    def _row(self, key):
        return self.session.execute(select(AppSetting.key, AppSetting.value_json)
                                    .where(AppSetting.key == key)).first()


class AssistantData:
    def __init__(self, manager):
        self.manager = manager

    @staticmethod
    def tool_definitions() -> list:
        identifier = {"type": "integer", "minimum": 1, "maximum": 9223372036854775807}
        offset = {"type": "integer", "minimum": 0, "maximum": 9223372036854775807}
        limit = {"type": "integer", "minimum": 1, "maximum": 100}
        definitions = []
        for name, description, properties, required in (
            ("list_sites", "Page sites by name.",
             {"query": {"type": "string", "maxLength": 200},
              "offset": dict(offset), "limit": dict(limit)}, []),
            ("get_site_config", "Sanitized site config.",
             {"site_id": dict(identifier)}, ["site_id"]),
            ("list_templates", "Page templates by name.",
             {"query": {"type": "string", "maxLength": 200},
              "offset": dict(offset), "limit": dict(limit)}, []),
            ("get_template_config", "Sanitized template config.",
             {"template_id": dict(identifier)}, ["template_id"]),
            ("get_system_status", "System status; model fields environment-only, not DB.",
             {}, []),
            ("list_recent_runs", "Page recent runs by site.",
             {"site_id": dict(identifier),
              "offset": dict(offset),
              "limit": {"type": "integer", "minimum": 1, "maximum": 20}}, []),
            ("get_run_summary", "Run/job metadata page.",
             {"run_id": dict(identifier), "offset": dict(offset), "limit": dict(limit)}, ["run_id"]),
        ):
            definitions.append({"type": "function", "function": {
                "name": name, "description": description,
                "parameters": {"type": "object", "properties": properties,
                               "required": required, "additionalProperties": False},
            }})
        return definitions

    async def dispatch(self, name: str, args: dict) -> dict:
        definition = next((item["function"] for item in self.tool_definitions()
                           if item["function"]["name"] == name), None)
        if definition is None:
            return {"ok": False, "code": "unknown_tool"}
        schema = definition["parameters"]
        if not isinstance(args, dict) or set(args) - schema["properties"].keys() or any(
            field not in args for field in schema["required"]
        ):
            return {"ok": False, "code": "invalid_arguments"}
        for field, value in args.items():
            rule = schema["properties"][field]
            if rule["type"] == "integer":
                valid = type(value) is int and rule["minimum"] <= value <= rule["maximum"]
            else:
                valid = isinstance(value, str) and len(value) <= rule["maxLength"]
            if not valid:
                return {"ok": False, "code": "invalid_arguments"}
        try:
            with self.manager.session_factory() as db, db.no_autoflush:
                result = self._read(db, name, args)
            if name == "get_system_status":
                result["integrations"].append(await self._bitwarden_status())
                result.update(await self._lane_status())
            # Sanitize before returning anything to the caller/model, including
            # resolver defaults and inherited configuration, not just local JSON.
            result = _safe_value(result)
            if len(json.dumps(result, ensure_ascii=True, allow_nan=False).encode()) > OUTPUT_LIMIT:
                return {"ok": False, "code": "output_limit_exceeded"}
            return result
        except WorkflowNotFoundError:
            return {"ok": False, "code": "not_found"}
        except Exception:
            return {"ok": False, "code": "data_unavailable"}

    def _read(self, db, name, args):
        if name in {"list_sites", "list_templates"}:
            model, fields = (Site, _SITE_FIELDS) if name == "list_sites" else (Template, _TEMPLATE_FIELDS)
            statement = select(*(getattr(model, field) for field in fields))
            if args.get("query"):
                query = args["query"]
                # Search names, never raw URL userinfo/query/fragment secrets.
                statement = statement.where(model.name.icontains(query, autoescape=True))
            offset, limit = args.get("offset", 0), args.get("limit", 100)
            rows = db.execute(statement.order_by(model.name, model.id)
                              .offset(offset).limit(limit + 1)).mappings().all()
            return {"ok": True, "items": [dict(row) for row in rows[:limit]],
                    "offset": offset, "limit": limit,
                    "next_offset": offset + len(rows[:limit]) if len(rows) > limit else None,
                    "truncated": len(rows) > limit}
        if name in {"get_site_config", "get_template_config"}:
            kind = "site" if name == "get_site_config" else "template"
            identifier = args[f"{kind}_id"]
            # No settings/crypto: resolution must not consult/decrypt integrations
            # or credentials. Integration availability is reported separately.
            service = _ReadWorkflowService(db)
            row = service._get_site(identifier) if kind == "site" else service._get_template(identifier)
            metadata = dict(row._mapping)
            if kind == "site":
                metadata["credentials_configured"] = db.scalar(select(
                    select(SiteCredential.site_id).where(SiteCredential.site_id == identifier).exists()
                ))
            local = {
                "steps": [service._step_view(step).model_dump()
                          for step in service._steps(kind, identifier)],
                "groups": [service._group_view(group).model_dump()
                           for group in service._groups(kind, identifier)],
                "signals": [service._signal_view(signal).model_dump()
                            for signal in service._signals(kind, identifier)],
            }
            signals = (service._resolved_site_signals(row) if kind == "site"
                       else service._resolved_template_signals(row))
            resolved, error = None, None
            try:
                resolved = (service.resolve_site(identifier) if kind == "site"
                            else service.resolve_template(identifier)).model_dump()
                resolved.pop("warnings", None)
            except WorkflowError:
                error = "workflow_invalid"
            # Descriptions/notes/errors are not needed for configuration inspection.
            for signal in local["signals"]:
                signal.pop("description", None)
            resolved_signals = [signal.model_dump(exclude={"description"}) for signal in signals]
            if resolved is not None:
                resolved["signals"] = resolved_signals
            return {"ok": True, kind: metadata, "local": redact_config(local),
                    "resolved": redact_config(resolved),
                    "resolved_signals": redact_config(resolved_signals), "resolution_error": error}
        if name == "get_system_status":
            return self._system_status(db)
        if name == "list_recent_runs":
            statement = select(*(getattr(Run, field) for field in _RUN_FIELDS))
            if "site_id" in args:
                statement = statement.where(select(SiteJob.id).where(
                    SiteJob.run_id == Run.id, SiteJob.site_id == args["site_id"],
                ).exists())
            offset, limit = args.get("offset", 0), args.get("limit", 20)
            rows = db.execute(statement.order_by(Run.created_at.desc(), Run.id.desc())
                              .offset(offset).limit(limit + 1)).mappings().all()
            return {"ok": True, "items": [self._run_summary(db, dict(row)) for row in rows[:limit]],
                    "offset": offset, "limit": limit,
                    "next_offset": offset + len(rows[:limit]) if len(rows) > limit else None,
                    "truncated": len(rows) > limit}
        row = db.execute(select(*(getattr(Run, field) for field in _RUN_FIELDS))
                         .where(Run.id == args["run_id"])).mappings().first()
        if row is None:
            raise WorkflowNotFoundError("Run not found")
        summary = self._run_summary(db, dict(row))
        offset, limit = args.get("offset", 0), args.get("limit", 100)
        jobs = db.execute(select(*(getattr(SiteJob, field) for field in _JOB_FIELDS))
                          .where(SiteJob.run_id == row["id"])
                          .order_by(SiteJob.id).offset(offset).limit(limit + 1)).mappings().all()
        return {"ok": True, "run": summary, "jobs": [dict(job) for job in jobs[:limit]],
                "offset": offset, "limit": limit,
                "next_offset": offset + len(jobs[:limit]) if len(jobs) > limit else None,
                "jobs_truncated": len(jobs) > limit}

    @staticmethod
    def _run_summary(db, row):
        counts = db.execute(select(SiteJob.status, func.count()).where(
            SiteJob.run_id == row["id"],
        ).group_by(SiteJob.status)).all()
        row["job_counts"] = {status: count for status, count in counts}
        row["job_count"] = sum(count for _, count in counts)
        return row

    def _system_status(self, db):
        settings = self.manager.settings
        # Other integrations remain metadata-only; Bitwarden uses its settings
        # view separately, with an explicit safe projection and its own session.
        fields = {
            "proxy": ("url",), "twofauth": ("url", "token"),
            "cfbox": ("url", "api_key"),
            "vision": ("base_url", "api_key", "model", "supports_vision", "max_tokens"),
        }
        keys = {f"{name}.{field}" for name, items in fields.items() for field in items}
        present = set(db.scalars(select(IntegrationConfig.key).where(IntegrationConfig.key.in_(keys))))
        integrations = []
        for name, items in fields.items():
            sources = {}
            for field in items:
                value = getattr(settings, f"{name}_{field}", None)
                is_set = (value is not None
                          if field in {"supports_vision", "max_tokens"} else bool(value))
                sources[field] = "env" if is_set else "db" if f"{name}.{field}" in present else "unset"
            required = ("url",) if name in {"proxy", "cfbox"} else (
                ("base_url", "api_key", "model") if name == "vision" else
                ("url", "token")
            )
            integrations.append({
                "name": name, "configured": all(sources[field] != "unset" for field in required),
                "sources": sources, "available": None, "health_checked": False,
            })
        model = getattr(settings, "vision_model", None)
        # Model IDs only, never free text or provider endpoints.
        safe_model = (model if isinstance(model, str)
                      and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", model)
                      and "://" not in model else None)
        return {"ok": True, "config_ai_enabled": bool(settings.config_ai_enabled),
                "integrations": integrations, "health_checked": False,
                "health_scope": ["bitwarden"],
                "model_config_scope": "environment_only",
                "model": safe_model, "supports_vision": settings.vision_supports_vision,
                "max_tokens": settings.vision_max_tokens}

    async def _bitwarden_status(self):
        def read():
            # Construct, use and close the session in the worker thread. Never
            # return the settings view itself: it contains private endpoint data.
            with self.manager.session_factory() as db, db.no_autoflush:
                view = BitwardenService(
                    db, self.manager.settings, self.manager.crypto,
                ).view(force=False, deep=False)
                return {
                    "name": "bitwarden", "enabled": view.enabled,
                    "enabled_setting": view.enabled_setting,
                    "available": view.available, "configured": view.configured,
                    "auto_detected": view.auto_detected, "source": view.source.value,
                    "enabled_source": view.enabled_source.value,
                    "url": {"source": view.url.source.value},
                    "token": {"configured": view.token.configured,
                              "source": view.token.source.value},
                    "sources": {"url": view.url.source.value,
                                "token": view.token.source.value,
                                "enabled": view.enabled_source.value},
                    "health_checked": True,
                }

        return await asyncio.to_thread(read)

    async def _lane_status(self):
        try:
            response = await LaneService(self.manager.settings).get_lanes()
            lanes = []
            allowed = {item["key"] for item in LANE_DEFINITIONS}
            states = {"running", "stopped", "starting", "stopping", "ready", "busy", "idle",
                      "unavailable", "error", "disabled"}
            for lane in response.lanes:
                if lane.key not in allowed:
                    continue
                lanes.append({"key": lane.key, "enabled": lane.enabled,
                              "status": lane.status if lane.status in states else "unknown",
                              "in_use": max(0, lane.in_use),
                              "max_concurrency": max(0, lane.max_concurrency)})
            return {"worker_connected": response.worker_connected, "lanes": lanes}
        except Exception:
            return {"worker_connected": False, "lanes": []}
