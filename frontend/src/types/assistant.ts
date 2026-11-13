import { isConsumptionReview, isCookieClearReview, isSiteConfigReview, templateImportReview, isSchedulerConfigReview, runCancelReview } from "@/utils/assistant-review";

export type AssistantStatus = "idle" | "running";
export type CredentialMode = "manual" | "bitwarden" | "cookie" | "none";
export const assistantRunLanePolicies = ["auto", "general", "interaction", "proxy_general", "proxy_interaction"] as const;
export type AssistantRunLanePolicy = typeof assistantRunLanePolicies[number];
export const assistantExecutionFeatures = [
  "flow.open", "action.navigate", "auth.form_login", "auth.password_verify", "action.wait",
  "sign.button", "sign.unit3d_claim", "challenge.altcha", "challenge.slider", "action.click",
  "challenge.2fa", "challenge.turnstile", "challenge.image_captcha", "challenge.grid_captcha",
  "sign.iframe.image_captcha", "sign.image_quiz", "action.fill", "flow.result",
] as const;
export type AssistantExecutionFeature = typeof assistantExecutionFeatures[number];

export interface AssistantContext {
  revision: number;
  summary_through_message_id: number | null;
  compressed: boolean;
}

export interface AssistantUsageCall {
  id: string;
  kind: "conversation" | "summary" | "title";
  created_at: string;
  prompt_tokens: number | null;
  completion_tokens: number | null;
  total_tokens: number | null;
  cached_tokens: number | null;
  reasoning_tokens: number | null;
  context_budget_tokens?: number | null;
  context_revision?: number | null;
  target_revision?: number | null;
}

export interface AssistantUsage {
  revision: number;
  last_context_tokens: number | null;
  last_context_cached_tokens: number | null;
  last_context_budget_tokens?: number | null;
  last_context_prompt_tokens?: number | null;
  last_context_completion_tokens?: number | null;
  last_context_revision?: number | null;
  last_context_target_revision?: number | null;
  last_context_usage_missing?: boolean;
  calls: AssistantUsageCall[];
  totals: {
    request_count: number;
    prompt_tokens: number;
    completion_tokens: number;
    total_tokens: number;
    cached_tokens: number;
    reasoning_tokens: number;
  };
  unreported_usage_count: number;
}

export interface AssistantSession {
  id: string;
  title: string;
  title_locked: boolean;
  title_revision: number;
  status: AssistantStatus;
  created_at: string;
  updated_at: string;
  context?: AssistantContext;
}

export type AssistantSessionList = { items: AssistantSession[] } & (
  { offset: number; limit: number; next_offset: number | null; truncated: boolean } |
  { offset?: never; limit?: never; next_offset?: never; truncated?: never }
);

export interface AssistantTarget {
  name: string;
  base_url: string;
  credential_mode: CredentialMode;
  authorized?: true;
  proxy: boolean;
  site_id?: number | null;
  run_lane_policy?: AssistantRunLanePolicy;
  workflow_site_id?: number | null;
  workflow_consent?: boolean;
  trusted_origins?: string[];
  screenshot_consent?: boolean;
  // Persisted legacy grants remain readable but never imply unified authorization.
  confirmed?: true;
  execution_consent?: boolean;
  credential_consent?: boolean;
  allow_sign?: boolean;
  execution_features?: AssistantExecutionFeature[];
}

export interface AssistantMessage {
  id: number;
  role: "user" | "assistant" | "tool" | "system";
  content: string;
  title?: string;
  tool_name?: string;
  status?: "running" | "success" | "error";
  created_at: string;
}

export interface AssistantDetail {
  id: string;
  title: string;
  title_locked: boolean;
  title_revision: number;
  status: AssistantStatus;
  target: AssistantTarget | null;
  browser: {
    lane_key: "interaction" | "proxy_interaction";
    status: "ready" | "expired";
    expires_at: string;
  } | null;
  messages: AssistantMessage[];
  context?: AssistantContext;
  usage?: AssistantUsage;
  pending_approval?: AssistantPendingApproval | null;
  pending_interaction?: AssistantPendingInteraction | null;
  retry_authorizations?: AssistantRetryAuthorization[];
  // Authoritative when present, including []; absent only in legacy snapshots.
  unresolved_operations?: AssistantUnresolvedOperation[];
}

export interface AssistantPendingInteraction {
  id: string;
  kind: "authorization" | "choice" | "input";
  purpose: string;
  title: string;
  description: string;
  expires_at: string;
  options?: { id: string; label: string }[];
  multiple?: boolean;
  fields?: { id: string; label: string; required: boolean }[];
  review?: Record<string, unknown>;
}

export interface AssistantPendingApproval {
  id: string;
  operation_id: string;
  feature_type: string;
  status: "pending";
  created_at: string;
  expires_at: string;
  title: string;
}

export interface AssistantRetryAuthorization {
  operation_id: string;
  retry_operation_id: string | null;
  expires_at: string;
  status: "approved" | "used" | "expired";
  target_revision: number;
}

export interface AssistantUnresolvedOperation {
  operation_id: string;
  feature_type?: string;
  status: "unknown";
  code: "outcome_uncertain";
  // Host eligibility for a new approval, including reconsideration of a used grant.
  can_authorize: boolean;
}

export interface AssistantScreenshot {
  mime_type: "image/jpeg";
  image_base64: string;
  width: number;
  height: number;
  masked: true;
}

const record = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);
const uuid = (value: unknown): value is string => typeof value === "string" &&
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value);
const status = (value: unknown) => value === "idle" || value === "running";
const credential = (value: unknown) => ["manual", "bitwarden", "cookie", "none"].includes(value as string);
const date = (value: unknown) => typeof value === "string" && Number.isFinite(Date.parse(value));
const titleFields = (value: Record<string, unknown>) => typeof value.title === "string" &&
  typeof value.title_locked === "boolean" && Number.isSafeInteger(value.title_revision) && (value.title_revision as number) >= 0;
const contextFields = (value: Record<string, unknown>) => value.context === undefined ||
  (record(value.context) && Number.isSafeInteger(value.context.revision) && (value.context.revision as number) >= 1 &&
    typeof value.context.compressed === "boolean" &&
    (value.context.summary_through_message_id === null ||
      (Number.isSafeInteger(value.context.summary_through_message_id) && (value.context.summary_through_message_id as number) >= 1)) &&
    Object.keys(value.context).every(key => ["revision", "summary_through_message_id", "compressed"].includes(key)));
const tokenFields = ["prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens", "reasoning_tokens"] as const;
const count = (value: unknown): value is number => Number.isSafeInteger(value) && (value as number) >= 0;
const nullableCount = (value: unknown) => value === null || count(value);
const isoDate = (value: unknown): value is string => typeof value === "string" &&
  /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(value) && date(value);

export function isAssistantPendingInteraction(value: unknown): value is AssistantPendingInteraction {
  const text = (item: unknown, max: number) => typeof item === "string" && item.trim().length > 0 && item.length <= max;
  const identifier = (item: unknown) => typeof item === "string" && /^[a-zA-Z0-9_-]{1,64}$/.test(item) && !["__proto__", "constructor", "prototype"].includes(item);
  const items = (list: unknown, fields: boolean) => Array.isArray(list) && list.length <= (fields ? 8 : 20) &&
    list.every(item => record(item) && identifier(item.id) && text(item.label, 160) &&
      (!fields || typeof item.required === "boolean") &&
      Object.keys(item).every(key => (fields ? ["id", "label", "required"] : ["id", "label"]).includes(key))) &&
    new Set(list.map(item => item.id)).size === list.length;
  // Review is canonical public data from the server, rendered only as escaped text.
  let nodes = 0;
  const safeReview = (item: unknown, depth = 0): boolean => {
    if (++nodes > 500 || depth > 6) return false;
    if (item === null || typeof item === "boolean") return true;
    if (typeof item === "number") return Number.isFinite(item);
    if (typeof item === "string") return item.length <= 4096;
    if (Array.isArray(item)) return item.length <= 50 && item.every(child => safeReview(child, depth + 1));
    return record(item) && Object.keys(item).length <= 50 && Object.entries(item).every(([key, child]) =>
      /^[a-zA-Z][a-zA-Z0-9_]{0,127}$/.test(key) &&
      !/^(?:password|cookie|token|secret|credentials|authorization|__proto__|constructor|prototype)$/i.test(key) && safeReview(child, depth + 1));
  };
  return record(value) && uuid(value.id) && ["authorization", "choice", "input"].includes(value.kind as string) &&
    text(value.purpose, 128) && text(value.title, 256) && typeof value.description === "string" && value.description.length <= 4096 && isoDate(value.expires_at) &&
    (value.options === undefined || items(value.options, false)) &&
    (value.multiple === undefined || typeof value.multiple === "boolean") &&
    (value.fields === undefined || items(value.fields, true)) &&
    (value.kind === "choice" ? Array.isArray(value.options) && value.options.length > 0 : value.options === undefined || (Array.isArray(value.options) && value.options.length === 0)) &&
    (value.kind === "input" ? Array.isArray(value.fields) && value.fields.length > 0 : value.fields === undefined || (Array.isArray(value.fields) && value.fields.length === 0)) &&
    (value.multiple !== true || value.kind === "choice") &&
    (value.purpose === "consumption" ? value.kind === "authorization" && isConsumptionReview(value.review) :
      value.purpose === "cookie_clear" ? value.kind === "authorization" && isCookieClearReview(value.review) :
      value.purpose === "template_import" ? value.kind === "authorization" && templateImportReview(value.review) !== null :
      value.purpose === "scheduler_config" ? value.kind === "authorization" && isSchedulerConfigReview(value.review) :
      value.purpose === "run_cancel" ? value.kind === "authorization" && runCancelReview(value.review) !== null :
      value.review === undefined || (record(value.review) && safeReview(value.review))) &&
    (value.purpose !== "site_config" || (value.kind === "authorization" && isSiteConfigReview(value.review))) &&
    Object.keys(value).every(key => ["id", "kind", "purpose", "title", "description", "expires_at", "options", "multiple", "fields", "review"].includes(key));
}

export function isAssistantUnresolvedOperation(value: unknown): value is AssistantUnresolvedOperation {
  return record(value) && uuid(value.operation_id) && value.status === "unknown" && value.code === "outcome_uncertain" &&
    typeof value.can_authorize === "boolean" &&
    (value.feature_type === undefined || (typeof value.feature_type === "string" && value.feature_type.length > 0 && value.feature_type.length <= 128)) &&
    Object.keys(value).every(key => ["operation_id", "feature_type", "status", "code", "can_authorize"].includes(key));
}

export function isAssistantRetryAuthorization(value: unknown): value is AssistantRetryAuthorization {
  return record(value) && uuid(value.operation_id) && (value.retry_operation_id === null || uuid(value.retry_operation_id)) &&
    isoDate(value.expires_at) && ["approved", "used", "expired"].includes(value.status as string) && count(value.target_revision) &&
    Object.keys(value).every(key => ["operation_id", "retry_operation_id", "expires_at", "status", "target_revision"].includes(key));
}

export function isAssistantPendingApproval(value: unknown): value is AssistantPendingApproval {
  return record(value) && uuid(value.id) && uuid(value.operation_id) && value.status === "pending" &&
    typeof value.feature_type === "string" && value.feature_type.length > 0 && value.feature_type.length <= 128 &&
    typeof value.title === "string" && value.title.trim().length > 0 &&
    isoDate(value.created_at) && isoDate(value.expires_at) &&
    Object.keys(value).every(key => ["id", "operation_id", "feature_type", "status", "created_at", "expires_at", "title"].includes(key));
}

export function isAssistantScreenshot(value: unknown): value is AssistantScreenshot {
  if (!record(value) || value.mime_type !== "image/jpeg" || value.masked !== true ||
    !count(value.width) || value.width < 1 || value.width > 1600 ||
    !count(value.height) || value.height < 1 || value.height > 1200 ||
    typeof value.image_base64 !== "string" || !value.image_base64.length || value.image_base64.length > 524288 ||
    value.image_base64.length % 4 !== 0 || !/^[A-Za-z0-9+/]*={0,2}$/.test(value.image_base64)) return false;
  try {
    const bytes = atob(value.image_base64);
    return btoa(bytes) === value.image_base64 && bytes.length >= 5 &&
      bytes.charCodeAt(0) === 0xff && bytes.charCodeAt(1) === 0xd8 && bytes.charCodeAt(2) === 0xff &&
      bytes.charCodeAt(bytes.length - 2) === 0xff && bytes.charCodeAt(bytes.length - 1) === 0xd9;
  } catch { return false; }
}

const usageFields = (value: unknown): value is AssistantUsage => {
  if (!record(value) || !count(value.revision) || value.revision < 1 ||
    !nullableCount(value.last_context_tokens) || !nullableCount(value.last_context_cached_tokens) ||
    (value.last_context_usage_missing !== undefined && typeof value.last_context_usage_missing !== "boolean") ||
    ["last_context_budget_tokens", "last_context_prompt_tokens", "last_context_completion_tokens", "last_context_revision", "last_context_target_revision"].some(key => value[key] !== undefined && !nullableCount(value[key])) ||
    !count(value.unreported_usage_count) ||
    Object.keys(value).some(key => !["revision", "last_context_tokens", "last_context_cached_tokens", "last_context_budget_tokens", "last_context_prompt_tokens", "last_context_completion_tokens", "last_context_revision", "last_context_target_revision", "last_context_usage_missing", "calls", "totals", "unreported_usage_count"].includes(key)) ||
    !record(value.totals) || !count(value.totals.request_count) ||
    !tokenFields.every(key => count((value.totals as Record<string, unknown>)[key])) ||
    Object.keys(value.totals).some(key => key !== "request_count" && !tokenFields.includes(key as typeof tokenFields[number])) ||
    !Array.isArray(value.calls) || value.calls.length > 100) return false;
  const ids = new Set<string>();
  return value.calls.every(call => {
    if (!record(call) || !uuid(call.id) || ids.has(call.id.toLowerCase()) ||
      !["conversation", "summary", "title"].includes(call.kind as string) ||
      typeof call.created_at !== "string" ||
      !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(call.created_at) || !date(call.created_at) ||
      !tokenFields.every(key => nullableCount(call[key])) ||
      ["context_budget_tokens", "context_revision", "target_revision"].some(key => call[key] !== undefined && !nullableCount(call[key])) ||
      Object.keys(call).some(key => !["id", "kind", "created_at", "context_budget_tokens", "context_revision", "target_revision", ...tokenFields].includes(key))) return false;
    ids.add(call.id.toLowerCase());
    return true;
  });
};

// Syntactic public-origin checks only; the server/worker must still enforce DNS safety.
export function normalizeAssistantOrigin(value: unknown): string {
  if (typeof value !== "string" || !value || value.length > 4096 ||
    !/^https?:\/\//i.test(value) || /[\s\\?#@%\u0000-\u001f\u007f-\u009f]/.test(value)) return "";
  try {
    // Reject paths before URL parsing can collapse dot segments to '/'.
    if (!/^https?:\/\/[^/]+\/?$/i.test(value)) return "";
    const url = new URL(value);
    const host = url.hostname;
    if (url.username || url.password || url.port === "0" || host.endsWith(".") ||
      /(^|\.)(localhost|local|internal)$/.test(host)) return "";
    if (host.startsWith("[")) {
      const ip = host.slice(1, -1);
      // Only global unicast, excluding transition, documentation and special-use ranges.
      if (!/^[23][0-9a-f]{3}:/.test(ip) || /^(2002:|3fff:)/.test(ip) ||
        (ip.startsWith("2001:") && (parseInt(ip.split(":")[1] || "0", 16) < 0x200 || ip.startsWith("2001:db8:")))) return "";
    } else if (/^[\d.]+$/.test(host)) {
      const [a, b, c] = host.split(".").map(Number);
      if (a === undefined || b === undefined || a === 0 || a === 10 || a === 127 || a >= 224 ||
        (a === 100 && b >= 64 && b <= 127) || (a === 169 && b === 254) || (a === 172 && b >= 16 && b <= 31) ||
        (a === 192 && (b === 0 || b === 168 || (b === 88 && c === 99))) ||
        (a === 198 && (b === 18 || b === 19 || (b === 51 && c === 100))) ||
        (a === 203 && b === 0 && c === 113)) return "";
    } else if (!host.includes(".") || host.split(".").some(label =>
      !/^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(label)) || /^\d+$/.test(host.split(".").at(-1) ?? "")) return "";
    return url.origin;
  } catch { return ""; }
}

export function isAssistantSession(value: unknown): value is AssistantSession {
  return record(value) && value.usage === undefined && uuid(value.id) && titleFields(value) && contextFields(value) && status(value.status) &&
    date(value.created_at) && date(value.updated_at);
}

export function isAssistantSessionList(value: unknown): value is AssistantSessionList {
  if (!record(value) || !Array.isArray(value.items) || value.items.length > 100 ||
    !value.items.every(isAssistantSession) ||
    new Set(value.items.map(item => item.id.toLowerCase())).size !== value.items.length) return false;
  const metadata = ["offset", "limit", "next_offset", "truncated"];
  if (!metadata.some(key => key in value)) return true; // Shipped legacy list responses.
  return count(value.offset) && count(value.limit) && value.limit >= 1 && value.limit <= 100 &&
    value.items.length <= value.limit && typeof value.truncated === "boolean" &&
    (value.next_offset === null ? value.truncated === false :
      count(value.next_offset) && value.truncated === true && value.items.length === value.limit &&
      Number.isSafeInteger(value.offset + value.limit) && value.next_offset === value.offset + value.limit);
}

// Both HTTP and SSE are untrusted boundaries; never publish partial/invalid snapshots.
export function isAssistantDetail(value: unknown): value is AssistantDetail {
  if (!record(value) || !uuid(value.id) || !titleFields(value) || !contextFields(value) || !status(value.status) ||
    (value.usage !== undefined && !usageFields(value.usage)) ||
    (value.pending_interaction !== undefined && value.pending_interaction !== null && !isAssistantPendingInteraction(value.pending_interaction)) ||
    (value.pending_approval !== undefined && value.pending_approval !== null && !isAssistantPendingApproval(value.pending_approval))) return false;
  for (const [key, guard] of [["retry_authorizations", isAssistantRetryAuthorization],
    ["unresolved_operations", isAssistantUnresolvedOperation]] as const) {
    const items = value[key];
    if (items !== undefined && (!Array.isArray(items) || !items.every(guard) ||
      new Set(items.map(item => item.operation_id.toLowerCase())).size !== items.length)) return false;
  }
  const target = value.target;
  if (target !== null && (!record(target) || typeof target.name !== "string" ||
    typeof target.base_url !== "string" || !credential(target.credential_mode) ||
    (target.authorized !== true && target.confirmed !== true) ||
    (target.authorized !== undefined && target.authorized !== true) ||
    (target.confirmed !== undefined && target.confirmed !== true) ||
    typeof target.proxy !== "boolean" || Object.keys(target).some(key => key.startsWith("_")) ||
    (target.trusted_origins !== undefined && (!Array.isArray(target.trusted_origins) || target.trusted_origins.length > 20 ||
      new Set(target.trusted_origins).size !== target.trusted_origins.length ||
      !target.trusted_origins.every(item => typeof item === "string" && Boolean(item) && normalizeAssistantOrigin(item) === item))) ||
    ["site_id", "workflow_site_id"].some(key => target[key] !== undefined && target[key] !== null &&
      (!Number.isSafeInteger(target[key]) || (target[key] as number) < 1)) ||
    (target.run_lane_policy !== undefined && !assistantRunLanePolicies.includes(target.run_lane_policy as AssistantRunLanePolicy)) ||
    ["execution_consent", "credential_consent", "allow_sign", "screenshot_consent", "workflow_consent"].some(key => target[key] !== undefined && typeof target[key] !== "boolean") ||
    (target.execution_features !== undefined && (!Array.isArray(target.execution_features) || target.execution_features.length > assistantExecutionFeatures.length ||
      new Set(target.execution_features).size !== target.execution_features.length ||
      !target.execution_features.every(key => assistantExecutionFeatures.includes(key as AssistantExecutionFeature)))))) return false;
  const browser = value.browser;
  if (browser !== null && (!record(browser) || !["interaction", "proxy_interaction"].includes(browser.lane_key as string) ||
    !["ready", "expired"].includes(browser.status as string) || !date(browser.expires_at))) return false;
  if (!Array.isArray(value.messages)) return false;
  let previous = 0;
  return value.messages.every(message => {
    if (!record(message) || !Number.isSafeInteger(message.id) || (message.id as number) <= previous ||
      !["user", "assistant", "tool", "system"].includes(message.role as string) ||
      typeof message.content !== "string" || !date(message.created_at) ||
      (message.title !== undefined && typeof message.title !== "string") ||
      (message.tool_name !== undefined && typeof message.tool_name !== "string") ||
      (message.status !== undefined && !["running", "success", "error"].includes(message.status as string))) return false;
    previous = message.id as number;
    return true;
  });
}
