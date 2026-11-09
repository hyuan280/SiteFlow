import type { AssistantMessage } from "@/types/assistant";

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
  site_config: "站点配置变更", question: "信息确认",
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
  const fields = purpose === "target" || purpose === "site_config" ? ["name", "base_url"] :
    ["name", "base_url", "scope", "origins", "trusted_origins", "trusted_urls", "suggested_lane", "current_browser_lane",
      "current_run_lane_policy", "run_lane_policy", "lane_policy", "proxy", "capability", "credential_mode", "site_id",
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

export function siteConfigResult(message: AssistantMessage): { site_id: number; name?: string; fields: string[] } | null {
  if (message.role !== "tool" || message.tool_name !== "update_site_config" || message.status !== "success") return null;
  try {
    const result: unknown = JSON.parse(message.content);
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
