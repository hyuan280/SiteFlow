export type PluginStatusType = "success" | "warning" | "danger" | "info";

export function pluginStatusLabel(status: string): string {
  if (status === "available") return "正常";
  if (status === "unapproved") return "待审批";
  if (status === "unhealthy") return "异常";
  if (status === "disabled") return "已停止";
  if (status === "removed") return "已移除接入";
  if (status === "incompatible") return "不兼容";
  if (status === "registering") return "注册中";
  return status || "未知";
}

export function pluginStatusType(status: string): PluginStatusType {
  if (status === "available") return "success";
  if (status === "unapproved") return "warning";
  if (status === "unhealthy" || status === "incompatible") return "danger";
  return "info";
}

export function capabilityLabel(key: string): string {
  const labels: Record<string, string> = {
    features: "功能步骤",
    captcha_methods: "验证码方法",
    browser_actions: "浏览器动作",
    "secret.credentials": "密码提供（高信任）",
    "secret.totp": "TOTP 提供（高信任）",
  };
  return labels[key] ?? key;
}

export function capabilityType(key: string): "success" | "warning" | "danger" | "info" {
  if (key.startsWith("secret.")) return "danger";
  if (key === "browser_actions") return "warning";
  if (key === "features" || key === "captcha_methods") return "success";
  return "info";
}

export function checkStatusLabel(check: {
  ok: boolean;
  skipped: boolean;
}): string {
  if (check.skipped) return "已跳过";
  return check.ok ? "通过" : "失败";
}

export function checkStatusType(check: {
  ok: boolean;
  skipped: boolean;
}): "success" | "danger" | "info" {
  if (check.skipped) return "info";
  return check.ok ? "success" : "danger";
}
