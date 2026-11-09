"""Value-free validation metadata and the model-facing diagnostic projection."""

from __future__ import annotations

from dataclasses import dataclass, replace
import math
import re
from typing import Any


MAX_DIAGNOSTICS = 10
_KEY = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_MESSAGES = {
    "field_required": "Supply this required field; omission or null cannot satisfy the current policy.",
    "field_type": "Use the JSON type specified by this field's schema.",
    "field_enum": "Choose one of the allowed values in this field's schema.",
    "field_bounds": "Adjust this value to satisfy the field's numeric or length constraints.",
    "field_pattern": "Use the format specified by this field's schema.",
    "field_invalid": "Correct this field using the feature schema and its validation requirements.",
    "totp_service_hint_required": "Supply a nonblank service_hint for the 2FA provider.",
    "flow_jump_evidence_required": "Add at least one page text in texts or a selector in selectors.",
    "flow_jump_target_required": "Supply a nonblank target_step_key when action is jump.",
    "flow_jump_target_invalid": "Remove colons from target_step_key; use a workflow step key.",
    "buy_card_product_selectors_required": "Supply product_selectors when layout is custom.",
    "buy_card_price_selectors_required": "Supply price_selectors when layout is grid or custom.",
    "resign_selector_template_invalid": "Use only {date}, {day}, {month}, or {year} template placeholders.",
    "resign_selector_date_required": "Include {date} or {day} in each target-date selector template.",
    "extra_fields": "Remove unknown or protected fields; consult the permitted schema.",
    "duplicate_step": "Use a unique step_key for each local step.",
    "duplicate_signal": "Use a unique signal_key for each local signal.",
    "duplicate_group": "Use a unique group_key for each local group.",
    "signal_reserved": "Choose a non-system signal key.",
    "signal_evidence_required": "Add at least one nonblank include text or include selector.",
    "group_name_required": "Supply a nonblank group name.",
    "group_missing": "Define this local group or remove the step's group_key.",
    "group_inherited": "Keep inherited grouping unchanged; new steps require a local group.",
    "signal_missing": "Define the signal referenced by this condition or change the condition.",
    "signal_disabled": "Enable the referenced signal or change the condition.",
    "condition_invalid": "Use always, signal:<key>:true/false, or all(...)/any(...) conditions.",
    "feature_unavailable": "Choose an available feature from the feature catalog.",
    "feature_version_unsupported": "Use the currently supported feature version.",
    "feature_inheritance_mismatch": "Keep the inherited feature type or use a different step_key.",
    "singleton_duplicate": "Keep only one enabled step for this single-instance feature.",
    "order_collision": "Assign distinct positions to ungrouped steps.",
    "cfbox_required": "Ask an administrator to configure and enable the CFbox integration.",
    "error_policy_invalid": "Choose a supported on_error policy from the step schema.",
    "template_missing": "Select an existing template or remove the template binding.",
    "template_cycle": "Choose a parent template that does not create an inheritance cycle.",
    "template_in_use": "Preserve the structure of this template while descendants depend on it.",
    "signal_in_use": "Remove dependent step conditions before removing signals.",
    "name_conflict": "Choose a different workflow name; this name is already in use.",
}

# Root model validators have no Pydantic field location. Only recognized,
# code-owned error types may supply one; never infer it from exception messages.
_SEMANTIC_PATHS = {
    "totp_service_hint_required": ("service_hint",),
    "flow_jump_evidence_required": ("texts",),
    "flow_jump_target_required": ("target_step_key",),
    "flow_jump_target_invalid": ("target_step_key",),
    "buy_card_product_selectors_required": ("product_selectors",),
    "buy_card_price_selectors_required": ("price_selectors",),
    "resign_selector_template_invalid": (),
    "resign_selector_date_required": (),
}


@dataclass(frozen=True)
class WorkflowDiagnostic:
    code: str
    path: tuple[str | int, ...] = ()
    step_key: str | None = None
    feature_type: str | None = None
    hint: str | None = None


def schema_fields(schema: dict) -> set[str]:
    """Only schema-owned property names may survive in validation locations."""
    names: set[str] = set()
    if isinstance(schema, dict):
        names.update(schema.get("properties", {}))
        for value in schema.values():
            if isinstance(value, dict):
                names.update(schema_fields(value))
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        names.update(schema_fields(item))
    return names


def safe_errors(errors: list[dict], fields: set[str]) -> list[dict]:
    result = []
    for error in errors[:MAX_DIAGNOSTICS]:
        loc = []
        for part in error.get("loc", ()):
            if type(part) is int and 0 <= part <= 1_000_000:
                loc.append(part)
            elif isinstance(part, str) and part in fields and _KEY.fullmatch(part):
                loc.append(part)
            else:
                break
        if error.get("type") == "extra_forbidden":
            # The final location is an attacker-controlled property, not a schema field.
            loc = loc[:len(error.get("loc", ())) - 1]
        ctx = {key: value for key, value in (error.get("ctx") or {}).items()
               if key in {"ge", "gt", "le", "lt", "min_length", "max_length"}
               and type(value) in (int, float) and math.isfinite(value)
               and abs(value) <= 1_000_000_000}
        result.append({"loc": tuple(loc), "type": error.get("type"), "ctx": ctx})
    return result


def validation_diagnostics(errors: list[dict]) -> list[WorkflowDiagnostic]:
    result = []
    for error in errors[:MAX_DIAGNOSTICS]:
        kind = error.get("type", "")
        path = tuple(error.get("loc", ()))
        if kind in _SEMANTIC_PATHS:
            code = kind
            path = path or _SEMANTIC_PATHS[kind]
        elif kind == "missing":
            code = "field_required"
        elif kind in {"extra_forbidden", "additionalProperties"}:
            code = "extra_fields"
        elif kind in {"literal_error", "enum", "const"}:
            code = "field_enum"
        elif kind in {"greater_than", "greater_than_equal", "less_than", "less_than_equal",
                      "too_short", "too_long", "string_too_short", "string_too_long",
                      "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
                      "minLength", "maxLength", "minItems", "maxItems"}:
            code = "field_bounds"
        elif kind in {"string_pattern_mismatch", "pattern"}:
            code = "field_pattern"
        elif kind == "type" or kind in {
            "string_type", "list_type", "dict_type", "model_type", "bool_type",
            "bool_parsing", "int_type", "int_parsing", "int_from_float",
            "float_type", "float_parsing", "finite_number",
        }:
            code = "field_type"
        else:
            code = "field_invalid"
        bounds = safe_errors([error], set())[0]["ctx"]
        hint = "; ".join(f"{key}={bounds[key]}" for key in sorted(bounds)) or None
        result.append(WorkflowDiagnostic(code, path, hint=hint))
    return result


def jsonschema_diagnostics(validator, config: dict) -> list[WorkflowDiagnostic]:
    errors = []

    def collect(error):
        if error.validator in {"anyOf", "oneOf"} and error.context:
            # Override schemas wrap the actual field schema with a null branch.
            branches = [child for child in error.context if child.schema.get("type") != "null"]
            for child in branches or error.context[:1]:
                collect(child)
            return
        loc = tuple(error.absolute_path)
        if error.validator == "required":
            for key in error.schema.get("required", []):
                if key not in error.instance:
                    errors.append({"loc": (*loc, key), "type": "missing"})
        else:
            bound = {"minimum": "ge", "maximum": "le", "exclusiveMinimum": "gt",
                     "exclusiveMaximum": "lt", "minLength": "min_length",
                     "maxLength": "max_length", "minItems": "min_length",
                     "maxItems": "max_length"}.get(error.validator)
            errors.append({"loc": loc, "type": error.validator,
                           "ctx": {bound: error.validator_value} if bound else {}})

    for error in sorted(validator.iter_errors(config), key=lambda item: repr(list(item.absolute_path))):
        collect(error)
        if len(errors) >= MAX_DIAGNOSTICS:
            break
    return validation_diagnostics(safe_errors(errors, schema_fields(validator.schema)))


def feature_diagnostics(error, step_key: str, feature_type: str) -> list[WorkflowDiagnostic]:
    return [replace(issue, path=("steps", "config", *issue.path), step_key=step_key,
                    feature_type=feature_type)
            for issue in validation_diagnostics(error.errors)]


def public_diagnostics(issues, draft=None, features=()) -> list[dict[str, Any]]:
    result = []
    for issue in issues[:MAX_DIAGNOSTICS]:
        if not isinstance(issue, WorkflowDiagnostic) or issue.code not in _MESSAGES:
            continue
        path = list(issue.path)
        step = None
        if draft is not None and path[:1] == ["steps"]:
            if len(path) > 1 and type(path[1]) is int:
                index = path[1]
                if 0 <= index < len(draft.steps):
                    step = draft.steps[index]
            elif issue.step_key is not None:
                for index, candidate in enumerate(draft.steps):
                    if candidate.step_key == issue.step_key:
                        step = candidate
                        path.insert(1, index)
                        break
                if step is None:
                    # An inherited/descendant issue cannot identify a local draft field.
                    path = ["steps"]
        item = {"code": issue.code, "message": _MESSAGES[issue.code],
                "path": (["draft", *path] if draft is not None and path else path)}
        if step is not None:
            if _KEY.fullmatch(step.step_key):
                item["step_key"] = step.step_key
            if step.feature_type in features:
                item["feature_type"] = step.feature_type
        if "config" in path:
            config_index = path.index("config")
            if len(path) > config_index + 1 and isinstance(path[config_index + 1], str):
                item["field"] = path[config_index + 1]
        if issue.hint:
            item["hint"] = issue.hint
        result.append(item)
    return result
