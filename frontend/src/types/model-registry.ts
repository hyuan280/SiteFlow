import type { VisionUpdate, VisionView } from "./integrations";

export type ModelUsage = "feature_cards" | "assistant" | "mcp";

export interface ModelMetadata {
  id: string;
  name: string;
  enabled: boolean;
  usages: ModelUsage[];
}

export interface ModelView extends ModelMetadata {
  source: "env" | "db";
  config_editable: boolean;
  config: VisionView;
}

export interface ModelRegistryView {
  revision: number;
  items: ModelView[];
}

export interface ModelRegistryUpdate {
  expected_revision: number;
  items: (ModelMetadata & { config?: VisionUpdate })[];
}

export type ModelTestConfig = Omit<VisionUpdate, "api_key">;

export interface ModelDraftTest {
  expected_revision: number;
  config: ModelTestConfig;
}

export interface ModelProbeDraft {
  base_url: string;
  model: string;
  api_key: string;
  supports_vision: boolean;
  max_tokens: string;
}

export interface ModelProbeConfig {
  base_url: string;
  model: string;
  api_key: string;
  supports_vision: boolean;
  max_tokens: number | null;
}

export function isModelRegistryView(value: unknown): value is ModelRegistryView {
  const object = (value: unknown, keys: string[]): value is Record<string, unknown> =>
    typeof value === "object" && value !== null && !Array.isArray(value) &&
    Object.keys(value).length === keys.length && keys.every(key => Object.hasOwn(value, key));
  const nullableString = (value: unknown, max: number) =>
    value === null || typeof value === "string" && value.length <= max;
  const source = (value: unknown) => value === "env" || value === "db" || value === "auto" || value === "unset";
  const scalar = (value: unknown, max: number): value is Record<string, unknown> =>
    object(value, ["value", "source", "editable", "error"]) &&
    nullableString(value.value, max) && source(value.source) && typeof value.editable === "boolean" &&
    nullableString(value.error, 2048);

  if (!object(value, ["revision", "items"]) || typeof value.revision !== "number" ||
    !Number.isSafeInteger(value.revision) || value.revision < 0 || !Array.isArray(value.items) ||
    value.items.length > 21) return false;
  const ids = new Set<string>();
  let dbCount = 0;
  let envCount = 0;
  for (const item of value.items) {
    if (!object(item, ["id", "name", "enabled", "usages", "config", "source", "config_editable"]) || typeof item.id !== "string" ||
      !(item.source === "env" || item.source === "db") || item.config_editable !== (item.source === "db") ||
      (item.id !== "primary" && !/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(item.id)) ||
      ids.has(item.id) || typeof item.name !== "string" || !item.name.trim() || item.name.length > 128 ||
      typeof item.enabled !== "boolean" || !Array.isArray(item.usages) || item.usages.length > 3 ||
      new Set(item.usages).size !== item.usages.length ||
      !item.usages.every(usage => usage === "feature_cards" || usage === "assistant" || usage === "mcp")) return false;
    if (item.source === "env") {
      if (++envCount > 1 || item.id !== "primary") return false;
    } else if (++dbCount > 20) return false;
    ids.add(item.id);
    const config = item.config;
    if (!object(config, ["base_url", "model", "api_key", "supports_vision", "max_tokens", "available"]) ||
      !scalar(config.base_url, 2048) || !scalar(config.model, 256) || typeof config.available !== "boolean") return false;
    const key = config.api_key;
    if (!object(key, ["configured", "source", "editable", "masked"]) || typeof key.configured !== "boolean" ||
      !source(key.source) || typeof key.editable !== "boolean" ||
      !(key.masked === null || typeof key.masked === "string" && /^\*{4}[^\s<>]{0,124}$/.test(key.masked))) return false;
    const vision = config.supports_vision;
    if (!object(vision, ["value", "source", "editable"]) || typeof vision.value !== "boolean" ||
      !source(vision.source) || typeof vision.editable !== "boolean") return false;
    const cap = config.max_tokens;
    // Legacy primary caps were unbounded; viewing them must not require rewriting them.
    if (!object(cap, ["value", "source", "editable", "error"]) ||
      !(cap.value === null || typeof cap.value === "number" && Number.isSafeInteger(cap.value) && cap.value >= 0) ||
      !(cap.source === "env" || cap.source === "db" || cap.source === "unset") || typeof cap.editable !== "boolean" ||
      !nullableString(cap.error, 2048)) return false;
    for (const field of [config.base_url, config.model, key, vision, cap]) {
      if (field.editable !== item.config_editable || item.source === "db" && field.source === "env") return false;
    }
  }
  return true;
}
