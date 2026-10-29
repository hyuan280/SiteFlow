<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue";
import { useRouter } from "vue-router";
import { ElMessage } from "element-plus";

import { ApiError, apiRequest } from "@/api/client";
import VncViewer from "@/components/VncViewer.vue";
import { useAuthStore } from "@/stores/auth";

interface LaneStatus {
  key: string;
  label: string;
  enabled: boolean;
  status: string;
  browser: string;
  display: string | null;
  profile: string | null;
  vnc_available: boolean;
  in_use: number;
  max_concurrency: number;
  viewers: number;
  idle_stop_seconds: number;
  idle_remaining: number | null;
  can_start: boolean;
  can_stop: boolean;
  error: string | null;
  started_at: string | null;
}

interface LaneSettingValue {
  value: number;
  source: "env" | "db" | "default";
  editable: boolean;
}

interface LaneSettings {
  idle_stop_seconds: LaneSettingValue;
  general_concurrency: LaneSettingValue;
  proxy_concurrency: LaneSettingValue;
}

interface LanesResponse {
  worker_connected: boolean;
  lanes: LaneStatus[];
  error: string | null;
}

const auth = useAuthStore();
const router = useRouter();
const lanes = ref<LaneStatus[]>([]);
const lanesError = ref("");
const lanesLoading = ref(true);
const laneSettings = ref<LaneSettings | null>(null);
const idleSeconds = ref(300);
const generalConcurrency = ref(5);
const proxyConcurrency = ref(3);
const laneSettingsSaving = ref(false);
const laneActionKey = ref("");
const vncOpen = ref(false);
const activeLane = ref<LaneStatus | null>(null);
let lanesTimer: ReturnType<typeof setInterval> | null = null;
let lanesPolling = false;

function laneStatusLabel(lane: LaneStatus): string {
  if (lane.status === "running") return "运行中";
  if (lane.status === "stopped") return "已停止";
  if (lane.status === "starting") return "启动中";
  if (lane.status === "stopping") return "关闭中";
  if (lane.status === "disabled") return "未启用";
  if (lane.status === "error") return "异常";
  if (lane.status === "unavailable") return "不可用";
  return lane.status;
}

function laneMode(lane: LaneStatus): "并行" | "串行" {
  return lane.key === "interaction" || lane.key === "proxy_interaction" ? "串行" : "并行";
}

function laneSummary(lane: LaneStatus): string {
  const parts = [`${laneMode(lane)} - ${laneStatusLabel(lane)}`];
  if (lane.status === "running") {
    if (lane.display) parts.push(`显示号:${lane.display.replace(/^:/, "")}`);
    parts.push(`${lane.in_use} 正在使用`);
    parts.push(`并发 ${lane.max_concurrency}`);
  }
  if (lane.viewers) parts.push(`VNC 查看中 ${lane.viewers}`);
  return parts.join(" · ");
}

async function handleUnauthorized(error: unknown): Promise<boolean> {
  if (error instanceof ApiError && error.status === 401) {
    auth.clear();
    await router.replace({ name: "login" });
    return true;
  }
  return false;
}

async function loadLanes(silent = false): Promise<void> {
  if (lanesPolling) return;
  lanesPolling = true;
  if (!silent) lanesLoading.value = true;
  try {
    const response = await apiRequest<LanesResponse>("/lanes");
    lanes.value = response.lanes;
    lanesError.value = response.error ?? (response.worker_connected ? "" : "Worker 不可达");
  } catch (error) {
    if (await handleUnauthorized(error)) return;
    if (!silent) lanes.value = [];
    lanesError.value = error instanceof Error ? error.message : "加载车道状态失败";
  } finally {
    lanesPolling = false;
    if (!silent) lanesLoading.value = false;
  }
}

const settingsError = computed(() => {
  if (idleSeconds.value < 0 || idleSeconds.value > 86400) {
    return "空闲自动关闭时间必须在 0 到 86400 秒之间";
  }
  if (generalConcurrency.value < 1 || generalConcurrency.value > 10) {
    return "普通车道并行任务数必须在 1 到 10 之间";
  }
  if (proxyConcurrency.value < 1 || proxyConcurrency.value > 10) {
    return "代理车道并行任务数必须在 1 到 10 之间";
  }
  return "";
});
const canSaveSettings = computed(
  () =>
    Boolean(laneSettings.value) &&
    Boolean(
      laneSettings.value?.idle_stop_seconds.editable ||
        laneSettings.value?.general_concurrency.editable ||
        laneSettings.value?.proxy_concurrency.editable,
    ) &&
    !settingsError.value,
);

function settingSourceLabel(source: string): string {
  if (source === "env") return "环境变量";
  if (source === "db") return "数据库";
  return "默认";
}

function settingSourceType(source: string): "warning" | "success" | "info" {
  if (source === "env") return "warning";
  if (source === "db") return "success";
  return "info";
}

async function loadLaneSettings(): Promise<void> {
  try {
    const response = await apiRequest<LaneSettings>("/lane-settings");
    laneSettings.value = response;
    idleSeconds.value = response.idle_stop_seconds.value;
    generalConcurrency.value = response.general_concurrency.value;
    proxyConcurrency.value = response.proxy_concurrency.value;
  } catch (error) {
    if (await handleUnauthorized(error)) return;
    laneSettings.value = null;
  }
}

async function saveLaneSettings(): Promise<void> {
  if (!laneSettings.value || settingsError.value) {
    if (settingsError.value) ElMessage.warning(settingsError.value);
    return;
  }
  laneSettingsSaving.value = true;
  try {
    const payload: Record<string, number> = {};
    if (laneSettings.value.idle_stop_seconds.editable) {
      payload.idle_stop_seconds = Math.round(idleSeconds.value);
    }
    if (laneSettings.value.general_concurrency.editable) {
      payload.general_concurrency = Math.round(generalConcurrency.value);
    }
    if (laneSettings.value.proxy_concurrency.editable) {
      payload.proxy_concurrency = Math.round(proxyConcurrency.value);
    }
    if (!Object.keys(payload).length) {
      ElMessage.warning("所有车道设置均已由环境变量锁定");
      return;
    }
    const response = await apiRequest<LaneSettings>("/lane-settings", {
      method: "PUT",
      body: JSON.stringify(payload),
    });
    laneSettings.value = response;
    idleSeconds.value = response.idle_stop_seconds.value;
    generalConcurrency.value = response.general_concurrency.value;
    proxyConcurrency.value = response.proxy_concurrency.value;
    ElMessage.success("车道设置已保存");
    await loadLanes();
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "保存失败");
  } finally {
    laneSettingsSaving.value = false;
  }
}

function openVnc(lane: LaneStatus): void {
  if (!lane.vnc_available) return;
  activeLane.value = lane;
  vncOpen.value = true;
}

async function startBrowser(lane: LaneStatus): Promise<void> {
  laneActionKey.value = lane.key;
  try {
    await apiRequest(`/lanes/${encodeURIComponent(lane.key)}/start`, { method: "POST" });
    ElMessage.success(`${lane.label} 浏览器已启动`);
    await loadLanes();
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "启动浏览器失败");
  } finally {
    laneActionKey.value = "";
  }
}

async function stopBrowser(lane: LaneStatus): Promise<void> {
  laneActionKey.value = lane.key;
  try {
    await apiRequest(`/lanes/${encodeURIComponent(lane.key)}/stop`, { method: "POST" });
    ElMessage.success(`${lane.label} 浏览器已关闭`);
    await loadLanes();
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "关闭浏览器失败");
  } finally {
    laneActionKey.value = "";
  }
}

onMounted(async () => {
  await loadLaneSettings();
  await loadLanes();
  lanesTimer = setInterval(() => {
    void loadLanes(true);
  }, 10000);
});

onUnmounted(() => {
  if (lanesTimer !== null) {
    clearInterval(lanesTimer);
    lanesTimer = null;
  }
});
</script>

<template>
  <section class="page-head">
    <div>
      <p class="eyebrow">LANES</p>
      <h1>车道状态</h1>
      <p class="muted">四类车道按需启动；任务占用和 VNC 查看会阻止空闲关闭。</p>
    </div>
    <el-button @click="loadLanes()">刷新状态</el-button>
  </section>

  <el-card shadow="never" class="panel" v-loading="lanesLoading">
    <el-alert
      v-if="lanesError"
      :title="lanesError"
      type="error"
      :closable="false"
      show-icon
      class="notice"
    />

    <div v-if="lanes.length" class="lane-grid">
      <div v-for="lane in lanes" :key="lane.key" class="lane-item">
        <div class="lane-head">
          <strong>{{ lane.label }}</strong>
          <span class="lane-key mono">{{ lane.key }}</span>
        </div>
        <p class="lane-summary" :class="`is-${lane.status}`">
          <span class="status-dot"></span>
          {{ laneSummary(lane) }}
          <span v-if="lane.idle_remaining !== null" class="lane-idle">
            · 空闲 {{ lane.idle_remaining }}s 后自动关闭
          </span>
        </p>
        <p v-if="lane.error" class="error">{{ lane.error }}</p>
        <p v-if="lane.in_use" class="lane-busy">
          任务运行中（{{ lane.in_use }}），禁止关闭浏览器
        </p>
        <div class="lane-actions">
          <el-button
            v-if="lane.can_start"
            size="small"
            type="success"
            :loading="laneActionKey === lane.key"
            @click="startBrowser(lane)"
          >
            启动浏览器
          </el-button>
          <el-button
            v-else-if="lane.status === 'running'"
            size="small"
            type="danger"
            plain
            :disabled="!lane.can_stop"
            :loading="laneActionKey === lane.key"
            @click="stopBrowser(lane)"
          >
            关闭浏览器
          </el-button>
          <el-button v-else size="small" disabled>{{ laneStatusLabel(lane) }}</el-button>
          <el-button
            size="small"
            type="primary"
            :disabled="!lane.vnc_available"
            @click="openVnc(lane)"
          >
            进入 VNC
          </el-button>
        </div>
      </div>
    </div>
    <p v-else class="muted">暂无浏览器数据</p>
  </el-card>

  <el-card shadow="never" class="panel settings-panel">
    <template #header>
      <div class="card-head">
        <div>
          <strong>车道设置</strong>
          <span class="muted">空闲关闭、普通/代理车道并行任务数</span>
        </div>
      </div>
    </template>
    <div class="settings-grid">
      <div class="setting-item">
        <div class="setting-label">
          空闲自动关闭（秒）
          <el-tag
            v-if="laneSettings"
            size="small"
            :type="settingSourceType(laneSettings.idle_stop_seconds.source)"
            effect="plain"
          >
            {{ settingSourceLabel(laneSettings.idle_stop_seconds.source) }}
          </el-tag>
        </div>
        <el-input-number
          v-model="idleSeconds"
          :min="0"
          :max="86400"
          :step="30"
          :disabled="Boolean(laneSettings && !laneSettings.idle_stop_seconds.editable)"
        />
        <span class="muted setting-hint">0 = 禁用</span>
      </div>
      <div class="setting-item">
        <div class="setting-label">
          普通车道并行任务数
          <el-tag
            v-if="laneSettings"
            size="small"
            :type="settingSourceType(laneSettings.general_concurrency.source)"
            effect="plain"
          >
            {{ settingSourceLabel(laneSettings.general_concurrency.source) }}
          </el-tag>
        </div>
        <el-input-number
          v-model="generalConcurrency"
          :min="1"
          :max="10"
          :disabled="Boolean(laneSettings && !laneSettings.general_concurrency.editable)"
        />
        <span class="muted setting-hint">1–10</span>
      </div>
      <div class="setting-item">
        <div class="setting-label">
          代理车道并行任务数
          <el-tag
            v-if="laneSettings"
            size="small"
            :type="settingSourceType(laneSettings.proxy_concurrency.source)"
            effect="plain"
          >
            {{ settingSourceLabel(laneSettings.proxy_concurrency.source) }}
          </el-tag>
        </div>
        <el-input-number
          v-model="proxyConcurrency"
          :min="1"
          :max="10"
          :disabled="Boolean(laneSettings && !laneSettings.proxy_concurrency.editable)"
        />
        <span class="muted setting-hint">1–10</span>
      </div>
    </div>
    <div class="settings-actions">
      <span v-if="settingsError" class="settings-error">{{ settingsError }}</span>
      <el-button
        type="primary"
        :disabled="!canSaveSettings"
        :loading="laneSettingsSaving"
        @click="saveLaneSettings"
      >
        保存车道设置
      </el-button>
    </div>
  </el-card>

  <VncViewer
    v-model="vncOpen"
    :lane-key="activeLane?.key ?? ''"
    :lane-label="activeLane?.label ?? ''"
  />
</template>

<style scoped>
.page-head {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 20px;
  margin-bottom: 22px;
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

.panel {
  border-radius: 16px;
  border-color: #e4e9f0;
}

.settings-panel {
  margin-top: 16px;
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

.settings-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 16px;
}

.setting-item {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.setting-label {
  display: flex;
  align-items: center;
  gap: 8px;
  color: #606266;
  font-size: 13px;
  font-weight: 600;
}

.setting-hint {
  font-size: 11px;
}

.settings-actions {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 12px;
  margin-top: 18px;
}

.settings-error {
  color: #d9534f;
  font-size: 12px;
}

.notice {
  margin-bottom: 14px;
}

.lane-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 14px;
}

.lane-item {
  border: 1px solid #e4e9f0;
  border-radius: 12px;
  padding: 14px;
  background: #fbfcfe;
}

.lane-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.lane-key {
  color: #8a96a3;
  font-size: 12px;
}

.lane-summary {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  margin: 8px 0 10px;
  font-size: 13px;
  line-height: 1.6;
}

.status-dot {
  width: 7px;
  height: 7px;
  flex: 0 0 auto;
  border-radius: 50%;
  background: #909399;
}

.lane-summary.is-running .status-dot {
  background: #67c23a;
}

.lane-summary.is-starting .status-dot,
.lane-summary.is-stopping .status-dot {
  background: #e6a23c;
}

.lane-summary.is-error .status-dot,
.lane-summary.is-unavailable .status-dot {
  background: #f56c6c;
}

.lane-summary.is-disabled .status-dot {
  background: #b1b3b8;
}

.lane-idle {
  color: #8a96a3;
  font-size: 12px;
}

.lane-busy {
  color: #b88230;
  font-size: 12px;
}

.lane-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.error {
  color: #d9534f;
  font-size: 12px;
}

@media (max-width: 900px) {
  .lane-grid,
  .settings-grid {
    grid-template-columns: 1fr;
  }

  .page-head {
    align-items: flex-start;
    flex-direction: column;
  }
}
</style>
