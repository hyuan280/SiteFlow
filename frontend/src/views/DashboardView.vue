<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue";
import { useRouter } from "vue-router";
import { ElMessage } from "element-plus";

import { ApiError, apiRequest } from "@/api/client";
import PageHeader from "@/components/PageHeader.vue";
import ResourceRing from "@/components/ResourceRing.vue";
import { useAuthStore } from "@/stores/auth";
import type { MatrixView } from "@/types/analytics";
import type { Feature, RunDetail, RunSummary, SiteJobView, SiteOption, TemplateSummary } from "@/types/workflow";
import { dashboardCards, dashboardStatus, dashboardStatuses, defaultDashboardLayout, parseDashboardLayout } from "@/utils/dashboard";
import { formatServerDateTime } from "@/utils/datetime";

interface Resources {
  cpu_percent: number | null;
  memory_percent: number | null;
  memory_used_bytes: number | null;
  memory_total_bytes: number | null;
  scope: string;
  sampled_at: string;
  error: string | null;
  docker_available: boolean;
  siteflow_cpu_percent: number | null;
  siteflow_memory_percent: number | null;
  siteflow_memory_used_bytes: number | null;
  siteflow_container_count: number;
  docker_error: string | null;
}

interface LanesResponse {
  worker_connected: boolean;
  lanes: { key: string; status: string }[];
}

const auth = useAuthStore();
const router = useRouter();
const loading = ref(true);
const refreshing = ref(false);
const errors = ref<string[]>([]);
const sites = ref<SiteOption[]>([]);
const templates = ref<TemplateSummary[]>([]);
const features = ref<Feature[]>([]);
const lanes = ref<LanesResponse | null>(null);
const runningLanes = computed(() => lanes.value?.lanes.filter((lane) => lane.status === "running").length ?? 0);
const dailyMatrix = ref<MatrixView | null>(null);
const latestRun = ref<RunDetail | null>(null);
const resources = ref<Resources | null>(null);
const showResources = ref(false);
const scopeOpen = ref(false);
const layoutDialog = ref(false);
const storageKey = "siteflow.dashboard.layout.v1";
const layout = ref(defaultDashboardLayout());
const draftLayout = ref(defaultDashboardLayout());
const displayTimezone = ref("Asia/Shanghai");
let timer: ReturnType<typeof setInterval> | undefined;
let active = true;

try { layout.value = parseDashboardLayout(localStorage.getItem(storageKey)); } catch { /* Storage may be unavailable. */ }

const visibleCards = computed(() => layout.value.filter((card) => card.key !== "resources" || showResources.value));
const enabledSites = computed(() => sites.value.filter((site) => site.enabled).length);
const dailySites = computed(() => dailyMatrix.value?.sites.filter((site) => site.enabled) ?? []);
const slices = computed(() => {
  const counts: Record<string, number> = {};
  for (const site of dailySites.value) {
    const status = site.cells[dailyMatrix.value!.end_date]?.status ?? "not_run";
    counts[status] = (counts[status] ?? 0) + 1;
  }
  return [...dashboardStatuses.filter((item) => item.key !== "skipped"),
    { key: "not_run", label: "未运行", color: "#dcdfe6" }].map((item) => ({
    ...item, count: counts[item.key] ?? 0,
  }));
});
const dailySuccessRate = computed(() => dailySites.value.length
  ? (slices.value.find((item) => item.key === "success")?.count ?? 0) / dailySites.value.length * 100 : 0);
const pieStyle = computed(() => {
  const total = slices.value.reduce((sum, item) => sum + item.count, 0);
  if (!total) return { background: "#edf1f6" };
  let offset = 0;
  const stops = slices.value.filter((item) => item.count > 0).map((item) => {
    const start = offset;
    offset += item.count / total * 100;
    return `${item.color} ${start}% ${offset}%`;
  });
  return { background: `conic-gradient(${stops.join(", ")})` };
});
const finalJobs = computed(() => {
  const latest = new Map<string, SiteJobView>();
  for (const job of latestRun.value?.jobs ?? []) {
    const key = job.site_id === null ? job.site_name : String(job.site_id);
    const previous = latest.get(key);
    if (!previous || job.id > previous.id) latest.set(key, job);
  }
  return [...latest.values()].filter((job) => dashboardStatuses.some((status) => status.key === job.status));
});
const resourceMetrics = computed(() => [
  { label: "CPU", value: resources.value?.cpu_percent ?? null,
    siteflow: resources.value?.docker_available ? resources.value.siteflow_cpu_percent ?? null : null,
    detail: "占宿主机全部 CPU 算力" },
  { label: "内存", value: resources.value?.memory_percent ?? null,
    siteflow: resources.value?.docker_available ? resources.value.siteflow_memory_percent ?? null : null,
    detail: `${formatBytes(resources.value?.memory_used_bytes)} / ${formatBytes(resources.value?.memory_total_bytes)}` },
]);

function formatBytes(value: number | null | undefined): string {
  if (value == null) return "--";
  return `${(value / 1024 ** 3).toFixed(1)} GB`;
}

function formatDate(value: string): string {
  return formatServerDateTime(value, displayTimezone.value);
}

async function captureError(cause: unknown, label: string): Promise<void> {
  if (!active) return;
  if (cause instanceof ApiError && cause.status === 401) {
    auth.clear();
    await router.replace({ name: "login" });
    return;
  }
  errors.value.push(`${label}：${cause instanceof Error ? cause.message : "加载失败"}`);
}

async function load(): Promise<void> {
  if (refreshing.value) return;
  refreshing.value = true;
  errors.value = [];
  try {
    await Promise.all([
      (async () => {
        try {
          const [siteList, templateList, featureList] = await Promise.all([
            apiRequest<SiteOption[]>("/sites?view=options&include_valid=true"),
            apiRequest<TemplateSummary[]>("/templates"), apiRequest<Feature[]>("/features"),
          ]);
          if (active) { sites.value = siteList; templates.value = templateList; features.value = featureList; }
        } catch (cause) { await captureError(cause, "概况"); }
      })(),
      (async () => {
        try {
          const response = await apiRequest<LanesResponse>("/lanes");
          if (active) lanes.value = response;
        } catch (cause) {
          lanes.value = null;
          await captureError(cause, "车道状态");
        }
      })(),
      (async () => {
        try {
          const settings = await apiRequest<{ timezone: string }>("/scheduler-settings");
          if (active) displayTimezone.value = settings.timezone || "Asia/Shanghai";
        } catch (cause) { await captureError(cause, "显示时区"); }
      })(),
      (async () => {
        try {
          const response = await apiRequest<MatrixView>("/analytics/matrix?days=1&include_disabled=false");
          if (active) dailyMatrix.value = response;
        } catch (cause) { await captureError(cause, "今日分析"); }
      })(),
      (async () => {
        try {
          const runs = await apiRequest<RunSummary[]>("/runs?limit=1");
          const response = runs[0] ? await apiRequest<RunDetail>(`/runs/${runs[0].id}`) : null;
          if (active) latestRun.value = response;
        } catch (cause) { await captureError(cause, "最近调度"); }
      })(),
      (async () => {
        try {
          const settings = await apiRequest<{ show_system_resources: boolean }>("/system-settings");
          if (!active) return;
          showResources.value = settings.show_system_resources;
          if (!showResources.value) { resources.value = null; return; }
          const response = await apiRequest<Resources>("/system-resources");
          if (active) resources.value = response;
        } catch (cause) {
          resources.value = null;
          await captureError(cause, "系统资源");
        }
      })(),
    ]);
  } finally {
    loading.value = false;
    refreshing.value = false;
  }
}

function moveCard(index: number, direction: number): void {
  const next = index + direction;
  if (next < 0 || next >= draftLayout.value.length) return;
  const items = [...draftLayout.value];
  [items[index], items[next]] = [items[next]!, items[index]!];
  draftLayout.value = items;
}

function editLayout(): void {
  draftLayout.value = layout.value.map((card) => ({ ...card }));
  layoutDialog.value = true;
}

function saveLayout(): void {
  try {
    localStorage.setItem(storageKey, JSON.stringify(draftLayout.value));
    layout.value = draftLayout.value.map((card) => ({ ...card }));
    layoutDialog.value = false;
    ElMessage.success("面板布局已保存在当前浏览器");
  } catch { ElMessage.error("无法保存布局，请检查浏览器存储权限"); }
}

function openRun(job?: SiteJobView): void {
  if (!latestRun.value) return;
  void router.push({ name: "runs", query: { run: latestRun.value.id,
    ...(job ? { job: job.id, ...(job.site_id !== null ? { site: job.site_id } : {}) } : {}) } });
}

function openAnalytics(): void {
  void router.push({ name: "analytics" });
}

function refreshVisible(): void { if (!document.hidden) void load(); }
onMounted(() => {
  void load();
  timer = setInterval(refreshVisible, 15000);
  document.addEventListener("visibilitychange", refreshVisible);
});
onUnmounted(() => {
  active = false;
  if (timer) clearInterval(timer);
  document.removeEventListener("visibilitychange", refreshVisible);
});
</script>

<template>
  <PageHeader eyebrow="DASHBOARD" title="数据面板" description="今日运行结果、最近任务调度与系统资源，一眼掌握运行概况。">
    <template #actions>
      <el-button @click="editLayout">调整布局</el-button>
      <el-button :loading="refreshing" @click="load">刷新</el-button>
    </template>
  </PageHeader>
  <el-alert v-for="message in errors" :key="message" :title="message" type="error" :closable="false" show-icon class="notice" />

  <div class="dashboard-grid">
    <article v-for="card in visibleCards" :key="card.key" class="dashboard-card" :class="{ 'is-full': card.width === 'full' }">
      <header class="card-head">
        <h2>{{ dashboardCards[card.key] }}</h2>
        <el-button v-if="card.key === 'analytics'" link type="primary" @click="openAnalytics">查看分析 →</el-button>
        <el-button v-else-if="card.key === 'latest-run'" link type="primary" @click="latestRun ? openRun() : router.push({ name: 'runs' })">调度详情 →</el-button>
        <el-button v-else-if="card.key === 'resources'" link type="primary" @click="router.push({ name: 'settings' })">系统设置 →</el-button>
      </header>

      <div v-if="card.key === 'overview'" class="stat-grid" v-loading="loading">
        <button class="stat-card" type="button" @click="router.push({ name: 'sites' })">
          <span>站点</span><strong>{{ sites.length }}</strong><small>已启用 {{ enabledSites }} 个</small>
        </button>
        <button class="stat-card" type="button" @click="router.push({ name: 'templates' })">
          <span>模板</span><strong>{{ templates.length }}</strong><small>可复用工作流</small>
        </button>
        <button class="stat-card" type="button" @click="router.push({ name: 'features' })">
          <span>功能块</span><strong>{{ features.length }}</strong><small>已注册功能类型</small>
        </button>
        <button class="stat-card" type="button" @click="router.push({ name: 'browsers' })">
          <span>车道状态</span><strong>{{ lanes ? `${runningLanes} / ${lanes.lanes.length}` : '--' }}</strong>
          <small>{{ lanes ? (lanes.worker_connected ? 'Worker 在线' : 'Worker 不可达') : '状态暂不可用' }}</small>
        </button>
      </div>
      <template v-else-if="card.key === 'analytics'">
        <div v-if="dailyMatrix" class="analytics-body">
          <button type="button" class="pie-button" aria-label="查看今日运行分析" @click="openAnalytics">
            <span class="pie-chart" :style="pieStyle"><span class="pie-center"><strong>{{ dailySites.length }}</strong><small>启用站点</small></span></span>
          </button>
          <div class="chart-legend">
            <div class="success-rate"><strong>{{ dailySuccessRate.toFixed(1) }}<small>%</small></strong><span>成功率</span></div>
            <button v-for="slice in slices" :key="slice.key" type="button" class="legend-row" @click="openAnalytics">
              <i :style="{ background: slice.color }" /><span>{{ slice.label }}</span><strong>{{ slice.count }}</strong>
            </button>
          </div>
        </div>
        <el-empty v-else description="暂无今日运行分析" :image-size="64" />
        <p class="card-note">{{ dailyMatrix?.end_date ?? '今日' }} · 仅统计当前启用站点，每站计一次；无当日结果计为未运行，同日成功优先。</p>
      </template>

      <template v-else-if="card.key === 'latest-run'">
        <template v-if="latestRun">
          <button type="button" class="run-summary" @click="openRun()">
            <span><strong>任务 #{{ latestRun.id }}</strong><small>{{ formatDate(latestRun.created_at) }}</small></span>
            <span class="status-label" :style="{ color: dashboardStatus(latestRun.status).color }">{{ dashboardStatus(latestRun.status).label }}</span>
          </button>
          <div class="job-list">
            <button v-for="job in finalJobs" :key="job.id" type="button" class="job-row" @click="openRun(job)">
              <span class="job-name">{{ job.site_name }}</span>
              <span class="status-label" :style="{ color: dashboardStatus(job.status).color }"><i />{{ dashboardStatus(job.status).label }}</span>
            </button>
            <p v-if="!finalJobs.length" class="empty-note">尚无已结束的站点任务，完成后会在此显示最终状态。</p>
          </div>
          <p class="card-note">{{ latestRun.site_count }} 个站点 · 已显示 {{ finalJobs.length }} 个最终结果，不展开步骤和日志。</p>
        </template>
        <el-empty v-else description="还没有任务调度记录" :image-size="64" />
      </template>

      <template v-else>
        <div class="resource-grid">
          <ResourceRing v-for="metric in resourceMetrics" :key="metric.label" :label="metric.label"
            :host-percent="metric.value" :siteflow-percent="metric.siteflow" :detail="metric.detail" />
        </div>
        <p v-if="resources?.error" class="resource-error">{{ resources.error }}</p>
        <p v-if="resources?.docker_error" class="resource-error">{{ resources.docker_error }}，无法拆分的指标仅显示宿主机占用。</p>
        <p class="card-note">
          <template v-if="resources?.docker_available">
            已汇总 {{ resources.siteflow_container_count }} 个 SiteFlow 服务容器<span v-if="resources.siteflow_memory_used_bytes !== null">，内存 {{ formatBytes(resources.siteflow_memory_used_bytes) }}</span>。
            SiteFlow 已包含在宿主机已用资源中，所有百分比均以宿主机总资源为分母。
          </template>
          <template v-else>当前仅显示宿主机资源；挂载 Docker Socket 后可显示 SiteFlow 服务占用。</template>
          <el-tooltip v-if="resources" :visible="scopeOpen" :content="resources.scope" placement="top">
            <template #content><span id="resource-scope-help">{{ resources.scope }}</span></template>
            <button type="button" class="resource-scope" :aria-describedby="scopeOpen ? 'resource-scope-help' : undefined"
              @focus="scopeOpen = true" @blur="scopeOpen = false" @mouseenter="scopeOpen = true" @mouseleave="scopeOpen = false"
              @click="scopeOpen = true" @keydown.esc.prevent="scopeOpen = false">统计口径</button>
          </el-tooltip><br />
          <template v-if="resources">采样时间 {{ formatDate(resources.sampled_at) }}</template>
        </p>
      </template>
    </article>
  </div>

  <el-dialog v-model="layoutDialog" title="调整面板布局" width="min(560px, 94vw)">
    <p class="muted layout-hint">按顺序从左到右排列；整宽独占一行，半宽可并排。窄屏自动单列，设置仅保存在当前浏览器。</p>
    <div v-for="(card, index) in draftLayout" :key="card.key" class="layout-row">
      <strong>{{ dashboardCards[card.key] }}</strong>
      <el-select v-model="card.width" aria-label="卡片宽度" class="width-select">
        <el-option label="半宽" value="half" /><el-option label="整宽" value="full" />
      </el-select>
      <el-button :disabled="index === 0" :aria-label="`上移${dashboardCards[card.key]}`" @click="moveCard(index, -1)">↑</el-button>
      <el-button :disabled="index === draftLayout.length - 1" :aria-label="`下移${dashboardCards[card.key]}`" @click="moveCard(index, 1)">↓</el-button>
    </div>
    <p v-if="!showResources" class="muted">系统资源卡片将在系统设置中开启后显示。</p>
    <template #footer><el-button @click="draftLayout = defaultDashboardLayout()">恢复默认</el-button><el-button @click="layoutDialog = false">取消</el-button><el-button type="primary" @click="saveLayout">保存布局</el-button></template>
  </el-dialog>
</template>

<style scoped>
.notice { margin-bottom: 14px; }
.stat-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 140px), 1fr)); gap: 12px; padding: 18px; }
.stat-card { border: 1px solid #edf1f6; border-radius: 10px; padding: 14px; background: #f8fafc; color: inherit; text-align: left; cursor: pointer; }
.stat-card:hover { border-color: #b9d7fb; }
.stat-card span, .stat-card small { display: block; color: #7a8794; font-size: 12px; }
.stat-card strong { display: block; font-size: 30px; margin: 6px 0; }
.dashboard-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 18px; align-items: start; }
.dashboard-card { min-width: 0; border: 1px solid #e4e9f0; border-radius: 16px; background: #fff; overflow: hidden; container-type: inline-size; }
.dashboard-card.is-full { grid-column: 1 / -1; }
.card-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; padding: 16px 20px; border-bottom: 1px solid #edf1f6; }
.card-head h2 { margin: 0; font-size: 15px; }
.card-note { color: #8a96a3; font-size: 12px; line-height: 1.7; margin: 0; padding: 12px 20px 16px; }
.analytics-body { display: flex; align-items: center; justify-content: center; gap: 28px; padding: 22px 20px 4px; }
.pie-button { padding: 0; background: none; border: 0; cursor: pointer; flex: none; width: 243px; max-width: 100%; border-radius: 50%; }
.pie-chart { width: 100%; aspect-ratio: 1; border-radius: 50%; display: grid; place-items: center; }
.pie-center { display: grid; place-content: center; width: 74%; height: 74%; border-radius: 50%; background: white; }
.pie-center strong { font-size: 30px; color: #1f2d3d; }
.pie-center small { margin-top: 4px; color: #7a8794; }
.chart-legend { width: 160px; min-width: 0; flex: none; }
.success-rate { display: flex; align-items: baseline; gap: 10px; margin-bottom: 10px; }
.success-rate strong { font-size: 24px; color: #509c2d; }
.success-rate small, .success-rate span { font-size: 12px; white-space: nowrap; }
.success-rate span { color: #7a8794; }
.legend-row { display: flex; align-items: center; width: 100%; gap: 8px; border: 0; background: none; padding: 4px 0; color: #606266; cursor: pointer; text-align: left; }
.legend-row i { width: 7px; height: 7px; border-radius: 50%; }
.legend-row strong { margin-left: auto; }
.run-summary { border: 0; background: #f8fafc; display: flex; justify-content: space-between; align-items: center; width: 100%; gap: 12px; padding: 16px 20px; color: inherit; cursor: pointer; text-align: left; }
.run-summary small { display: block; margin-top: 5px; color: #8a96a3; font-size: 11px; }
.job-list { max-height: 242px; overflow-y: auto; padding: 0 20px; }
.job-row { display: flex; align-items: center; justify-content: space-between; gap: 14px; width: 100%; border: 0; border-bottom: 1px solid #edf1f6; padding: 13px 0; background: none; color: inherit; cursor: pointer; text-align: left; }
.job-row:hover { color: #409eff; }
.job-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.status-label { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; white-space: nowrap; flex: none; }
.status-label i { width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
.empty-note { margin: 0; padding: 30px 0; color: #8a96a3; font-size: 13px; line-height: 1.7; }
.resource-grid { display: flex; flex-wrap: wrap; justify-content: space-evenly; gap: 24px; padding: 24px 20px 8px; }
.resource-scope { color: #409eff; cursor: help; margin-left: 6px; border: 0; border-bottom: 1px dotted currentColor; padding: 0; background: none; font: inherit; }
.resource-error { color: #c45656; margin: 12px 20px 0; font-size: 13px; }
.layout-hint { margin-top: 0; line-height: 1.7; }
.layout-row { display: flex; align-items: center; gap: 8px; padding: 12px 0; border-bottom: 1px solid #edf1f6; }
.layout-row strong { flex: 1; font-size: 13px; }
.layout-row .el-button + .el-button { margin-left: 0; }
.width-select { width: 90px; flex: none; }
@container (max-width: 470px) { .analytics-body { flex-direction: column; gap: 20px; padding-inline: 14px; } .chart-legend { width: 243px; max-width: 100%; } }
@media (max-width: 1050px) { .dashboard-grid { grid-template-columns: 1fr; } }
@media (max-width: 600px) { .stat-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; } .stat-card { padding: 14px 10px; } .stat-card strong { font-size: 24px; } .stat-card small { font-size: 10px; } .card-head { padding: 14px; } .layout-row { flex-wrap: wrap; } .layout-row strong { min-width: 100%; } }
</style>
