export type DashboardCardKey = "overview" | "analytics" | "latest-run" | "resources";
export interface DashboardCardLayout {
  key: DashboardCardKey;
  width: "half" | "full";
}

export const dashboardCards: Record<DashboardCardKey, string> = {
  overview: "基础概况",
  analytics: "今日运行分析",
  "latest-run": "最近一次任务调度",
  resources: "系统资源",
};

export function defaultDashboardLayout(): DashboardCardLayout[] {
  return [
    { key: "overview", width: "full" },
    { key: "analytics", width: "half" },
    { key: "latest-run", width: "half" },
    { key: "resources", width: "full" },
  ];
}

export function parseDashboardLayout(raw: string | null): DashboardCardLayout[] {
  const defaults = defaultDashboardLayout();
  try {
    const parsed: unknown = JSON.parse(raw ?? "null");
    if (!Array.isArray(parsed)) return defaults;
    const result: DashboardCardLayout[] = [];
    for (const item of parsed) {
      if (!item || typeof item !== "object" || !("key" in item) || !("width" in item)) continue;
      if (!defaults.some((card) => card.key === item.key) || result.some((card) => card.key === item.key)) continue;
      result.push({ key: item.key as DashboardCardKey, width: item.width === "full" ? "full" : "half" });
    }
    return [...result, ...defaults.filter((card) => !result.some((item) => item.key === card.key))];
  } catch {
    return defaults;
  }
}

export const dashboardStatuses = [
  { key: "success", label: "成功", color: "#67c23a" },
  { key: "fail", label: "失败", color: "#f56c6c" },
  { key: "error", label: "错误", color: "#c45656" },
  { key: "waiting_human", label: "待人工", color: "#e6a23c" },
  { key: "unknown", label: "未知", color: "#909399" },
  { key: "deferred", label: "已推迟", color: "#b88230" },
  { key: "canceled", label: "已取消", color: "#a8abb2" },
  { key: "interrupted", label: "已中断", color: "#8e78b5" },
  { key: "skipped", label: "已跳过", color: "#79bbff" },
];

export function dashboardStatus(status: string): { label: string; color: string } {
  return dashboardStatuses.find((item) => item.key === status)
    ?? { label: status === "running" ? "运行中" : status === "queued" ? "排队中" : status, color: "#409eff" };
}
