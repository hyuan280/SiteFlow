<script setup lang="ts">
import { computed, onBeforeUnmount, reactive, ref, watch } from "vue";
import { useRouter } from "vue-router";
import { ElMessageBox } from "element-plus";

import { ApiError, apiRequest } from "@/api/client";
import { useAuthStore } from "@/stores/auth";
import { assistantSettingBounds, isAssistantSettingsValues, isAssistantSettingsView, parseSettingsInteger } from "@/types/assistant-settings";
import type { AssistantSettingsValues, AssistantSettingsView } from "@/types/assistant-settings";

const props = defineProps<{ modelValue: boolean }>();
const emit = defineEmits<{ (event: "update:modelValue", value: boolean): void }>();
const auth = useAuthStore();
const router = useRouter();
const view = ref<AssistantSettingsView | null>(null);
const draft = reactive({ max_tool_calls: "", max_model_rounds: "", max_parallel_tool_calls: "",
  turn_timeout_seconds: "", max_tokens: "", context_max_tokens: "", auto_compact: false, auto_title: false,
  auto_authorize_execution: false, auto_authorize_screenshot: false, auto_authorize_workflow: false });
const inheritTokens = ref(true);
const loading = ref(false);
const busy = ref<"" | "refresh" | "save" | "reset">("");
const error = ref("");
const notice = ref("");
const needsRefresh = ref(false);
const sessionOpen = ref(false);
let session = 0;
let readController: AbortController | null = null;

const budgetFields = [
  { key: "max_tool_calls", label: "工具调用次数" },
  { key: "max_model_rounds", label: "模型轮数" },
  { key: "max_parallel_tool_calls", label: "每轮并行工具调用数" },
  { key: "turn_timeout_seconds", label: "单轮超时（秒）" },
] as const;
const values = computed<AssistantSettingsValues | null>(() => {
  const parsed = {
    ...Object.fromEntries(Object.entries(assistantSettingBounds).map(([key, [min, max]]) =>
      [key, parseSettingsInteger(draft[key as keyof typeof assistantSettingBounds], min, max)])),
    max_tokens: inheritTokens.value === true ? null : parseSettingsInteger(draft.max_tokens, 0, Number.MAX_SAFE_INTEGER, true),
    auto_compact: draft.auto_compact, auto_title: draft.auto_title,
    auto_authorize_execution: draft.auto_authorize_execution,
    auto_authorize_screenshot: draft.auto_authorize_screenshot,
    auto_authorize_workflow: draft.auto_authorize_workflow,
  };
  if (typeof inheritTokens.value !== "boolean" || (!inheritTokens.value && parsed.max_tokens === null)) return null;
  return isAssistantSettingsValues(parsed) ? parsed : null;
});
const canSave = computed(() => Boolean(sessionOpen.value && props.modelValue && view.value && values.value &&
  !loading.value && !busy.value && !needsRefresh.value));
const disabled = computed(() => loading.value || !!busy.value || !view.value);

function current(id: number): boolean {
  return sessionOpen.value && props.modelValue && session === id;
}

function invalidate(): void {
  session++;
  sessionOpen.value = false;
  readController?.abort();
  readController = null;
  loading.value = false;
  busy.value = "";
  view.value = null;
  error.value = "";
  notice.value = "";
  needsRefresh.value = false;
}

function apply(value: unknown): void {
  if (!isAssistantSettingsView(value)) throw new Error("Invalid assistant settings response");
  view.value = value;
  for (const key of Object.keys(assistantSettingBounds) as (keyof typeof assistantSettingBounds)[]) {
    draft[key] = String(value.values[key]);
  }
  draft.max_tokens = value.values.max_tokens === null ? "" : String(value.values.max_tokens);
  inheritTokens.value = value.values.max_tokens === null;
  draft.auto_compact = value.values.auto_compact;
  draft.auto_title = value.values.auto_title;
  draft.auto_authorize_execution = value.values.auto_authorize_execution;
  draft.auto_authorize_screenshot = value.values.auto_authorize_screenshot;
  draft.auto_authorize_workflow = value.values.auto_authorize_workflow;
  needsRefresh.value = false;
}

async function failure(cause: unknown, mutation = false): Promise<void> {
  if (cause instanceof ApiError && cause.status === 401) {
    invalidate();
    emit("update:modelValue", false);
    auth.clear();
    await router.replace({ name: "login" });
    return;
  }
  if (cause instanceof ApiError && cause.code === "assistant_settings_conflict") {
    needsRefresh.value = true;
    error.value = "设置已被其他客户端修改（assistant_settings_conflict）。草稿已保留，未自动重试；请刷新后重新编辑。";
  } else if (cause instanceof ApiError && cause.code === "assistant_settings_invalid") {
    error.value = "服务端助手设置无效（assistant_settings_invalid），请联系管理员。未使用默认值覆盖。";
  } else {
    error.value = mutation
      ? "操作失败或返回格式无效，服务端可能已保存。草稿已保留，未自动重试；请刷新核对后再操作。"
      : "无法加载助手设置或返回格式无效，请手动刷新重试。";
  }
  needsRefresh.value = true;
}

async function load(): Promise<void> {
  const id = session;
  const controller = new AbortController();
  readController = controller;
  loading.value = true;
  error.value = "";
  notice.value = "";
  try {
    const result = await apiRequest<unknown>("/assistant/settings", { signal: controller.signal });
    if (current(id)) apply(result);
  } catch (cause) {
    if (current(id)) await failure(cause);
  } finally {
    if (current(id)) { loading.value = false; readController = null; }
  }
}

async function refresh(): Promise<void> {
  if (!sessionOpen.value || !props.modelValue || loading.value || busy.value) return;
  const id = session;
  busy.value = "refresh";
  try {
    if (view.value) await ElMessageBox.confirm("刷新将丢弃当前草稿，并加载最新设置。是否继续？", "刷新助手设置", {
      type: "warning", confirmButtonText: "丢弃草稿并刷新", cancelButtonText: "保留草稿",
    });
    if (current(id)) await load();
  } catch { /* Confirmation cancelled; keep the draft. */ }
  finally { if (current(id)) busy.value = ""; }
}

async function save(): Promise<void> {
  if (!canSave.value || !view.value || !values.value) return;
  const id = session;
  const payload = { expected_revision: view.value.revision, values: values.value };
  busy.value = "save";
  error.value = "";
  notice.value = "";
  try {
    // Do not abort mutations: cancelling a request cannot roll back a committed save.
    const result = await apiRequest<unknown>("/assistant/settings", { method: "PUT", body: JSON.stringify(payload) });
    if (!current(id)) return;
    apply(result);
    notice.value = "助手设置已保存；仅对之后发送的新消息生效。";
  } catch (cause) {
    if (current(id)) await failure(cause, true);
  } finally { if (current(id)) busy.value = ""; }
}

async function reset(): Promise<void> {
  if (!sessionOpen.value || !props.modelValue || !view.value || loading.value || busy.value || needsRefresh.value) return;
  const id = session;
  const expected_revision = view.value.revision;
  busy.value = "reset";
  try {
    await ElMessageBox.confirm("恢复全局默认设置将丢弃当前草稿。仅影响之后发送的新消息，正在运行的任务不变。是否继续？", "恢复默认设置", {
      type: "warning", confirmButtonText: "恢复默认", cancelButtonText: "取消",
    });
  } catch {
    if (current(id)) busy.value = "";
    return;
  }
  if (!current(id)) return;
  error.value = "";
  notice.value = "";
  try {
    const result = await apiRequest<unknown>("/assistant/settings/reset", { method: "POST", body: JSON.stringify({ expected_revision }) });
    if (!current(id)) return;
    apply(result);
    notice.value = "已恢复全局默认设置；仅对之后发送的新消息生效。";
  } catch (cause) {
    if (current(id)) await failure(cause, true);
  } finally { if (current(id)) busy.value = ""; }
}

function close(): void {
  if (busy.value) return;
  invalidate();
  emit("update:modelValue", false);
}

watch(() => props.modelValue, open => {
  invalidate();
  if (open) { sessionOpen.value = true; void load(); }
}, { immediate: true, flush: "sync" });
onBeforeUnmount(invalidate);
</script>

<template>
  <el-dialog :model-value="modelValue" title="助手设置" width="min(680px, 94vw)" top="5vh"
    :close-on-click-modal="!busy" :close-on-press-escape="!busy" :show-close="!busy" @update:model-value="close">
    <el-alert type="info" :closable="false" show-icon
      title="全局设置仅对之后发送的新消息生效，正在运行的任务不变。不会改变网站目标确认要求或扩大凭据访问范围；能力默认授权按下方选项生效，其他安全限制保持不变。本对话框不会调用模型。" />
    <p v-if="view" class="muted" data-testid="settings-revision">来源：{{ view.source === 'default' ? '默认值' : '数据库' }} · 修订：{{ view.revision }}</p>
    <p v-if="loading" role="status">正在加载助手设置...</p>
    <el-form v-if="view" label-position="top" :disabled="disabled" @submit.prevent="save">
      <h3>执行预算</h3>
      <div class="settings-grid">
        <el-form-item v-for="field in budgetFields" :key="field.key" :label="`${field.label}（${assistantSettingBounds[field.key][0]}..${assistantSettingBounds[field.key][1]}）`">
          <el-input v-model="draft[field.key]" inputmode="numeric" :aria-label="field.label" />
        </el-form-item>
      </div>
      <h3>模型与上下文</h3>
      <el-form-item label="模型输出上限 max_tokens">
        <div class="token-controls">
          <el-checkbox v-model="inheritTokens">继承模型配置（发送 null）</el-checkbox>
          <el-input v-model="draft.max_tokens" :disabled="disabled || inheritTokens" inputmode="numeric" aria-label="max_tokens" placeholder="留空或 0：不发送输出上限" />
        </div>
      </el-form-item>
      <p class="muted">取消继承后，留空或 0 表示不发送 max_tokens；正整数指定输出上限，最大 9007199254740991。只接受十进制整数，不接受小数或科学计数法。</p>
      <el-form-item label="上下文预算（token，16000..1000000）">
        <el-tooltip content="默认 64000 token。发送前使用包含系统提示、工具定义及图像标记的本地保守估算；回复后显示供应商报告的输入加输出 token，不是累计用量。max_tokens 仅限制输出，不代表上下文容量。">
          <el-input v-model="draft.context_max_tokens" inputmode="numeric" aria-label="上下文预算（token）" />
        </el-tooltip>
      </el-form-item>
      <div class="settings-toggles">
        <el-switch v-model="draft.auto_compact" active-text="自动整理上下文" />
        <el-switch v-model="draft.auto_title" active-text="自动生成会话标题" />
      </div>
      <h3>全局授权默认值</h3>
      <p class="muted">仅为对应能力提供默认授权。目标确认、信任与代理授权始终需要显式确认，不会因此跳过；其他安全限制保持不变。</p>
      <el-checkbox v-model="draft.auto_authorize_execution">默认授权执行步骤（仅执行能力）</el-checkbox>
      <el-checkbox v-model="draft.auto_authorize_screenshot">默认授权发送截图（仅截图能力）</el-checkbox>
      <el-checkbox v-model="draft.auto_authorize_workflow" class="workflow-authorization">默认授权导入保存配置并触发真实完整工作流运行</el-checkbox>
      <p class="muted">此工作流授权同时允许实际保存配置和触发目标站点的完整任务运行，可能使用已保存凭据或 Cookie，执行真实登录、签到、点击并产生费用或其他业务副作用。即使关闭“默认授权执行步骤”仍然有效：该开关仅控制助手的单步验证，不限制已授权的完整工作流运行。目标确认、凭据范围及其他安全限制仍由服务端执行。</p>
      <p v-if="!values" class="error-text" role="alert">请输入各项范围内的整数；max_tokens 仅可留空、0 或 JavaScript 安全范围内的正整数。</p>
    </el-form>
    <p v-if="error" class="error-text" role="alert">{{ error }}</p>
    <p v-if="notice" class="success-text" role="status">{{ notice }}</p>
    <p v-if="busy === 'save' || busy === 'reset'" class="muted" role="status">正在确认或提交设置，请等待。请求取消不能撤销服务端保存。</p>
    <template #footer>
      <div class="settings-footer">
        <el-button :disabled="disabled || needsRefresh" :loading="busy === 'reset'" @click="reset">恢复默认</el-button>
        <el-button :disabled="loading || !!busy" @click="refresh">刷新设置</el-button>
        <el-button :disabled="!!busy" @click="close">关闭</el-button>
        <el-button type="primary" :disabled="!canSave" :loading="busy === 'save'" @click="save">保存设置</el-button>
      </div>
    </template>
  </el-dialog>
</template>

<style scoped>
h3 { margin: 22px 0 14px; font-size: 15px; }
.settings-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0 18px; }
.token-controls { width: 100%; }
.settings-toggles, .settings-footer { display: flex; flex-wrap: wrap; gap: 12px; }
.settings-toggles { margin: 18px 0; }
.settings-footer { justify-content: flex-end; }
.settings-footer .el-button { margin-left: 0; }
.muted { font-size: 13px; line-height: 1.6; overflow-wrap: anywhere; }
.error-text { color: #c45656; font-size: 13px; overflow-wrap: anywhere; }
.success-text { color: #529b2e; font-size: 13px; }
.workflow-authorization { height: auto; }
.workflow-authorization :deep(.el-checkbox__label) { white-space: normal; line-height: 1.6; }
@media (max-width: 540px) { .settings-grid { grid-template-columns: minmax(0, 1fr); } }
</style>
