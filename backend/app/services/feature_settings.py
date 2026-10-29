import json
from copy import deepcopy

from pydantic import TypeAdapter, ValidationError
from sqlalchemy.orm import Session

from app.features.base import (
    CAPTCHA_DEFAULT_PROMPT,
    FeatureConfigError,
    IMAGE_QUIZ_DEFAULT_PROMPT,
    VISION_FEATURES,
)
from app.features.plugin_registry import (
    RemoteFeature,
    schema_defaults,
    schema_required,
)
from app.models.setting import AppSetting
from app.schemas.workflow import FeatureView, FeatureExecutionSettings


def _is_remote(feature) -> bool:
    return isinstance(feature, RemoteFeature)


def _properties(feature) -> dict:
    if _is_remote(feature):
        return feature.schema.get("properties") or {}
    return feature.config_model.model_fields


def _field_names(feature) -> set[str]:
    return set(_properties(feature))


def _base_required(feature) -> list[str]:
    if feature.key == "challenge.2fa":
        return ["service_hint"]
    if _is_remote(feature):
        return schema_required(feature.schema)
    return [
        key
        for key, field in feature.config_model.model_fields.items()
        if field.is_required()
    ]


def _initial_defaults(feature) -> dict:
    if _is_remote(feature):
        return schema_defaults(feature.schema)
    return {
        key: deepcopy(field.get_default(call_default_factory=True))
        for key, field in feature.config_model.model_fields.items()
        if not field.is_required()
    }


def _validate_default(feature, key: str, value) -> None:
    if _is_remote(feature):
        feature.validate_field(key, value)
        return
    field = deepcopy(feature.config_model.model_fields[key])
    from typing import Annotated

    annotation = (
        Annotated[field.annotation, *field.metadata]
        if field.metadata
        else field.annotation
    )
    TypeAdapter(annotation).validate_python(value)


class FeatureSettingsService:
    def __init__(self, session: Session):
        self.session = session
        self._policy_cache: dict[str, tuple[dict, list[str], bool]] = {}

    def policy(self, feature):
        cached = self._policy_cache.get(feature.key)
        if cached is not None:
            return cached
        defaults = _initial_defaults(feature)
        required = _base_required(feature)
        row = self.session.get(AppSetting, f"feature.{feature.key}")
        if row:
            policy = json.loads(row.value_json)
            policy_defaults = policy.get("defaults", {})
            field_names = _field_names(feature)
            defaults.update({k: v for k, v in policy_defaults.items()
                              if k in field_names})
            required = [k for k in policy["required_fields"]
                         if k in field_names]
        if feature.key == "challenge.2fa":
            required = sorted(set(required) | {"service_hint"})
        result = (defaults, required, row is not None)
        self._policy_cache[feature.key] = result
        return result

    def execution_settings(self, feature):
        if feature.key not in VISION_FEATURES:
            return None
        row = self.session.get(AppSetting, f"feature.{feature.key}")
        policy = json.loads(row.value_json) if row else {}
        default_prompt = (
            IMAGE_QUIZ_DEFAULT_PROMPT
            if feature.key == "sign.image_quiz"
            else CAPTCHA_DEFAULT_PROMPT
        )
        stored = policy.get("execution_settings") or {}
        prompt = stored.get("llm_prompt")
        if not isinstance(prompt, str) or not prompt:
            prompt = default_prompt
        return FeatureExecutionSettings.model_validate({"llm_prompt": prompt})

    def view(self, feature):
        defaults, required, customized = self.policy(feature)
        schema = feature.config_schema()
        schema["required"] = required
        for key, value in defaults.items():
            schema["properties"][key]["default"] = value
        return FeatureView(
            key=feature.key,
            name=feature.name,
            category=feature.category,
            cardinality=feature.cardinality,
            requires=sorted(feature.requires),
            description=feature.description,
            version=feature.version,
            config_schema=schema,
            defaults=defaults,
            required_fields=required,
            base_required_fields=_base_required(feature),
            customized=customized,
            execution_settings=self.execution_settings(feature),
            source=getattr(feature, "plugin_id", "builtin"),
        )

    def update(self, feature, defaults, required, execution_settings=None):
        if execution_settings is not None and feature.key not in VISION_FEATURES:
            raise FeatureConfigError("Execution settings are not supported for this feature")
        if execution_settings is not None:
            values = dict(execution_settings)
            if feature.key == "sign.image_quiz":
                values.setdefault("llm_prompt", IMAGE_QUIZ_DEFAULT_PROMPT)
            try:
                behavior = FeatureExecutionSettings.model_validate(values)
            except ValidationError:
                raise FeatureConfigError(
                    "Invalid execution settings: must satisfy code type and constraints"
                ) from None
        else:
            behavior = self.execution_settings(feature)
        field_names = _field_names(feature)
        if feature.key == "challenge.2fa" and "service_hint" not in required:
            raise FeatureConfigError("Cannot relax required field service_hint")
        unknown = (set(defaults) | set(required)) - field_names
        if unknown:
            raise FeatureConfigError(f"Unknown fields: {', '.join(sorted(unknown))}")
        for key, value in defaults.items():
            try:
                _validate_default(feature, key, value)
            except ValidationError:
                raise FeatureConfigError(
                    f"Invalid default for {key}: must satisfy code type and constraints"
                ) from None
            except FeatureConfigError:
                raise
        for key in _base_required(feature):
            if key not in required and key not in defaults:
                raise FeatureConfigError(
                    f"Cannot relax required field {key}: supply a valid default first"
                )
        # Run schema validation whenever the supplied defaults form a complete config.
        if all(key in defaults for key in _base_required(feature)) and not (
            feature.key == "challenge.2fa"
            and not defaults.get("service_hint", "").strip()
        ):
            feature.validate_full(defaults)
        row = self.session.get(AppSetting, f"feature.{feature.key}")
        if row is None:
            row = AppSetting(key=f"feature.{feature.key}")
            self.session.add(row)
        policy = json.loads(row.value_json) if row.value_json else {}
        policy.update({"defaults": defaults, "required_fields": sorted(set(required))})
        if behavior is not None:
            policy["execution_settings"] = behavior.model_dump(mode="json")
        row.value_json = json.dumps(policy)
        self.session.commit()
        self._policy_cache.clear()
        return self.view(feature)

    def validate(self, feature, config):
        defaults, required, _ = self.policy(feature)
        merged = {**defaults, **config}
        missing = [key for key in required if key not in merged or merged[key] is None]
        if missing:
            raise FeatureConfigError(f"Required fields missing: {', '.join(missing)}")
        return feature.validate_full(merged)
