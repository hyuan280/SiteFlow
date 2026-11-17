from __future__ import annotations

import json

from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, OperationalError

from app.core.crypto import CryptoError
from app.models.integration import IntegrationConfig, utcnow
from app.schemas.integration import (
    BooleanView, IntegrationTestResult, NumericView, TokenView, ValueView, VisionView,
)
from app.schemas.model_registry import (
    ModelConfigUpdate, ModelTestRequest, ModelUpdate, ModelView,
    RegistryDocument, RegistryUpdate, RegistryView,
)
from app.services.integrations import (
    ConfigLockedError, IntegrationService, IntegrationValidationError, MODEL_CONFIG_FIELDS,
)

REGISTRY_KEY = "models.registry"


class ModelRegistryError(RuntimeError):
    def __init__(self, code: str, status: int):
        self.code = code
        self.status = status
        super().__init__(code)


def strict_json(value: str | bytes):
    def pairs(entries):
        result = {}
        for key, item in entries:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = item
        return result

    def invalid_constant(_):
        raise ValueError("invalid JSON")

    return json.loads(value, object_pairs_hook=pairs, parse_constant=invalid_constant)


class ModelRegistryService:
    def __init__(self, session, settings, crypto):
        self.session = session
        self.settings = settings
        self.crypto = crypto
        self.primary = IntegrationService(session, settings, crypto)

    def _load(self, *, lock=False):
        if lock and self.session.get_bind().dialect.name == "sqlite":
            # Also locks the absent-row case, without creating a registry on reads.
            self.session.execute(update(IntegrationConfig).where(
                IntegrationConfig.key == REGISTRY_KEY,
            ).values(key=REGISTRY_KEY).execution_options(synchronize_session=False, autoflush=False))
        query = select(IntegrationConfig).where(IntegrationConfig.key == REGISTRY_KEY)
        if lock:
            query = query.with_for_update()
        row = self.session.scalar(query.execution_options(populate_existing=True, autoflush=False))
        if lock:
            # Discard pre-lock primary snapshots before validation or mutation.
            for cached in list(self.session.identity_map.values()):
                if isinstance(cached, IntegrationConfig) and cached.key.startswith("vision."):
                    self.session.expire(cached)
        if row is None:
            items = [self._legacy_item()] if self._has_legacy_config() or self.primary.model_env_configured() else []
            return RegistryDocument(version=1, revision=0, items=items), None
        snapshot = (row.value_ciphertext, row.nonce, row.key_version)
        try:
            raw = self.crypto.decrypt(REGISTRY_KEY, *snapshot)
            if len(raw) > 256 * 1024:
                raise ValueError("registry too large")
            doc = RegistryDocument.model_validate(strict_json(raw))
        except (CryptoError, ValueError, TypeError, RecursionError):
            raise ModelRegistryError("model_registry_unavailable", 503) from None
        env = self.primary.model_env_configured()
        if doc.environment_only and not env and not self._has_legacy_config():
            # Keep stored metadata intact so re-enabling ENV before a save restores it.
            doc = doc.model_copy(update={"items": [item for item in doc.items if item.id != "primary"]})
        if env and not any(item.id == "primary" for item in doc.items):
            # Deployment configuration is visible immediately, without a migration write.
            doc = doc.model_copy(update={"items": [*doc.items, self._legacy_item()]})
        return doc, snapshot

    def _has_legacy_config(self):
        return self.session.scalar(select(IntegrationConfig.key).where(
            IntegrationConfig.key.in_([f"vision.{field}" for field in MODEL_CONFIG_FIELDS]),
        ).limit(1).execution_options(autoflush=False)) is not None

    def _legacy_item(self):
        return ModelUpdate(
            id="primary", enabled=True, usages=["feature_cards", "assistant"],
            name="\u73af\u5883\u53d8\u91cf\u6a21\u578b" if self.primary.model_env_configured() else "\u9ed8\u8ba4\u6a21\u578b",
        )

    def _validate_item_limit(self, items):
        env = self.primary.model_env_configured()
        if len(items) > 21 or sum(not (env and item.id == "primary") for item in items) > 20:
            raise ModelRegistryError("model_registry_limit", 422)

    def _save(self, doc, snapshot, owner):
        self._validate_item_limit(doc.items)
        doc.environment_only = (
            self.primary.model_env_configured()
            and any(item.id == "primary" for item in doc.items)
            and not self._has_legacy_config()
        )
        if doc.revision >= 9007199254740991:
            raise ModelRegistryError("model_registry_unavailable", 503)
        doc.revision += 1
        raw = json.dumps(doc.model_dump(exclude_unset=True), ensure_ascii=True)
        if len(raw) > 256 * 1024:
            raise ModelRegistryError("model_registry_limit", 422)
        encrypted = self.crypto.encrypt(REGISTRY_KEY, raw)
        values = dict(value_ciphertext=encrypted.ciphertext, nonce=encrypted.nonce,
                      key_version=encrypted.key_version, is_secret=True,
                      fingerprint=self.crypto.fingerprint(raw), updated_at=utcnow(), updated_by=owner)
        if snapshot is None:
            self.session.add(IntegrationConfig(key=REGISTRY_KEY, **values))
            self.session.flush()
        else:
            result = self.session.execute(update(IntegrationConfig).where(
                IntegrationConfig.key == REGISTRY_KEY,
                IntegrationConfig.value_ciphertext == snapshot[0],
                IntegrationConfig.nonce == snapshot[1],
                IntegrationConfig.key_version == snapshot[2],
            ).values(**values).execution_options(synchronize_session=False))
            if result.rowcount != 1:
                raise ModelRegistryError("model_registry_conflict", 409)

    def touch_legacy(self, owner="api", *, add_primary=False):
        try:
            doc, snapshot = self._load(lock=True)
            if add_primary and not any(item.id == "primary" for item in doc.items):
                doc.items.append(self._legacy_item())
            self._archive(doc, self._primary_api_keys())
            self._save(doc, snapshot, owner)
        except (IntegrityError, OperationalError):
            self.session.rollback()
            raise ModelRegistryError("model_registry_conflict", 409) from None
        except ModelRegistryError:
            self.session.rollback()
            raise

    def _primary_api_keys(self):
        try:
            # Column reads bypass stale ORM identities and never flush pending mutations.
            row = self.session.execute(select(
                IntegrationConfig.value_ciphertext, IntegrationConfig.nonce, IntegrationConfig.key_version,
            ).where(IntegrationConfig.key == "vision.api_key").execution_options(autoflush=False)).first()
            env = self.settings.vision_api_key
            env = env.get_secret_value() if hasattr(env, "get_secret_value") else env
            keys = [(env or "").strip() or None,
                    self.crypto.decrypt("vision.api_key", *row) if row else None]
            if any(key is not None and (not key.strip() or len(key) > 4096) for key in keys):
                raise ValueError("invalid primary key")
            return list(dict.fromkeys(key for key in keys if key))
        except (CryptoError, IntegrationValidationError, ValueError, TypeError):
            raise ModelRegistryError("model_registry_unavailable", 503) from None

    def _archive(self, doc, keys):
        archived = list(dict.fromkeys([*doc.redaction_keys, *keys]))
        if len(archived) > 512:
            raise ModelRegistryError("model_registry_limit", 422)
        doc.redaction_keys = archived

    def _primary_view(self, *, strict=True):
        try:
            view = self.primary._vision_view()
            supports = self.primary.resolve_vision_supports_vision().value
            invalid = supports not in {"true", "false"} or view.max_tokens.error or view.base_url.error
            for field, limit in (("base_url", 2048), ("model", 256), ("api_key", 4096)):
                value = self.primary.resolve_vision(field).value
                if value is not None and (not value.strip() or len(value) > limit):
                    invalid = True
            if invalid:
                if strict:
                    raise ValueError("invalid primary")
                view.available = False
            return view
        except (IntegrationValidationError, ValueError, TypeError):
            raise ModelRegistryError("model_registry_unavailable", 503) from None

    def _runtime(self, item):
        if item.id == "primary":
            self._primary_view()
            return self.primary.vision_runtime()
        config = item.config.model_dump(exclude_unset=True)
        if not all(config.get(field) for field in ("base_url", "model", "api_key")):
            return None
        return {**{field: config[field] for field in ("base_url", "model", "api_key")},
                "supports_vision": config.get("supports_vision", True),
                "timeout": self.settings.vision_timeout,
                **({"max_tokens": config["max_tokens"]} if config.get("max_tokens") else {})}

    def view(self):
        doc, _ = self._load()
        items = []
        for item in doc.items:
            if item.id == "primary":
                config = self._primary_view(strict=False)
            else:
                fields = item.config.model_dump(exclude_unset=True)
                def source(field):
                    return "db" if field in fields else "unset"
                key = fields.get("api_key")
                config = VisionView(
                    base_url=ValueView(value=fields.get("base_url"), source=source("base_url"), editable=True),
                    model=ValueView(value=fields.get("model"), source=source("model"), editable=True),
                    api_key=TokenView(configured=bool(key), source=source("api_key"), editable=True,
                                      masked="****" + self.crypto.fingerprint(key) if key else None),
                    supports_vision=BooleanView(value=fields.get("supports_vision", True),
                                               source=source("supports_vision"), editable=True),
                    max_tokens=NumericView(value=fields.get("max_tokens"), source=source("max_tokens"), editable=True),
                    available=self._runtime(item) is not None,
                )
            env = item.id == "primary" and self.primary.model_env_configured()
            items.append(ModelView(**item.model_dump(exclude={"config"}), config=config,
                                   source="env" if env else "db", config_editable=not env))
        return RegistryView(revision=doc.revision, items=items)

    def update(self, payload, owner):
        try:
            if not isinstance(payload, RegistryUpdate):
                payload = RegistryUpdate.model_validate(payload)
            self._validate_item_limit(payload.items)
            doc, snapshot = self._load(lock=True)
            if payload.expected_revision != doc.revision:
                raise ModelRegistryError("model_registry_conflict", 409)
            old = {item.id: item for item in doc.items}
            has_primary = any(item.id == "primary" for item in payload.items)
            if self.primary.model_env_configured() and (
                not has_primary or any(item.id == "primary" and item.config.model_fields_set
                                       for item in payload.items)
            ):
                raise ModelRegistryError("model_config_locked", 409)
            remove_primary = "primary" in old and not has_primary
            items = []
            primary_fields = {}
            for item in payload.items:
                fields = item.config.model_dump(exclude_unset=True)
                if item.id == "primary":
                    primary_fields = fields
                    items.append(ModelUpdate(**item.model_dump(exclude={"config"})))
                else:
                    previous = old[item.id].config.model_dump(exclude_unset=True) if item.id in old else {}
                    fields = {**previous, **fields}
                    if "max_tokens" in fields and fields["max_tokens"] is None:
                        fields["max_tokens"] = 0
                    merged = ModelUpdate(**item.model_dump(exclude={"config"}), config=fields)
                    if item.id not in old and self._runtime(merged) is None:
                        raise ModelRegistryError("model_config_incomplete", 422)
                    items.append(merged)
            next_keys = {item.id: item.config.api_key for item in items if item.id != "primary"}
            retired = [item.config.api_key for item in doc.items if item.id != "primary"
                       and item.config.api_key and item.config.api_key != next_keys.get(item.id)]
            if primary_fields or remove_primary:
                retired.extend(self._primary_api_keys())
            self._archive(doc, retired)
            doc.items = items
            self._save(doc, snapshot, owner)
            if remove_primary:
                self.primary._delete_vision_config()
            self.primary.update_vision(_commit=False, **primary_fields)
            self.session.flush()
            result = self.view()
            self.session.commit()
            return result
        except ConfigLockedError:
            self.session.rollback()
            raise ModelRegistryError("model_config_locked", 409) from None
        except (ValidationError, IntegrationValidationError):
            self.session.rollback()
            raise ModelRegistryError("invalid_model_registry", 422) from None
        except (IntegrityError, OperationalError):
            self.session.rollback()
            raise ModelRegistryError("model_registry_conflict", 409) from None
        except ModelRegistryError:
            self.session.rollback()
            raise

    def select_runtime(self, usage, requires_vision=False):
        if usage not in {"feature_cards", "assistant", "mcp"}:
            raise ModelRegistryError("invalid_model_usage", 422)
        doc, _ = self._load()
        for item in doc.items:
            if not item.enabled or usage not in item.usages:
                continue
            runtime = self._runtime(item)
            if runtime and (not requires_vision or runtime.get("supports_vision", True)):
                return runtime
        return None

    def known_api_keys(self):
        doc, _ = self._load()
        keys = [*doc.redaction_keys, *self._primary_api_keys()]
        keys.extend(item.config.api_key for item in doc.items if item.id != "primary" and item.config.api_key)
        return list(dict.fromkeys(keys))

    def test(self, model_id, payload=None):
        from app.services.model_probe import probe_model

        # Testing is deliberately independent of selection, writes and the key archive.
        with self.session.no_autoflush:
            try:
                if payload is not None and not isinstance(payload, ModelTestRequest):
                    payload = ModelTestRequest.model_validate(payload)
                doc, _ = self._load()
                if payload is not None and payload.expected_revision != doc.revision:
                    raise ModelRegistryError("model_registry_conflict", 409)
                item = next((item for item in doc.items if item.id == model_id), None)
                if item is None:
                    raise ModelRegistryError("model_not_found", 404)
                fields = payload.config.model_dump(exclude_unset=True) if payload else {}
                if item.id == "primary":
                    if fields and self.primary.model_env_configured():
                        raise ModelRegistryError("model_config_locked", 409)
                    view = self._primary_view(strict=False)
                    config = {field: self.primary.resolve_vision(field).value
                              for field in ("base_url", "model", "api_key")}
                    supports = self.primary.resolve_vision_supports_vision().value
                    if ((view.base_url.error and "base_url" not in fields)
                            or (view.max_tokens.error and "max_tokens" not in fields)
                            or (supports not in {"true", "false"} and "supports_vision" not in fields)):
                        raise ValueError("invalid saved configuration")
                    config.update(supports_vision=supports == "true")
                else:
                    config = item.config.model_dump(exclude_unset=True)
                config = ModelConfigUpdate.model_validate({**config, **fields}).model_dump()
                if item.id == "primary" and "max_tokens" not in fields:
                    # Legacy caps are already validated by NumericView, without the registry's int32 limit.
                    config["max_tokens"] = view.max_tokens.value
                if not all(config.get(field) for field in ("base_url", "model", "api_key")):
                    raise ValueError("incomplete configuration")
            except (ValidationError, ValueError, IntegrationValidationError):
                if payload is not None:
                    raise ModelRegistryError("invalid_model_registry", 422) from None
                return IntegrationTestResult(
                    ok=False, message="模型配置不完整或无效，请检查地址、模型和密钥。",
                    data={"mode": "text", "stage": "configuration"},
                )
        return probe_model({**config, "timeout": self.settings.vision_timeout})
