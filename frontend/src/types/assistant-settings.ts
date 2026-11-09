export interface AssistantSettingsValues {
  max_tool_calls: number;
  max_model_rounds: number;
  max_parallel_tool_calls: number;
  turn_timeout_seconds: number;
  max_tokens: number | null;
  context_max_tokens: number;
  auto_compact: boolean;
  auto_title: boolean;
  auto_authorize_execution: boolean;
  auto_authorize_screenshot: boolean;
  auto_authorize_workflow: boolean;
}

export interface AssistantSettingsView {
  revision: number;
  source: "default" | "db";
  values: AssistantSettingsValues;
}

export const assistantSettingBounds = {
  max_tool_calls: [1, 200],
  max_model_rounds: [1, 100],
  max_parallel_tool_calls: [1, 8],
  turn_timeout_seconds: [30, 1800],
  context_max_tokens: [16000, 1000000],
} as const;

export function parseSettingsInteger(value: unknown, min: number, max: number, blankZero = false): number | null {
  if (typeof value !== "string") return null;
  const text = value.trim();
  if (blankZero && text === "") return 0;
  if (!/^\d+$/.test(text)) return null;
  const number = Number(text);
  return Number.isSafeInteger(number) && number >= min && number <= max ? number : null;
}

export function isAssistantSettingsValues(value: unknown): value is AssistantSettingsValues {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const values = value as Record<string, unknown>;
  return Object.entries(assistantSettingBounds).every(([key, [min, max]]) =>
    typeof values[key] === "number" && Number.isSafeInteger(values[key]) && values[key] >= min && values[key] <= max) &&
    (values.max_tokens === null || (typeof values.max_tokens === "number" && Number.isSafeInteger(values.max_tokens) && values.max_tokens >= 0)) &&
    ["auto_compact", "auto_title", "auto_authorize_execution", "auto_authorize_screenshot",
      "auto_authorize_workflow"].every(key => typeof values[key] === "boolean");
}

export function isAssistantSettingsView(value: unknown): value is AssistantSettingsView {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const view = value as Record<string, unknown>;
  return typeof view.revision === "number" && Number.isSafeInteger(view.revision) && view.revision >= 0 &&
    (view.source === "default" || view.source === "db") && isAssistantSettingsValues(view.values);
}
