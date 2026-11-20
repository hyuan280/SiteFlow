import type { AssistantMessage } from "@/types/assistant";
import { normalizeAssistantOrigin } from "@/types/assistant";

export const reviewFieldLabels: Record<string, string> = {
  name: "网站名称", base_url: "网站地址", site_id: "凭据绑定站点", workflow_site_id: "工作流绑定站点",
  enabled: "启用站点", lane_policy: "运行车道", run_lane_policy: "运行车道",
  trusted_urls: "可信 URL 正则（仅本站点）", trusted_origins: "可信来源", origins: "待信任来源",
  steps: "工作流步骤", groups: "步骤分组", signals: "判定信号", template_id: "站点模板",
  support_resign: "支持补签", resign_max_days: "补签最大天数", checkin_after: "签到开始时间",
  timezone: "时区", tags: "标签", notes: "备注", credential_mode: "凭据来源",
  credential_provider_id: "凭据提供商", bitwarden_enabled: "使用 Bitwarden",
  bitwarden_match_mode: "凭据匹配方式", bitwarden_custom_match: "凭据自定义匹配",
  scope: "适用范围", suggested_lane: "建议车道（非授权）", current_browser_lane: "当前观察浏览器车道",
  current_run_lane_policy: "当前运行车道", proxy: "使用代理", capability: "授权能力",
  feature_type: "请求功能", execution_features: "允许执行的功能", credential_consent: "允许使用私有凭据",
  allow_sign: "允许签到、点击等业务操作", screenshot_consent: "允许发送截图", workflow_consent: "允许保存并运行",
  execution_consent: "允许真实执行", required: "是否必须回答", successful_run_id: "成功测试任务",
  "observation.proxy": "观察浏览器使用代理", "observation.trusted_origins": "观察浏览器可信来源",
};

const purposeLabels: Record<string, string> = {
  target: "目标网站", trusted_origins: "可信来源", proxy: "车道与代理", authorization: "能力授权",
  site_config: "站点配置变更", consumption: "购卡与补签消费授权", question: "信息确认",
  template_import: "工作流导入模板", scheduler_config: "全局调度变更", run_cancel: "请求取消任务", cookie_clear: "清除绑定站点 Cookie",
};
export const interactionPurposeLabel = (purpose: string): string => Object.hasOwn(purposeLabels, purpose) ? purposeLabels[purpose]! : "交互确认";

const lanes: Record<string, string> = {
  auto: "自动选择", general: "通用车道（不使用代理）", interaction: "交互车道（不使用代理）",
  proxy_general: "代理通用车道（使用已配置代理）", proxy_interaction: "代理交互车道（使用已配置代理）",
};
const features: Record<string, string> = {
  "flow.open": "打开网页", "action.navigate": "导航网页", "auth.form_login": "表单登录",
  "auth.password_verify": "密码验证", "action.wait": "等待", "sign.button": "按钮签到",
  "sign.unit3d_claim": "UNIT3D 领取", "challenge.altcha": "ALTCHA 验证", "challenge.slider": "滑块验证",
  "action.click": "点击",
  "challenge.2fa": "私有双因素验证", "challenge.turnstile": "Turnstile 验证",
  "challenge.image_captcha": "图片验证码", "challenge.grid_captcha": "网格验证码",
  "sign.iframe.image_captcha": "内嵌页面验证码签到", "sign.image_quiz": "图片问答签到",
  "action.fill": "填写非秘密表单", "flow.result": "结果判定",
};
const enums: Record<string, Record<string, string>> = {
  scope: { browser: "仅观察浏览器", workflow: "仅工作流保存与完整运行", both: "观察浏览器及工作流保存与完整运行" },
  credential_mode: { manual: "已保存站点凭据", bitwarden: "Bitwarden", cookie: "已保存 Cookie", none: "不使用凭据" },
  capability: { execution: "真实单步执行", screenshot: "发送遮罩截图给模型", workflow: "保存配置并完整运行" },
  bitwarden_match_mode: { domain: "域名匹配", root_domain: "根域名匹配", custom: "自定义匹配" },
  credential_provider_id: { "builtin.manual": "站点私有凭据", "builtin.bitwarden": "Bitwarden" },
};
const summaryLabels: Record<string, string> = { count: "数量", step_count: "步骤数", group_count: "分组数", signal_count: "信号数" };
const record = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === "object" && !Array.isArray(value);
const text = (value: unknown, max = 4096): value is string => typeof value === "string" && value.length <= max;

// Never serialize arbitrary objects: only public scalar/list values and count summaries are readable.
export function reviewValue(field: string, value: unknown): string[] {
  if (value === null) return ["未设置"];
  if (typeof value === "boolean") return [value ? "是（已启用）" : "否（未启用）"];
  if (typeof value === "number" && Number.isFinite(value)) return [String(value)];
  if (text(value)) {
    const labels = field.includes("lane") ? lanes : field === "execution_features" || field === "feature_type" ? features : enums[field];
    return [labels && Object.hasOwn(labels, value) ? labels[value]! : (value || "空值")];
  }
  if (Array.isArray(value) && value.length <= (field === "tags" ? 50 : 20) && value.every(item => item === null || ["string", "boolean", "number"].includes(typeof item))) {
    return value.length ? value.flatMap(item => reviewValue(field, item)) : ["空列表（0 项）"];
  }
  if (record(value) && Object.keys(value).length > 0 && Object.keys(value).length <= 4 &&
    Object.entries(value).every(([key, count]) => Object.hasOwn(summaryLabels, key) && Number.isSafeInteger(count) && (count as number) >= 0)) {
    return Object.entries(value).map(([key, count]) => `${summaryLabels[key]}：${count}`);
  }
  return [];
}

export function interactionReviewRows(purpose: string, review: Record<string, unknown>): { field: string; label: string; values: string[] }[] {
  if (["template_import", "scheduler_config", "run_cancel", "cookie_clear"].includes(purpose)) return [];
  const fields = ["target", "site_config", "consumption"].includes(purpose) ? ["name", "base_url"] :
    ["name", "base_url", "scope", "origins", "trusted_origins", "trusted_urls", "suggested_lane", "current_browser_lane",
      "current_run_lane_policy", "run_lane_policy", "lane_policy", "proxy", "capability", "credential_mode", "credential_provider_id", "site_id",
      "workflow_site_id", "feature_type", "execution_features", "credential_consent", "allow_sign", "execution_consent",
      "screenshot_consent", "workflow_consent", "required"];
  return fields.filter(field => Object.hasOwn(review, field)).map(field => ({
    field, label: reviewFieldLabels[field]!, values: reviewValue(field, review[field]),
  })).filter(row => row.values.length > 0);
}

export interface SiteConfigChange { field: string; label: string; before: unknown; after: unknown }
export interface SiteConfigReview {
  name: string; base_url: string; site_id?: number; changes: SiteConfigChange[]; warnings: string[]; successful_run_id?: number;
}
const positiveId = (value: unknown): value is number => Number.isSafeInteger(value) && (value as number) > 0;
const only = (value: Record<string, unknown>, fields: string[]) => Object.keys(value).every(key => fields.includes(key));
const nonemptyText = (value: unknown, max = 4096): value is string => text(value, max) && Boolean(value.trim());
const warnings = (value: unknown): value is string[] => Array.isArray(value) && value.length <= 20 && value.every(item => nonemptyText(item));
const namedId = (value: unknown): value is { id: number; name: string } => record(value) && positiveId(value.id) && nonemptyText(value.name, 128) && only(value, ["id", "name"]);
const nonnegative = (value: unknown): value is number => Number.isSafeInteger(value) && (value as number) >= 0;

export interface CookieClearReview {
  site_id: number; name: string; base_url: string; lane_key: "interaction" | "proxy_interaction";
  saved_cookie_count: number; warnings: string[];
}
export function isCookieClearReview(value: unknown): value is CookieClearReview {
  if (!record(value) || !positiveId(value.site_id) || !nonemptyText(value.name, 128) || !nonemptyText(value.base_url, 512) ||
    !["interaction", "proxy_interaction"].includes(value.lane_key as string) ||
    !nonnegative(value.saved_cookie_count) || value.saved_cookie_count > 300 || !warnings(value.warnings) ||
    !only(value, ["site_id", "name", "base_url", "lane_key", "saved_cookie_count", "warnings"]) ||
    !/^https?:\/\//i.test(value.base_url) || /[\s\\\u0000-\u001f\u007f-\u009f]/.test(value.base_url)) return false;
  try {
    const url = new URL(value.base_url);
    return !url.username && !url.password && Boolean(normalizeAssistantOrigin(url.origin));
  } catch { return false; }
}

export interface TemplateImportReview {
  source_site: { id: number; name: string };
  destination: { id?: number; name: string; operation: "create" | "update" };
  parent: { id: number; name: string } | null;
  counts: { steps: number; groups: number; signals: number };
  warnings: string[];
  candidate?: TemplateLocalWorkflow;
  before?: TemplateLocalWorkflow | null;
  description?: string;
  enabled?: boolean;
}

export interface TemplateLocalWorkflow {
  parent_id: number | null;
  steps: Record<string, unknown>[];
  groups: Record<string, unknown>[];
  signals: Record<string, unknown>[];
}
const templateArrays = [
  ["steps", ["step_key", "feature_type", "label", "group_key", "position", "enabled", "config", "condition", "on_error", "timeout_seconds"]],
  ["groups", ["group_key", "name", "position", "step_anchor"]],
  ["signals", ["signal_key", "label", "description", "include_texts", "include_selectors", "exclude_texts", "exclude_selectors", "match_mode", "enabled"]],
] as const;

export function isTemplateImportReview(value: unknown): value is TemplateImportReview {
  if (!record(value) || !namedId(value.source_site) || !record(value.destination) ||
    !nonemptyText(value.destination.name, 128) || !["create", "update"].includes(value.destination.operation as string) ||
    (value.destination.operation === "update" ? !positiveId(value.destination.id) : value.destination.id !== undefined && !positiveId(value.destination.id)) ||
    !only(value.destination, ["id", "name", "operation"]) || (value.parent !== null && !namedId(value.parent)) ||
    !record(value.counts) || !["steps", "groups", "signals"].every(key => nonnegative((value.counts as Record<string, unknown>)[key])) ||
    !only(value.counts, ["steps", "groups", "signals"]) || !warnings(value.warnings)) return false;
  return only(value, ["source_site", "destination", "parent", "counts", "warnings"]);
}

// Candidate and baseline share the same public allowlist and resource bounds.
function templateLocalWorkflow(value: unknown): value is TemplateLocalWorkflow {
  if (!record(value) || (value.parent_id !== null && !positiveId(value.parent_id)) ||
    !only(value, ["parent_id", "steps", "groups", "signals"])) return false;
  let nodes = 0;
  const safe = (item: unknown, depth = 0): boolean => {
    if (++nodes > 20000 || depth > 8) return false;
    if (item === null || typeof item === "boolean") return true;
    if (typeof item === "number") return Number.isFinite(item);
    if (typeof item === "string") return item.length <= 4096;
    if (Array.isArray(item)) return item.length <= 500 && item.every(child => safe(child, depth + 1));
    return record(item) && Object.keys(item).length <= 100 && Object.entries(item).every(([key, child]) =>
      /^[a-zA-Z][a-zA-Z0-9_]{0,127}$/.test(key) &&
      (["username_selector", "password_selector", "token_selector", "username_field", "password_field", "credential_provider_id", "totp_provider_id"].includes(key) ||
        !/password|cookie|token|secret|credential|authorization|^(?:constructor|prototype)$/i.test(key)) && safe(child, depth + 1));
  };
  for (const [key, fields] of templateArrays) {
    const items = value[key];
    if (!Array.isArray(items) || items.length > 500 || !items.every(item => record(item) && only(item, [...fields]) && safe(item))) return false;
    const identifier = key === "steps" ? "step_key" : key === "groups" ? "group_key" : "signal_key";
    if (!items.every(item => nonemptyText(item[identifier], 64)) || new Set(items.map(item => item[identifier])).size !== items.length) return false;
    for (const item of items) {
      if (!/^[a-z][a-z0-9_.-]*$/.test(item[identifier])) return false;
      if (key === "steps" && (!nonemptyText(item.feature_type, 128) || (item.config !== undefined && !record(item.config)) ||
        (item.label !== undefined && item.label !== null && !text(item.label, 128)) ||
        (item.enabled !== undefined && item.enabled !== null && typeof item.enabled !== "boolean"))) return false;
      if (key === "groups" && !nonemptyText(item.name, 128)) return false;
      if (key === "signals" && (!nonemptyText(item.label, 128) ||
        ["include_texts", "include_selectors", "exclude_texts", "exclude_selectors"].some(field => item[field] !== undefined &&
          (!Array.isArray(item[field]) || !(item[field] as unknown[]).every(entry => text(entry)))) ||
        (item.match_mode !== undefined && !["any", "all"].includes(item.match_mode)) ||
        (item.enabled !== undefined && typeof item.enabled !== "boolean"))) return false;
    }
  }
  return true;
}

export function templateImportReview(value: unknown): TemplateImportReview | null {
  // Persisted summary-only approvals and host approvals without `before` remain readable.
  if (isTemplateImportReview(value)) return value;
  if (!record(value) || !positiveId(value.source_site_id) || (value.destination_template_id !== null && !positiveId(value.destination_template_id)) ||
    !nonemptyText(value.name, 128) || !text(value.description, 4000) || typeof value.enabled !== "boolean" || !warnings(value.warnings) ||
    !only(value, ["source_site_id", "destination_template_id", "name", "description", "enabled", "parent_id", "steps", "groups", "signals", "before", "warnings"])) return null;
  const candidate = { parent_id: value.parent_id, steps: value.steps, groups: value.groups, signals: value.signals };
  if (!templateLocalWorkflow(candidate)) return null;
  if (Object.hasOwn(value, "before") && (value.destination_template_id === null ? value.before !== null : !templateLocalWorkflow(value.before))) return null;
  return {
    source_site: { id: value.source_site_id, name: `站点 #${value.source_site_id}` },
    destination: { ...(positiveId(value.destination_template_id) ? { id: value.destination_template_id } : {}), name: value.name,
      operation: value.destination_template_id === null ? "create" : "update" },
    parent: value.parent_id === null ? null : { id: value.parent_id as number, name: `模板 #${value.parent_id}` },
    counts: { steps: candidate.steps.length, groups: candidate.groups.length, signals: candidate.signals.length }, warnings: value.warnings,
    candidate, ...(Object.hasOwn(value, "before") ? { before: value.before as TemplateLocalWorkflow | null } : {}),
    description: value.description, enabled: value.enabled,
  };
}

// Stable object-key order avoids false config changes due to JSON property ordering.
export function templateReviewValue(value: unknown, pretty = true): string {
  if (value === undefined) return "未提供";
  return JSON.stringify(value, (_key, item) => record(item) ? Object.fromEntries(Object.keys(item).sort().map(key => [key, item[key]])) : item, pretty ? 2 : undefined);
}

export interface TemplateWorkflowDifference {
  section: "parent_id" | "steps" | "groups" | "signals";
  key: string;
  status: "added" | "removed" | "updated" | "unchanged";
  fields: { field: string; before: unknown; after: unknown }[];
}

export function templateWorkflowDifferences(review: TemplateImportReview): TemplateWorkflowDifference[] | null {
  if (!review.candidate || review.before === undefined) return null;
  const before = review.before ?? { parent_id: null, steps: [], groups: [], signals: [] };
  const after = review.candidate;
  const parentChanged = before.parent_id !== after.parent_id;
  const differences: TemplateWorkflowDifference[] = [{ section: "parent_id", key: "parent_id", status: parentChanged ? "updated" : "unchanged",
    fields: parentChanged ? [{ field: "parent_id", before: before.parent_id, after: after.parent_id }] : [] }];
  for (const [section, fields] of templateArrays) {
    const identifier = fields[0];
    const oldItems = new Map(before[section].map(item => [item[identifier] as string, item]));
    const newItems = new Map(after[section].map(item => [item[identifier] as string, item]));
    const oldOrder = [...oldItems.keys()].filter(key => newItems.has(key));
    const newOrder = [...newItems.keys()].filter(key => oldItems.has(key));
    for (const key of [...new Set([...oldItems.keys(), ...newItems.keys()])].sort()) {
      const oldItem = oldItems.get(key), newItem = newItems.get(key);
      const changes = fields.filter(field => templateReviewValue(oldItem?.[field], false) !== templateReviewValue(newItem?.[field], false))
        .map(field => ({ field: field as string, before: oldItem?.[field], after: newItem?.[field] }));
      // Insertions do not change the relative order of retained items.
      if (oldItem && newItem && oldOrder.indexOf(key) !== newOrder.indexOf(key)) changes.push({ field: "array_order", before: oldOrder.indexOf(key) + 1, after: newOrder.indexOf(key) + 1 });
      differences.push({ section, key, status: !oldItem ? "added" : !newItem ? "removed" : changes.length ? "updated" : "unchanged", fields: changes });
    }
  }
  return differences;
}

export const schedulerFieldLabels: Record<string, string> = {
  enabled: "启用全局调度", mode: "调度模式", cron: "Cron 表达式", interval_seconds: "间隔秒数", ranges: "随机时间范围",
};
export interface SchedulerConfigReview {
  scope: "global_scheduler"; timezone: string; revision: number; changes: SiteConfigChange[]; warnings: string[];
}

function schedulerValueValid(field: string, value: unknown): boolean {
  if (field === "enabled") return typeof value === "boolean";
  if (field === "mode") return ["cron", "interval", "random"].includes(value as string);
  if (field === "cron") return text(value, 128);
  if (field === "interval_seconds") return nonnegative(value) && value >= 60 && value <= 2592000;
  return field === "ranges" && Array.isArray(value) && value.length <= 20 && value.every(item => nonemptyText(item, 128));
}

export function schedulerReviewValue(field: string, value: unknown): string[] {
  if (!schedulerValueValid(field, value)) return [];
  if (field === "mode") return [({ cron: "Cron 定时", interval: "固定间隔", random: "随机时间范围" } as Record<string, string>)[value as string]!];
  return reviewValue(field, value);
}

export function isSchedulerConfigReview(value: unknown): value is SchedulerConfigReview {
  if (!record(value) || value.scope !== "global_scheduler" || !nonemptyText(value.timezone, 64) || !nonnegative(value.revision) ||
    !Array.isArray(value.changes) || !value.changes.length || value.changes.length > 5 || !warnings(value.warnings) ||
    !only(value, ["scope", "timezone", "revision", "changes", "warnings"])) return false;
  try { new Intl.DateTimeFormat("en", { timeZone: value.timezone }); } catch { return false; }
  return value.changes.every(change => record(change) && typeof change.field === "string" && Object.hasOwn(schedulerFieldLabels, change.field) &&
    nonemptyText(change.label, 160) && schedulerValueValid(change.field, change.before) && schedulerValueValid(change.field, change.after) &&
    only(change, ["field", "label", "before", "after"])) && new Set(value.changes.map(change => change.field)).size === value.changes.length;
}

export const runStatusLabels: Record<string, string> = {
  pending: "等待中", queued: "排队中", running: "运行中", cancelling: "正在请求取消", cancel_requested: "已请求取消",
  cancelled: "已取消", canceled: "已取消", completed: "已完成", success: "成功", failed: "失败", fail: "失败", error: "错误", partial: "部分完成",
  deferred: "已延后", unknown: "结果不确定", waiting_human: "等待人工处理", skipped: "已跳过", interrupted: "已中断",
};
export interface RunCancelReview { run_id: number; status: string; site_count: number; warnings: string[] }
export function isRunCancelReview(value: unknown): value is RunCancelReview {
  return record(value) && positiveId(value.run_id) && typeof value.status === "string" && /^[a-z_]{1,64}$/.test(value.status) &&
    nonnegative(value.site_count) && warnings(value.warnings) && only(value, ["run_id", "status", "site_count", "warnings"]);
}
export interface RunCancelDisplay extends Omit<RunCancelReview, "site_count"> {
  site_count: number | null;
  jobs?: { id: number; site_id: number | null; site_name: string; status: string }[];
  jobs_truncated?: boolean;
}
export function runCancelReview(value: unknown): RunCancelDisplay | null {
  if (isRunCancelReview(value)) return value;
  if (!record(value) || !positiveId(value.run_id) || !nonemptyText(value.status, 64) || !/^[a-z_]+$/.test(value.status) ||
    !nonemptyText(value.mode, 64) || !nonemptyText(value.trigger, 64) || !text(value.created_at, 64) || !Number.isFinite(Date.parse(value.created_at)) ||
    typeof value.jobs_truncated !== "boolean" || !Array.isArray(value.jobs) || value.jobs.length > 50 ||
    !only(value, ["run_id", "status", "mode", "trigger", "created_at", "jobs", "jobs_truncated"])) return null;
  const jobs: NonNullable<RunCancelDisplay["jobs"]> = [];
  for (const job of value.jobs) {
    if (!record(job) || !positiveId(job.id) || (job.site_id !== null && !positiveId(job.site_id)) || !nonemptyText(job.site_name, 128) ||
      !nonemptyText(job.status, 64) || !/^[a-z_]+$/.test(job.status) || !only(job, ["id", "site_id", "site_name", "status"])) return null;
    jobs.push({ id: job.id, site_id: job.site_id, site_name: job.site_name, status: job.status });
  }
  if (new Set(jobs.map(job => job.id)).size !== jobs.length) return null;
  return { run_id: value.run_id, status: value.status, site_count: value.jobs_truncated ? null : new Set(jobs.map(job => job.site_id ?? `job:${job.id}`)).size,
    jobs, jobs_truncated: value.jobs_truncated, warnings: [] };
}

export function operationResultLabel(message: AssistantMessage, parse = JSON.parse): string | null {
  if (message.role === "tool" && message.tool_name === "clear_bound_site_cookies" &&
    ["success", "error"].includes(message.status ?? "")) {
    try {
      const result: unknown = parse(message.content);
      if (!record(result) || result.purpose !== "cookie_clear" || result.saved_cookies_cleared !== true ||
        typeof result.operation_id !== "string" || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(result.operation_id)) return null;
      if (result.browser_cookies_cleared === false && result.ok === false) {
        return "已清除站点保存的 Cookie；浏览器 Cookie 清除结果未知，不代表全部清除成功。未刷新页面，不会自动重试。";
      }
      if (result.browser_cookies_cleared === true && result.ok === true && message.status === "success") {
        return "已清除站点保存的 Cookie 和当前车道的精确主机浏览器 Cookie；未刷新页面。";
      }
    } catch { /* Malformed receipts cannot claim cookies were cleared. */ }
    return null;
  }
  if (message.role !== "tool" || message.status !== "success" ||
    !["create_template_from_site", "update_scheduler_settings", "cancel_run"].includes(message.tool_name ?? "")) return null;
  try {
    const result: unknown = parse(message.content);
    if (!record(result) || result.ok !== true) return null;
    if (message.tool_name === "create_template_from_site" && result.purpose === "template_import" && result.persisted === true &&
      positiveId(result.template_id) && nonemptyText(result.name, 128) && ["created", "updated"].includes(result.operation as string) && positiveId(result.version)) {
      return `已${result.operation === "created" ? "创建" : "更新"}模板「${result.name}」（#${result.template_id}），仅导入工作流；未运行任务。`;
    }
    if (message.tool_name === "update_scheduler_settings" && result.purpose === "scheduler_config" && result.persisted === true &&
      result.scope === "global_scheduler" && positiveId(result.revision)) return `已保存全局调度变更（修订 ${result.revision}），不是单站点设置。`;
    if (message.tool_name === "cancel_run" && positiveId(result.run_id) && result.cancel_requested === true && typeof result.finished === "boolean") {
      return `已请求取消任务 #${result.run_id}；${result.finished ? "服务端记录任务已结束，请核对最终结果，已有业务副作用不会撤销。" : "不代表任务已停止，正在执行的操作可能继续完成。"}`;
    }
  } catch { /* Invalid receipts cannot claim a saved change or a stopped run. */ }
  return null;
}
export const consumptionFeatureLabels = {
  "post.buy_card": "购买补签卡", "post.resign": "历史日历补签",
  "post.resign_loop": "页面循环补签", "post.resign_dialog": "弹窗补签",
} as const;
interface ConsumptionStep {
  feature_type: keyof typeof consumptionFeatureLabels; step_key: string; label: string;
}
interface ConsumptionPurchase extends ConsumptionStep {
  feature_type: "post.buy_card"; purchase_quantity: number; maximum_quantity: number; max_price: number; maximum_spend: number;
}
export interface ConsumptionReview {
  name: string; base_url: string; site_id: number; steps: (ConsumptionPurchase | (ConsumptionStep & { feature_type: Exclude<ConsumptionStep["feature_type"], "post.buy_card"> }))[];
  dates: string[]; date_count: number; timezone: string; today: string; warnings: string[];
}
const calendarDate = (value: unknown): value is string => text(value, 10) && /^\d{4}-\d{2}-\d{2}$/.test(value) &&
  Number.isFinite(Date.parse(value)) && new Date(value).toISOString().slice(0, 10) === value;
const quantity = (value: unknown): value is number => Number.isSafeInteger(value) && (value as number) >= 0 && (value as number) <= 365;

// This separate public allowlist admits full consumption scopes, not larger generic reviews.
export function isConsumptionReview(value: unknown): value is ConsumptionReview {
  if (!record(value) || !text(value.name, 128) || !value.name.trim() || !text(value.base_url, 512) ||
    !positiveId(value.site_id) || !text(value.timezone, 64) || !value.timezone.trim() || !calendarDate(value.today) ||
    !Array.isArray(value.dates) || value.dates.length > 365 || value.date_count !== value.dates.length ||
    !value.dates.every(day => calendarDate(day) && day < (value.today as string)) || new Set(value.dates).size !== value.dates.length ||
    !Array.isArray(value.warnings) || !value.warnings.length || value.warnings.length > 3 ||
    !value.warnings.every(warning => text(warning) && Boolean(warning.trim())) ||
    !Array.isArray(value.steps) || !value.steps.length || value.steps.length > 365 ||
    Object.keys(value).some(key => !["name", "base_url", "site_id", "steps", "dates", "date_count", "timezone", "today", "warnings"].includes(key))) return false;
  try {
    const url = new URL(value.base_url);
    if (!/^https?:\/\//i.test(value.base_url) || url.username || url.password) return false;
    new Intl.DateTimeFormat("en", { timeZone: value.timezone });
  } catch { return false; }
  const dateCount = value.dates.length;
  return value.steps.every(step => {
    if (!record(step) || !text(step.feature_type, 128) || !Object.hasOwn(consumptionFeatureLabels, step.feature_type) ||
      !text(step.step_key, 64) || !/^[a-z][a-z0-9_.-]*$/.test(step.step_key) || !text(step.label, 128) || !step.label.trim()) return false;
    const fields = ["feature_type", "step_key", "label"];
    if (step.feature_type === "post.buy_card") {
      fields.push("purchase_quantity", "maximum_quantity", "max_price", "maximum_spend");
      if (!quantity(step.purchase_quantity) || !quantity(step.maximum_quantity) || !positiveId(step.max_price) ||
        !Number.isSafeInteger(step.maximum_spend) || step.maximum_quantity !== (step.purchase_quantity || dateCount) ||
        step.maximum_spend !== step.maximum_quantity * step.max_price) return false;
    }
    return Object.keys(step).every(key => fields.includes(key));
  }) && new Set(value.steps.map(step => step.step_key)).size === value.steps.length;
}

const changeFields = ["name", "enabled", "lane_policy", "trusted_urls",
  "support_resign", "resign_max_days", "checkin_after", "timezone", "tags", "notes",
  "observation.proxy", "observation.trusted_origins"];

export function isSiteConfigReview(value: unknown): value is SiteConfigReview {
  return record(value) && text(value.name, 128) && Boolean(value.name.trim()) && text(value.base_url) && Boolean(value.base_url.trim()) &&
    (value.site_id === undefined || positiveId(value.site_id)) && (value.successful_run_id === undefined || positiveId(value.successful_run_id)) &&
    Array.isArray(value.changes) && value.changes.length > 0 && value.changes.length <= 12 &&
    value.changes.every(change => record(change) && typeof change.field === "string" && changeFields.includes(change.field) &&
      text(change.label, 160) && Boolean(change.label.trim()) && Object.hasOwn(change, "before") && Object.hasOwn(change, "after") &&
      reviewValue(change.field, change.before).length > 0 && reviewValue(change.field, change.after).length > 0 &&
      (change.field !== "trusted_urls" || (Array.isArray(change.after) && change.after.length <= 20 &&
        change.after.every(pattern => text(pattern, 2048) && [...pattern].length <= 1024 && Boolean(pattern.trim())))) &&
      Object.keys(change).every(key => ["field", "label", "before", "after"].includes(key))) &&
    new Set(value.changes.map(change => change.field)).size === value.changes.length &&
    Array.isArray(value.warnings) && value.warnings.length <= 3 && value.warnings.every(warning => text(warning) && Boolean(warning.trim())) &&
    Object.keys(value).every(key => ["name", "base_url", "site_id", "changes", "warnings", "successful_run_id"].includes(key));
}

export function siteConfigResult(message: AssistantMessage, parse = JSON.parse): { site_id: number; name?: string; fields: string[] } | null {
  if (message.role !== "tool" || message.tool_name !== "update_site_config" || message.status !== "success") return null;
  try {
    const result: unknown = parse(message.content);
    if (!record(result) || result.ok !== true || result.purpose !== "site_config" || result.persisted !== true ||
      typeof result.operation_id !== "string" || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(result.operation_id) ||
      !positiveId(result.site_id) || (result.name !== undefined && (!text(result.name, 128) || !result.name.trim())) ||
      !positiveId(result.config_revision) || typeof result.history_key !== "string" ||
      !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(result.history_key) ||
      (result.changed_fields !== undefined && (!Array.isArray(result.changed_fields) || !result.changed_fields.length || result.changed_fields.length > 12 ||
        !result.changed_fields.every(field => typeof field === "string" && changeFields.includes(field)) ||
        new Set(result.changed_fields).size !== result.changed_fields.length))) return null;
    return { site_id: result.site_id, ...(typeof result.name === "string" ? { name: result.name } : {}),
      fields: ((result.changed_fields ?? []) as string[]).map(field => reviewFieldLabels[field]!) };
  } catch { return null; }
}
