<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue";
import { ElMessageBox } from "element-plus";
import { ApiError, apiRequest } from "@/api/client";
import { compareBrowserVersion, isBrowserSettings, KEYLESS_VERSION_REFERENCE, type BrowserSettings } from "@/types/browser-settings";

const emit = defineEmits<{ unauthorized: [error: ApiError] }>();
const settings = ref<BrowserSettings | null>(null);
const draft = ref("");
const busy = ref<"read" | "save" | "clear" | "">("");
const confirming = ref(false);
const refreshRequired = ref(true);
const notice = ref<{ type: "success" | "warning" | "error"; message: string } | null>(null);
const sourceLabels = { env: "环境变量", manager: "Manager配置", unset: "未配置", unknown: "未知" };
const tierLabels = { keyless: "无密钥", free: "免费版", pro: "专业版", none: "无有效许可", unknown: "未知" };
let active = true;
let generation = 0;
let controller: AbortController | null = null;
const inputDisabled = computed(() => Boolean(busy.value) || confirming.value || refreshRequired.value ||
  settings.value?.backend !== "cloak-manager" || !settings.value.connected ||
  settings.value.error !== null || settings.value.license.source === "env" || !settings.value.license.editable);
const canSave = computed(() => !inputDisabled.value && Boolean(draft.value.trim()));
const canClear = computed(() => !inputDisabled.value && Boolean(settings.value?.license.configured));
const versionComparison = computed(() => compareBrowserVersion(settings.value?.binary_version));
const versionHint = computed(() => {
  const value = settings.value;
  if (!value || value.backend !== "cloak-manager") return "";
  const comparison = versionComparison.value;
  if (comparison === null) {
    if (value.license_tier === "free" || value.license_tier === "pro") {
      return `浏览器版本未确认；Manager 当前报告${tierLabels[value.license_tier]}，此许可模式需要有效 LICENSE_KEY，不能因版本未知推断可无密钥运行。`;
    }
    if (value.license_tier === "none") return "浏览器版本未确认；Manager 当前报告无有效许可，需要配置有效 LICENSE_KEY。";
    if (value.license_tier === "keyless") return "Manager 当前报告旧版无密钥模式（keyless），但浏览器版本未确认；不能据此保证可无密钥启动，请核实 Manager 支持和实际许可状态。";
    return "浏览器版本 / 许可模式未知，无法根据版本判断是否可无密钥运行；请以 Manager 的实际支持和有效许可状态为准。";
  }
  if (comparison > 0) return `此版本高于参考版本 ${KEYLESS_VERSION_REFERENCE}，新启动浏览器需要有效 LICENSE_KEY；清除后将无法新启动。` +
    (value.license_tier === "keyless" ? " Manager 返回的 keyless 与版本信息冲突，请刷新并核实，不能据此认定可无密钥运行。" : "");
  if (["free", "pro", "none"].includes(value.license_tier)) {
    return `此版本不高于参考版本 ${KEYLESS_VERSION_REFERENCE}，但当前 Manager 返回的许可等级优先：${tierLabels[value.license_tier]}，不能仅凭旧版本推断可无密钥运行。`;
  }
  if (!value.connected || value.error !== null) return "旧版本的许可模式尚未确认，请恢复 Manager 连接及正常状态后刷新。";
  if (comparison === 0 && value.license_tier === "keyless") {
    return `Manager 正在使用无密钥模式。Chromium ${KEYLESS_VERSION_REFERENCE} 是 SiteFlow 默认镜像自带的版本，无需配置 LICENSE_KEY。`;
  }
  return `此版本不高于参考版本 ${KEYLESS_VERSION_REFERENCE}；仅支持旧版无密钥模式的 Manager 可能允许不使用密钥，实际以 Manager 支持和许可状态为准。`;
});
const tierLabel = computed(() => settings.value?.license_tier === "keyless" &&
  (versionComparison.value === null || versionComparison.value > 0)
  ? "模式待确认（Manager 返回 keyless）" : settings.value ? tierLabels[settings.value.license_tier] : "未知");
const licenseWarning = computed(() => {
  const value = settings.value;
  if (!value || value.backend !== "cloak-manager") return "";
  if (!value.connected) return "Manager 未连接，无法修改许可证。请检查服务后刷新。";
  if (value.error !== null) return "Manager 报告异常，请检查服务日志并刷新确认状态。";
  if (value.license_tier === "none" && value.license.configured) {
    return "许可证已配置，但 Manager 尚未确认有效许可；请检查许可证和下载状态后刷新。";
  }
  if (value.license_tier === "unknown") return "许可状态未知，请刷新确认；配置存在不代表许可已生效。";
  if (value.license_tier === "none") return "当前无有效许可，请配置免费或付费 LICENSE_KEY。";
  return "";
});

async function run(mode: "read" | "save" | "clear", keepNotice = false): Promise<void> {
  if (!active || busy.value || mode === "save" && !canSave.value || mode === "clear" && !canClear.value) return;
  if (mode === "clear") {
    const confirmedGeneration = generation;
    confirming.value = true;
    try {
      await ElMessageBox.confirm(
        "确认清除 Manager 保存的许可证？此操作仅清除 Manager 保存的密钥，不会修改环境变量或 wrapper 的 license.key 文件，不保证所有来源的密钥都已移除。新版本需要有效 LICENSE_KEY，清除后将无法新启动浏览器；旧版本清除后仅在 Manager 支持旧版无密钥模式时才可能无密钥运行。此操作不会停止正在运行的浏览器，也不保证正在运行的实例重新授权或许可状态立即改变。",
        "清除许可证", { type: "warning", confirmButtonText: "清除许可证", cancelButtonText: "取消" },
      );
    } catch { return; }
    finally { confirming.value = false; }
    // A confirmation never authorizes a write against refreshed or locked state.
    if (!active || generation !== confirmedGeneration || !canClear.value) return;
  }
  const write = mode !== "read";
  busy.value = mode;
  if (!keepNotice) notice.value = null;
  const request = ++generation;
  const abort = new AbortController();
  controller = abort;
  // Writes can include a Manager binary download. Never retry an uncertain write.
  const timer = setTimeout(() => abort.abort(), write ? 370000 : 35000);
  let conflict = false;
  try {
    const value = await apiRequest<unknown>("/lanes/browser-settings", {
      method: write ? "PUT" : "GET",
      signal: abort.signal,
      ...(write ? { body: JSON.stringify({ license_key: mode === "clear" ? "" : draft.value.trim() }) } : {}),
    });
    if (!active || request !== generation) return;
    if (write) draft.value = "";
    if (!isBrowserSettings(value)) throw new ApiError(502, "invalid_browser_settings_response");
    settings.value = value;
    refreshRequired.value = false;
    if (value.license.source === "env" || value.backend === "unsupported") draft.value = "";
    if (mode === "clear") {
      const confirmed = value.backend === "cloak-manager" && value.connected && value.error === null &&
        value.license.source === "unset" && !value.license.configured;
      notice.value = confirmed
        ? { type: "success", message: "Manager 保存的许可证已清除，未停止正在运行的浏览器，也不保证运行实例重新授权。" +
          (value.license_tier === "keyless" && versionComparison.value !== null && versionComparison.value <= 0
            ? "Manager 当前报告旧版无密钥模式，实际启动仍以 Manager 状态为准。"
            : "清除不代表可以无密钥启动；新版本仍需要有效 LICENSE_KEY。") }
        : { type: "warning", message: "清除请求已完成，但尚未确认 Manager 保存的许可证已清除，当前状态可能未知或仍已配置。请刷新确认；不会自动重试。" };
    } else if (mode === "save") {
      const confirmed = value.backend === "cloak-manager" && value.connected && value.license.configured &&
        (value.license_tier === "free" || value.license_tier === "pro") && value.error === null;
      notice.value = confirmed
        ? { type: "success", message: "许可证配置已保存，未启动浏览器。" }
        : { type: "warning", message: "配置请求已完成，但尚未确认有效许可。请检查 Manager 状态并刷新；不会自动启动或重试。" };
    }
  } catch (error) {
    if (!active || request !== generation) return;
    refreshRequired.value = true;
    if (error instanceof ApiError && error.status === 401) {
      draft.value = "";
      emit("unauthorized", error);
      notice.value = { type: "error", message: "登录已失效，请重新登录。" };
    } else if (write && error instanceof ApiError && error.status === 409) {
      conflict = true;
      notice.value = { type: "warning", message: "许可证设置已变更或被环境变量锁定，已请求刷新状态；不会自动重试保存或清除。" };
    } else {
      // Do not display server error text: it could echo a submitted secret.
      notice.value = { type: "error", message: write
        ? "许可证写入未能确认，结果可能未知（包括超时或响应异常）。请先点击刷新设置确认状态，再决定是否重新保存或清除；不会自动重试。"
        : "无法加载浏览器后端设置或响应格式无效。请点击刷新设置重试；未保存的输入会保留。" };
    }
  } finally {
    clearTimeout(timer);
    if (active && request === generation) {
      busy.value = "";
      controller = null;
    }
  }
  if (conflict && active && request === generation) await run("read", true);
}

onMounted(() => { void run("read"); });
onUnmounted(() => {
  active = false;
  generation++;
  controller?.abort();
  draft.value = "";
});
</script>

<template>
  <el-card shadow="never" class="backend-panel">
    <template #header>
      <div class="backend-head">
        <strong>{{ settings?.backend === "cloak-manager" ? "CloakBrowser Manager" : "浏览器后端设置" }}</strong>
        <el-button :disabled="Boolean(busy)" :loading="busy === 'read'" @click="run('read')">刷新设置</el-button>
      </div>
    </template>
    <el-alert v-if="notice" :title="notice.message" :type="notice.type" :closable="false" show-icon class="notice" />
    <p v-if="!settings" class="muted" role="status">{{ busy ? "正在加载浏览器后端设置…" : "浏览器后端设置暂不可用。" }}</p>
    <p v-else-if="settings.backend === 'unsupported'" class="muted">当前浏览器后端不支持在此配置许可证。</p>
    <template v-else>
      <div class="backend-summary">
        <el-tag :type="settings.connected ? 'success' : 'danger'" effect="plain">{{ settings.connected ? "已连接" : "未连接" }}</el-tag>
        <span>许可证：{{ settings.license.configured ? "已配置" : "未配置" }}</span>
        <el-tag :type="settings.license.source === 'env' ? 'warning' : 'info'" effect="plain">
          {{ sourceLabels[settings.license.source] }}{{ settings.license.source === "env" ? " · 只读" : "" }}
        </el-tag>
        <span>许可等级：{{ tierLabel }}</span>
        <span>浏览器版本：{{ settings.binary_version ?? "未知 / 尚未下载" }}</span>
      </div>
      <el-alert v-if="licenseWarning" :title="licenseWarning" type="warning" :closable="false" show-icon class="notice" />
      <el-alert v-if="versionHint" :title="versionHint" type="info" :closable="false" show-icon class="notice" />
      <p v-if="settings.license.source === 'env'" class="muted">
        LICENSE_KEY 由环境变量锁定（部署变量 CLOAKBROWSER_LICENSE_KEY），只能在部署配置中修改。
      </p>
      <form class="license-form" @submit.prevent="run('save')">
        <label for="browser-license-key">LICENSE_KEY</label>
        <div class="license-controls">
          <el-input
            id="browser-license-key"
            v-model="draft"
            type="password"
            autocomplete="new-password"
            aria-label="LICENSE_KEY"
            placeholder="输入新的许可证，不会读取或显示现有密钥"
            :disabled="inputDisabled"
          />
          <el-button native-type="submit" type="primary" :disabled="!canSave" :loading="busy === 'save'">保存许可证</el-button>
          <el-button native-type="button" type="danger" plain :disabled="!canClear" :loading="busy === 'clear'" @click="run('clear')">清除许可证</el-button>
        </div>
      </form>
      <p class="muted backend-hint">仅保存非空的新密钥，空白输入不会清除；清除须单独确认。Manager 首次下载浏览器可能需要数分钟，请等待结果；不会自动启动浏览器或重试任务。</p>
      <p class="muted backend-hint">免费版仅允许 1 个同时运行的 profile。同一车道的多个标签页共享 profile；不同车道使用不同 profile，容量不足时进入队列等待释放，不代表任务并发数可增加许可容量。</p>
    </template>
  </el-card>
</template>

<style scoped>
.backend-panel {
  margin-bottom: 16px;
  border-radius: 16px;
  border-color: #e4e9f0;
}
.backend-head,
.backend-summary,
.license-controls {
  display: flex;
  align-items: center;
  gap: 12px;
}
.backend-head { justify-content: space-between; }
.backend-summary { flex-wrap: wrap; margin-bottom: 16px; font-size: 13px; }
.notice { margin-bottom: 14px; }
.license-form label { display: block; margin-bottom: 8px; color: #606266; font-size: 13px; font-weight: 600; }
.license-controls .el-input { flex: 1; min-width: 0; }
.backend-hint { margin: 12px 0 0; font-size: 12px; line-height: 1.7; overflow-wrap: anywhere; }
@media (max-width: 600px) {
  .license-controls { align-items: stretch; flex-direction: column; }
  .license-controls .el-button { margin-left: 0; }
}
</style>
