from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.features.base import ERROR_POLICIES, Feature, FeatureConfigError
from app.core.config import Settings
from app.core.crypto import CryptoService
from app.features.catalog import get_feature, list_features
from app.models.workflow import (
    Site,
    SiteCredential,
    Template,
    WorkflowGroup,
    WorkflowSignal,
    WorkflowStep,
)
from app.schemas.credential import SiteCredentialView
from app.services.credentials import CredentialService
from app.services.integrations import IntegrationService
from app.schemas.workflow import (
    SiteOptionView,
    FeatureView,
    GroupInput,
    GroupView,
    ResolvedGroupView,
    ResolvedSignalView,
    ResolvedStepView,
    ResolvedWorkflowView,
    SignalInput,
    SignalView,
    SiteDetail,
    SiteSummary,
    SiteWrite,
    StepInput,
    StepView,
    TemplateDetail,
    TemplateSummary,
    TemplateWrite,
)

OWNER_TEMPLATE = "template"
OWNER_SITE = "site"
DEFAULT_CONDITION = "always"
DEFAULT_ERROR_POLICY = "stop"
SIGNAL_CONDITION_PREFIX = "signal:"
SIGNAL_KEY_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
SYSTEM_RESIGN_SIGNAL_KEY = "sys_resign_pending"
SYSTEM_RESIGN_SIGNAL_LABEL = "断签"


class WorkflowError(ValueError):
    pass


class WorkflowValidationError(WorkflowError):
    pass


class WorkflowConflictError(WorkflowError):
    pass


class WorkflowNotFoundError(WorkflowError):
    pass


@dataclass
class _EffectiveStep:
    step_key: str
    feature: Feature
    label: str
    group_key: str | None
    position: float
    enabled: bool
    config: dict[str, Any]
    condition: str
    on_error: str
    timeout_seconds: int | None
    source: str
    seq: int


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class ConditionExpr:
    kind: str
    key: str | None = None
    expected: bool | None = None
    children: tuple["ConditionExpr", ...] = ()


def _split_condition_args(body: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    start = 0
    for index, char in enumerate(body):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                raise WorkflowValidationError(f"执行条件括号不匹配: {body}")
        elif char == "," and depth == 0:
            parts.append(body[start:index].strip())
            start = index + 1
    parts.append(body[start:].strip())
    if any(not part for part in parts):
        raise WorkflowValidationError(f"执行条件包含空项: {body}")
    return parts


def _parse_condition_expression(value: str | None) -> ConditionExpr:
    text = (value or "").strip()
    if not text or text == DEFAULT_CONDITION:
        return ConditionExpr("always")
    for kind in ("all", "any"):
        prefix = f"{kind}("
        if text.startswith(prefix):
            if not text.endswith(")"):
                raise WorkflowValidationError(f"执行条件无效: {text}")
            body = text[len(prefix):-1].strip()
            if not body:
                raise WorkflowValidationError(f"执行条件不能为空: {text}")
            children = tuple(
                _parse_condition_expression(part)
                for part in _split_condition_args(body)
            )
            return ConditionExpr(kind, children=children)
    if not text.startswith(SIGNAL_CONDITION_PREFIX):
        raise WorkflowValidationError(f"执行条件无效: {text}")
    body = text[len(SIGNAL_CONDITION_PREFIX):]
    key, separator, expected = body.rpartition(":")
    if not separator or expected not in {"true", "false"} or not SIGNAL_KEY_RE.fullmatch(key):
        raise WorkflowValidationError(f"执行条件无效: {value}")
    return ConditionExpr("signal", key=key, expected=expected == "true")


def _condition_signal_keys(value: str | None) -> set[str]:
    result: set[str] = set()

    def walk(node: ConditionExpr) -> None:
        if node.kind == "signal" and node.key:
            result.add(node.key)
        for child in node.children:
            walk(child)

    walk(_parse_condition_expression(value))
    return result


def _json_list(raw: str) -> list[str]:
    try:
        value = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    return [str(item) for item in value] if isinstance(value, list) else []


def _parse_config(raw: str, step_key: str) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise WorkflowValidationError(f"步骤 {step_key} 的 config_json 不是合法 JSON") from exc
    if not isinstance(value, dict):
        raise WorkflowValidationError(f"步骤 {step_key} 的 config 必须是 JSON 对象")
    return value


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if value is None:
            result.pop(key, None)
        elif isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _substitute(value: Any, base_url: str) -> Any:
    if isinstance(value, str):
        return value.replace("{base_url}", base_url)
    if isinstance(value, list):
        return [_substitute(item, base_url) for item in value]
    if isinstance(value, dict):
        return {key: _substitute(item, base_url) for key, item in value.items()}
    return value


def _feature_view(feature: Feature) -> FeatureView:
    return FeatureView(
        key=feature.key,
        name=feature.name,
        category=feature.category,
        cardinality=feature.cardinality,
        requires=sorted(feature.requires),
        description=feature.description,
        version=feature.version,
        config_schema=feature.config_schema(),
        source=getattr(feature, "plugin_id", "builtin"),
    )


class WorkflowService:
    def __init__(
        self,
        session: Session,
        settings: Settings | None = None,
        crypto: CryptoService | None = None,
    ) -> None:
        self.session = session
        self.settings = settings
        self.crypto = crypto
        self._templates_by_id: dict[int, Template] | None = None
        self._steps_by_owner: dict[tuple[str, int], list[WorkflowStep]] | None = None
        self._signals_by_owner: dict[tuple[str, int], list[WorkflowSignal]] | None = None
        self._groups_by_owner: dict[tuple[str, int], list[WorkflowGroup]] | None = None
        self._resolved_template_steps_cache: dict[int, list[_EffectiveStep]] = {}
        self._resolved_template_groups_cache: dict[int, list[ResolvedGroupView]] = {}
        self._resolved_template_signals_cache: dict[int, list[ResolvedSignalView]] = {}
        self._resolved_site_signals_cache: dict[int, list[ResolvedSignalView]] = {}
        self._credential_views: dict[int, SiteCredentialView] = {}
        self._credential_service = None
        self._credential_rows_preloaded = False
        self._feature_settings = None
        self._cfbox_loaded = False
        self._cfbox_value = True

    # ── catalog ─────────────────────────────────────────────────────────

    def features(self) -> list[FeatureView]:
        return [self._feature_settings_service().view(feature) for feature in list_features()]

    def _feature_settings_service(self):
        from app.services.feature_settings import FeatureSettingsService

        if self._feature_settings is None:
            self._feature_settings = FeatureSettingsService(self.session)
        return self._feature_settings

    def _cfbox_enabled(self) -> bool:
        if self.settings is None or self.crypto is None:
            return True
        if self._cfbox_loaded:
            return self._cfbox_value
        self._cfbox_value = IntegrationService(
            self.session, self.settings, self.crypto
        ).cfbox_enabled()
        self._cfbox_loaded = True
        return self._cfbox_value

    # ── lookup helpers ──────────────────────────────────────────────────

    def _get_template(self, template_id: int) -> Template:
        row = (
            self._templates_by_id.get(template_id)
            if self._templates_by_id is not None
            else self.session.get(Template, template_id)
        )
        if row is None:
            raise WorkflowNotFoundError(f"模板 {template_id} 不存在")
        return row

    def _get_site(self, site_id: int) -> Site:
        row = self.session.get(Site, site_id)
        if row is None:
            raise WorkflowNotFoundError(f"站点 {site_id} 不存在")
        return row

    def _template_chain(self, template_id: int) -> list[Template]:
        chain: list[Template] = []
        seen: set[int] = set()
        current = self._get_template(template_id)
        while True:
            if current.id in seen:
                raise WorkflowValidationError("模板继承存在循环")
            seen.add(current.id)
            chain.append(current)
            if current.parent_id is None:
                break
            parent = self.session.get(Template, current.parent_id)
            if parent is None:
                raise WorkflowValidationError(f"父模板 {current.parent_id} 不存在")
            current = parent
        chain.reverse()
        return chain

    def _steps(self, owner_type: str, owner_id: int) -> list[WorkflowStep]:
        if self._steps_by_owner is not None:
            return list(self._steps_by_owner.get((owner_type, owner_id), []))
        return list(
            self.session.scalars(
                select(WorkflowStep)
                .where(
                    WorkflowStep.owner_type == owner_type,
                    WorkflowStep.owner_id == owner_id,
                )
                .order_by(WorkflowStep.id)
            )
        )

    def _credential_view(self, site_id: int) -> SiteCredentialView:
        if self.crypto is None:
            return SiteCredentialView(configured=False)
        if site_id in self._credential_views:
            return self._credential_views[site_id]
        if self._credential_service is None:
            self._credential_service = CredentialService(self.session, self.crypto)
        if self._steps_by_owner is not None and not self._credential_rows_preloaded:
            self._credential_service.preload()
            self._credential_rows_preloaded = True
        view = self._credential_service.view(site_id)
        if self._steps_by_owner is not None:
            self._credential_views[site_id] = view
        return view

    def _template_name(self, template_id: int | None) -> str | None:
        if template_id is None:
            return None
        if self._templates_by_id is not None:
            row = self._templates_by_id.get(template_id)
        else:
            row = self.session.get(Template, template_id)
        return row.name if row is not None else None

    # ── signals ─────────────────────────────────────────────────────────

    def _signals(self, owner_type: str, owner_id: int) -> list[WorkflowSignal]:
        if self._signals_by_owner is not None:
            return list(self._signals_by_owner.get((owner_type, owner_id), []))
        return list(
            self.session.scalars(
                select(WorkflowSignal)
                .where(
                    WorkflowSignal.owner_type == owner_type,
                    WorkflowSignal.owner_id == owner_id,
                )
                .order_by(WorkflowSignal.id)
            )
        )

    @staticmethod
    def _signal_view(row: WorkflowSignal) -> SignalView:
        return SignalView(
            signal_key=row.signal_key,
            label=row.label,
            description=row.description,
            include_texts=_json_list(row.include_texts_json),
            include_selectors=_json_list(row.include_selectors_json),
            exclude_texts=_json_list(row.exclude_texts_json),
            exclude_selectors=_json_list(row.exclude_selectors_json),
            match_mode=row.match_mode,
            enabled=row.enabled,
        )

    def _resolved_template_signals(self, template: Template) -> list[ResolvedSignalView]:
        if self._steps_by_owner is not None and template.id in self._resolved_template_signals_cache:
            return self._resolved_template_signals_cache[template.id]
        resolved: dict[str, ResolvedSignalView] = {}
        for item in self._template_chain(template.id):
            for row in self._signals(OWNER_TEMPLATE, item.id):
                view = self._signal_view(row)
                resolved[view.signal_key] = ResolvedSignalView(
                    **view.model_dump(), source=f"template:{item.name}"
                )
        resolved[SYSTEM_RESIGN_SIGNAL_KEY] = self._system_resign_signal()
        result = list(resolved.values())
        if self._steps_by_owner is not None:
            self._resolved_template_signals_cache[template.id] = result
        return result

    def _resolved_site_signals(self, site: Site) -> list[ResolvedSignalView]:
        if self._steps_by_owner is not None and site.id in self._resolved_site_signals_cache:
            return self._resolved_site_signals_cache[site.id]
        resolved: dict[str, ResolvedSignalView] = {}
        if site.template_id is not None:
            template = self._get_template(site.template_id)
            for signal in self._resolved_template_signals(template):
                resolved[signal.signal_key] = signal
        for row in self._signals(OWNER_SITE, site.id):
            view = self._signal_view(row)
            resolved[view.signal_key] = ResolvedSignalView(
                **view.model_dump(), source="site"
            )
        resolved[SYSTEM_RESIGN_SIGNAL_KEY] = self._system_resign_signal()
        result = list(resolved.values())
        if self._steps_by_owner is not None:
            self._resolved_site_signals_cache[site.id] = result
        return result

    @staticmethod
    def _system_resign_signal() -> ResolvedSignalView:
        return ResolvedSignalView(
            signal_key=SYSTEM_RESIGN_SIGNAL_KEY,
            label=SYSTEM_RESIGN_SIGNAL_LABEL,
            description="站点存在未签到日期时为真；站点不支持补签时恒为假",
            include_texts=[],
            include_selectors=[],
            exclude_texts=[],
            exclude_selectors=[],
            match_mode="any",
            enabled=True,
            source="system",
        )

    @staticmethod
    def _validate_signal_inputs(signals: list[SignalInput]) -> None:
        seen: set[str] = set()
        for signal in signals:
            if signal.signal_key in seen:
                raise WorkflowValidationError(f"信号 key 重复: {signal.signal_key}")
            if signal.signal_key == SYSTEM_RESIGN_SIGNAL_KEY:
                raise WorkflowValidationError(
                    f"信号 key {SYSTEM_RESIGN_SIGNAL_KEY} 为系统保留"
                )
            seen.add(signal.signal_key)
            includes = [
                item.strip() for item in [*signal.include_texts, *signal.include_selectors]
            ]
            if not any(includes):
                raise WorkflowValidationError(
                    f"信号 {signal.label or signal.signal_key} 至少需要一个包含文本或选择器"
                )

    def _replace_signals(
        self, owner_type: str, owner_id: int, signals: list[SignalInput]
    ) -> None:
        self._validate_signal_inputs(signals)
        self.session.query(WorkflowSignal).filter(
            WorkflowSignal.owner_type == owner_type,
            WorkflowSignal.owner_id == owner_id,
        ).delete(synchronize_session=False)
        now = _utcnow()
        for signal in signals:
            self.session.add(
                WorkflowSignal(
                    owner_type=owner_type,
                    owner_id=owner_id,
                    signal_key=signal.signal_key,
                    label=signal.label.strip(),
                    description=signal.description.strip(),
                    include_texts_json=json.dumps(
                        [item.strip() for item in signal.include_texts if item.strip()],
                        ensure_ascii=False,
                    ),
                    include_selectors_json=json.dumps(
                        [item.strip() for item in signal.include_selectors if item.strip()],
                        ensure_ascii=False,
                    ),
                    exclude_texts_json=json.dumps(
                        [item.strip() for item in signal.exclude_texts if item.strip()],
                        ensure_ascii=False,
                    ),
                    exclude_selectors_json=json.dumps(
                        [item.strip() for item in signal.exclude_selectors if item.strip()],
                        ensure_ascii=False,
                    ),
                    match_mode=signal.match_mode,
                    enabled=signal.enabled,
                    created_at=now,
                    updated_at=now,
                )
            )

    def _descendant_template_ids(self, template_id: int) -> list[int]:
        result: list[int] = []
        queue = [template_id]
        while queue:
            current = queue.pop()
            result.append(current)
            queue.extend(
                self.session.scalars(
                    select(Template.id).where(Template.parent_id == current)
                ).all()
            )
        return result

    def _referenced_signal_keys(
        self, owner_type: str, owner_id: int, candidates: set[str]
    ) -> set[str]:
        steps: list[WorkflowStep] = []
        if owner_type == OWNER_SITE:
            steps = self._steps(OWNER_SITE, owner_id)
        else:
            template_ids = self._descendant_template_ids(owner_id)
            steps = list(
                self.session.scalars(
                    select(WorkflowStep).where(
                        WorkflowStep.owner_type == OWNER_TEMPLATE,
                        WorkflowStep.owner_id.in_(template_ids),
                    )
                )
            )
            site_ids = self.session.scalars(
                select(Site.id).where(Site.template_id.in_(template_ids))
            ).all()
            if site_ids:
                steps.extend(
                    self.session.scalars(
                        select(WorkflowStep).where(
                            WorkflowStep.owner_type == OWNER_SITE,
                            WorkflowStep.owner_id.in_(site_ids),
                        )
                    )
                )
        referenced: set[str] = set()
        for step in steps:
            if not step.condition:
                continue
            try:
                keys = _condition_signal_keys(step.condition)
            except WorkflowValidationError:
                continue
            referenced.update(keys & candidates)
        return referenced

    def _check_removed_signals(
        self, owner_type: str, owner_id: int, new_signals: list[SignalInput]
    ) -> None:
        old_keys = {row.signal_key for row in self._signals(owner_type, owner_id)}
        new_keys = {signal.signal_key for signal in new_signals}
        inherited: set[str] = set()
        if owner_type == OWNER_SITE:
            site = self._get_site(owner_id)
            if site.template_id is not None:
                template = self._get_template(site.template_id)
                inherited = {
                    signal.signal_key for signal in self._resolved_template_signals(template)
                }
        else:
            template = self._get_template(owner_id)
            if template.parent_id is not None:
                parent = self._get_template(template.parent_id)
                inherited = {
                    signal.signal_key for signal in self._resolved_template_signals(parent)
                }
        removed = old_keys - new_keys - inherited
        if not removed:
            return
        used = self._referenced_signal_keys(owner_type, owner_id, removed)
        if used:
            raise WorkflowConflictError(
                f"信号仍被步骤条件引用，不能删除: {', '.join(sorted(used))}"
            )

    # ── groups ──────────────────────────────────────────────────────────

    def _groups(self, owner_type: str, owner_id: int) -> list[WorkflowGroup]:
        if self._groups_by_owner is not None:
            return list(self._groups_by_owner.get((owner_type, owner_id), []))
        return list(
            self.session.scalars(
                select(WorkflowGroup)
                .where(
                    WorkflowGroup.owner_type == owner_type,
                    WorkflowGroup.owner_id == owner_id,
                )
                .order_by(WorkflowGroup.position.asc(), WorkflowGroup.id.asc())
            ).all()
        )

    @staticmethod
    def _group_view(row: WorkflowGroup) -> GroupView:
        return GroupView(
            group_key=row.group_key,
            name=row.name,
            position=row.position,
            step_anchor=row.step_anchor,
        )

    def _replace_groups(
        self, owner_type: str, owner_id: int, groups: list[GroupInput]
    ) -> None:
        existing = {row.group_key: row for row in self._groups(owner_type, owner_id)}
        for group in groups:
            if group.step_anchor is None and group.group_key in existing:
                group.step_anchor = existing[group.group_key].step_anchor
        self.session.query(WorkflowGroup).filter(
            WorkflowGroup.owner_type == owner_type,
            WorkflowGroup.owner_id == owner_id,
        ).delete(synchronize_session=False)
        now = _utcnow()
        for group in groups:
            self.session.add(
                WorkflowGroup(
                    owner_type=owner_type,
                    owner_id=owner_id,
                    group_key=group.group_key,
                    name=group.name.strip(),
                    position=group.position,
                    step_anchor=group.step_anchor,
                    created_at=now,
                    updated_at=now,
                )
            )

    def _resolved_template_groups(self, template: Template) -> list[ResolvedGroupView]:
        if self._steps_by_owner is not None and template.id in self._resolved_template_groups_cache:
            return self._resolved_template_groups_cache[template.id]
        groups: dict[str, ResolvedGroupView] = {}
        for item in self._template_chain(template.id):
            groups = self._merge_groups(groups, self._groups(OWNER_TEMPLATE, item.id), f"template:{item.name}")
        result = sorted(groups.values(), key=lambda group: (group.position, group.group_key))
        if self._steps_by_owner is not None:
            self._resolved_template_groups_cache[template.id] = result
        return result

    @staticmethod
    def _merge_groups(
        inherited: dict[str, ResolvedGroupView], rows: list[WorkflowGroup], source: str,
    ) -> dict[str, ResolvedGroupView]:
        groups = {key: group.model_copy() for key, group in inherited.items()}
        for row in rows:
            for group in groups.values():
                if group.position >= row.position:
                    group.position += 1_000_000
        groups.update({row.group_key: ResolvedGroupView(
            group_key=row.group_key, name=row.name, position=row.position,
            step_anchor=row.step_anchor, source=source,
        ) for row in rows})
        return groups

    def _resolved_site_groups(self, site: Site) -> list[ResolvedGroupView]:
        groups: dict[str, ResolvedGroupView] = {}
        if site.template_id is not None:
            template = self._get_template(site.template_id)
            for group in self._resolved_template_groups(template):
                groups[group.group_key] = group
        groups = self._merge_groups(groups, self._groups(OWNER_SITE, site.id), "site")
        return sorted(groups.values(), key=lambda group: (group.position, group.group_key))

    def _template_dependents(self, template_id: int) -> tuple[int, int]:
        sites = len(
            self.session.scalars(
                select(Site.id).where(Site.template_id == template_id)
            ).all()
        )
        children = len(
            self.session.scalars(
                select(Template.id).where(Template.parent_id == template_id)
            ).all()
        )
        return sites, children

    def _validate_group_inputs(
        self,
        groups: list[GroupInput],
        inherited: list[ResolvedGroupView] | None = None,
    ) -> None:
        seen: set[str] = set()
        inherited_keys = {group.group_key for group in inherited or []}
        for group in groups:
            if group.group_key in seen:
                raise WorkflowValidationError(f"分组 key 重复: {group.group_key}")
            if group.group_key in inherited_keys:
                raise WorkflowValidationError(
                    f"不能覆盖模板分组: {group.group_key}"
                )
            seen.add(group.group_key)
            if not group.name.strip():
                raise WorkflowValidationError("分组名称不能为空")

    def _step_group_map(self, owner_type: str, owner_id: int) -> dict[str, str | None]:
        return {
            row.step_key: row.group_key
            for row in self._steps(owner_type, owner_id)
        }

    def _validate_step_groups(
        self,
        steps: list[StepInput],
        inherited_steps: dict[str, str | None],
        own_group_keys: set[str],
        inherited_group_keys: set[str] | None = None,
    ) -> None:
        for step in steps:
            if step.step_key in inherited_steps:
                expected = inherited_steps[step.step_key]
                if step.group_key is not None and step.group_key != expected:
                    raise WorkflowValidationError(
                        f"步骤 {step.step_key} 不能改变模板分组"
                    )
            elif step.group_key is not None and step.group_key not in own_group_keys:
                if step.group_key in (inherited_group_keys or set()):
                    raise WorkflowValidationError(
                        f"步骤 {step.step_key} 不能使用模板分组 {step.group_key}"
                    )
                raise WorkflowValidationError(
                    f"步骤 {step.step_key} 引用了不存在的本地分组 {step.group_key}"
                )

    def _descendant_has_override(self, template_id: int, step_key: str) -> bool:
        template_ids = {template_id}
        pending = [template_id]
        while pending:
            current = pending.pop()
            children = self.session.scalars(
                select(Template.id).where(Template.parent_id == current)
            ).all()
            for child in children:
                if child not in template_ids:
                    template_ids.add(child)
                    pending.append(child)
        for owner_id in template_ids - {template_id}:
            if self.session.scalar(
                select(WorkflowStep.id).where(
                    WorkflowStep.owner_type == OWNER_TEMPLATE,
                    WorkflowStep.owner_id == owner_id,
                    WorkflowStep.step_key == step_key,
                )
            ):
                return True
        site_ids = self.session.scalars(
            select(Site.id).where(Site.template_id.in_(template_ids))
        ).all()
        for site_id in site_ids:
            if self.session.scalar(
                select(WorkflowStep.id).where(
                    WorkflowStep.owner_type == OWNER_SITE,
                    WorkflowStep.owner_id == site_id,
                    WorkflowStep.step_key == step_key,
                )
            ):
                return True
        return False

    def _validate_template_structure(
        self,
        template: Template,
        groups: list[GroupInput],
        steps: list[StepInput],
        new_parent_id: int | None,
    ) -> None:
        site_count, child_count = self._template_dependents(template.id)
        if not site_count and not child_count:
            return
        if new_parent_id != template.parent_id:
            raise WorkflowConflictError("模板已被使用，不能修改父模板")
        old_steps = self._step_group_map(OWNER_TEMPLATE, template.id)
        new_steps = {step.step_key: step.group_key for step in steps}
        for step_key in old_steps:
            if step_key not in new_steps:
                if self._descendant_has_override(template.id, step_key):
                    raise WorkflowConflictError(
                        f"步骤 {step_key} 被子模板或站点覆盖，不能删除"
                    )
        existing_groups = self._groups(OWNER_TEMPLATE, template.id)
        if not existing_groups:
            return
        old_groups = {
            row.group_key: row.name
            for row in sorted(existing_groups, key=lambda item: (item.position, item.group_key))
        }
        new_groups = {
            group.group_key: group.name
            for group in sorted(groups, key=lambda item: (item.position, item.group_key))
        }
        for group_key, name in old_groups.items():
            if group_key not in new_groups:
                raise WorkflowConflictError(
                    f"模板已被使用，不能删除分组 {group_key}"
                )
            if new_groups[group_key] != name:
                raise WorkflowConflictError(
                    f"模板已被使用，不能重命名分组 {group_key}"
                )
        old_order = list(old_groups)
        new_order = [key for key in new_groups if key in old_groups]
        if old_order != new_order:
            raise WorkflowConflictError("模板已被使用，不能重排已有分组")
        for step_key, old_group in old_steps.items():
            if step_key not in new_steps:
                continue
            new_group = new_steps[step_key]
            if new_group == old_group or old_group is None:
                continue
            raise WorkflowConflictError(
                f"模板已被使用，步骤 {step_key} 不能改变分组"
            )

    # ── template resolution ─────────────────────────────────────────────

    def _apply_step(
        self,
        steps: dict[str, _EffectiveStep],
        row: WorkflowStep,
        source: str,
        counter: list[int],
    ) -> None:
        feature = get_feature(row.feature_type)
        if feature is None:
            if row.feature_type.startswith("plugin."):
                raise WorkflowValidationError(f"步骤 {row.step_key}: 插件不可用、已移除或功能不兼容；已保存配置保持不变")
            raise WorkflowValidationError(
                f"步骤 {row.step_key} 引用了未知功能类型 {row.feature_type}"
            )
        if row.feature_version != feature.version:
            raise WorkflowValidationError(
                f"步骤 {row.step_key} 的功能版本 {row.feature_version} 不受支持"
            )
        config = _parse_config(row.config_json, row.step_key)
        existing = steps.get(row.step_key)
        if existing is not None and existing.feature.key != feature.key:
            raise WorkflowValidationError(
                f"步骤 {row.step_key} 覆盖时不能改变功能类型 "
                f"({existing.feature.key} -> {feature.key})"
            )

        if existing is None:
            group_key = row.group_key
            position = row.position if row.position is not None else self._next_position(steps)
            label = row.label or feature.name
            enabled = True if row.enabled is None else row.enabled
            condition = row.condition or DEFAULT_CONDITION
            on_error = row.on_error or DEFAULT_ERROR_POLICY
            timeout_seconds = row.timeout_seconds
        else:
            group_key = existing.group_key if row.group_key is None else row.group_key
            position = existing.position + 1
            label = existing.label if row.label is None else (row.label or feature.name)
            enabled = existing.enabled if row.enabled is None else row.enabled
            condition = existing.condition if row.condition is None else row.condition
            on_error = existing.on_error if row.on_error is None else row.on_error
            timeout_seconds = (
                existing.timeout_seconds if row.timeout_seconds is None else row.timeout_seconds
            )
            config = _deep_merge(existing.config, config)

        steps[row.step_key] = _EffectiveStep(
            step_key=row.step_key,
            feature=feature,
            label=label,
            group_key=group_key,
            position=position,
            enabled=enabled,
            config=config,
            condition=condition,
            on_error=on_error,
            timeout_seconds=timeout_seconds,
            source=source,
            seq=counter[0],
        )
        counter[0] += 1

    @staticmethod
    def _next_position(steps: dict[str, _EffectiveStep]) -> float:
        if not steps:
            return 1000.0
        return max(step.position for step in steps.values()) + 1000.0

    @staticmethod
    def _apply_override_positions(
        steps: list[StepInput],
        inherited: list[_EffectiveStep],
    ) -> None:
        base_by_key = {step.step_key: step for step in inherited}
        for step in steps:
            base = base_by_key.get(step.step_key)
            if base is not None:
                step.position = base.position + 1
        # Validate writes only: historical data remains readable and untouched.
        # Overrides are one effective step, not a new local ordering entry.
        effective = {step.step_key: (step.group_key, step.position) for step in inherited}
        local_keys: set[str] = set()
        for step in steps:
            base = base_by_key.get(step.step_key)
            position = step.position
            if position is None:
                position = max((value[1] for value in effective.values()), default=0) + 1000
            effective[step.step_key] = (
                base.group_key if base is not None else step.group_key, position,
            )
            if base is None and step.group_key is None:
                local_keys.add(step.step_key)
        positions: dict[float, list[str]] = {}
        for key, (group_key, position) in effective.items():
            if group_key is None:
                positions.setdefault(position, []).append(key)
        for position, keys in positions.items():
            if len(keys) > 1 and local_keys.intersection(keys):
                raise WorkflowValidationError(
                    f"Ambiguous ungrouped step position {position}: {', '.join(keys)}"
                )

    def _resolved_template_steps(self, template: Template) -> list[_EffectiveStep]:
        if self._steps_by_owner is not None and template.id in self._resolved_template_steps_cache:
            return self._resolved_template_steps_cache[template.id]
        steps: dict[str, _EffectiveStep] = {}
        counter = [0]
        for item in self._template_chain(template.id):
            for row in self._steps(OWNER_TEMPLATE, item.id):
                self._apply_step(steps, row, f"template:{item.name}", counter)
        result = sorted(steps.values(), key=lambda step: (step.position, step.seq))
        if self._steps_by_owner is not None:
            self._resolved_template_steps_cache[template.id] = result
        return result

    def _resolved_site_steps(self, site: Site) -> list[_EffectiveStep]:
        steps: dict[str, _EffectiveStep] = {}
        counter = [0]
        if site.template_id is not None:
            template = self._get_template(site.template_id)
            for step in self._resolved_template_steps(template):
                steps[step.step_key] = step
                counter[0] = max(counter[0], step.seq + 1)
        for row in self._steps(OWNER_SITE, site.id):
            self._apply_step(steps, row, "site", counter)
        return sorted(steps.values(), key=lambda step: (step.position, step.seq))

    def _finalize(
        self,
        steps: list[_EffectiveStep],
        *,
        owner_type: str,
        owner_id: int,
        name: str,
        base_url: str | None = None,
        lane: str | None = None,
        signals: list[ResolvedSignalView] | None = None,
        groups: list[ResolvedGroupView] | None = None,
    ) -> ResolvedWorkflowView:
        grouped: dict[str, list[_EffectiveStep]] = {}
        ungrouped: list[_EffectiveStep] = []
        for step in steps:
            if step.group_key:
                grouped.setdefault(step.group_key, []).append(step)
            else:
                ungrouped.append(step)
        blocks: list[tuple[float, int, str]] = []
        ordered_groups = sorted(groups or [], key=lambda group: (group.position, group.group_key))
        # Step-space anchors locate ungrouped blocks; group ranks order grouped blocks.
        anchors = sorted(
            group.step_anchor if group.step_anchor is not None else
            min((step.position for step in grouped.get(group.group_key, [])), default=0)
            for group in ordered_groups
        )
        slots = {group.group_key: anchors[index] for index, group in enumerate(ordered_groups)}
        for group_key, group_steps in grouped.items():
            blocks.append(
                (
                    slots.get(group_key, min(item.position for item in group_steps)),
                    0,
                    group_key,
                )
            )
        for step in ungrouped:
            blocks.append((step.position, 1, step.step_key))
        blocks.sort(key=lambda item: (item[0], item[1], item[2]))
        ordered_steps: list[_EffectiveStep] = []
        for _, kind, key in blocks:
            if kind == 0:
                ordered_steps.extend(
                    sorted(grouped[key], key=lambda step: (step.position, step.seq))
                )
            else:
                ordered_steps.extend(
                    step for step in ungrouped if step.step_key == key
                )
        steps = ordered_steps
        resolved_steps: list[ResolvedStepView] = []
        singleton_counts: dict[str, int] = {}
        requires: set[str] = set()
        warnings: list[str] = []
        signal_map = {signal.signal_key: signal for signal in (signals or [])}

        for step in steps:
            for condition_key in _condition_signal_keys(step.condition):
                signal = signal_map.get(condition_key)
                if signal is None:
                    raise WorkflowValidationError(
                        f"步骤 {step.step_key} 引用了不存在的信号 {condition_key}"
                    )
                if not signal.enabled:
                    raise WorkflowValidationError(
                        f"步骤 {step.step_key} 引用了已禁用的信号 {signal.label}"
                    )
            if step.enabled and "cfbox" in step.feature.requires and not self._cfbox_enabled():
                raise WorkflowValidationError(
                    f"步骤 {step.step_key} 需要 CFbox，请先在系统设置中配置服务地址和 API Key"
                )
            if step.on_error not in ERROR_POLICIES:
                raise WorkflowValidationError(f"步骤 {step.step_key} 的错误策略 {step.on_error} 无效")
            config = _substitute(step.config, base_url) if base_url else step.config
            try:
                normalized = self._feature_settings_service().validate(step.feature, config)
            except FeatureConfigError as exc:
                raise WorkflowValidationError(
                    f"步骤 {step.step_key} ({step.feature.key}) 配置无效: {exc}"
                ) from exc
            if base_url:
                # Defaults such as flow.open.url are inserted by validation.
                normalized = _substitute(normalized, base_url)

            if step.enabled:
                requires.update(step.feature.requires)
                if step.feature.cardinality == "single":
                    singleton_counts[step.feature.key] = (
                        singleton_counts.get(step.feature.key, 0) + 1
                    )

            resolved_steps.append(
                ResolvedStepView(
                    step_key=step.step_key,
                    feature_type=step.feature.key,
                    feature_version=step.feature.version,
                    label=step.label,
                    category=step.feature.category,
                    group_key=step.group_key,
                    position=step.position,
                    enabled=step.enabled,
                    config=normalized,
                    condition=step.condition,
                    on_error=step.on_error,
                    timeout_seconds=step.timeout_seconds,
                    requires=sorted(step.feature.requires),
                    source=step.source,
                )
            )

        duplicates = [key for key, count in singleton_counts.items() if count > 1]
        if duplicates:
            raise WorkflowValidationError(f"单例功能被启用多次: {', '.join(sorted(duplicates))}")

        enabled_steps = [step for step in resolved_steps if step.enabled]
        if enabled_steps and not any(
            step.category in {"sign", "post"} for step in enabled_steps
        ):
            warnings.append("工作流没有启用的签到或补签步骤")

        return ResolvedWorkflowView(
            owner_type=owner_type,  # type: ignore[arg-type]
            owner_id=owner_id,
            name=name,
            lane=lane,
            requires=sorted(requires),
            steps=resolved_steps,
            groups=list(groups or []),
            signals=list(signals or []),
            warnings=warnings,
        )

    def resolve_template(self, template_id: int) -> ResolvedWorkflowView:
        template = self._get_template(template_id)
        return self._finalize(
            self._resolved_template_steps(template),
            owner_type=OWNER_TEMPLATE,
            owner_id=template.id,
            name=template.name,
            signals=self._resolved_template_signals(template),
            groups=self._resolved_template_groups(template),
        )

    @staticmethod
    def _site_lane(site: Site) -> str:
        return "general" if site.lane_policy == "auto" else site.lane_policy

    def resolve_site(self, site_id: int) -> ResolvedWorkflowView:
        site = self._get_site(site_id)
        return self._finalize(
            self._resolved_site_steps(site),
            owner_type=OWNER_SITE,
            owner_id=site.id,
            name=site.name,
            base_url=site.base_url,
            lane=self._site_lane(site),
            signals=self._resolved_site_signals(site),
            groups=self._resolved_site_groups(site),
        )

    # ── views ───────────────────────────────────────────────────────────

    def _step_view(self, row: WorkflowStep) -> StepView:
        return StepView(
            step_key=row.step_key,
            feature_type=row.feature_type,
            feature_version=row.feature_version,
            label=row.label,
            category=row.category,
            group_key=row.group_key,
            position=row.position,
            enabled=row.enabled,
            config=_parse_config(row.config_json, row.step_key),
            condition=row.condition,
            on_error=row.on_error,
            timeout_seconds=row.timeout_seconds,
        )

    def _template_summary(self, template: Template) -> TemplateSummary:
        steps = self._steps(OWNER_TEMPLATE, template.id)
        signals = self._resolved_template_signals(template)
        lane = None
        error = None
        resolved = None
        try:
            resolved = self.resolve_template(template.id)
        except WorkflowError as exc:
            error = str(exc)
        return TemplateSummary(
            id=template.id,
            name=template.name,
            description=template.description,
            parent_id=template.parent_id,
            enabled=template.enabled,
            version=template.version,
            step_count=len(resolved.steps) if resolved is not None else len(steps),
            signal_count=len([signal for signal in signals if signal.source != "system"]),
            lane=lane,
            valid=error is None,
            validation_error=error,
            created_at=template.created_at,
            updated_at=template.updated_at,
        )

    def _site_summary(self, site: Site) -> SiteSummary:
        steps = self._steps(OWNER_SITE, site.id)
        signals = self._resolved_site_signals(site)
        lane = None
        error = None
        resolved = None
        try:
            resolved = self.resolve_site(site.id)
            lane = resolved.lane
        except WorkflowError as exc:
            error = str(exc)
        return SiteSummary(
            id=site.id,
            name=site.name,
            base_url=site.base_url,
            enabled=site.enabled,
            template_id=site.template_id,
            template_name=self._template_name(site.template_id),
            lane_policy=site.lane_policy,  # type: ignore[arg-type]
            lane=lane,
            support_resign=site.support_resign,
            resign_max_days=site.resign_max_days,
            bitwarden_enabled=site.bitwarden_enabled,
            credential_provider_id=site.credential_provider_id,
            bitwarden_match_mode=site.bitwarden_match_mode,  # type: ignore[arg-type]
            bitwarden_custom_match=site.bitwarden_custom_match,
            checkin_after=site.checkin_after,
            timezone=site.timezone,
            tags=self._tags(site),
            notes=site.notes,
            config_revision=site.config_revision,
            step_count=len(resolved.steps) if resolved is not None else len(steps),
            signal_count=len([signal for signal in signals if signal.source != "system"]),
            credential=self._credential_view(site.id),
            valid=error is None,
            validation_error=error,
            created_at=site.created_at,
            updated_at=site.updated_at,
        )

    @staticmethod
    def _tags(site: Site) -> list[str]:
        try:
            value = json.loads(site.tags_json or "[]")
        except json.JSONDecodeError:
            return []
        return [str(tag) for tag in value] if isinstance(value, list) else []

    def list_templates(self) -> list[TemplateSummary]:
        rows = self.session.scalars(select(Template).order_by(Template.name.asc())).all()
        return [self._template_summary(row) for row in rows]

    def get_template(self, template_id: int) -> TemplateDetail:
        template = self._get_template(template_id)
        summary = self._template_summary(template)
        resolved = None
        try:
            resolved = self.resolve_template(template_id)
        except WorkflowError:
            pass
        site_count, child_count = self._template_dependents(template_id)
        return TemplateDetail(
            **summary.model_dump(),
            steps=[self._step_view(row) for row in self._steps(OWNER_TEMPLATE, template_id)],
            groups=[
                self._group_view(row)
                for row in self._groups(OWNER_TEMPLATE, template_id)
            ],
            groups_locked=site_count > 0 or child_count > 0,
            dependent_site_count=site_count,
            dependent_template_count=child_count,
            signals=[self._signal_view(row) for row in self._signals(OWNER_TEMPLATE, template_id)],
            resolved_signals=self._resolved_template_signals(template),
            resolved=resolved,
        )

    def _preload_site_list(self) -> None:
        self._templates_by_id = {
            row.id: row for row in self.session.scalars(select(Template)).all()
        }
        self._steps_by_owner = {}
        for row in self.session.scalars(
            select(WorkflowStep)
            .where(WorkflowStep.owner_type.in_((OWNER_TEMPLATE, OWNER_SITE)))
            .order_by(WorkflowStep.id)
        ):
            self._steps_by_owner.setdefault((row.owner_type, row.owner_id), []).append(row)
        self._signals_by_owner = {}
        for row in self.session.scalars(
            select(WorkflowSignal)
            .where(WorkflowSignal.owner_type.in_((OWNER_TEMPLATE, OWNER_SITE)))
            .order_by(WorkflowSignal.id)
        ):
            self._signals_by_owner.setdefault((row.owner_type, row.owner_id), []).append(row)
        self._groups_by_owner = {}
        for row in self.session.scalars(
            select(WorkflowGroup)
            .where(WorkflowGroup.owner_type.in_((OWNER_TEMPLATE, OWNER_SITE)))
            .order_by(WorkflowGroup.position.asc(), WorkflowGroup.id.asc())
        ):
            self._groups_by_owner.setdefault((row.owner_type, row.owner_id), []).append(row)

    def list_site_options(self, *, include_valid: bool = False) -> list[SiteOptionView]:
        rows = list(
            self.session.scalars(select(Site).order_by(Site.name.asc())).all()
        )
        templates = {
            row.id: row
            for row in self.session.scalars(select(Template)).all()
        }
        summaries: dict[int, SiteSummary] = {}
        if include_valid:
            self._templates_by_id = templates
            self._preload_site_list()
            summaries = {row.id: self._site_summary(row) for row in rows}
        return [
            SiteOptionView(
                id=row.id,
                name=row.name,
                base_url=row.base_url,
                enabled=row.enabled,
                template_id=row.template_id,
                template_name=(
                    templates[row.template_id].name
                    if row.template_id in templates
                    else None
                ),
                lane_policy=row.lane_policy,  # type: ignore[arg-type]
                lane=(
                    summaries[row.id].lane
                    if row.id in summaries
                    else self._site_lane(row)  # type: ignore[arg-type]
                ),
                valid=summaries[row.id].valid if row.id in summaries else None,
                validation_error=(
                    summaries[row.id].validation_error if row.id in summaries else None
                ),
            )
            for row in rows
        ]

    def list_sites(self) -> list[SiteSummary]:
        self._preload_site_list()
        rows = self.session.scalars(select(Site).order_by(Site.name.asc())).all()
        return [self._site_summary(row) for row in rows]

    def get_site(self, site_id: int) -> SiteDetail:
        site = self._get_site(site_id)
        summary = self._site_summary(site)
        resolved = None
        try:
            resolved = self.resolve_site(site_id)
        except WorkflowError:
            pass
        return SiteDetail(
            **summary.model_dump(),
            steps=[self._step_view(row) for row in self._steps(OWNER_SITE, site_id)],
            groups=[
                self._group_view(row) for row in self._groups(OWNER_SITE, site_id)
            ],
            signals=[self._signal_view(row) for row in self._signals(OWNER_SITE, site_id)],
            resolved_signals=self._resolved_site_signals(site),
            resolved=resolved,
        )

    # ── mutations ───────────────────────────────────────────────────────

    @staticmethod
    def _validate_step_inputs(steps: list[StepInput]) -> None:
        seen: set[str] = set()
        for step in steps:
            if step.step_key in seen:
                raise WorkflowValidationError(f"step_key 重复: {step.step_key}")
            seen.add(step.step_key)
            feature = get_feature(step.feature_type)
            if feature is None:
                raise WorkflowValidationError(
                    f"步骤 {step.step_key} 引用了未知功能类型 {step.feature_type}"
                )
            _parse_condition_expression(step.condition)
            if step.on_error is not None and step.on_error not in ERROR_POLICIES:
                raise WorkflowValidationError(f"步骤 {step.step_key} 的错误策略无效")
            try:
                step.config = feature.validate_override(step.config)
            except FeatureConfigError as exc:
                raise WorkflowValidationError(
                    f"步骤 {step.step_key} ({feature.key}) 配置无效: {exc}"
                ) from exc

    def _replace_steps(self, owner_type: str, owner_id: int, steps: list[StepInput]) -> None:
        self._validate_step_inputs(steps)
        self.session.query(WorkflowStep).filter(
            WorkflowStep.owner_type == owner_type,
            WorkflowStep.owner_id == owner_id,
        ).delete(synchronize_session=False)
        now = _utcnow()
        for step in steps:
            feature = get_feature(step.feature_type)
            assert feature is not None
            row = WorkflowStep(
                owner_type=owner_type,
                owner_id=owner_id,
                step_key=step.step_key,
                feature_type=feature.key,
                feature_version=feature.version,
                label=step.label,
                category=feature.category,
                group_key=step.group_key,
                position=step.position,
                enabled=step.enabled,
                config_json=json.dumps(
                    step.config, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ),
                condition=step.condition,
                on_error=step.on_error,
                timeout_seconds=step.timeout_seconds,
                created_at=now,
                updated_at=now,
            )
            self.session.add(row)

    def _ensure_name_available(
        self,
        model: type[Template] | type[Site],
        name: str,
        exclude_id: int | None = None,
    ) -> None:
        query = select(model).where(model.name == name)
        if exclude_id is not None:
            query = query.where(model.id != exclude_id)
        if self.session.scalar(query) is not None:
            raise WorkflowConflictError(f"名称已存在: {name}")

    def create_template(self, payload: TemplateWrite) -> TemplateDetail:
        self._ensure_name_available(Template, payload.name)
        if payload.parent_id is not None:
            self._get_template(payload.parent_id)
        template = Template(
            name=payload.name,
            description=payload.description,
            parent_id=payload.parent_id,
            enabled=payload.enabled,
            version=1,
        )
        self.session.add(template)
        try:
            self.session.flush()
            inherited_groups: list[ResolvedGroupView] = []
            inherited_effective: list[_EffectiveStep] = []
            if payload.parent_id is not None:
                parent = self._get_template(payload.parent_id)
                inherited_groups = self._resolved_template_groups(parent)
                inherited_effective = self._resolved_template_steps(parent)
            inherited_steps = {
                step.step_key: step.group_key for step in inherited_effective
            }
            self._validate_group_inputs(payload.groups, inherited_groups)
            self._validate_step_groups(
                payload.steps,
                inherited_steps,
                {group.group_key for group in payload.groups},
                {group.group_key for group in inherited_groups},
            )
            self._apply_override_positions(payload.steps, inherited_effective)
            self._replace_signals(OWNER_TEMPLATE, template.id, payload.signals)
            self._replace_groups(OWNER_TEMPLATE, template.id, payload.groups)
            self._replace_steps(OWNER_TEMPLATE, template.id, payload.steps)
            self.session.flush()
            self.resolve_template(template.id)
            self.session.commit()
        except WorkflowError:
            self.session.rollback()
            raise
        except IntegrityError as exc:
            self.session.rollback()
            raise WorkflowConflictError("模板名称或继承关系冲突") from exc
        return self.get_template(template.id)

    def update_template(self, template_id: int, payload: TemplateWrite) -> TemplateDetail:
        template = self._get_template(template_id)
        self._ensure_name_available(Template, payload.name, exclude_id=template_id)
        if payload.parent_id is not None:
            if payload.parent_id == template_id:
                raise WorkflowValidationError("模板不能继承自身")
            parent_chain = self._template_chain(payload.parent_id)
            if any(item.id == template_id for item in parent_chain):
                raise WorkflowValidationError("模板继承存在循环")
        inherited_groups: list[ResolvedGroupView] = []
        inherited_effective: list[_EffectiveStep] = []
        if payload.parent_id is not None:
            parent = self._get_template(payload.parent_id)
            inherited_groups = self._resolved_template_groups(parent)
            inherited_effective = self._resolved_template_steps(parent)
        inherited_steps = {
            step.step_key: step.group_key for step in inherited_effective
        }
        self._validate_template_structure(
            template, payload.groups, payload.steps, payload.parent_id
        )
        self._validate_group_inputs(payload.groups, inherited_groups)
        self._validate_step_groups(
            payload.steps,
            inherited_steps,
            {group.group_key for group in payload.groups},
            {group.group_key for group in inherited_groups},
        )
        self._apply_override_positions(payload.steps, inherited_effective)
        template.name = payload.name
        template.description = payload.description
        template.parent_id = payload.parent_id
        template.enabled = payload.enabled
        template.version += 1
        template.updated_at = _utcnow()
        try:
            self._replace_groups(OWNER_TEMPLATE, template.id, payload.groups)
            self._replace_steps(OWNER_TEMPLATE, template.id, payload.steps)
            self.session.flush()
            self._check_removed_signals(OWNER_TEMPLATE, template.id, payload.signals)
            self._replace_signals(OWNER_TEMPLATE, template.id, payload.signals)
            self.session.flush()
            self.resolve_template(template.id)
            self.session.commit()
        except WorkflowError:
            self.session.rollback()
            raise
        except IntegrityError as exc:
            self.session.rollback()
            raise WorkflowConflictError("模板名称或继承关系冲突") from exc
        return self.get_template(template.id)

    def delete_template(self, template_id: int) -> None:
        template = self._get_template(template_id)
        child = self.session.scalar(select(Template.id).where(Template.parent_id == template_id))
        if child is not None:
            raise WorkflowConflictError("模板仍有子模板，不能删除")
        used = self.session.scalar(select(Site.id).where(Site.template_id == template_id))
        if used is not None:
            raise WorkflowConflictError("模板仍被站点引用，不能删除")
        self.session.query(WorkflowStep).filter(
            WorkflowStep.owner_type == OWNER_TEMPLATE,
            WorkflowStep.owner_id == template_id,
        ).delete(synchronize_session=False)
        self.session.query(WorkflowGroup).filter(
            WorkflowGroup.owner_type == OWNER_TEMPLATE,
            WorkflowGroup.owner_id == template_id,
        ).delete(synchronize_session=False)
        self.session.query(WorkflowSignal).filter(
            WorkflowSignal.owner_type == OWNER_TEMPLATE,
            WorkflowSignal.owner_id == template_id,
        ).delete(synchronize_session=False)
        self.session.delete(template)
        self.session.commit()

    def create_site(self, payload: SiteWrite) -> SiteDetail:
        self._ensure_name_available(Site, payload.name)
        if payload.template_id is not None:
            self._get_template(payload.template_id)
        site = Site(
            name=payload.name,
            base_url=payload.base_url,
            enabled=payload.enabled,
            template_id=payload.template_id,
            lane_policy=payload.lane_policy,
            support_resign=payload.support_resign,
            resign_max_days=payload.resign_max_days,
            bitwarden_enabled=payload.bitwarden_enabled,
            credential_provider_id=payload.credential_provider_id,
            bitwarden_match_mode=payload.bitwarden_match_mode,
            bitwarden_custom_match=payload.bitwarden_custom_match,
            checkin_after=payload.checkin_after,
            timezone=payload.timezone,
            tags_json=json.dumps(payload.tags, ensure_ascii=False),
            notes=payload.notes,
            config_revision=1,
        )
        self.session.add(site)
        try:
            self.session.flush()
            inherited_groups: list[ResolvedGroupView] = []
            inherited_steps: list[_EffectiveStep] = []
            if payload.template_id is not None:
                template = self._get_template(payload.template_id)
                inherited_groups = self._resolved_template_groups(template)
                inherited_steps = self._resolved_template_steps(template)
            self._validate_group_inputs(payload.groups, inherited_groups)
            self._validate_step_groups(
                payload.steps,
                {step.step_key: step.group_key for step in inherited_steps},
                {group.group_key for group in payload.groups},
                {group.group_key for group in inherited_groups},
            )
            self._apply_override_positions(payload.steps, inherited_steps)
            self._replace_groups(OWNER_SITE, site.id, payload.groups)
            self._replace_signals(OWNER_SITE, site.id, payload.signals)
            self._replace_steps(OWNER_SITE, site.id, payload.steps)
            self.session.flush()
            self.resolve_site(site.id)
            self.session.commit()
        except WorkflowError:
            self.session.rollback()
            raise
        except IntegrityError as exc:
            self.session.rollback()
            raise WorkflowConflictError("站点名称冲突") from exc
        return self.get_site(site.id)

    def update_site(self, site_id: int, payload: SiteWrite) -> SiteDetail:
        site = self._get_site(site_id)
        self._ensure_name_available(Site, payload.name, exclude_id=site_id)
        if payload.template_id is not None:
            self._get_template(payload.template_id)
        inherited_groups: list[ResolvedGroupView] = []
        inherited_steps: list[_EffectiveStep] = []
        if payload.template_id is not None:
            template = self._get_template(payload.template_id)
            inherited_groups = self._resolved_template_groups(template)
            inherited_steps = self._resolved_template_steps(template)
        self._validate_group_inputs(payload.groups, inherited_groups)
        self._validate_step_groups(
            payload.steps,
            {step.step_key: step.group_key for step in inherited_steps},
            {group.group_key for group in payload.groups},
            {group.group_key for group in inherited_groups},
        )
        self._apply_override_positions(payload.steps, inherited_steps)
        site.name = payload.name
        site.base_url = payload.base_url
        site.enabled = payload.enabled
        site.template_id = payload.template_id
        site.lane_policy = payload.lane_policy
        site.support_resign = payload.support_resign
        site.resign_max_days = payload.resign_max_days
        site.bitwarden_enabled = payload.bitwarden_enabled
        site.credential_provider_id = payload.credential_provider_id
        site.bitwarden_match_mode = payload.bitwarden_match_mode
        site.bitwarden_custom_match = payload.bitwarden_custom_match
        site.checkin_after = payload.checkin_after
        site.timezone = payload.timezone
        site.tags_json = json.dumps(payload.tags, ensure_ascii=False)
        site.notes = payload.notes
        site.config_revision += 1
        site.updated_at = _utcnow()
        try:
            self._replace_groups(OWNER_SITE, site.id, payload.groups)
            self._replace_steps(OWNER_SITE, site.id, payload.steps)
            self.session.flush()
            self._check_removed_signals(OWNER_SITE, site.id, payload.signals)
            self._replace_signals(OWNER_SITE, site.id, payload.signals)
            self.session.flush()
            self.resolve_site(site.id)
            self.session.commit()
        except WorkflowError:
            self.session.rollback()
            raise
        except IntegrityError as exc:
            self.session.rollback()
            raise WorkflowConflictError("站点名称冲突") from exc
        return self.get_site(site.id)

    def delete_site(self, site_id: int) -> None:
        site = self._get_site(site_id)
        self.session.query(WorkflowStep).filter(
            WorkflowStep.owner_type == OWNER_SITE,
            WorkflowStep.owner_id == site_id,
        ).delete(synchronize_session=False)
        self.session.query(WorkflowGroup).filter(
            WorkflowGroup.owner_type == OWNER_SITE,
            WorkflowGroup.owner_id == site_id,
        ).delete(synchronize_session=False)
        self.session.query(WorkflowSignal).filter(
            WorkflowSignal.owner_type == OWNER_SITE,
            WorkflowSignal.owner_id == site_id,
        ).delete(synchronize_session=False)
        self.session.query(SiteCredential).filter(
            SiteCredential.site_id == site_id,
        ).delete(synchronize_session=False)
        self.session.delete(site)
        self.session.commit()
