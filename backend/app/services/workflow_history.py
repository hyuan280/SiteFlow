from __future__ import annotations

import hashlib
import json
from functools import wraps

from pydantic import ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError, OperationalError

from app.core.crypto import CryptoError
from app.features.catalog import get_feature
from app.models.workflow import Site, Template, WorkflowHistory
from app.schemas.workflow import (
    SiteWrite, StepInput, TemplateWrite, WorkflowVersionDetail, WorkflowVersionList,
    WorkflowVersionSummary,
)


def atomic_workflow_write(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        from app.services.workflows import WorkflowConflictError

        try:
            # Service instances may have been used for preloaded list views.
            self._templates_by_id = None
            self._steps_by_owner = None
            self._signals_by_owner = None
            self._groups_by_owner = None
            self._feature_settings = None
            self._resolved_template_steps_cache.clear()
            self._resolved_template_groups_cache.clear()
            self._resolved_template_signals_cache.clear()
            self._resolved_site_signals_cache.clear()
            return method(self, *args, **kwargs)
        except (IntegrityError, OperationalError) as exc:
            self.session.rollback()
            raise WorkflowConflictError("配置已变化或存在冲突，请刷新后重试") from exc
        except Exception:
            self.session.rollback()
            raise
    return wrapped


class WorkflowHistoryMixin:
    @staticmethod
    def _revision(row):
        return row.version if isinstance(row, Template) else row.config_revision

    @staticmethod
    def _owner_type(row):
        return "template" if isinstance(row, Template) else "site"

    def _history_owner(self, owner_type, owner_id):
        return self._get_template(owner_id) if owner_type == "template" else self._get_site(owner_id)

    def _local_snapshot(self, row):
        owner_type = self._owner_type(row)
        model = TemplateWrite if owner_type == "template" else SiteWrite
        local = {}
        for key in model.model_fields:
            if key in {"expected_revision", "expected_history_key", "steps", "groups", "signals"}:
                continue
            if key in {"trusted_urls", "tags"}:
                raw = getattr(row, f"{key}_json")
                try:
                    local[key] = json.loads(raw)
                except (ValueError, TypeError):
                    local[key] = raw
            else:
                local[key] = getattr(row, key)
        step_fields = set(StepInput.model_fields)
        local["steps"] = []
        for step in self._steps(owner_type, row.id):
            item = {key: getattr(step, key) for key in step_fields if key != "config"}
            try:
                item["config"] = json.loads(step.config_json)
            except (ValueError, TypeError):
                # Preserve corrupt legacy input too; restore will reject it via Site/TemplateWrite.
                item["config"] = step.config_json
            local["steps"].append(item)
        local["groups"] = [self._group_view(group).model_dump() for group in self._groups(owner_type, row.id)]
        local["signals"] = [self._signal_view(signal).model_dump() for signal in self._signals(owner_type, row.id)]
        return local

    def _dependency_snapshot(self, row):
        dependencies = []
        parent_id = row.parent_id if isinstance(row, Template) else row.template_id
        seen = {row.id} if isinstance(row, Template) else set()
        while parent_id is not None:
            if parent_id in seen:
                break
            seen.add(parent_id)
            parent = self.session.get(Template, parent_id)
            if parent is None:
                break
            dependencies.append({
                "owner_type": "template", "owner_id": parent.id,
                "history_key": parent.history_key, "revision": parent.version,
                "local": self._local_snapshot(parent),
            })
            parent_id = parent.parent_id
        dependencies.reverse()
        return dependencies

    def _snapshot(self, row, *, allow_unresolved=False):
        from app.services.workflows import WorkflowValidationError

        local = self._local_snapshot(row)
        dependencies = self._dependency_snapshot(row)
        features = {}
        resolution_failed = False
        versions_by_feature = {}
        for owner_type, owner_id in [
            (self._owner_type(row), row.id),
            *(("template", item["owner_id"]) for item in dependencies),
        ]:
            for step in self._steps(owner_type, owner_id):
                versions_by_feature.setdefault(step.feature_type, set()).add(step.feature_version)
        for config in [local, *(item["local"] for item in dependencies)]:
            for step in config["steps"]:
                key = step["feature_type"]
                if key in features:
                    continue
                feature = get_feature(key)
                versions = versions_by_feature[key]
                if feature is None:
                    features[key] = {"versions": sorted(versions), "available": False}
                else:
                    try:
                        view = self._feature_settings_service().view(feature).model_dump(mode="json")
                    except (ValueError, KeyError, TypeError):
                        if not allow_unresolved:
                            raise WorkflowValidationError("功能设置无法解析") from None
                        view = {"version": feature.version, "available": True}
                        resolution_failed = True
                    view["versions"] = sorted(versions)
                    features[key] = view
        resolved = None
        error = None
        try:
            model = TemplateWrite if isinstance(row, Template) else SiteWrite
            model.model_validate(local)
            resolved = (self.resolve_template(row.id) if isinstance(row, Template) else self.resolve_site(row.id)).model_dump(mode="json")
        except (ValueError, KeyError, TypeError):
            if not allow_unresolved:
                raise WorkflowValidationError("配置无法解析，未保存历史") from None
            error = "当前配置无法解析；此基线仅记录迁移时的配置，未推断旧版本。"
        if resolution_failed:
            resolved = None
            error = "当前配置无法解析；此基线仅记录迁移时的配置，未推断旧版本。"
        return {"local": local, "dependencies": dependencies, "features": features,
                "resolved": resolved, "resolution_error": error}

    @staticmethod
    def _history_aad(row):
        return f"workflow-history:{row.owner_type}:{row.history_key}:{row.revision}:{row.schema_version}"

    def _record_history(self, row, operation, *, restored_from_revision=None):
        from app.services.workflows import WorkflowValidationError

        if self.crypto is None:
            raise WorkflowValidationError("历史加密服务不可用")
        snapshot = self._snapshot(row, allow_unresolved=operation in {"baseline", "delete"})
        raw = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        history = WorkflowHistory(
            history_key=row.history_key, owner_type=self._owner_type(row),
            revision=self._revision(row), operation=operation,
            created_by=self.actor.username if self.actor is not None else None,
            restored_from_revision=restored_from_revision, schema_version=1,
            content_hash=hashlib.sha256(raw.encode()).hexdigest(),
        )
        try:
            encrypted = self.crypto.encrypt(self._history_aad(history), raw)
        except CryptoError:
            raise WorkflowValidationError("历史配置无法安全保存") from None
        history.ciphertext = encrypted.ciphertext
        history.nonce = encrypted.nonce
        history.key_version = encrypted.key_version
        self.session.add(history)
        self.session.flush()

    @atomic_workflow_write
    def baseline_workflows(self):
        for model in (Template, Site):
            for row in self.session.scalars(select(model)).all():
                exists = self.session.scalar(select(WorkflowHistory.id).where(
                    WorkflowHistory.history_key == row.history_key
                ).limit(1))
                if exists is None:
                    self._record_history(row, "baseline")
        self.session.commit()

    def _cas_revision(self, row, expected_revision, expected_history_key=None):
        from app.services.workflows import WorkflowConflictError

        revision = self._revision(row)
        exists = self.session.scalar(select(WorkflowHistory.id).where(
            WorkflowHistory.history_key == row.history_key,
            WorkflowHistory.revision == revision,
        ))
        if exists is None:
            raise WorkflowConflictError("配置缺少当前版本历史，请完成启动迁移后重试")
        model = type(row)
        column = model.version if isinstance(row, Template) else model.config_revision
        expected = revision if expected_revision is None else expected_revision
        identity = row.history_key if expected_history_key is None else str(expected_history_key)
        result = self.session.execute(update(model).where(
            model.id == row.id, model.history_key == identity, column == expected,
        ).values({column.key: column + 1}).execution_options(synchronize_session=False))
        if result.rowcount != 1:
            raise WorkflowConflictError("配置版本已变化，请刷新后重试")
        self.session.refresh(row)

    @staticmethod
    def _version_summary(row):
        return WorkflowVersionSummary.model_validate(row, from_attributes=True)

    def list_versions(self, owner_type, owner_id, *, limit=20, offset=0):
        owner = self._history_owner(owner_type, owner_id)
        return self.list_archived_versions(owner.history_key, limit=limit, offset=offset,
                                           current_revision=self._revision(owner))

    def list_archived_versions(self, history_key, *, limit=20, offset=0, current_revision=None):
        from app.services.workflows import WorkflowNotFoundError

        condition = WorkflowHistory.history_key == history_key
        total = self.session.scalar(select(func.count()).select_from(WorkflowHistory).where(condition))
        if not total:
            # A live owner without history is not silently baselined by a read.
            if current_revision is None:
                raise WorkflowNotFoundError("历史不存在")
        rows = self.session.scalars(select(WorkflowHistory).where(condition)
            .order_by(WorkflowHistory.revision.desc()).limit(limit).offset(offset)).all()
        if current_revision is None:
            current_revision = self.session.scalar(select(func.max(WorkflowHistory.revision)).where(condition))
        return WorkflowVersionList(history_key=history_key, current_revision=current_revision,
                                   total=total, items=[self._version_summary(row) for row in rows])

    def get_version(self, owner_type, owner_id, revision):
        owner = self._history_owner(owner_type, owner_id)
        return self.get_archived_version(owner.history_key, revision)

    def get_archived_version(self, history_key, revision):
        from app.services.workflows import WorkflowNotFoundError, WorkflowValidationError

        row = self.session.scalar(select(WorkflowHistory).where(
            WorkflowHistory.history_key == history_key, WorkflowHistory.revision == revision))
        if row is None:
            raise WorkflowNotFoundError("历史版本不存在")
        try:
            if self.crypto is None or row.schema_version != 1:
                raise ValueError()
            raw = self.crypto.decrypt(self._history_aad(row), row.ciphertext, row.nonce, row.key_version)
            if hashlib.sha256(raw.encode()).hexdigest() != row.content_hash:
                raise ValueError()
            return WorkflowVersionDetail(**self._version_summary(row).model_dump(),
                history_key=row.history_key, owner_type=row.owner_type, snapshot=json.loads(raw))
        except (CryptoError, ValueError, TypeError):
            raise WorkflowValidationError("历史配置无法安全读取") from None

    @atomic_workflow_write
    def restore_version(self, owner_type, owner_id, revision, payload):
        from app.services.workflows import WorkflowConflictError, WorkflowValidationError

        owner = self._history_owner(owner_type, owner_id)
        self._cas_revision(owner, payload.expected_revision, payload.expected_history_key)
        version = self.get_version(owner_type, owner_id, revision)
        snapshot = version.snapshot
        # Check the saved ancestry, not the owner's currently selected parent.
        for dependency in snapshot.dependencies:
            current = self.session.scalar(select(Template).where(
                Template.id == dependency["owner_id"]
            ).with_for_update().execution_options(populate_existing=True))
            if current is None or current.history_key != dependency["history_key"]:
                raise WorkflowConflictError("历史依赖已缺失或身份已替换，不能恢复")
        local = snapshot.local
        parent_id = local.get("parent_id") if owner_type == "template" else local.get("template_id")
        current_dependencies = []
        if parent_id is not None:
            try:
                chain = self._template_chain(parent_id)
            except ValueError:
                raise WorkflowConflictError("历史依赖不可用") from None
            current_dependencies = [{"history_key": row.history_key, "revision": row.version} for row in chain]
        saved_dependencies = [{"history_key": item["history_key"], "revision": item["revision"]} for item in snapshot.dependencies]
        if [item["history_key"] for item in current_dependencies] != [item["history_key"] for item in saved_dependencies]:
            raise WorkflowConflictError("历史依赖身份或继承链已变化，不能恢复")
        if current_dependencies != saved_dependencies and not payload.accept_current_dependencies:
            raise WorkflowConflictError("历史依赖版本已变化；需明确接受当前依赖")
        for key, feature_info in snapshot.features.items():
            feature = get_feature(key)
            if feature is None or feature_info.get("versions") != [feature.version]:
                raise WorkflowConflictError("历史功能版本不兼容，不能恢复")
            try:
                current_feature = self._feature_settings_service().view(feature).model_dump(mode="json")
            except (ValueError, KeyError, TypeError):
                raise WorkflowValidationError("当前功能设置无法解析") from None
            saved_feature = {key: value for key, value in feature_info.items() if key != "versions"}
            if current_feature != saved_feature and not payload.accept_current_dependencies:
                raise WorkflowConflictError("功能设置已变化；需明确接受当前依赖")
        try:
            model = TemplateWrite if owner_type == "template" else SiteWrite
            write = model.model_validate({**local, "expected_revision": payload.expected_revision})
        except ValidationError:
            raise WorkflowValidationError("历史本地配置不符合当前校验规则") from None
        if owner_type == "template":
            return self.update_template(owner_id, write, _restored_from=revision, _revision_claimed=True)
        return self.update_site(owner_id, write, _restored_from=revision, _revision_claimed=True)
