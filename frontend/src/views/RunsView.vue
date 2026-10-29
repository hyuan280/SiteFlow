<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from "vue";
import { onBeforeRouteLeave, useRoute, useRouter } from "vue-router";
import { ElMessage, ElMessageBox } from "element-plus";

import { ApiError, apiRequest } from "@/api/client";
import { useAuthStore } from "@/stores/auth";
import type { RunDetail, RunSummary, SiteDetail, SiteOption } from "@/types/workflow";
import { formatServerDateTime } from "@/utils/datetime";
import { laneLabel } from "@/utils/workflow";

const auth = useAuthStore();
const route = useRoute();
const router = useRouter();
const runs = ref<RunSummary[]>([]);
const sites = ref<SiteOption[]>([]);
const selectedSiteIds = ref<number[]>([]);
const loading = ref(true);
const error = ref("");
const creating = ref(false);
const retriggering = ref(false);
const siteDetailOpen = ref(false);
const siteDetailLoading = ref(false);
const siteDetailError = ref("");
const siteDetail = ref<SiteDetail | null>(null);
const actionRunId = ref<number | null>(null);
const drawerOpen = ref(false);
const detail = ref<RunDetail | null>(null);
const detailLoading = ref(false);
const detailError = ref("");
const displayTimezone = ref("Asia/Shanghai");
const schedulerInfo = ref<{
  enabled: boolean;
  next_run_at: string | null;
  timezone: string;
} | null>(null);
const activeJobId = ref<number | null>(null);
const detailStreamState = ref<"idle" | "connecting" | "live" | "error">("idle");
const jobView = ref<"status" | "log">("status");
const jobLogText = ref("");
const jobLogLoading = ref(false);
const jobLogError = ref("");
const jobLogRef = ref<HTMLElement | null>(null);
let refreshTimer: ReturnType<typeof setInterval> | null = null;
let detailStream: EventSource | null = null;
let detailOpenSeq = 0;
let logRequestSeq = 0;
let refreshBusy = false;

const FINAL_RUN_STATUSES = new Set([
  "success",
  "fail",
  "error",
  "unknown",
  "waiting_human",
  "canceled",
  "interrupted",
]);
const activeJob = computed(() => {
  if (!detail.value) return null;
  return (
    detail.value.jobs.find((job) => job.id === activeJobId.value) ??
    detail.value.jobs[0] ??
    null
  );
});
const logArtifact = computed(
  () => activeJob.value?.artifacts.find((artifact) => artifact.kind === "log") ?? null,
);
const failureScreenshots = computed(
  () =>
    activeJob.value?.artifacts.filter(
      (artifact) => artifact.kind === "screenshot" || artifact.kind === "captcha",
    ) ?? [],
);
const failurePreviewOpen = ref(false);
const failurePreviewIndex = ref(0);
const currentFailureScreenshot = computed(
  () => failureScreenshots.value[failurePreviewIndex.value] ?? null,
);

const enabledSites = computed(() => sites.value.filter((site) => site.enabled));
const hasActiveRuns = computed(() =>
  runs.value.some((run) => run.status === "queued" || run.status === "running"),
);
const STATUS_LABELS: Record<string, string> = {
  queued: "排队中",
  running: "运行中",
  success: "成功",
  fail: "失败",
  error: "错误",
  deferred: "已推迟",
  unknown: "未知",
  skipped: "跳过",
  waiting_human: "等待人工",
  canceled: "已取消",
  interrupted: "已中断",
};

function statusLabel(status: string): string {
  return STATUS_LABELS[status] ?? status;
}

function statusType(status: string): "success" | "warning" | "danger" | "info" {
  if (status === "success") return "success";
  if (status === "running" || status === "queued" || status === "deferred") return "warning";
  if (status === "waiting_human" || status === "unknown" || status === "interrupted") return "danger";
  if (status === "fail" || status === "error") return "danger";
  return "info";
}

function artifactUrl(artifactId: number, download = false): string {
  return `/api/v1/artifacts/${artifactId}${download ? "?download=1" : ""}`;
}

function openFailurePreview(): void {
  failurePreviewIndex.value = 0;
  failurePreviewOpen.value = true;
}

function stepFailurePreview(delta: number): void {
  const count = failureScreenshots.value.length;
  if (!count) return;
  failurePreviewIndex.value = (failurePreviewIndex.value + delta + count) % count;
}

function downloadFailureScreenshot(): void {
  const artifact = currentFailureScreenshot.value;
  if (artifact) window.location.href = artifactUrl(artifact.id, true);
}

async function loadJobLog(): Promise<void> {
  const artifact = logArtifact.value;
  const seq = ++logRequestSeq;
  jobLogError.value = "";
  if (!artifact) {
    jobLogText.value = "";
    jobLogLoading.value = false;
    return;
  }
  jobLogLoading.value = true;
  try {
    const response = await fetch(artifactUrl(artifact.id), {
      credentials: "include",
    });
    if (seq !== logRequestSeq) return;
    if (!response.ok) {
      throw new Error(`加载详细日志失败（HTTP ${response.status}）`);
    }
    jobLogText.value = await response.text();
    await nextTick();
    const element = jobLogRef.value;
    if (element) element.scrollTop = element.scrollHeight;
  } catch (cause) {
    if (seq === logRequestSeq) {
      jobLogError.value = cause instanceof Error ? cause.message : "加载详细日志失败";
    }
  } finally {
    if (seq === logRequestSeq) jobLogLoading.value = false;
  }
}

watch(
  () => activeJob.value?.id ?? null,
  () => {
    failurePreviewOpen.value = false;
    failurePreviewIndex.value = 0;
    siteDetailOpen.value = false;
    siteDetail.value = null;
    siteDetailError.value = "";
  },
);

watch(
  [jobView, () => activeJob.value?.id ?? null, () => logArtifact.value?.sha256 ?? ""],
  () => {
    if (jobView.value === "log") {
      void loadJobLog();
    }
  },
);

function systemTimezone(): string {
  return displayTimezone.value;
}

function formatDate(value: string | null): string {
  return formatServerDateTime(value, systemTimezone());
}

function summaryText(summary: Record<string, number>): string {
  const parts = Object.entries(summary)
    .filter(([, count]) => count > 0)
    .map(([status, count]) => `${statusLabel(status)} ${count}`);
  return parts.length ? parts.join(" · ") : "-";
}

async function handleUnauthorized(cause: unknown): Promise<boolean> {
  if (cause instanceof ApiError && cause.status === 401) {
    auth.clear();
    await router.replace({ name: "login" });
    return true;
  }
  return false;
}

async function loadRuns(silent = false): Promise<void> {
  if (refreshBusy) return;
  refreshBusy = true;
  if (!silent) loading.value = true;
  try {
    const [runList, siteList] = await Promise.all([
      apiRequest<RunSummary[]>("/runs?limit=100"),
      silent
        ? Promise.resolve(sites.value)
        : apiRequest<SiteOption[]>("/sites?view=options"),
    ]);
    runs.value = runList;
    sites.value = siteList;
    error.value = "";
  } catch (cause) {
    if (await handleUnauthorized(cause)) return;
    if (!silent) error.value = cause instanceof Error ? cause.message : "加载运行记录失败";
  } finally {
    refreshBusy = false;
    if (!silent) loading.value = false;
  }
}

async function loadSchedulerInfo(): Promise<void> {
  try {
    const response = await apiRequest<{
      enabled: boolean;
      next_run_at: string | null;
      timezone: string;
    }>("/scheduler-settings");
    schedulerInfo.value = response;
    displayTimezone.value = response.timezone || "Asia/Shanghai";
  } catch (cause) {
    await handleUnauthorized(cause);
  }
}

async function loadDetail(
  runId: number,
  silent = false,
  isCurrent?: () => boolean,
): Promise<RunDetail | null> {
  if (!silent) {
    detailLoading.value = true;
    detailError.value = "";
  }
  try {
    const response = await apiRequest<RunDetail>(`/runs/${runId}`);
    if (isCurrent && !isCurrent()) return null;
    detail.value = response;
    if (silent && drawerOpen.value) {
      const index = runs.value.findIndex((run) => run.id === runId);
      if (index >= 0) runs.value[index] = response;
    }
    return response;
  } catch (cause) {
    if (await handleUnauthorized(cause)) return null;
    if (!silent && (!isCurrent || isCurrent())) {
      detailError.value = cause instanceof Error ? cause.message : "加载运行详情失败";
    }
    return null;
  } finally {
    if (!silent && (!isCurrent || isCurrent())) detailLoading.value = false;
  }
}

function openLaneConsole(): void {
  const run = detail.value;
  const job = activeJob.value;
  if (!run || !job) return;
  void router.push({
    name: "run-lane",
    params: { runId: String(run.id), laneKey: job.lane },
    query: { job: String(job.id) },
  });
}

function pickJobId(run: RunDetail): number | null {
  const running = run.jobs.find((job) => job.status === "running");
  if (running) return running.id;
  const queued = run.jobs.find((job) => job.status === "queued");
  if (queued) return queued.id;
  return run.jobs[0]?.id ?? null;
}

function closeDetailStream(): void {
  if (detailStream !== null) {
    detailStream.close();
    detailStream = null;
  }
}

function applyDetailUpdate(incoming: RunDetail): void {
  detail.value = incoming;
  const index = runs.value.findIndex((run) => run.id === incoming.id);
  if (index >= 0) runs.value[index] = incoming;
  if (
    activeJobId.value === null ||
    !incoming.jobs.some((job) => job.id === activeJobId.value)
  ) {
    activeJobId.value = pickJobId(incoming);
  }
  if (FINAL_RUN_STATUSES.has(incoming.status)) {
    closeDetailStream();
    detailStreamState.value = "idle";
  }
}

function connectDetailStream(runId: number): void {
  closeDetailStream();
  detailStreamState.value = "connecting";
  const source = new EventSource(`/api/v1/runs/${runId}/events`, {
    withCredentials: true,
  });
  detailStream = source;
  source.onopen = () => {
    if (source === detailStream) detailStreamState.value = "live";
  };
  source.addEventListener("update", (event) => {
    if (source !== detailStream) return;
    try {
      const incoming = JSON.parse((event as MessageEvent).data) as RunDetail;
      if (incoming.id !== runId) return;
      applyDetailUpdate(incoming);
    } catch {
      // Ignore malformed frames and wait for the next snapshot.
    }
  });
  source.addEventListener("gone", () => {
    if (source !== detailStream) return;
    closeDetailStream();
    detailStreamState.value = "idle";
  });
  source.onerror = () => {
    if (source === detailStream) detailStreamState.value = "error";
  };
}

async function revealJobTab(jobId: number): Promise<void> {
  await nextTick();
  const element = document.querySelector<HTMLElement>(
    `.job-tabs [data-job-id="${jobId}"]`,
  );
  element?.scrollIntoView({ block: "nearest", inline: "center" });
}

async function openDetail(
  runId: number,
  target: { jobId?: number; siteId?: number } = {},
): Promise<void> {
  const seq = ++detailOpenSeq;
  closeDetailStream();
  detailStreamState.value = "idle";
  drawerOpen.value = true;
  detail.value = null;
  activeJobId.value = null;
  jobView.value = "status";
  jobLogText.value = "";
  jobLogError.value = "";
  const loaded = await loadDetail(
    runId,
    false,
    () => seq === detailOpenSeq && drawerOpen.value,
  );
  if (seq !== detailOpenSeq || !drawerOpen.value) return;
  if (loaded && !detailError.value) {
    const targetJob =
      (target.jobId !== undefined
        ? loaded.jobs.find((job) => job.id === target.jobId)
        : undefined) ??
      (target.siteId !== undefined
        ? loaded.jobs.find((job) => job.site_id === target.siteId)
        : undefined);
    activeJobId.value = targetJob?.id ?? pickJobId(loaded);
    if (targetJob) {
      void revealJobTab(targetJob.id);
    }
    if (!FINAL_RUN_STATUSES.has(loaded.status)) {
      connectDetailStream(runId);
    }
  }
}

function handleDrawerClosed(): void {
  detailOpenSeq += 1;
  logRequestSeq += 1;
  closeDetailStream();
  detailStreamState.value = "idle";
  detail.value = null;
  detailError.value = "";
  activeJobId.value = null;
  jobView.value = "status";
  jobLogText.value = "";
  jobLogError.value = "";
  jobLogLoading.value = false;
  siteDetailOpen.value = false;
  siteDetail.value = null;
  siteDetailError.value = "";
}

async function createRun(
  siteIds: number[] | null,
  options: { todayUnsuccessful?: boolean } = {},
): Promise<void> {
  if (siteIds !== null && siteIds.length === 0) {
    ElMessage.warning("请选择至少一个站点");
    return;
  }
  creating.value = true;
  try {
    const body =
      siteIds === null
        ? options.todayUnsuccessful
          ? { today_unsuccessful: true }
          : {}
        : { site_ids: siteIds };
    const run = await apiRequest<RunDetail>("/runs", {
      method: "POST",
      body: JSON.stringify(body),
    });
    ElMessage.success(`已创建运行 #${run.id}`);
    selectedSiteIds.value = [];
    await loadRuns(true);
    await openDetail(run.id);
  } catch (cause) {
    if (await handleUnauthorized(cause)) return;
    ElMessage.error(cause instanceof Error ? cause.message : "创建运行失败");
  } finally {
    creating.value = false;
  }
}

async function openSiteDetail(): Promise<void> {
  const job = activeJob.value;
  if (!job?.site_id) return;
  siteDetailOpen.value = true;
  siteDetailLoading.value = true;
  siteDetailError.value = "";
  siteDetail.value = null;
  try {
    siteDetail.value = await apiRequest<SiteDetail>(`/sites/${job.site_id}`);
  } catch (cause) {
    if (await handleUnauthorized(cause)) return;
    siteDetailError.value =
      cause instanceof Error ? cause.message : "加载站点信息失败";
  } finally {
    siteDetailLoading.value = false;
  }
}

function openSiteInNewTab(): void {
  if (!siteDetail.value?.base_url) return;
  window.open(siteDetail.value.base_url, "_blank", "noopener,noreferrer");
}

async function retriggerSite(): Promise<void> {
  const job = activeJob.value;
  if (!job?.site_id || retriggering.value) return;
  retriggering.value = true;
  try {
    const run = await apiRequest<RunDetail>("/runs", {
      method: "POST",
      body: JSON.stringify({ site_ids: [job.site_id] }),
    });
    siteDetailOpen.value = false;
    ElMessage.success(`已重新触发站点「${job.site_name}」`);
    await loadRuns(true);
    await openDetail(run.id);
  } catch (cause) {
    if (await handleUnauthorized(cause)) return;
    ElMessage.error(cause instanceof Error ? cause.message : "重新触发任务失败");
  } finally {
    retriggering.value = false;
  }
}

function runSelected(): void {
  void createRun(selectedSiteIds.value);
}

function runAll(): void {
  void createRun(null);
}

function runTodayUnsuccessful(): void {
  void createRun(null, { todayUnsuccessful: true });
}

async function cancelRun(run: RunSummary | RunDetail): Promise<void> {
  if (run.status !== "queued" && run.status !== "running") return;
  try {
    await ElMessageBox.confirm(`确认取消运行 #${run.id}？`, "取消运行", {
      confirmButtonText: "取消运行",
      cancelButtonText: "返回",
      type: "warning",
    });
  } catch {
    return;
  }
  actionRunId.value = run.id;
  try {
    const response = await apiRequest<RunDetail>(`/runs/${run.id}/cancel`, {
      method: "POST",
    });
    ElMessage.success("已请求取消运行");
    if (detail.value?.id === run.id) detail.value = response;
    await loadRuns(true);
  } catch (cause) {
    if (await handleUnauthorized(cause)) return;
    ElMessage.error(cause instanceof Error ? cause.message : "取消运行失败");
  } finally {
    actionRunId.value = null;
  }
}

function stopRefreshTimer(): void {
  if (refreshTimer !== null) {
    clearInterval(refreshTimer);
    refreshTimer = null;
  }
}

function startRefreshTimer(): void {
  stopRefreshTimer();
  if (route.name !== "runs" || document.visibilityState !== "visible") return;
  refreshTimer = setInterval(() => {
    if (route.name !== "runs" || document.visibilityState !== "visible") {
      stopRefreshTimer();
      return;
    }
    void loadRuns(true);
    void loadSchedulerInfo();
    if (
      drawerOpen.value &&
      detail.value &&
      !FINAL_RUN_STATUSES.has(detail.value.status) &&
      detailStreamState.value !== "live" &&
      detailStreamState.value !== "connecting"
    ) {
      void loadDetail(detail.value.id, true);
    }
  }, 5000);
}

function handleVisibilityChange(): void {
  if (route.name !== "runs") {
    stopRefreshTimer();
    return;
  }
  if (document.visibilityState === "visible") {
    void loadRuns(true);
    void loadSchedulerInfo();
    startRefreshTimer();
  } else {
    stopRefreshTimer();
  }
}

onMounted(async () => {
  await loadSchedulerInfo();
  await loadRuns();
  const runId = Number(route.query.run);
  if (Number.isInteger(runId) && runId > 0) {
    const jobId = Number(route.query.job);
    const siteId = Number(route.query.site);
    await openDetail(runId, {
      jobId: Number.isInteger(jobId) && jobId > 0 ? jobId : undefined,
      siteId: Number.isInteger(siteId) && siteId > 0 ? siteId : undefined,
    });
  }
  document.addEventListener("visibilitychange", handleVisibilityChange);
  startRefreshTimer();
});

onBeforeRouteLeave(() => {
  stopRefreshTimer();
});

onUnmounted(() => {
  stopRefreshTimer();
  document.removeEventListener("visibilitychange", handleVisibilityChange);
  closeDetailStream();
});
</script>

<template>
  <section class="page-head">
    <div>
      <p class="eyebrow">SCHEDULE</p>
      <h1>任务调度</h1>
      <p class="muted">手动执行站点任务，查看每次运行产生的任务和步骤结果。</p>
    </div>
    <div class="head-actions">
      <el-tag v-if="hasActiveRuns" type="warning" effect="plain">有运行中的任务</el-tag>
      <el-button @click="loadRuns()">刷新</el-button>
    </div>
  </section>

  <el-alert
    v-if="schedulerInfo"
    :type="schedulerInfo.enabled ? 'info' : 'warning'"
    :closable="false"
    show-icon
    class="notice"
  >
    <template #title>
      <span v-if="!schedulerInfo.enabled">
        未开启自动调度，请在 系统设置->调度设置 中启用
      </span>
      <span v-else-if="schedulerInfo.next_run_at">
        下次自动运行：{{ formatDate(schedulerInfo.next_run_at) }}（{{ schedulerInfo.timezone }}）
      </span>
      <span v-else>暂无下次自动运行时间</span>
    </template>
  </el-alert>

  <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon class="notice" />

  <el-card shadow="never" class="panel">
    <div class="run-trigger">
      <el-select
        v-model="selectedSiteIds"
        multiple
        collapse-tags
        collapse-tags-tooltip
        filterable
        clearable
        placeholder="选择要执行的站点；不选则执行全部启用站点"
        class="site-select"
      >
        <el-option
          v-for="site in sites"
          :key="site.id"
          :label="site.name"
          :value="site.id"
          :disabled="!site.enabled"
        >
          <span>{{ site.name }}</span>
          <span class="option-meta">
            {{ laneLabel(site.lane) }}
            <template v-if="!site.enabled"> · 未启用</template>
          </span>
        </el-option>
      </el-select>
      <el-button
        type="primary"
        :loading="creating"
        :disabled="selectedSiteIds.length === 0"
        @click="runSelected"
      >
        执行选中站点
      </el-button>
      <el-button :loading="creating" @click="runTodayUnsuccessful">
        执行今日未成功的站点
      </el-button>
      <el-button :loading="creating" @click="runAll">执行全部启用站点</el-button>
    </div>
    <p class="muted trigger-hint">
      当前可执行站点 {{ enabledSites.length }} 个；「今日未成功」按调度时区 {{ displayTimezone }}
      判断，并跳过正在排队或运行中的站点；每 5 秒自动刷新运行状态。
    </p>
  </el-card>

  <el-card shadow="never" class="panel" v-loading="loading">
    <el-table :data="runs" row-key="id">
      <el-table-column label="#" width="80">
        <template #default="{ row }"><span class="mono">#{{ row.id }}</span></template>
      </el-table-column>
      <el-table-column label="站点" min-width="220" show-overflow-tooltip>
        <template #default="{ row }">
          {{ row.site_names.length ? row.site_names.join("、") : "-" }}
        </template>
      </el-table-column>
      <el-table-column label="状态" width="110">
        <template #default="{ row }">
          <el-tag :type="statusType(row.status)" size="small" effect="plain">
            {{ statusLabel(row.status) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="创建时间" min-width="170">
        <template #default="{ row }">{{ formatDate(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="结果汇总" min-width="220">
        <template #default="{ row }">{{ summaryText(row.summary) }}</template>
      </el-table-column>
      <el-table-column prop="created_by" label="创建人" width="100" />
      <el-table-column label="操作" width="150" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" @click="openDetail(row.id)">详情</el-button>
          <el-button
            v-if="row.status === 'queued' || row.status === 'running'"
            link
            type="danger"
            :loading="actionRunId === row.id"
            @click="cancelRun(row)"
          >
            取消
          </el-button>
        </template>
      </el-table-column>
    </el-table>
    <p v-if="!runs.length && !loading" class="muted empty">还没有运行记录。</p>
  </el-card>

  <el-drawer
    v-model="drawerOpen"
    :title="detail ? `运行 #${detail.id}` : '运行详情'"
    size="min(900px, 96vw)"
    class="run-drawer"
    @closed="handleDrawerClosed"
  >
    <div v-loading="detailLoading" class="detail-shell">
      <el-alert v-if="detailError" :title="detailError" type="error" :closable="false" show-icon />
      <template v-if="detail">
        <div class="detail-fixed">
          <div class="detail-meta">
            <el-tag :type="statusType(detail.status)" effect="plain">{{ statusLabel(detail.status) }}</el-tag>
            <el-tag
              v-if="detailStreamState === 'live'"
              type="success"
              effect="plain"
              size="small"
            >
              实时更新
            </el-tag>
            <el-tag
              v-else-if="detailStreamState === 'connecting'"
              type="info"
              effect="plain"
              size="small"
            >
              连接中
            </el-tag>
            <el-tag
              v-else-if="detailStreamState === 'error'"
              type="warning"
              effect="plain"
              size="small"
            >
              实时连接中断，重连中
            </el-tag>
            <span class="muted">{{ detail.site_count }} 个站点 · {{ summaryText(detail.summary) }}</span>
            <span class="muted">创建于 {{ formatDate(detail.created_at) }}</span>
            <span v-if="detail.started_at" class="muted">开始于 {{ formatDate(detail.started_at) }}</span>
            <span v-if="detail.finished_at" class="muted">结束于 {{ formatDate(detail.finished_at) }}</span>
            <el-button
              v-if="detail.status === 'queued' || detail.status === 'running'"
              size="small"
              type="danger"
              plain
              :loading="actionRunId === detail.id"
              @click="cancelRun(detail)"
            >
              取消运行
            </el-button>
          </div>
          <el-alert
            v-if="detail.cancel_requested && detail.status === 'running'"
            title="已请求取消，当前步骤结束后停止"
            type="warning"
            :closable="false"
            show-icon
            class="notice"
          />

          <div v-if="detail.jobs.length > 1" class="job-tabs" role="tablist">
            <button
              v-for="job in detail.jobs"
              :key="job.id"
              type="button"
              role="tab"
              class="job-tab"
              :class="{ active: activeJob?.id === job.id }"
              :aria-selected="activeJob?.id === job.id"
              :data-job-id="job.id"
              @click="activeJobId = job.id"
            >
              <span class="job-tab-name">{{ job.site_name }}</span>
              <el-tag :type="statusType(job.status)" size="small" effect="plain">
                {{ statusLabel(job.status) }}
              </el-tag>
            </button>
          </div>

          <div v-if="activeJob" class="job-head">
            <div>
              <button
                v-if="activeJob.site_id !== null"
                type="button"
                class="site-link"
                title="站点详情"
                @click="openSiteDetail"
              >
                {{ activeJob.site_name }}
              </button>
              <strong v-else>{{ activeJob.site_name }}</strong>
              <span class="muted"> · </span>
              <button
                type="button"
                class="lane-link"
                title="查看车道画面"
                @click="openLaneConsole"
              >
                {{ laneLabel(activeJob.lane) }}
              </button>
            </div>
            <div class="job-head-actions">
              <el-button
                v-if="failureScreenshots.length"
                size="small"
                type="danger"
                plain
                @click="openFailurePreview"
              >
                查看截图
              </el-button>
              <el-radio-group v-model="jobView" size="small">
                <el-radio-button value="status">状态日志</el-radio-button>
                <el-radio-button value="log">详细日志</el-radio-button>
              </el-radio-group>
            </div>
          </div>
          <p v-if="activeJob?.message" class="job-message">{{ activeJob.message }}</p>
          <p v-if="activeJob" class="muted mono job-time">
            {{ formatDate(activeJob.started_at) }} → {{ formatDate(activeJob.finished_at) }}
          </p>
        </div>

        <div v-if="activeJob" class="detail-scroll">
          <template v-if="jobView === 'status'">
            <div v-if="activeJob.steps.length" class="step-list">
              <div v-for="step in activeJob.steps" :key="step.step_key" class="step-row">
                <el-tag :type="statusType(step.status)" size="small" effect="plain">
                  {{ statusLabel(step.status) }}
                </el-tag>
                <div class="step-body">
                  <strong>{{ step.label || step.step_key }}</strong>
                  <span class="muted mono">{{ step.step_key }} · {{ step.feature_type }}</span>
                  <p v-if="step.message" class="step-message">{{ step.message }}</p>
                  <p v-if="step.data.url" class="muted mono step-url">{{ step.data.url }}</p>
                  <p v-if="step.data.value !== undefined" class="step-output">
                    输出值：{{ step.data.value }}
                  </p>
                </div>
              </div>
            </div>
            <p v-else class="muted">尚无步骤结果。</p>
          </template>
          <template v-else>
            <el-alert
              v-if="jobLogError"
              :title="jobLogError"
              type="error"
              :closable="false"
              show-icon
            />
            <div v-else-if="jobLogLoading && !jobLogText" class="muted">日志加载中…</div>
            <pre v-else-if="jobLogText" ref="jobLogRef" class="detail-log">{{ jobLogText }}</pre>
            <p v-else class="muted">暂无详细日志。</p>
          </template>
        </div>
        <p v-else class="muted">该运行没有站点任务。</p>
      </template>
    </div>
  </el-drawer>

  <el-dialog
    v-model="failurePreviewOpen"
    title="截图"
    width="min(1000px, 96vw)"
    @closed="failurePreviewIndex = 0"
  >
    <div v-if="currentFailureScreenshot" class="failure-preview">
      <div v-if="failureScreenshots.length > 1" class="failure-preview-nav">
        <el-button size="small" @click="stepFailurePreview(-1)">上一张</el-button>
        <span class="muted">
          {{ failurePreviewIndex + 1 }} / {{ failureScreenshots.length }}
        </span>
        <el-button size="small" @click="stepFailurePreview(1)">下一张</el-button>
      </div>
      <img
        :src="artifactUrl(currentFailureScreenshot.id)"
        :alt="currentFailureScreenshot.label"
        class="failure-preview-image"
      />
      <p class="muted failure-preview-meta">
        {{ currentFailureScreenshot.label }}
        <template v-if="currentFailureScreenshot.step_key">
          · {{ currentFailureScreenshot.step_key }}
        </template>
        · {{ formatDate(currentFailureScreenshot.created_at) }}
      </p>
    </div>
    <template #footer>
      <el-button @click="failurePreviewOpen = false">关闭</el-button>
      <el-button type="primary" @click="downloadFailureScreenshot">下载</el-button>
    </template>
  </el-dialog>

  <el-dialog
    v-model="siteDetailOpen"
    :title="siteDetail?.name ?? activeJob?.site_name ?? '站点详情'"
    width="min(560px, 94vw)"
    @closed="siteDetail = null"
  >
    <div v-loading="siteDetailLoading" class="site-detail-body">
      <el-alert
        v-if="siteDetailError"
        :title="siteDetailError"
        type="error"
        :closable="false"
        show-icon
      />
      <dl v-else-if="siteDetail" class="site-detail-list">
        <div>
          <dt>网址</dt>
          <dd>
            <a :href="siteDetail.base_url" target="_blank" rel="noopener noreferrer">
              {{ siteDetail.base_url }}
            </a>
          </dd>
        </div>
        <div>
          <dt>模板</dt>
          <dd>{{ siteDetail.template_name ?? "无模板" }}</dd>
        </div>
      </dl>
    </div>
    <template #footer>
      <el-button @click="siteDetailOpen = false">取消</el-button>
      <el-button :disabled="!siteDetail" @click="openSiteInNewTab">
        打开站点
      </el-button>
      <el-button
        type="primary"
        :loading="retriggering"
        :disabled="!siteDetail || !activeJob?.site_id"
        @click="retriggerSite"
      >
        重新触发任务
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.page-head {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 18px;
  margin-bottom: 20px;
}

.eyebrow {
  margin: 0 0 6px;
  color: #409eff;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.16em;
}

.page-head h1 {
  margin: 0 0 6px;
  font-size: 26px;
}

.page-head p {
  margin: 0;
}

.head-actions {
  display: flex;
  align-items: center;
  gap: 10px;
}

.panel {
  border-radius: 16px;
  border-color: #e4e9f0;
  margin-bottom: 16px;
}

.notice {
  margin-bottom: 14px;
}

.card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.card-head > div {
  display: flex;
  align-items: baseline;
  gap: 8px;
}

.run-trigger {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.site-select {
  min-width: 280px;
  flex: 1;
  max-width: 620px;
}

.option-meta {
  float: right;
  margin-left: 16px;
  color: #8a96a3;
  font-size: 12px;
}

.trigger-hint {
  margin: 10px 0 0;
  font-size: 12px;
}

.empty {
  margin: 14px 0 0;
}

.detail-meta {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 14px;
}

.job-tabs {
  display: flex;
  gap: 8px;
  overflow-x: auto;
  padding: 2px 2px 8px;
  margin-bottom: 10px;
  border-bottom: 1px solid #eef1f6;
}

.job-tab {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  flex: 0 0 auto;
  padding: 7px 10px;
  border: 1px solid #dfe6ef;
  border-radius: 9px;
  background: #fff;
  color: #606266;
  cursor: pointer;
  font-size: 13px;
  transition: border-color 0.15s, background 0.15s, color 0.15s;
}

.job-tab:hover {
  border-color: #b9d7f5;
  background: #f7fbff;
}

.job-tab.active {
  border-color: #409eff;
  background: #eef7ff;
  color: #2f7ed8;
}

.job-tab-name {
  max-width: 180px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

:global(.run-drawer .el-drawer__body) {
  display: flex;
  flex-direction: column;
  overflow: hidden;
  padding: 0;
}

.detail-shell {
  display: flex;
  flex: 1;
  flex-direction: column;
  min-height: 0;
  padding: 16px 20px 18px;
}

.detail-fixed {
  flex: none;
}

.detail-scroll {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding-right: 4px;
}

.detail-log {
  min-height: 100%;
  margin: 0;
  padding: 12px;
  border: 1px solid #e7edf5;
  border-radius: 8px;
  background: #f8fafc;
  color: #303133;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px;
  line-height: 1.55;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.job-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  flex-wrap: wrap;
}

.site-link {
  padding: 0;
  border: none;
  background: none;
  color: #303133;
  cursor: pointer;
  font: inherit;
  font-weight: 600;
}

.site-link:hover {
  color: #409eff;
  text-decoration: underline;
  text-underline-offset: 2px;
}

.site-link:disabled {
  color: #a8b0b9;
  cursor: not-allowed;
  text-decoration: none;
}

.lane-link {
  padding: 0;
  border: none;
  background: none;
  color: #409eff;
  cursor: pointer;
  font-size: inherit;
  text-decoration: underline;
  text-underline-offset: 2px;
}

.job-head-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.site-detail-body {
  min-height: 92px;
}

.site-detail-list {
  display: flex;
  flex-direction: column;
  gap: 14px;
  margin: 0;
}

.site-detail-list > div {
  display: grid;
  grid-template-columns: 56px minmax(0, 1fr);
  gap: 12px;
  align-items: start;
}

.site-detail-list dt {
  color: #7a8794;
  font-size: 13px;
}

.site-detail-list dd {
  margin: 0;
  color: #303133;
  font-size: 13px;
  overflow-wrap: anywhere;
}

.site-detail-list a {
  color: #409eff;
  text-decoration: none;
}

.site-detail-list a:hover {
  text-decoration: underline;
}

.failure-preview {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.failure-preview-nav {
  display: flex;
  align-items: center;
  gap: 10px;
}

.failure-preview-image {
  display: block;
  max-width: 100%;
  max-height: 66vh;
  margin: 0 auto;
  border: 1px solid #e7edf5;
  border-radius: 8px;
}

.failure-preview-meta {
  margin: 0;
  font-size: 12px;
  text-align: center;
}

.job-message {
  margin: 8px 0 0;
  color: #b88230;
  font-size: 13px;
}

.job-time {
  margin: 4px 0 8px;
  font-size: 11px;
}

.step-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.step-row {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 8px 10px;
  border-radius: 8px;
  background: #fff;
}

.step-body {
  display: flex;
  flex-direction: column;
  gap: 3px;
  min-width: 0;
}

.step-body .mono {
  font-size: 11px;
}

.step-message {
  margin: 2px 0 0;
  color: #606266;
  font-size: 12px;
}

.step-url {
  margin: 2px 0 0;
  font-size: 11px;
  overflow-wrap: anywhere;
}

.step-output {
  margin: 2px 0 0;
  color: #2f7ed8;
  font-size: 12px;
}

@media (max-width: 760px) {
  .page-head {
    align-items: flex-start;
    flex-direction: column;
  }

  .site-select {
    min-width: 0;
    width: 100%;
  }
}
</style>
