<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ElMessage } from "element-plus";

import { ApiError, apiRequest } from "@/api/client";
import VncViewer from "@/components/VncViewer.vue";
import { useAuthStore } from "@/stores/auth";
import type { RunDetail } from "@/types/workflow";
import { formatServerDateTime } from "@/utils/datetime";
import { laneLabel } from "@/utils/workflow";

const route = useRoute();
const router = useRouter();
const auth = useAuthStore();
const runId = Number(route.params.runId);
const laneKey = String(route.params.laneKey || "");
const requestedJobId = Number(route.query.job);
const detail = ref<RunDetail | null>(null);
const loading = ref(true);
const error = ref("");
const streamState = ref<"idle" | "connecting" | "live" | "error">("idle");
const vncOpen = ref(true);
const focusing = ref(false);
const displayTimezone = ref("Asia/Shanghai");
let stream: EventSource | null = null;
let focusTried = false;

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
  if (
    status === "waiting_human" ||
    status === "unknown" ||
    status === "interrupted" ||
    status === "fail" ||
    status === "error"
  ) {
    return "danger";
  }
  return "info";
}

function formatDate(value: string | null): string {
  return formatServerDateTime(value, displayTimezone.value);
}

async function loadTimezone(): Promise<void> {
  try {
    const settings = await apiRequest<{ timezone: string }>("/scheduler-settings");
    displayTimezone.value = settings.timezone || "Asia/Shanghai";
  } catch {
    // Keep the default display timezone.
  }
}

async function handleUnauthorized(cause: unknown): Promise<boolean> {
  if (cause instanceof ApiError && cause.status === 401) {
    auth.clear();
    await router.replace({ name: "login" });
    return true;
  }
  return false;
}

const activeJob = computed(() => {
  if (!detail.value) return null;
  return (
    detail.value.jobs.find((job) => job.id === requestedJobId) ??
    detail.value.jobs.find((job) => job.lane === laneKey) ??
    detail.value.jobs[0] ??
    null
  );
});

const jobRunning = computed(() => activeJob.value?.status === "running");

function applyDetail(incoming: RunDetail): void {
  detail.value = incoming;
  if (new Set(["success", "fail", "error", "unknown", "waiting_human", "canceled", "interrupted"]).has(incoming.status)) {
    closeStream();
  }
}

function closeStream(): void {
  if (stream !== null) {
    stream.close();
    stream = null;
  }
  if (streamState.value === "live" || streamState.value === "connecting") {
    streamState.value = "idle";
  }
}

function connectStream(): void {
  closeStream();
  streamState.value = "connecting";
  const source = new EventSource(`/api/v1/runs/${runId}/events`, {
    withCredentials: true,
  });
  stream = source;
  source.onopen = () => {
    if (source === stream) streamState.value = "live";
  };
  source.addEventListener("update", (event) => {
    if (source !== stream) return;
    try {
      const incoming = JSON.parse((event as MessageEvent).data) as RunDetail;
      if (incoming.id === runId) applyDetail(incoming);
    } catch {
      // Ignore malformed frames.
    }
  });
  source.addEventListener("gone", () => {
    if (source === stream) closeStream();
  });
  source.onerror = () => {
    if (source === stream) streamState.value = "error";
  };
}

async function loadDetail(): Promise<void> {
  loading.value = true;
  error.value = "";
  try {
    const response = await apiRequest<RunDetail>(`/runs/${runId}`);
    applyDetail(response);
  } catch (cause) {
    if (await handleUnauthorized(cause)) return;
    error.value = cause instanceof Error ? cause.message : "加载运行详情失败";
  } finally {
    loading.value = false;
  }
}

async function focusTaskPage(showMessage = true): Promise<void> {
  const job = activeJob.value;
  if (!job || job.status !== "running") {
    if (showMessage) ElMessage.warning("任务当前未运行，标签页可能已关闭");
    return;
  }
  focusing.value = true;
  try {
    await apiRequest(`/lanes/${encodeURIComponent(laneKey)}/focus`, {
      method: "POST",
      body: JSON.stringify({ job_id: String(job.id) }),
    });
    if (showMessage) ElMessage.success("已定位任务标签页");
  } catch (cause) {
    if (await handleUnauthorized(cause)) return;
    if (showMessage) {
      ElMessage.warning(cause instanceof Error ? cause.message : "定位任务标签页失败");
    }
  } finally {
    focusing.value = false;
  }
}

async function autoFocus(): Promise<void> {
  if (focusTried) return;
  focusTried = true;
  for (let attempt = 0; attempt < 15; attempt += 1) {
    if (!jobRunning.value) return;
    const job = activeJob.value;
    if (!job) return;
    try {
      await apiRequest(`/lanes/${encodeURIComponent(laneKey)}/focus`, {
        method: "POST",
        body: JSON.stringify({ job_id: String(job.id) }),
      });
      return;
    } catch {
      await new Promise((resolve) => setTimeout(resolve, 800));
    }
  }
}

watch(
  () => [activeJob.value?.id, activeJob.value?.status] as const,
  ([jobId, status], [oldJobId, oldStatus]) => {
    if (jobId !== oldJobId) {
      focusTried = false;
      return;
    }
    if (status === "running" && oldStatus !== "running") {
      void autoFocus();
    }
  },
);

function backToRun(): void {
  void router.push({ name: "runs", query: { run: String(runId) } });
}

onMounted(async () => {
  void loadTimezone();
  await loadDetail();
  if (detail.value && !error.value) {
    connectStream();
    void autoFocus();
  }
});

onUnmounted(closeStream);
</script>

<template>
  <section>
    <div class="page-head">
      <div>
        <el-button link @click="backToRun">← 返回运行详情</el-button>
        <h1>任务对照</h1>
        <p class="muted">
          运行 #{{ runId }}
          <template v-if="activeJob"> · {{ activeJob.site_name }}</template>
          · {{ laneLabel(laneKey) }}
        </p>
      </div>
      <div class="head-actions">
        <el-tag
          v-if="streamState === 'live'"
          type="success"
          effect="plain"
          size="small"
        >
          实时更新
        </el-tag>
        <el-tag
          v-else-if="streamState === 'error'"
          type="warning"
          effect="plain"
          size="small"
        >
          连接中断
        </el-tag>
        <el-tag
          v-if="activeJob"
          :type="statusType(activeJob.status)"
          effect="plain"
        >
          {{ statusLabel(activeJob.status) }}
        </el-tag>
        <el-button
          :loading="focusing"
          :disabled="!jobRunning"
          @click="focusTaskPage(true)"
        >
          定位任务标签页
        </el-button>
        <el-button @click="backToRun">返回</el-button>
      </div>
    </div>

    <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon class="notice" />

    <div class="console-grid" v-loading="loading">
      <el-card shadow="never" class="status-pane">
        <template #header>
          <div class="pane-head">
            <div>
              <strong>{{ activeJob?.site_name || "站点任务" }}</strong>
              <span class="muted"> · {{ laneLabel(laneKey) }}</span>
            </div>
            <el-tag
              v-if="activeJob"
              :type="statusType(activeJob.status)"
              size="small"
              effect="plain"
            >
              {{ statusLabel(activeJob.status) }}
            </el-tag>
          </div>
          <p v-if="activeJob" class="muted pane-time">
            {{ formatDate(activeJob.started_at) }} → {{ formatDate(activeJob.finished_at) }}
          </p>
          <p v-if="activeJob?.message" class="pane-message">{{ activeJob.message }}</p>
        </template>
        <div class="status-scroll">
          <template v-if="activeJob?.steps.length">
            <div v-for="step in activeJob.steps" :key="step.step_key" class="step-item">
              <el-tag :type="statusType(step.status)" size="small" effect="plain">
                {{ statusLabel(step.status) }}
              </el-tag>
              <div class="step-main">
                <strong>{{ step.label || step.step_key }}</strong>
                <span class="muted mono step-key">
                  {{ step.step_key }} · {{ step.feature_type }}
                </span>
                <p v-if="step.message" class="step-message">{{ step.message }}</p>
                <p v-if="step.data.url" class="muted mono step-url">{{ step.data.url }}</p>
              </div>
            </div>
          </template>
          <p v-else class="muted">尚无步骤结果。</p>
        </div>
      </el-card>

      <el-card shadow="never" class="vnc-pane">
        <VncViewer
          v-model="vncOpen"
          :lane-key="laneKey"
          :lane-label="laneLabel(laneKey)"
          embedded
        />
      </el-card>
    </div>
  </section>
</template>

<style scoped>
.page-head {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 18px;
  margin-bottom: 18px;
}

.page-head h1 {
  margin: 4px 0 6px;
  font-size: 26px;
}

.page-head p {
  margin: 0;
}

.head-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.notice {
  margin-bottom: 14px;
}

.console-grid {
  display: grid;
  grid-template-columns: minmax(300px, 1fr) minmax(0, 2fr);
  gap: 14px;
  height: calc(100vh - 178px);
  min-height: 540px;
}

.status-pane,
.vnc-pane {
  display: flex;
  flex-direction: column;
  border-radius: 14px;
  border-color: #e4e9f0;
  min-height: 0;
}

.status-pane :deep(.el-card__body) {
  display: flex;
  flex: 1;
  flex-direction: column;
  min-height: 0;
  padding: 12px;
}

.vnc-pane :deep(.el-card__body) {
  flex: 1;
  min-height: 0;
  padding: 12px;
}

.pane-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.pane-time {
  margin: 4px 0 0;
  font-size: 11px;
}

.pane-message {
  margin: 6px 0 0;
  color: #b88230;
  font-size: 12px;
}

.status-scroll {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.step-item {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 8px;
  border: 1px solid #edf1f6;
  border-radius: 8px;
  background: #fbfcfe;
}

.step-main {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.step-key,
.step-url {
  font-size: 11px;
  overflow-wrap: anywhere;
}

.step-message {
  margin: 2px 0 0;
  color: #606266;
  font-size: 12px;
  overflow-wrap: anywhere;
}

@media (max-width: 1100px) {
  .console-grid {
    grid-template-columns: 1fr;
    height: auto;
    min-height: 0;
  }

  .status-pane {
    height: 42vh;
  }

  .vnc-pane {
    height: 68vh;
  }
}
</style>
