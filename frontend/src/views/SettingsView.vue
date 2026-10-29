<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue";
import { useRouter } from "vue-router";
import { ElMessage, ElMessageBox } from "element-plus";

import { ApiError, apiRequest } from "@/api/client";
import StringListField from "@/components/StringListField.vue";
import { useAuthStore } from "@/stores/auth";
import { formatServerDateTime } from "@/utils/datetime";
import VisionSettingsCard from "@/components/VisionSettingsCard.vue";
import type { VisionView } from "@/types/integrations";

interface ProxyView {
  enabled: boolean;
  source: "env" | "db" | "unset";
  editable: boolean;
  protocol: string | null;
  url_masked: string | null;
  error: string | null;
}

interface ValueView {
  value: string | null;
  source: "env" | "db" | "unset";
  editable: boolean;
  error: string | null;
}

interface TokenView {
  configured: boolean;
  source: "env" | "db" | "unset";
  editable: boolean;
  masked: string | null;
}

interface TwoFAuthView {
  enabled: boolean;
  url: ValueView;
  token: TokenView;
  error: string | null;
}

interface CFBoxView {
  enabled: boolean;
  url: ValueView;
  api_key: TokenView;
  error: string | null;
}

interface BitwardenView {
  enabled: boolean;
  enabled_setting: boolean;
  enabled_source: "env" | "db" | "auto" | "unset";
  available: boolean;
  configured: boolean;
  auto_detected: boolean;
  source: "env" | "db" | "auto" | "unset";
  url: ValueView;
  token: TokenView;
  default_url: string;
  server_version: string | null;
  user_email: string | null;
  error: string | null;
}

interface IntegrationsView {
  vision: VisionView;
  proxy: ProxyView;
  twofauth: TwoFAuthView;
  cfbox: CFBoxView;
  bitwarden: BitwardenView;
}

interface SchedulerSettingsView {
  enabled: boolean;
  enabled_source: "env" | "db" | "default";
  mode: "cron" | "interval" | "random";
  cron: string;
  interval_seconds: number;
  ranges: string[];
  source: "env" | "db" | "default";
  editable: boolean;
  timezone: string;
  next_run_at: string | null;
}

interface ArtifactStatsView {
  total_count: number;
  total_bytes: number;
  log_count: number;
  log_bytes: number;
  screenshot_count: number;
  screenshot_bytes: number;
  missing_count: number;
  oldest_created_at: string | null;
  retention_days: number;
  cleanup_interval_seconds: number;
  cleanup_enabled: boolean;
}

const auth = useAuthStore();
const router = useRouter();
const loading = ref(true);
const loadError = ref("");
const integrations = ref<IntegrationsView | null>(null);
const proxyUrl = ref("");
const twofauthUrl = ref("");
const twofauthToken = ref("");
const cfboxUrl = ref("");
const cfboxApiKey = ref("");
const bitwardenUrl = ref("");
const bitwardenToken = ref("");
const bitwardenEnabled = ref(false);
const savingProxy = ref(false);
const savingTwofauth = ref(false);
const savingCfbox = ref(false);
const savingBitwarden = ref(false);
const artifactStats = ref<ArtifactStatsView | null>(null);
const artifactStatsLoading = ref(false);
const artifactStatsError = ref("");
const artifactCleanupRunning = ref(false);
const scheduler = ref<SchedulerSettingsView | null>(null);
const schedulerEnabled = ref(true);
const schedulerMode = ref<"cron" | "interval" | "random">("cron");
const schedulerChoice = computed({
  get: (): "off" | "cron" | "interval" | "random" =>
    schedulerEnabled.value ? schedulerMode.value : "off",
  set: (value: "off" | "cron" | "interval" | "random") => {
    if (value === "off") {
      schedulerEnabled.value = false;
      return;
    }
    schedulerEnabled.value = true;
    schedulerMode.value = value;
  },
});
const schedulerCron = ref("0 10 * * *");
const schedulerIntervalMinutes = ref(60);
const schedulerRanges = ref<string[]>([]);
const schedulerSaving = ref(false);
let schedulerTimer: ReturnType<typeof setInterval> | null = null;

const proxyLocked = computed(() => integrations.value?.proxy.source === "env");
const twofauthUrlLocked = computed(() => integrations.value?.twofauth.url.source === "env");
const twofauthTokenLocked = computed(
  () => integrations.value?.twofauth.token.source === "env",
);
const twofauthLocked = computed(
  () => twofauthUrlLocked.value && twofauthTokenLocked.value,
);
const twofauthDeleteDisabled = computed(
  () => twofauthUrlLocked.value || twofauthTokenLocked.value,
);
const cfboxUrlLocked = computed(() => integrations.value?.cfbox.url.source === "env");
const cfboxKeyLocked = computed(() => integrations.value?.cfbox.api_key.source === "env");
const cfboxLocked = computed(() => cfboxUrlLocked.value && cfboxKeyLocked.value);
const cfboxDeleteDisabled = computed(
  () => cfboxUrlLocked.value || cfboxKeyLocked.value,
);
const bitwardenUrlLocked = computed(
  () => integrations.value?.bitwarden.url.source === "env",
);
const bitwardenTokenLocked = computed(
  () => integrations.value?.bitwarden.token.source === "env",
);
const bitwardenEnabledLocked = computed(
  () => integrations.value?.bitwarden.enabled_source === "env",
);
const bitwardenDeleteDisabled = computed(
  () =>
    bitwardenUrlLocked.value ||
    bitwardenTokenLocked.value ||
    bitwardenEnabledLocked.value,
);
const bitwardenStatusLabel = computed(() => {
  const value = integrations.value?.bitwarden;
  if (!value) return "未配置";
  if (value.enabled) return "已启用";
  if (value.enabled_setting) return "未就绪";
  return "未启用";
});
const schedulerError = computed(() => {
  if (!schedulerEnabled.value) return "";
  if (schedulerMode.value === "cron" && !schedulerCron.value.trim()) {
    return "请填写 cron 表达式";
  }
  if (schedulerMode.value === "interval" && schedulerIntervalMinutes.value < 1) {
    return "间隔时间不能少于 1 分钟";
  }
  if (schedulerMode.value === "random") {
    if (!schedulerRanges.value.length) return "随机调度至少需要一个时间范围";
    for (const range of schedulerRanges.value) {
      const match = /^([01]\d|2[0-3]):([0-5]\d)-([01]\d|2[0-3]):([0-5]\d)$/.exec(
        range.trim(),
      );
      if (!match) return "时间范围格式应为 HH:MM-HH:MM";
      const start = Number(match[1]) * 60 + Number(match[2]);
      const end = Number(match[3]) * 60 + Number(match[4]);
      if (end <= start) return "时间范围的结束时间必须晚于开始时间";
    }
  }
  return "";
});
const canSaveScheduler = computed(
  () => Boolean(scheduler.value?.editable) && !schedulerError.value,
);
const authSourceLabel = computed(() => {
  const source = auth.user?.auth_source;
  if (source === "env") return "环境变量";
  if (source === "db") return "数据库";
  if (source === "bootstrap") return "初始随机密码";
  return "未配置";
});

function schedulerSourceLabel(): string {
  if (scheduler.value?.source === "env") return "环境变量";
  if (scheduler.value?.source === "db") return "数据库";
  return "默认";
}

function schedulerSourceType(): "warning" | "success" | "info" {
  if (scheduler.value?.source === "env") return "warning";
  if (scheduler.value?.source === "db") return "success";
  return "info";
}

function formatSchedulerDate(value: string | null): string {
  return formatServerDateTime(value, scheduler.value?.timezone ?? "Asia/Shanghai");
}

function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  if (value < 1024 * 1024 * 1024) {
    return `${(value / (1024 * 1024)).toFixed(1)} MB`;
  }
  return `${(value / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

function formatArtifactDate(value: string | null): string {
  if (!value) return "-";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function cleanupIntervalLabel(seconds: number): string {
  if (seconds % 3600 === 0) return `${seconds / 3600} 小时`;
  if (seconds % 60 === 0) return `${seconds / 60} 分钟`;
  return `${seconds} 秒`;
}

async function loadArtifactStats(): Promise<void> {
  artifactStatsLoading.value = true;
  artifactStatsError.value = "";
  try {
    artifactStats.value = await apiRequest<ArtifactStatsView>("/artifacts/stats");
  } catch (error) {
    if (await handleUnauthorized(error)) return;
    artifactStatsError.value = error instanceof Error ? error.message : "加载日志统计失败";
  } finally {
    artifactStatsLoading.value = false;
  }
}

async function cleanupArtifacts(): Promise<void> {
  const retention = artifactStats.value?.retention_days ?? 30;
  try {
    await ElMessageBox.confirm(
      `将删除超过 ${retention} 天的日志和截图文件，是否继续？`,
      "清理日志和截图",
      {
        confirmButtonText: "开始清理",
        cancelButtonText: "取消",
        type: "warning",
      },
    );
  } catch {
    return;
  }
  artifactCleanupRunning.value = true;
  try {
    const result = await apiRequest<{
      removed_count: number;
      removed_bytes: number;
      orphan_count: number;
      orphan_bytes: number;
      retention_days: number;
    }>("/artifacts/cleanup", { method: "POST" });
    ElMessage.success(
      `已清理 ${result.removed_count} 个记录、${formatBytes(
        result.removed_bytes + result.orphan_bytes,
      )} 空间`,
    );
    await loadArtifactStats();
  } catch (error) {
    if (await handleUnauthorized(error)) return;
    ElMessage.error(error instanceof Error ? error.message : "清理失败");
  } finally {
    artifactCleanupRunning.value = false;
  }
}

async function handleUnauthorized(error: unknown): Promise<boolean> {
  if (error instanceof ApiError && error.status === 401) {
    auth.clear();
    await router.replace({ name: "login" });
    return true;
  }
  return false;
}

async function loadSchedulerSettings(): Promise<void> {
  try {
    const response = await apiRequest<SchedulerSettingsView>("/scheduler-settings");
    scheduler.value = response;
    schedulerEnabled.value = response.enabled;
    schedulerMode.value = response.mode;
    schedulerCron.value = response.cron;
    schedulerIntervalMinutes.value = Math.max(1, Math.round(response.interval_seconds / 60));
    schedulerRanges.value = [...response.ranges];
  } catch (error) {
    if (await handleUnauthorized(error)) return;
    scheduler.value = null;
  }
}

async function refreshSchedulerNextRun(): Promise<void> {
  if (!scheduler.value) return;
  try {
    const response = await apiRequest<SchedulerSettingsView>("/scheduler-settings");
    if (scheduler.value) {
      scheduler.value.enabled = response.enabled;
      scheduler.value.next_run_at = response.next_run_at;
    }
  } catch (error) {
    await handleUnauthorized(error);
  }
}

async function saveSchedulerSettings(): Promise<void> {
  if (!scheduler.value?.editable || schedulerError.value) {
    if (schedulerError.value) ElMessage.warning(schedulerError.value);
    return;
  }
  schedulerSaving.value = true;
  try {
    const response = await apiRequest<SchedulerSettingsView>("/scheduler-settings", {
      method: "PUT",
      body: JSON.stringify({
        enabled: schedulerEnabled.value,
        mode: schedulerMode.value,
        cron: schedulerCron.value.trim(),
        interval_seconds: Math.round(schedulerIntervalMinutes.value * 60),
        ranges: schedulerRanges.value.map((item) => item.trim()).filter(Boolean),
      }),
    });
    scheduler.value = response;
    schedulerEnabled.value = response.enabled;
    schedulerMode.value = response.mode;
    schedulerCron.value = response.cron;
    schedulerIntervalMinutes.value = Math.max(1, Math.round(response.interval_seconds / 60));
    schedulerRanges.value = [...response.ranges];
    ElMessage.success("调度设置已保存");
  } catch (error) {
    if (await handleUnauthorized(error)) return;
    ElMessage.error(error instanceof Error ? error.message : "保存调度设置失败");
  } finally {
    schedulerSaving.value = false;
  }
}

async function load(): Promise<void> {
  loading.value = true;
  loadError.value = "";
  try {
    integrations.value = await apiRequest<IntegrationsView>("/integrations");
    proxyUrl.value = integrations.value.proxy.source === "db" ? "" : "";
    twofauthUrl.value = integrations.value.twofauth.url.value ?? "";
    twofauthToken.value = "";
    cfboxUrl.value = integrations.value.cfbox.url.value ?? "";
    cfboxApiKey.value = "";
    bitwardenUrl.value =
      integrations.value.bitwarden.url.value ??
      integrations.value.bitwarden.default_url;
    bitwardenToken.value = "";
    bitwardenEnabled.value = integrations.value.bitwarden.enabled_setting;
  } catch (error) {
    if (await handleUnauthorized(error)) return;
    loadError.value = error instanceof Error ? error.message : "加载系统设置失败";
  } finally {
    loading.value = false;
  }
}

async function saveProxy(): Promise<void> {
  if (!proxyUrl.value.trim()) {
    ElMessage.warning("请填写代理 URL");
    return;
  }
  savingProxy.value = true;
  try {
    await apiRequest("/integrations/proxy", {
      method: "PUT",
      body: JSON.stringify({ url: proxyUrl.value.trim() }),
    });
    ElMessage.success("代理配置已保存");
    await load();
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "保存代理失败");
  } finally {
    savingProxy.value = false;
  }
}

async function deleteProxy(): Promise<void> {
  savingProxy.value = true;
  try {
    await apiRequest("/integrations/proxy", { method: "DELETE" });
    ElMessage.success("代理配置已清除");
    await load();
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "清除代理失败");
  } finally {
    savingProxy.value = false;
  }
}

async function testProxy(): Promise<void> {
  savingProxy.value = true;
  try {
    const result = await apiRequest<{ ok: boolean; message: string }>(
      "/integrations/proxy/test",
      { method: "POST" },
    );
    if (result.ok) ElMessage.success(result.message);
    else ElMessage.error(result.message);
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "测试代理失败");
  } finally {
    savingProxy.value = false;
  }
}

async function saveTwoFAuth(): Promise<void> {
  const url = twofauthUrl.value.trim();
  const token = twofauthToken.value.trim();
  const payload: Record<string, string> = {};
  if (!twofauthUrlLocked.value && url) payload.url = url;
  if (!twofauthTokenLocked.value && token) payload.token = token;
  if (!Object.keys(payload).length) {
    ElMessage.warning("请填写 2FAuth URL 或 Token");
    return;
  }
  savingTwofauth.value = true;
  try {
    await apiRequest("/integrations/twofauth", {
      method: "PUT",
      body: JSON.stringify(payload),
    });
    ElMessage.success("2FAuth 配置已保存");
    await load();
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "保存 2FAuth 失败");
  } finally {
    savingTwofauth.value = false;
  }
}

async function deleteTwoFAuth(): Promise<void> {
  savingTwofauth.value = true;
  try {
    await apiRequest("/integrations/twofauth", { method: "DELETE" });
    ElMessage.success("2FAuth 配置已清除");
    await load();
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "清除 2FAuth 失败");
  } finally {
    savingTwofauth.value = false;
  }
}

async function saveCFBox(): Promise<void> {
  const url = cfboxUrl.value.trim();
  const apiKey = cfboxApiKey.value.trim();
  const payload: Record<string, string> = {};
  if (!cfboxUrlLocked.value && url) payload.url = url;
  if (!cfboxKeyLocked.value && apiKey) payload.api_key = apiKey;
  if (!Object.keys(payload).length) {
    ElMessage.warning("请填写 CFbox URL 或 API Key");
    return;
  }
  savingCfbox.value = true;
  try {
    await apiRequest("/integrations/cfbox", {
      method: "PUT",
      body: JSON.stringify(payload),
    });
    ElMessage.success("CFbox 配置已保存");
    await load();
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "保存 CFbox 失败");
  } finally {
    savingCfbox.value = false;
  }
}

async function deleteCFBox(): Promise<void> {
  savingCfbox.value = true;
  try {
    await apiRequest("/integrations/cfbox", { method: "DELETE" });
    ElMessage.success("CFbox 配置已清除");
    await load();
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "清除 CFbox 失败");
  } finally {
    savingCfbox.value = false;
  }
}

async function testCFBox(): Promise<void> {
  savingCfbox.value = true;
  try {
    const result = await apiRequest<{ ok: boolean; message: string }>(
      "/integrations/cfbox/test",
      { method: "POST" },
    );
    if (result.ok) ElMessage.success(result.message);
    else ElMessage.error(result.message);
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "测试 CFbox 失败");
  } finally {
    savingCfbox.value = false;
  }
}

function bitwardenPayload(): Record<string, unknown> {
  const payload: Record<string, unknown> = {};
  const current = integrations.value?.bitwarden;
  const url = bitwardenUrl.value.trim();
  const token = bitwardenToken.value.trim();
  if (!bitwardenUrlLocked.value && url && url !== (current?.url.value ?? "")) {
    payload.url = url;
  }
  if (!bitwardenTokenLocked.value && token) payload.token = token;
  if (
    !bitwardenEnabledLocked.value &&
    bitwardenEnabled.value !== (current?.enabled_setting ?? false)
  ) {
    payload.enabled = bitwardenEnabled.value;
  }
  return payload;
}

async function saveBitwarden(): Promise<void> {
  const payload = bitwardenPayload();
  if (!Object.keys(payload).length) {
    ElMessage.info("Bitwarden 配置没有变化");
    return;
  }
  savingBitwarden.value = true;
  try {
    await apiRequest("/integrations/bitwarden", {
      method: "PUT",
      body: JSON.stringify(payload),
    });
    ElMessage.success("Bitwarden 配置已保存");
    await load();
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "保存 Bitwarden 失败");
  } finally {
    savingBitwarden.value = false;
  }
}

async function deleteBitwarden(): Promise<void> {
  savingBitwarden.value = true;
  try {
    await apiRequest("/integrations/bitwarden", { method: "DELETE" });
    ElMessage.success("Bitwarden 配置已清除");
    await load();
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "清除 Bitwarden 失败");
  } finally {
    savingBitwarden.value = false;
  }
}

async function testBitwarden(): Promise<void> {
  savingBitwarden.value = true;
  try {
    const result = await apiRequest<{ ok: boolean; message: string }>(
      "/integrations/bitwarden/test",
      { method: "POST" },
    );
    if (result.ok) ElMessage.success(result.message);
    else ElMessage.error(result.message);
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "测试 Bitwarden 失败");
  } finally {
    savingBitwarden.value = false;
  }
}

async function testTwoFAuth(): Promise<void> {
  savingTwofauth.value = true;
  try {
    const result = await apiRequest<{ ok: boolean; message: string }>(
      "/integrations/twofauth/test",
      { method: "POST" },
    );
    if (result.ok) ElMessage.success(result.message);
    else ElMessage.error(result.message);
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "测试 2FAuth 失败");
  } finally {
    savingTwofauth.value = false;
  }
}

function sourceLabel(source: string): string {
  if (source === "env") return "环境变量";
  if (source === "db") return "数据库";
  if (source === "auto") return "自动检测";
  return "未配置";
}

function sourceTagType(source: string): "success" | "warning" | "info" {
  if (source === "env") return "warning";
  if (source === "db") return "success";
  if (source === "auto") return "success";
  return "info";
}

onMounted(() => {
  void load();
  void loadArtifactStats();
  void loadSchedulerSettings();
  schedulerTimer = setInterval(() => {
    void refreshSchedulerNextRun();
  }, 30000);
});

onUnmounted(() => {
  if (schedulerTimer !== null) {
    clearInterval(schedulerTimer);
    schedulerTimer = null;
  }
});
</script>

<template>
  <section class="page-head">
    <div>
      <p class="eyebrow">SETTINGS</p>
      <h1>系统设置</h1>
      <p class="muted">调度、代理、Vision、2FAuth、CFbox、Bitwarden、日志与截图和账户安全设置。</p>
    </div>
  </section>

  <el-alert
    v-if="loadError"
    :title="loadError"
    type="error"
    :closable="false"
    show-icon
    class="notice"
  />

  <el-row :gutter="18" v-loading="loading">
    <el-col :xs="24" :lg="12">
      <VisionSettingsCard v-if="integrations" :vision="integrations.vision" @updated="integrations.vision = $event" />
    </el-col>
    <el-col :xs="24" :lg="12">
      <el-card shadow="never" class="panel">
        <template #header>
          <div class="card-head">
            <div>
              <strong>代理</strong>
              <el-tag :type="integrations?.proxy.enabled ? 'success' : 'info'" size="small">
                {{ integrations?.proxy.enabled ? "已启用" : "未配置" }}
              </el-tag>
            </div>
            <el-tag :type="sourceTagType(integrations?.proxy.source ?? 'unset')" size="small">
              {{ sourceLabel(integrations?.proxy.source ?? "unset") }}
            </el-tag>
          </div>
        </template>

        <el-descriptions :column="1" border class="current">
          <el-descriptions-item label="协议">
            {{ integrations?.proxy.protocol ?? "-" }}
          </el-descriptions-item>
          <el-descriptions-item label="当前地址">
            <span class="mono">{{ integrations?.proxy.url_masked ?? "未配置" }}</span>
          </el-descriptions-item>
        </el-descriptions>
        <p v-if="integrations?.proxy.error" class="error">{{ integrations.proxy.error }}</p>

        <el-form label-position="top" class="config-form">
          <el-form-item label="代理 URL">
            <el-input
              v-model="proxyUrl"
              :disabled="proxyLocked"
              placeholder="socks5://user:pass@proxy.example.com:1080"
            />
          </el-form-item>
        </el-form>
        <div class="form-actions">
          <el-button type="primary" :loading="savingProxy" :disabled="proxyLocked" @click="saveProxy">
            保存代理
          </el-button>
          <el-button :loading="savingProxy" :disabled="proxyLocked" @click="testProxy">测试</el-button>
          <el-button
            type="danger"
            plain
            :loading="savingProxy"
            :disabled="proxyLocked || integrations?.proxy.source !== 'db'"
            @click="deleteProxy"
          >
            清除
          </el-button>
        </div>
        <p v-if="proxyLocked" class="muted">已由 SITEFLOW_PROXY_URL 环境变量锁定。</p>
      </el-card>
    </el-col>

    <el-col :xs="24" :lg="12">
      <el-card shadow="never" class="panel">
        <template #header>
          <div class="card-head">
            <div>
              <strong>2FAuth</strong>
              <el-tag :type="integrations?.twofauth.enabled ? 'success' : 'info'" size="small">
                {{ integrations?.twofauth.enabled ? "已启用" : "未配置" }}
              </el-tag>
            </div>
            <el-tag :type="sourceTagType(integrations?.twofauth.url.source ?? 'unset')" size="small">
              {{ sourceLabel(integrations?.twofauth.url.source ?? "unset") }}
            </el-tag>
          </div>
        </template>

        <el-descriptions :column="1" border class="current">
          <el-descriptions-item label="当前地址">
            <span class="mono">{{ integrations?.twofauth.url.value ?? "未配置" }}</span>
          </el-descriptions-item>
          <el-descriptions-item label="Token">
            <span v-if="integrations?.twofauth.token.configured">
              {{ integrations.twofauth.token.masked ?? "已由环境变量配置" }}
            </span>
            <span v-else>未配置</span>
          </el-descriptions-item>
        </el-descriptions>
        <p v-if="integrations?.twofauth.error" class="error">{{ integrations.twofauth.error }}</p>

        <el-form label-position="top" class="config-form">
          <el-form-item label="服务地址">
            <el-input
              v-model="twofauthUrl"
              :disabled="twofauthUrlLocked"
              placeholder="https://2fa.example.com"
            />
          </el-form-item>
          <el-form-item label="API Token">
            <el-input
              v-model="twofauthToken"
              type="password"
              show-password
              :disabled="twofauthTokenLocked"
              placeholder="服务端未启用鉴权时可留空"
            />
          </el-form-item>
        </el-form>
        <div class="form-actions">
          <el-button
            type="primary"
            :loading="savingTwofauth"
            :disabled="twofauthLocked"
            @click="saveTwoFAuth"
          >
            保存 2FAuth
          </el-button>
          <el-button :loading="savingTwofauth" @click="testTwoFAuth">测试</el-button>
          <el-button
            type="danger"
            plain
            :loading="savingTwofauth"
            :disabled="twofauthDeleteDisabled || integrations?.twofauth.url.source !== 'db'"
            @click="deleteTwoFAuth"
          >
            清除
          </el-button>
        </div>
        <p v-if="twofauthUrlLocked || twofauthTokenLocked" class="muted">
          URL 或 Token 已由环境变量锁定；锁定项不能在页面修改。
        </p>
      </el-card>
    </el-col>

    <el-col :xs="24" :lg="12">
      <el-card shadow="never" class="panel">
        <template #header>
          <div class="card-head">
            <div>
              <strong>CFbox</strong>
              <el-tag :type="integrations?.cfbox.enabled ? 'success' : 'info'" size="small">
                {{ integrations?.cfbox.enabled ? "已启用" : "未配置" }}
              </el-tag>
            </div>
            <el-tag :type="sourceTagType(integrations?.cfbox.url.source ?? 'unset')" size="small">
              {{ sourceLabel(integrations?.cfbox.url.source ?? "unset") }}
            </el-tag>
          </div>
        </template>

        <el-descriptions :column="1" border class="current">
          <el-descriptions-item label="服务地址">
            <span class="mono">{{ integrations?.cfbox.url.value ?? "未配置" }}</span>
          </el-descriptions-item>
          <el-descriptions-item label="API Key">
            <span v-if="integrations?.cfbox.api_key.configured">
              {{ integrations.cfbox.api_key.masked ?? "已由环境变量配置" }}
            </span>
            <span v-else>未配置</span>
          </el-descriptions-item>
        </el-descriptions>
        <p v-if="integrations?.cfbox.error" class="error">{{ integrations.cfbox.error }}</p>

        <el-form label-position="top" class="config-form">
          <el-form-item label="服务地址">
            <el-input
              v-model="cfboxUrl"
              :disabled="cfboxUrlLocked"
              placeholder="http://cfbox:8000"
            />
          </el-form-item>
          <el-form-item label="API Key（可选）">
            <el-input
              v-model="cfboxApiKey"
              type="password"
              show-password
              :disabled="cfboxKeyLocked"
              placeholder="服务端未启用鉴权时可留空"
            />
          </el-form-item>
        </el-form>
        <div class="form-actions">
          <el-button type="primary" :loading="savingCfbox" :disabled="cfboxLocked" @click="saveCFBox">
            保存 CFbox
          </el-button>
          <el-button :loading="savingCfbox" @click="testCFBox">测试</el-button>
          <el-button
            type="danger"
            plain
            :loading="savingCfbox"
            :disabled="cfboxDeleteDisabled || integrations?.cfbox.url.source !== 'db'"
            @click="deleteCFBox"
          >
            清除
          </el-button>
        </div>
        <p v-if="cfboxUrlLocked || cfboxKeyLocked" class="muted">
          URL 或 API Key 已由环境变量锁定；锁定项不能在页面修改。
        </p>
        <p class="muted">CFbox 用于识别 Cloudflare 验证截图中的复选框位置；未配置时不能使用 CF 验证功能块。</p>
      </el-card>
    </el-col>

    <el-col :xs="24" :lg="12">
      <el-card shadow="never" class="panel">
        <template #header>
          <div class="card-head">
            <div>
              <strong>Bitwarden</strong>
              <el-tag
                :type="integrations?.bitwarden.enabled ? 'success' : 'info'"
                size="small"
              >
                {{ bitwardenStatusLabel }}
              </el-tag>
            </div>
            <el-tag
              :type="sourceTagType(integrations?.bitwarden.source ?? 'unset')"
              size="small"
            >
              {{ sourceLabel(integrations?.bitwarden.source ?? "unset") }}
            </el-tag>
          </div>
        </template>

        <el-descriptions :column="1" border class="current">
          <el-descriptions-item label="服务地址">
            <span class="mono">{{ integrations?.bitwarden.url.value ?? "未配置" }}</span>
          </el-descriptions-item>
          <el-descriptions-item label="API Token">
            <span v-if="integrations?.bitwarden.token.configured">
              {{ integrations.bitwarden.token.masked ?? "已由环境变量配置" }}
            </span>
            <span v-else>未配置（可选）</span>
          </el-descriptions-item>
          <el-descriptions-item label="启用来源">
            {{ sourceLabel(integrations?.bitwarden.enabled_source ?? "unset") }}
          </el-descriptions-item>
          <el-descriptions-item label="检测状态">
            <template v-if="integrations?.bitwarden.available">
              服务已连接
              <span v-if="integrations.bitwarden.configured"> · 保险库已配置</span>
              <span v-else> · 保险库未配置</span>
              <span v-if="integrations.bitwarden.server_version">
                · v{{ integrations.bitwarden.server_version }}
              </span>
            </template>
            <span v-else>未检测到服务</span>
          </el-descriptions-item>
        </el-descriptions>
        <p v-if="integrations?.bitwarden.error" class="error">
          {{ integrations.bitwarden.error }}
        </p>

        <el-form label-position="top" class="config-form">
          <el-form-item label="启用 Bitwarden 凭据">
            <el-switch
              v-model="bitwardenEnabled"
              :disabled="bitwardenEnabledLocked"
              active-text="启用"
              inactive-text="禁用"
            />
          </el-form-item>
          <el-form-item label="服务地址">
            <el-input
              v-model="bitwardenUrl"
              :disabled="bitwardenUrlLocked"
              :placeholder="integrations?.bitwarden.default_url ?? 'http://bitwarden:8090'"
            />
          </el-form-item>
          <el-form-item label="API Token（可选）">
            <el-input
              v-model="bitwardenToken"
              type="password"
              show-password
              :disabled="bitwardenTokenLocked"
              placeholder="与包装容器的 BW_API_TOKEN 一致；未启用鉴权时可留空"
            />
          </el-form-item>
        </el-form>
        <div class="form-actions">
          <el-button
            type="primary"
            :loading="savingBitwarden"
            :disabled="bitwardenUrlLocked && bitwardenTokenLocked && bitwardenEnabledLocked"
            @click="saveBitwarden"
          >
            保存 Bitwarden
          </el-button>
          <el-button :loading="savingBitwarden" @click="testBitwarden">测试</el-button>
          <el-button
            type="danger"
            plain
            :loading="savingBitwarden"
            :disabled="bitwardenDeleteDisabled"
            @click="deleteBitwarden"
          >
            清除
          </el-button>
        </div>
        <p v-if="integrations?.bitwarden.auto_detected" class="muted">
          正在自动检测 compose 中的 bitwarden 容器（默认
          <span class="mono">{{ integrations.bitwarden.default_url }}</span>）。
        </p>
        <p
          v-if="
            bitwardenUrlLocked ||
            bitwardenTokenLocked ||
            bitwardenEnabledLocked
          "
          class="muted"
        >
          锁定项已由环境变量提供，不能在页面修改。
        </p>
        <p class="muted">
          站点凭据页启用后，任务执行时从自托管 Bitwarden 读取登录用户名和密码；SiteFlow
          不保存这些凭据。
        </p>
      </el-card>
    </el-col>
  </el-row>

  <el-card shadow="never" class="panel">
    <template #header>
      <div class="card-head">
        <div>
          <strong>调度设置</strong>
          <span class="muted">定时 / 间隔 / 随机时间范围</span>
        </div>
        <el-tag :type="schedulerSourceType()" size="small" effect="plain">
          {{ schedulerSourceLabel() }}
        </el-tag>
      </div>
    </template>
    <el-alert
      v-if="scheduler && !scheduler.editable"
      title="调度设置已由环境变量锁定"
      type="warning"
      :closable="false"
      show-icon
      class="notice"
    />
    <div class="schedule-form">
      <div class="schedule-mode">
        <span class="schedule-label">调度模式</span>
        <el-radio-group
          v-model="schedulerChoice"
          :disabled="Boolean(scheduler && !scheduler.editable)"
        >
          <el-radio-button label="off">关闭调度</el-radio-button>
          <el-radio-button label="cron">定时调度</el-radio-button>
          <el-radio-button label="interval">间隔调度</el-radio-button>
          <el-radio-button label="random">随机调度</el-radio-button>
        </el-radio-group>
        <el-tag
          :type="sourceTagType(scheduler?.enabled_source ?? 'default')"
          size="small"
          effect="plain"
        >
          {{
            scheduler?.enabled_source === "env"
              ? "环境变量"
              : scheduler?.enabled_source === "db"
                ? "数据库"
                : "默认"
          }}
        </el-tag>
      </div>
      <el-alert
        v-if="!schedulerEnabled"
        title="关闭调度后，系统不会自动触发签到任务，手动运行不受影响。"
        type="info"
        :closable="false"
        show-icon
      />
      <div v-if="schedulerEnabled && schedulerMode === 'cron'" class="schedule-field">
        <span class="schedule-label">Cron 表达式</span>
        <el-input
          v-model="schedulerCron"
          placeholder="0 10 * * *"
          :disabled="Boolean(scheduler && !scheduler.editable)"
        />
        <span class="muted schedule-hint">
          例：0 10 * * * 表示每天 10:00（{{ scheduler?.timezone || "Asia/Shanghai" }}）
        </span>
      </div>
      <div
        v-else-if="schedulerEnabled && schedulerMode === 'interval'"
        class="schedule-field"
      >
        <span class="schedule-label">间隔时间（分钟）</span>
        <el-input-number
          v-model="schedulerIntervalMinutes"
          :min="1"
          :max="43200"
          :disabled="Boolean(scheduler && !scheduler.editable)"
        />
      </div>
      <StringListField
        v-else-if="schedulerEnabled && schedulerMode === 'random'"
        v-model="schedulerRanges"
        label="随机时间范围"
        :description="`每项格式 HH:MM-HH:MM；每个范围每天随机调度一次（${scheduler?.timezone || 'Asia/Shanghai'}）`"
        add-label="添加时间范围"
      />
      <div class="schedule-actions">
        <span class="muted">
          <template v-if="!schedulerEnabled">已关闭调度</template>
          <template v-else-if="scheduler?.next_run_at">
            下次运行：{{ formatSchedulerDate(scheduler.next_run_at) }}
            （{{ scheduler.timezone }}）
          </template>
          <template v-else>暂无下次运行时间</template>
        </span>
        <span v-if="schedulerError" class="settings-error">{{ schedulerError }}</span>
        <el-button
          type="primary"
          :disabled="!canSaveScheduler"
          :loading="schedulerSaving"
          @click="saveSchedulerSettings"
        >
          保存调度设置
        </el-button>
      </div>
    </div>
  </el-card>

  <el-card shadow="never" class="panel" v-loading="artifactStatsLoading">
    <template #header>
      <div class="card-head">
        <div>
          <strong>日志与截图</strong>
          <el-tag :type="artifactStats?.cleanup_enabled ? 'success' : 'info'" size="small">
            {{ artifactStats?.cleanup_enabled ? "自动清理已启用" : "自动清理已禁用" }}
          </el-tag>
        </div>
        <div class="artifact-actions">
          <el-button size="small" @click="loadArtifactStats">刷新统计</el-button>
          <el-button
            size="small"
            type="danger"
            plain
            :loading="artifactCleanupRunning"
            :disabled="!artifactStats"
            @click="cleanupArtifacts"
          >
            立即清理
          </el-button>
        </div>
      </div>
    </template>

    <el-alert
      v-if="artifactStatsError"
      :title="artifactStatsError"
      type="error"
      :closable="false"
      show-icon
      class="notice"
    />

    <div v-if="artifactStats" class="artifact-stats-grid">
      <div class="artifact-stat">
        <span class="artifact-stat-label">执行日志</span>
        <strong>{{ artifactStats.log_count }} 个</strong>
        <span class="muted">{{ formatBytes(artifactStats.log_bytes) }}</span>
      </div>
      <div class="artifact-stat">
        <span class="artifact-stat-label">截图</span>
        <strong>{{ artifactStats.screenshot_count }} 个</strong>
        <span class="muted">{{ formatBytes(artifactStats.screenshot_bytes) }}</span>
      </div>
      <div class="artifact-stat is-total">
        <span class="artifact-stat-label">合计</span>
        <strong>{{ artifactStats.total_count }} 个</strong>
        <span class="muted">{{ formatBytes(artifactStats.total_bytes) }}</span>
      </div>
    </div>

    <el-descriptions v-if="artifactStats" :column="1" border class="artifact-descriptions">
      <el-descriptions-item label="清理策略">
        保留 {{ artifactStats.retention_days }} 天；每
        {{ cleanupIntervalLabel(artifactStats.cleanup_interval_seconds) }}自动清理
      </el-descriptions-item>
      <el-descriptions-item label="最早记录">
        {{ formatArtifactDate(artifactStats.oldest_created_at) }}
      </el-descriptions-item>
      <el-descriptions-item label="文件缺失">
        {{ artifactStats.missing_count }} 个
      </el-descriptions-item>
    </el-descriptions>
    <p class="muted artifact-hint">
      通过 <span class="mono">SITEFLOW_ARTIFACT_RETENTION_DAYS</span> 和
      <span class="mono">SITEFLOW_ARTIFACT_CLEANUP_INTERVAL_SECONDS</span> 调整策略。
    </p>
  </el-card>

  <el-card shadow="never" class="panel security-card">
    <template #header>
      <div class="card-head">
        <strong>账户安全</strong>
        <el-tag :type="sourceTagType(auth.user?.auth_source ?? 'unset')" size="small">
          {{ authSourceLabel }}
        </el-tag>
      </div>
    </template>
    <p class="muted">
      密码修改和退出登录在右上角账户菜单中操作。密码来源：{{ authSourceLabel }}。
    </p>
  </el-card>
</template>

<style scoped>
.page-head {
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
  margin-bottom: 18px;
}

.card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.card-head > div {
  display: flex;
  align-items: center;
  gap: 8px;
}

.current {
  margin-bottom: 12px;
}

.config-form {
  margin-top: 6px;
}

.form-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.notice {
  margin-bottom: 14px;
}

.error {
  color: #d9534f;
  font-size: 12px;
}

.schedule-form {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.schedule-mode,
.schedule-field {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}

.schedule-label {
  min-width: 110px;
  color: #606266;
  font-size: 13px;
  font-weight: 600;
}

.schedule-field .el-input {
  max-width: 260px;
}

.schedule-hint {
  font-size: 11px;
}

.schedule-actions {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 12px;
  flex-wrap: wrap;
}

.settings-error {
  color: #d9534f;
  font-size: 12px;
}

.security-card {
  margin-top: 2px;
}

.artifact-actions {
  display: flex;
  gap: 8px;
}

.artifact-stats-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px;
  margin-bottom: 14px;
}

.artifact-stat {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 12px;
  border: 1px solid #e7edf5;
  border-radius: 10px;
  background: #fbfcfe;
}

.artifact-stat.is-total {
  background: #f2f8ff;
  border-color: #cfe4fa;
}

.artifact-stat-label {
  color: #8a96a3;
  font-size: 12px;
}

.artifact-stat strong {
  color: #303133;
  font-size: 20px;
}

.artifact-descriptions {
  margin-bottom: 10px;
}

.artifact-hint {
  margin: 0;
  font-size: 12px;
}

@media (max-width: 760px) {
  .artifact-stats-grid {
    grid-template-columns: 1fr;
  }
}
</style>
