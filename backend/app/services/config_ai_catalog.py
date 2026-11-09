"""Reviewed authoring metadata, deliberately separate from the administrator catalog."""

from __future__ import annotations

import json
from copy import deepcopy

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.features.base import FIELD_TITLES
from app.features.catalog import FEATURE_LIST
from app.models.setting import AppSetting
from app.services.config_ai_protocol import _strip_schema_annotations
from app.services.workflows import WorkflowNotFoundError, WorkflowValidationError


class ConfigAICatalog:
    def __init__(self, session: Session):
        self.session = session

    @staticmethod
    def _features():
        # Plugin approval authorizes execution, not disclosure to a model. No plugin
        # descriptions, schemas, annotations or captcha enum extensions enter this view.
        return {feature.key: feature for feature in FEATURE_LIST if feature.generation_hints is not None}

    @staticmethod
    def _summary(feature):
        return {
            "feature_type": feature.key,
            "name": feature.name,
            "description": feature.description,
            "category": feature.category,
            "cardinality": feature.cardinality,
            "requires": sorted(feature.requires),
            "version": feature.version,
            "source": "builtin",
            "when_to_use": feature.generation_hints.when_to_use,
        }

    def list_features(self, category: str | None = None):
        return {
            "version": 1,
            "features": [self._summary(feature) for feature in self._features().values()
                         if category is None or feature.category == category],
            "plugin_policy": "Plugins require a separate reviewed model-export policy and are excluded.",
        }

    def get_feature_schema(self, feature_type: str):
        feature = self._features().get(feature_type)
        if feature is None:
            # Never reflect an arbitrary requested key into model-visible diagnostics.
            raise WorkflowNotFoundError("Feature unavailable for configuration generation")
        # Use code-owned schemas, not FeatureSettingsService.view() (effective secrets)
        # or config_schema() (which injects unreviewed plugin captcha metadata).
        schema = deepcopy(feature.config_model.model_json_schema())
        _strip_schema_annotations(schema)
        excluded = []
        properties = schema.get("properties", {})
        for key, field in list(properties.items()):
            if (field.get("ui:hidden") or field.get("readOnly") or field.get("writeOnly")
                    or field.get("format") == "password" or key in {"llm_prompt", "llm_enabled"}):
                excluded.append(key)
                del properties[key]
            else:
                if key in FIELD_TITLES:
                    field["title"] = FIELD_TITLES[key]
        required = list(schema.get("required", []))
        with self.session.no_autoflush:
            raw = self.session.scalar(select(AppSetting.value_json).where(
                AppSetting.key == f"feature.{feature.key}",
            ))
        if raw is not None:
            try:
                policy = json.loads(raw)
                configured = policy["required_fields"]
                if not isinstance(configured, list) or not all(isinstance(key, str) for key in configured):
                    raise ValueError()
                required = [key for key in configured if key in feature.config_model.model_fields]
            except (ValueError, TypeError, KeyError):
                raise WorkflowValidationError("Feature generation policy unavailable") from None
        if feature.key == "challenge.2fa":
            required = sorted(set(required) | {"service_hint"})
        schema["required"] = [key for key in required if key in properties]
        override_schema = deepcopy(schema)
        override_schema.pop("required", None)
        for key, field in override_schema.get("properties", {}).items():
            override_schema["properties"][key] = {"anyOf": [field, {"type": "null"}]}
        return {
            **self._summary(feature),
            "config_schema": schema,
            "override_config_schema": override_schema,
            "generation_hints": feature.generation_hints.model_dump(mode="json"),
            "required_fields": schema["required"],
            "excluded_fields": sorted(excluded),
            "user_input_required": sorted(set(required) & set(excluded)),
            "defaults_policy": "Values are omitted; server defaults apply during validation. Do not guess them.",
            "schema_scope": (
                "config_schema describes effective values; override_config_schema describes local overrides. "
                "Omission inherits and null clears a key before defaults. Required effective fields may be "
                "inherited. Neither schema replaces full draft validation or authorization."
            ),
        }
