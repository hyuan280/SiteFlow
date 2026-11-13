"""Frozen, explicitly reviewed template imports. The interaction caller commits
the template, encrypted action receipt and session decision together. Neither
prepare nor apply commits or modifies the session target.
"""

from copy import deepcopy
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, OperationalError

from app.models.assistant_workflow import AssistantWorkflowAction
from app.models.workflow import Site, Template
from app.schemas.workflow import TemplateWrite
from app.services.template_site_import import site_import_candidate, template_import_write
from app.services.workflows import WorkflowError


def fingerprint(value):
    from app.services.assistant_retry import fingerprint as canonical_fingerprint

    return canonical_fingerprint(value)


class TemplateFromSiteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    operation_id: UUID
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=4000)
    destination_template_id: int | None = Field(default=None, ge=1)
    expected_revision: int | None = Field(default=None, ge=1)
    expected_history_key: UUID | None = None

    @field_validator("operation_id", "expected_history_key", mode="before")
    @classmethod
    def identifier(cls, value):
        return UUID(value) if isinstance(value, str) else value

    @model_validator(mode="after")
    def destination(self):
        if not self.name.strip() or self.name != self.name.strip():
            raise ValueError("A nonblank, exact name is required")
        if self.destination_template_id is not None:
            if self.expected_revision is None or self.expected_history_key is None:
                raise ValueError("Replacing a template requires revision and identity")
        elif self.expected_revision is not None or self.expected_history_key is not None:
            raise ValueError("New templates have no existing revision or identity")
        return self


def canonical(request):
    return request.model_dump(mode="json", include=set(TemplateFromSiteRequest.model_fields))


def action_for(db, workflows, operation_id):
    return db.scalar(select(AssistantWorkflowAction).where(
        AssistantWorkflowAction.session_id == workflows.session_id,
        AssistantWorkflowAction.owner == workflows.owner,
        AssistantWorkflowAction.kind == "template_import",
        AssistantWorkflowAction.request_fingerprint == fingerprint({"operation_id": operation_id})))


def _context(db, tools, data):
    from app.services.assistant_site_config import check_context
    from app.services.assistant_workflows import AssistantWorkflows

    site = check_context(db, tools, data, True)
    workflows = AssistantWorkflows(tools.manager, tools.owner, tools.session_id, data["target"])
    return site, workflows, workflows._service(db)


def _state(service, site, destination_id):
    from app.services.assistant_workflows import workflow_binding

    destination = service._get_template(destination_id) if destination_id is not None else None
    return {
        "source": {"binding": workflow_binding(site), "snapshot": service._snapshot(site)},
        "destination": None if destination is None else {
            "id": destination.id, "history_key": destination.history_key,
            "revision": destination.version, "snapshot": service._snapshot(destination)},
    }


def _validate_replacement(service, write, destination_id):
    from app.services.assistant_workflows import WorkflowDenied
    from app.services.workflow_drafts import _DraftWorkflowService

    if destination_id is None:
        return
    with service.session.no_autoflush:
        context = _DraftWorkflowService(
            service.session, service.settings, service.crypto, service.actor)
        context._preload_site_list()
        before = context.resolve_template(destination_id).model_dump(mode="json")
        after = service.validate_draft("template", write, destination_id, preserve_local=True)["resolved"]
    # Compare effective identities and relative order, not numeric placement or
    # local rows: changing parents must not remove inherited structure either.
    for field, key, retained in (("groups", "group_key", "name"),
                                ("steps", "step_key", "group_key")):
        originals = {item[key]: item[retained] for item in before[field]}
        replacements = {item[key]: item[retained] for item in after[field]}
        if (any(identity not in replacements or replacements[identity] != value
                for identity, value in originals.items())
                or list(originals) != [identity for identity in replacements if identity in originals]):
            raise WorkflowDenied("template_import_structure_changed")


def _check_public_workflow(service, value, *, base_url, api_key):
    from app.services.assistant_site_config import unsafe_site_config
    from app.services.assistant_workflows import WorkflowDenied, _contains_protected
    from app.services.config_ai_catalog import ConfigAICatalog

    if _contains_protected(value, base_url=base_url) or unsafe_site_config(value, api_key):
        raise WorkflowDenied("protected_config_changed")
    catalog = ConfigAICatalog(service.session)
    for step in value["steps"]:
        schema = catalog.get_feature_schema(step["feature_type"])
        protected = set(schema["excluded_fields"]) | {"llm_prompt", "llm_enabled"}
        if _contains_protected(step["config"], protected, base_url=base_url):
            raise WorkflowDenied("protected_config_changed")


def prepare_template_import(db, tools, request, data):
    from app.services.assistant_site_config import unsafe_site_config
    from app.services.assistant_workflows import (
        WorkflowDenied,
        workflow_binding,
    )

    args = canonical(request)
    if unsafe_site_config(args, tools.config.get("api_key", "")):
        raise WorkflowDenied("secret_input")
    site, workflows, service = _context(db, tools, data)
    if action_for(db, workflows, args["operation_id"]) is not None:
        raise WorkflowDenied("template_import_operation_conflict")
    candidate = site_import_candidate(service, site.id)
    candidate_public = candidate.model_dump(mode="json")
    _check_public_workflow(service, candidate_public, base_url=site.base_url,
                          api_key=tools.config.get("api_key", ""))
    write = template_import_write(
        service, candidate, name=args["name"], description=args["description"],
        destination_id=args["destination_template_id"], expected_revision=args["expected_revision"],
        expected_history_key=args["expected_history_key"])
    _validate_replacement(service, write, args["destination_template_id"])
    if unsafe_site_config({"name": write.name, "description": write.description}, tools.config.get("api_key", "")):
        raise WorkflowDenied("protected_config_changed")
    state = _state(service, site, args["destination_template_id"])
    before = None
    if state["destination"] is not None:
        local = state["destination"]["snapshot"]["local"]
        before = deepcopy({key: local[key] for key in ("parent_id", "steps", "groups", "signals")})
        _check_public_workflow(service, before, base_url=site.base_url,
                              api_key=tools.config.get("api_key", ""))
    public = {
        "kind": "authorization", "purpose": "template_import", "options": [], "fields": [],
        "multiple": False, "title": "确认从站点导入模板",
        "description": "仅导入本地步骤、分组和信号，并将来源站点的模板设为父模板；不会修改来源站点。",
        "review": {"source_site_id": site.id, "destination_template_id": args["destination_template_id"],
                   "name": write.name, "description": write.description, "enabled": write.enabled,
                    **candidate_public, "before": before,
                    "warnings": [
                        "已有目标模板的名称、说明和启用状态保持不变；本地工作流整体替换，不与旧步骤合并。",
                        "已有分组的名称和相对顺序、已有步骤的分组和相对顺序保持不变；允许插入新分组和步骤。",
                        "本操作不会运行网站；引用此模板的站点和子模板会受到影响。",
                    ]},
    }
    private = {"request": args, "source_binding": workflow_binding(site),
               "state_hash": fingerprint(state), "write": write.model_dump(mode="json"),
               "state": state}
    return public, private, "template_import:" + fingerprint({
        "source": workflow_binding(site), "destination": args["destination_template_id"],
        "write": private["write"]})


def apply_template_import(db, tools, data, args):
    from app.services.assistant_workflows import WorkflowDenied, workflow_binding

    request = args["request"]
    site, workflows, service = _context(db, tools, data)
    if workflow_binding(site) != args["source_binding"]:
        raise WorkflowDenied("workflow_binding_changed")
    # No-op conditional updates serialize reads with workflow writes without
    # changing revisions, timestamps, local rows or history on the source.
    owners = [(Site, site.id, site.history_key, site.config_revision)]
    for item in (args["state"]["source"], args["state"]["destination"]):
        if item is None:
            continue
        owners.extend((Template, dep["owner_id"], dep["history_key"], dep["revision"])
                      for dep in item["snapshot"]["dependencies"])
    destination = args["state"]["destination"]
    if destination is not None:
        owners.append((Template, destination["id"], destination["history_key"], destination["revision"]))
    for model, owner_id, history_key, revision in sorted(set(owners), key=lambda x: (x[0].__name__, x[1])):
        column = model.config_revision if model is Site else model.version
        changed = db.execute(update(model).where(
            model.id == owner_id, model.history_key == history_key, column == revision,
        ).values({column.key: column}).execution_options(synchronize_session=False))
        if changed.rowcount != 1:
            raise WorkflowDenied("template_import_changed")
    db.expire_all()
    site, workflows, service = _context(db, tools, data)
    site_import_candidate(service, site.id)
    if fingerprint(_state(service, site, request["destination_template_id"])) != args["state_hash"]:
        raise WorkflowDenied("template_import_changed")
    if action_for(db, workflows, request["operation_id"]) is not None:
        raise WorkflowDenied("template_import_operation_conflict")
    write = TemplateWrite.model_validate(args["write"])
    destination_id = request["destination_template_id"]
    _validate_replacement(service, write, destination_id)
    if destination_id is None:
        saved = service.create_template(write, _commit=False, _preserve_local=True)
    else:
        saved = service.update_template(destination_id, write, _commit=False, _preserve_local=True)
    result = {"ok": True, "purpose": "template_import", "persisted": True,
              "operation_id": request["operation_id"], "template_id": saved.id,
              "name": saved.name, "history_key": saved.history_key, "version": saved.version,
              "operation": "created" if destination_id is None else "updated"}
    action, _ = workflows._action(db, "template_import", {"operation_id": request["operation_id"]})
    workflows._identity(action, site)
    action.state = "complete"
    workflows._seal(action, {"request": request, "source_binding": args["source_binding"], "result": result})
    return result


async def dispatch(tools, name, args):
    from app.services.assistant import AssistantError
    from app.services.assistant_interactions import request_interaction
    from app.services.assistant_workflows import WorkflowDenied, workflow_binding

    if name != "create_template_from_site":
        return {"ok": False, "code": "unknown_tool"}
    try:
        request = TemplateFromSiteRequest.model_validate(args, strict=True)
        arguments = canonical(request)
        with tools.manager.session_factory() as db:
            row = tools.manager._owned(db, tools.owner, tools.session_id)
            data = tools.manager._decrypt(row)
            site, workflows, _ = _context(db, tools, data)
            action = action_for(db, workflows, arguments["operation_id"])
            if action is not None:
                saved = workflows._open(action)
                if saved.get("request") != arguments:
                    raise WorkflowDenied("template_import_operation_conflict")
                if saved.get("source_binding") != workflow_binding(site):
                    raise WorkflowDenied("workflow_binding_changed")
                if action.state != "complete":
                    raise WorkflowDenied("template_import_cancelled")
                return {**deepcopy(saved["result"]), "replayed": True}
            for item in data.get("_interaction_decisions", {}).values():
                previous = item.get("arguments", {}).get("request", {})
                if item.get("purpose") == "template_import" and previous.get("operation_id") == arguments["operation_id"]:
                    if previous != arguments:
                        raise WorkflowDenied("template_import_operation_conflict")
                    if item.get("result") is not None:
                        return deepcopy(item["result"])
        return await request_interaction(tools, {"purpose": "template_import", **arguments})
    except ValidationError:
        return {"ok": False, "code": "invalid_arguments"}
    except (WorkflowDenied, AssistantError) as exc:
        return {"ok": False, "code": exc.code if isinstance(exc, AssistantError) else str(exc)}
    except (IntegrityError, OperationalError):
        return {"ok": False, "code": "template_import_conflict"}
    except WorkflowError:
        return {"ok": False, "code": "template_import_invalid"}
    except Exception:  # noqa: BLE001 - never expose private import data in tool errors
        return {"ok": False, "code": "template_import_unavailable"}
