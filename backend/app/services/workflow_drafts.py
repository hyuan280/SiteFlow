from __future__ import annotations

import json

from sqlalchemy import select

from app.models.integration import IntegrationConfig
from app.models.setting import AppSetting
from app.models.workflow import Site, Template, WorkflowGroup, WorkflowSignal, WorkflowStep
from app.schemas.workflow import SiteWrite, TemplateWrite
from app.services.feature_settings import FeatureSettingsService
from app.services.integrations import IntegrationService
from app.services.workflows import (
    OWNER_SITE,
    OWNER_TEMPLATE,
    WorkflowService,
    WorkflowValidationError,
)


def _fresh_row(session, model, key):
    column = next(iter(model.__table__.primary_key.columns))
    values = session.execute(select(model.__table__).where(column == key)).mappings().first()
    return model(**values) if values is not None else None


class _DraftFeatureSettings(FeatureSettingsService):
    def _row(self, key):
        return _fresh_row(self.session, AppSetting, key)


class _DraftIntegrations(IntegrationService):
    def _row(self, key):
        return _fresh_row(self.session, IntegrationConfig, key)


class _DraftWorkflowService(WorkflowService):
    candidate_site: Site | None = None

    def _feature_settings_service(self):
        if self._feature_settings is None:
            self._feature_settings = _DraftFeatureSettings(self.session)
        return self._feature_settings

    def _cfbox_enabled(self):
        if self.settings is None or self.crypto is None:
            return True
        if not self._cfbox_loaded:
            self._cfbox_value = _DraftIntegrations(self.session, self.settings, self.crypto).cfbox_enabled()
            self._cfbox_loaded = True
        return self._cfbox_value

    def _get_site(self, site_id: int) -> Site:
        if self.candidate_site is not None and self.candidate_site.id == site_id:
            return self.candidate_site
        from app.services.workflows import WorkflowNotFoundError

        values = self.session.execute(select(Site.__table__).where(Site.id == site_id)).mappings().first()
        if values is None:
            raise WorkflowNotFoundError(f"站点 {site_id} 不存在")
        return Site(**values)

    def _preload_site_list(self) -> None:
        # Core rows bypass the caller's identity map without refreshing its pending state.
        self._templates_by_id = {
            values["id"]: Template(**values)
            for values in self.session.execute(select(Template.__table__)).mappings()
        }
        for model, attribute, ordering in (
            (WorkflowStep, "_steps_by_owner", (WorkflowStep.id,)),
            (WorkflowSignal, "_signals_by_owner", (WorkflowSignal.id,)),
            (WorkflowGroup, "_groups_by_owner", (WorkflowGroup.position, WorkflowGroup.id)),
        ):
            rows = {}
            for values in self.session.execute(select(model.__table__).order_by(*ordering)).mappings():
                row = model(**values)
                rows.setdefault((row.owner_type, row.owner_id), []).append(row)
            setattr(self, attribute, rows)


def validate_draft(
    service: WorkflowService,
    owner_type: str,
    payload: SiteWrite | TemplateWrite,
    owner_id: int | None,
    *,
    preserve_local: bool = False,
) -> dict:
    model = {OWNER_SITE: SiteWrite, OWNER_TEMPLATE: TemplateWrite}.get(owner_type)
    if model is None or not isinstance(payload, model):
        raise WorkflowValidationError("Invalid workflow type or payload")

    # No caches or mutable inputs from the caller participate in candidate resolution.
    context = _DraftWorkflowService(service.session, service.settings, service.crypto, service.actor)
    payload = payload.model_copy(deep=True)
    if owner_type == OWNER_SITE and owner_id is None and payload.template_id is None:
        context._templates_by_id = {}
        context._steps_by_owner = {}
        context._groups_by_owner = {}
        context._signals_by_owner = {}
    else:
        context._preload_site_list()
    existing = context._history_owner(owner_type, owner_id) if owner_id is not None else None
    if existing is not None:
        context._check_revision(existing, payload.expected_revision, payload.expected_history_key)
    owner_model = Template if owner_type == OWNER_TEMPLATE else Site
    context._ensure_name_available(owner_model, payload.name, exclude_id=owner_id)
    parent_id = payload.parent_id if isinstance(payload, TemplateWrite) else payload.template_id
    context._prepare_local_inputs(
        owner_type, payload, parent_id, existing, preserve_positions=preserve_local)

    candidate_id = owner_id
    if candidate_id is None:
        # Never expose this lookup-only identity or allocate a database identity.
        candidate_id = min([0, *context._templates_by_id]) - 1
    values = payload.model_dump(exclude={
        "expected_revision", "expected_history_key", "steps", "groups", "signals",
    })
    if isinstance(payload, SiteWrite):
        values["tags_json"] = json.dumps(values.pop("tags"), ensure_ascii=False)
        values["trusted_urls_json"] = json.dumps(values.pop("trusted_urls"), ensure_ascii=False)
        if existing is not None and "trusted_urls" not in payload.model_fields_set:
            values["trusted_urls_json"] = existing.trusted_urls_json
    candidate = owner_model(id=candidate_id, **values)

    groups = sorted(context._group_rows(
        owner_type, candidate_id, payload.groups, preserve_null=preserve_local), key=lambda row: row.position)
    steps = context._step_rows(owner_type, candidate_id, payload.steps)
    signals = context._signal_rows(owner_type, candidate_id, payload.signals)

    if owner_type == OWNER_TEMPLATE:
        context._templates_by_id[candidate_id] = candidate
    else:
        context.candidate_site = candidate
    key = (owner_type, candidate_id)
    context._groups_by_owner[key] = groups
    context._steps_by_owner[key] = steps
    context._resolved_template_steps_cache.clear()
    context._resolved_template_groups_cache.clear()
    context._resolved_template_signals_cache.clear()
    context._resolved_site_signals_cache.clear()
    # Removal checks need the OLD local signals but the NEW parent and steps.
    if existing is not None:
        context._check_removed_signals(owner_type, candidate_id, payload.signals)
    context._signals_by_owner[key] = signals
    if owner_type == OWNER_TEMPLATE:
        context.resolve_template(candidate_id)
        context._validate_affected_descendants(candidate_id)
    else:
        context.resolve_site(candidate_id)
    # This is the same read-only preparation used before encrypted history writes.
    snapshot = context._snapshot(candidate)
    resolved = snapshot["resolved"]
    resolved["owner_id"] = owner_id
    return {
        "valid": True,
        "owner_type": owner_type,
        "owner_id": owner_id,
        "history_key": existing.history_key if existing is not None else None,
        "current_revision": context._revision(existing) if existing is not None else None,
        "local": snapshot["local"],
        "resolved": resolved,
    }
