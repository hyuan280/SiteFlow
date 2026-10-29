export interface JsonSchema {
  type?: string;
  title?: string;
  description?: string;
  default?: unknown;
  enum?: unknown[];
  enum_titles?: Record<string, string>;
  enum_descriptions?: Record<string, string>;
  items?: JsonSchema;
  properties?: Record<string, JsonSchema>;
  required?: string[];
  minimum?: number;
  maximum?: number;
  minLength?: number;
  maxLength?: number;
  minItems?: number;
  maxItems?: number;
  exclusiveMinimum?: number;
  exclusiveMaximum?: number;
  anyOf?: JsonSchema[];
  "ui:widget"?: string;
  "ui:trueLabel"?: string;
  "ui:falseLabel"?: string;
}

export type ConfigObject = Record<string, unknown>;

function cloneValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(cloneValue);
  if (value !== null && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>).map(([key, item]) => [
        key,
        cloneValue(item),
      ]),
    );
  }
  return value;
}

export function schemaDefaults(schema: JsonSchema): ConfigObject {
  const result: ConfigObject = {};
  for (const [key, property] of Object.entries(schema.properties ?? {})) {
    if (Object.prototype.hasOwnProperty.call(property, "default")) {
      result[key] = cloneValue(property.default);
    } else if (property.type === "array") {
      result[key] = [];
    } else if (property.type === "object" && property.properties) {
      result[key] = schemaDefaults(property);
    }
  }
  return result;
}

export function effectiveValue(
  key: string,
  raw: ConfigObject,
  inherited: ConfigObject | null | undefined,
  property: JsonSchema,
): unknown {
  if (Object.prototype.hasOwnProperty.call(raw, key)) return raw[key];
  if (inherited && Object.prototype.hasOwnProperty.call(inherited, key)) {
    return inherited[key];
  }
  return property.default;
}

export function missingRequiredFields(
  schema: JsonSchema,
  raw: ConfigObject,
  inherited: ConfigObject | null | undefined,
): string[] {
  const missing: string[] = [];
  const required = new Set(schema.required ?? []);
  if (schema.properties?.totp_provider_id) {
    required.add("service_hint");
  }
  for (const key of required) {
    const property = schema.properties?.[key] ?? {};
    const value = effectiveValue(key, raw, inherited, property);
    if (
      value === undefined ||
      value === null ||
      (typeof value === "string" && value.trim() === "") ||
      (Array.isArray(value) && value.length === 0)
    ) {
      missing.push(property.title ?? key);
    }
  }
  return missing;
}

export function itemDefault(schema: JsonSchema | undefined): unknown {
  if (!schema) return "";
  if (Object.prototype.hasOwnProperty.call(schema, "default")) {
    return cloneValue(schema.default);
  }
  if (schema.type === "number" || schema.type === "integer") return 0;
  if (schema.type === "boolean") return false;
  if (schema.type === "array") return [];
  if (schema.type === "object") return {};
  return "";
}
