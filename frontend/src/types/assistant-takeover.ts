export interface AssistantTakeoverValues {
  enabled: boolean;
  live_access: boolean;
  mode: "analysis" | "autofix";
  site_ids: number[] | null;
  max_attempts: number;
  conclusion_ttl_days: number;
}

export interface AssistantTakeoverSettings {
  revision: number;
  source?: "default" | "db";
  authorizing_owner?: string | null;
  authorized_at?: string | null;
  effective_from?: string | null;
  values: AssistantTakeoverValues;
}

export interface TakeoverSiteOption { id: number; name: string; enabled: boolean }
export interface AssistantTakeoverAction {
  id: string;
  site_id: number;
  source_run_id: number;
  session_id: string | null;
  mode: "analysis" | "autofix";
  status: string;
  created_at: string;
  conclusion?: string | null;
  notification_message?: string | null;
  attempts?: number;
  reused_from_action_id?: string | null;
  reused_from_run_id?: number | null;
}

const record = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === "object" && !Array.isArray(value);
const count = (value: unknown): value is number => Number.isSafeInteger(value) && (value as number) >= 0;
const id = (value: unknown): value is number => count(value) && value > 0;
const uuid = (value: unknown): value is string => typeof value === "string" && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value);

export function isAssistantTakeoverValues(value: unknown): value is AssistantTakeoverValues {
  return record(value) && typeof value.enabled === "boolean" && typeof value.live_access === "boolean" && ["analysis", "autofix"].includes(value.mode as string) &&
    (value.site_ids === null || (Array.isArray(value.site_ids) && value.site_ids.length <= 10000 && value.site_ids.every(id) && new Set(value.site_ids).size === value.site_ids.length)) &&
    count(value.max_attempts) && value.max_attempts >= 1 && value.max_attempts <= 3 &&
    count(value.conclusion_ttl_days) && value.conclusion_ttl_days >= 1 && value.conclusion_ttl_days <= 30 &&
    Object.keys(value).every(key => ["enabled", "live_access", "mode", "site_ids", "max_attempts", "conclusion_ttl_days"].includes(key));
}

export function isAssistantTakeoverSettings(value: unknown): value is AssistantTakeoverSettings {
  return record(value) && count(value.revision) && (value.source === undefined || ["default", "db"].includes(value.source as string)) && isAssistantTakeoverValues(value.values) &&
    (value.authorizing_owner === undefined || value.authorizing_owner === null || (typeof value.authorizing_owner === "string" && value.authorizing_owner.length <= 128)) &&
    ["authorized_at", "effective_from"].every(key => value[key] === undefined || value[key] === null ||
      (typeof value[key] === "string" && Number.isFinite(Date.parse(value[key] as string)))) &&
    Object.keys(value).every(key => ["revision", "source", "values", "authorizing_owner", "authorized_at", "effective_from"].includes(key));
}

export function isTakeoverSiteOptions(value: unknown): value is TakeoverSiteOption[] {
  return Array.isArray(value) && value.every(site => record(site) && id(site.id) && typeof site.name === "string" &&
    site.name.length > 0 && site.name.length <= 128 && typeof site.enabled === "boolean") && new Set(value.map(site => site.id)).size === value.length;
}

export function isAssistantTakeoverActions(value: unknown): value is { items: AssistantTakeoverAction[] } {
  return record(value) && Array.isArray(value.items) && value.items.length <= 20 && value.items.every(item => record(item) &&
    uuid(item.id) && id(item.site_id) && id(item.source_run_id) && (item.session_id === null || uuid(item.session_id)) &&
    ["analysis", "autofix"].includes(item.mode as string) && typeof item.status === "string" && /^[a-z_]{1,64}$/.test(item.status) &&
    typeof item.created_at === "string" && Number.isFinite(Date.parse(item.created_at)) &&
    (item.attempts === undefined || count(item.attempts)) &&
    (item.reused_from_action_id === undefined || item.reused_from_action_id === null || uuid(item.reused_from_action_id)) &&
    (item.reused_from_run_id === undefined || item.reused_from_run_id === null || id(item.reused_from_run_id)) &&
    ["conclusion", "notification_message"].every(key => item[key] === undefined || item[key] === null || typeof item[key] === "string")) &&
    new Set(value.items.map(item => item.id)).size === value.items.length;
}

const takeoverStatuses: Record<string, string> = {
  pending: "等待接管", queued: "排队中", running: "接管中", analyzing: "分析中", completed: "已完成",
  success: "已完成", failed: "失败", skipped: "已跳过", cancelled: "已取消", unknown: "结果待核对",
  dispatched: "已启动接管", completed_analysis: "分析完成", updated: "工作流已保存（尚未运行验证）", imported: "已导入工作流", imported_unverified: "已导入，尚未运行验证", revoked: "授权已撤销",
  reused: "相同失败，复用已有分析",
};
export const takeoverStatusLabel = (status: string): string => Object.hasOwn(takeoverStatuses, status) ? takeoverStatuses[status]! : "状态待核对";
