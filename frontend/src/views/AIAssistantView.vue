<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from "vue";
import { useRouter } from "vue-router";
import { ElMessageBox } from "element-plus";

import AssistantSettingsDialog from "@/components/AssistantSettingsDialog.vue";
import AssistantTakeoverDialog from "@/components/AssistantTakeoverDialog.vue";
import AssistantMarkdown from "@/components/AssistantMarkdown.vue";
import AssistantInteractionReview from "@/components/AssistantInteractionReview.vue";
import PageHeader from "@/components/PageHeader.vue";
import { interactionPurposeLabel, siteConfigResult, operationResultLabel } from "@/utils/assistant-review";
import { createAssistantMessageCache } from "@/utils/assistant-message-cache";
import { ApiError, apiRequest } from "@/api/client";
import { useAuthStore } from "@/stores/auth";
import { assistantRunLanePolicies, isAssistantDetail, isAssistantPendingInteraction, isAssistantSessionList, isAssistantScreenshot, isAssistantUnresolvedOperation } from "@/types/assistant";
import type { AssistantDetail, AssistantMessage, AssistantSession, AssistantUnresolvedOperation } from "@/types/assistant";

const auth = useAuthStore();
const router = useRouter();
const settingsOpen = ref(false);
const takeoverOpen = ref(false);
const sessions = ref<AssistantSession[]>([]);
const selectedId = ref("");
const detail = ref<AssistantDetail | null>(null);
const messageCache = createAssistantMessageCache();
const openMessageIds = reactive(new Set<number>());
function toggleMessage(id: number, event: Event): void {
  if ((event.currentTarget as HTMLDetailsElement).open) openMessageIds.add(id);
  else openMessageIds.delete(id);
}
const listLoading = ref(false);
const moreLoading = ref(false);
const nextOffset = ref<number | null>(null);
let historyPaged = false;
const detailLoading = ref(false);
const listError = ref("");
const error = ref("");
const connection = ref<"connecting" | "live" | "offline">("connecting");
const content = ref("");
type Mutation = "send" | "cancel" | "title" | "refresh" | "retry";
const action = ref<"" | Mutation | "delete" | "screenshot">("");
const cancelBusy = ref(false);
const approvalBusy = ref(false);
const approvalNow = ref(Date.now());
// Deduplicate only within this selection; reselecting a still-pending session prompts again.
const settledApprovals = reactive(new Set<string>());
const expiredApprovalReads = new Set<string>();
let approvalTimer: ReturnType<typeof setInterval> | undefined;
const screenshotOpen = ref(false);
const screenshotPreview = ref<{ src: string; width: number; height: number } | null>(null);
// Only a validated approval response settles a request; SSE grant state cannot correlate its ID.
const retryRequests = new Map<string, string>();
let screenshotRequest = 0;
const titleDialog = ref(false);
const titleInput = ref("");
const creating = ref(false);
const createError = ref("");
const usageOpen = ref(false);
const safetyDismissalKey = "siteflow.assistant.safety.v1";
const safetyVisible = ref(true);
function dismissSafety(): void {
  safetyVisible.value = false;
  try { localStorage.setItem(safetyDismissalKey, "dismissed"); } catch { /* Storage may be blocked. */ }
}
let alive = true;
let epoch = 0;
let listRequest = 0;
let detailRequest = 0;
let snapshot = 0;
let stream: EventSource | null = null;
let sendAccepted = false;

const running = computed(() => detail.value?.status === "running");
watch(running, value => {
  if (value && sendAccepted && action.value === "send") action.value = "";
}, { flush: "sync" });
const pendingApproval = computed(() => {
  const approval = detail.value?.pending_approval;
  return running.value && detail.value?.id === selectedId.value && approval &&
    !settledApprovals.has(`${selectedId.value}:${approval.id}`) ? approval : null;
});
const approvalKey = computed(() => pendingApproval.value ? `${selectedId.value}:${pendingApproval.value.id}` : "");
const approvalSeconds = computed(() => pendingApproval.value
  ? Math.max(0, Math.ceil((Date.parse(pendingApproval.value.expires_at) - approvalNow.value) / 1000)) : 0);
const approvalExpired = computed(() => Boolean(pendingApproval.value && approvalNow.value >= Date.parse(pendingApproval.value.expires_at)));
function clearApprovalTimer(): void {
  if (approvalTimer !== undefined) clearInterval(approvalTimer);
  approvalTimer = undefined;
}
function updateApprovalClock(): void {
  approvalNow.value = Date.now();
  if (!approvalExpired.value) return;
  clearApprovalTimer();
  const key = approvalKey.value;
  if (key && !expiredApprovalReads.has(key)) {
    expiredApprovalReads.add(key);
    // The server owns timeout feedback and resolution. Never submit a timeout decision.
    void refreshDetail();
  }
}
watch(approvalKey, (key, previous) => {
  clearApprovalTimer();
  if (previous) settledApprovals.add(previous);
  if (!key) return;
  approvalNow.value = Date.now();
  if (!approvalExpired.value) approvalTimer = setInterval(updateApprovalClock, 1000);
  else updateApprovalClock();
}, { flush: "sync" });
const browserReady = computed(() => detail.value?.browser?.status === "ready");
const canUseBrowser = computed(() => Boolean(detail.value && !detailLoading.value && !running.value && !action.value && browserReady.value));
const canScreenshot = computed(() => canUseBrowser.value && detail.value?.target?.screenshot_consent === true);
const recoveryOperations = computed(() => {
  const current = detail.value;
  if (!current) return [];
  const operations = new Map<string, AssistantUnresolvedOperation>();
  if (current.unresolved_operations !== undefined) {
    for (const item of current.unresolved_operations) operations.set(item.operation_id.toLowerCase(), item);
  } else {
    for (const message of current.messages) {
      if (message.role !== "tool" || !["execute_step", "retry_step"].includes(message.tool_name ?? "")) continue;
      try {
        const result = messageCache.parse(message);
        const item = { operation_id: result.operation_id, status: result.status, code: result.code,
          can_authorize: result.can_authorize === true, ...(typeof result.feature_type === "string" ? { feature_type: result.feature_type } : {}) };
        if (isAssistantUnresolvedOperation(item)) operations.set(item.operation_id.toLowerCase(), item);
      } catch { /* Legacy tool content may not be JSON. */ }
    }
  }
  return [...operations.values()].map(item => {
    const authorization = current.retry_authorizations?.find(grant => grant.operation_id.toLowerCase() === item.operation_id.toLowerCase());
    return { ...item, authorization, canRequestAuthorization: item.can_authorize && authorization?.status !== "approved" &&
      (current.unresolved_operations !== undefined || authorization?.status !== "used") };
  });
});
const usageKinds = { conversation: "对话", summary: "摘要", title: "标题" };
const usageMetrics = [
  { key: "prompt_tokens", label: "输入" }, { key: "completion_tokens", label: "输出" },
  { key: "cached_tokens", label: "缓存命中" }, { key: "reasoning_tokens", label: "思考" },
  { key: "total_tokens", label: "总计" },
] as const;
const tokenText = (value: number | null | undefined): string => value == null ? "--" : String(value);
const contextPercent = computed(() => {
  const usage = detail.value?.usage;
  // Legacy snapshots used input-only context tokens; do not mislabel them as the new numerator.
  if (usage?.last_context_tokens == null || usage.last_context_prompt_tokens == null ||
    usage.last_context_completion_tokens == null || !usage.last_context_budget_tokens) return null;
  return usage.last_context_tokens / usage.last_context_budget_tokens * 100;
});
const contextPercentText = computed(() => contextPercent.value === null ? "--" : `${Math.round(contextPercent.value)}%`);
const contextCircle = computed(() => `${Math.min(100, Math.max(0, contextPercent.value ?? 0))}%`);
const contextUsageStale = computed(() => detail.value?.usage?.last_context_usage_missing === true);
const contextUsageLabel = computed(() => contextUsageStale.value ? "上次已知（非当前）" : "本次上下文");
const contextUsageTooltip = computed(() => `${contextUsageLabel.value}用量 ${contextPercentText.value}，输入 + 输出 ${tokenText(detail.value?.usage?.last_context_tokens)} / 预算 ${tokenText(detail.value?.usage?.last_context_budget_tokens)} Token${contextUsageStale.value ? "；最近调用未返回完整用量，不代表当前上下文" : ""}，查看用量明细`);
const interactionBusy = ref(false);
const interactionNow = ref(Date.now());
const settledInteractions = reactive(new Set<string>());
const expiredInteractionReads = new Set<string>();
const interactionSelections = ref<string[]>([]);
const interactionValues = ref<Record<string, string>>({});
let interactionTimer: ReturnType<typeof setInterval> | undefined;
const pendingInteraction = computed(() => {
  const item = detail.value?.pending_interaction;
  return running.value && detail.value?.id === selectedId.value && item &&
    !settledInteractions.has(`${selectedId.value}:${item.id}`) ? item : null;
});
const interactionKey = computed(() => pendingInteraction.value ? `${selectedId.value}:${pendingInteraction.value.id}` : "");
const interactionExpired = computed(() => Boolean(pendingInteraction.value && interactionNow.value >= Date.parse(pendingInteraction.value.expires_at)));
const interactionSeconds = computed(() => pendingInteraction.value
  ? Math.max(0, Math.ceil((Date.parse(pendingInteraction.value.expires_at) - interactionNow.value) / 1000)) : 0);
const interactionValid = computed(() => {
  const item = pendingInteraction.value;
  if (!item || !isAssistantPendingInteraction(item)) return false;
  const options = item.options ?? [];
  const fields = item.fields ?? [];
  return (!options.length || (interactionSelections.value.length > 0 &&
    (item.multiple || interactionSelections.value.length === 1) &&
    new Set(interactionSelections.value).size === interactionSelections.value.length &&
    interactionSelections.value.every(id => options.some(option => option.id === id)))) &&
    fields.every(field => (!field.required || interactionValues.value[field.id]?.trim()) && (interactionValues.value[field.id]?.length ?? 0) <= 2000) &&
    Object.keys(interactionValues.value).every(id => fields.some(field => field.id === id));
});
function clearInteractionTimer(): void {
  if (interactionTimer !== undefined) clearInterval(interactionTimer);
  interactionTimer = undefined;
}
function updateInteractionClock(): void {
  interactionNow.value = Date.now();
  if (!interactionExpired.value) return;
  clearInteractionTimer();
  const key = interactionKey.value;
  if (key && !expiredInteractionReads.has(key)) {
    expiredInteractionReads.add(key);
    void refreshDetail();
  }
}
watch(interactionKey, (key, previous) => {
  clearInteractionTimer();
  if (previous) settledInteractions.add(previous);
  interactionSelections.value = [];
  interactionValues.value = {};
  if (!key) return;
  legacyRetry.value = null;
  interactionNow.value = Date.now();
  if (!interactionExpired.value) interactionTimer = setInterval(updateInteractionClock, 1000);
  else updateInteractionClock();
}, { flush: "sync" });
const legacyRetry = ref<{ operationId: string; target: string } | null>(null);
const legacyRetryOperation = computed(() => recoveryOperations.value.find(item => item.operation_id === legacyRetry.value?.operationId));
watch(() => [running.value, JSON.stringify(detail.value?.target), legacyRetryOperation.value?.canRequestAuthorization], () => {
  if (legacyRetry.value && (running.value || legacyRetry.value.target !== JSON.stringify(detail.value?.target) || !legacyRetryOperation.value?.canRequestAuthorization)) legacyRetry.value = null;
}, { flush: "sync" });
const contextTip = "较早历史会自动整理为有损的模型摘要，不构成授权或事实依据；完整聊天与工具记录仍保留。需要细节时应按消息引用重新读取原始工具结果；浏览器元素引用需重新观察确认。";
const contextLimitText = "上下文仍超过可保护预算，请缩小本轮需求或新建会话；旧记录保留";
const canSend = computed(() => Boolean(detail.value && !detailLoading.value && !running.value && !action.value &&
  content.value.trim() && content.value.length <= 12000 && !pendingInteraction.value && !legacyRetry.value));
const canSaveTitle = computed(() => Boolean(detail.value && !detailLoading.value && !running.value && !action.value &&
  titleInput.value.trim() && titleInput.value.length <= 128));

function clearScreenshot(): void {
  screenshotRequest++;
  screenshotPreview.value = null;
  screenshotOpen.value = false;
}
watch(screenshotOpen, open => { if (!open) clearScreenshot(); }, { flush: "sync" });
watch(() => JSON.stringify(detail.value?.target), clearScreenshot, { flush: "sync" });

function closeStream(): void {
  stream?.close();
  stream = null;
}

function resetSelection(id = ""): void {
  epoch++;
  detailRequest++;
  snapshot = 0;
  closeStream();
  selectedId.value = id;
  detail.value = null;
  messageCache.clear();
  openMessageIds.clear();
  detailLoading.value = false;
  content.value = "";
  action.value = "";
  sendAccepted = false;
  error.value = "";
  titleDialog.value = false;
  titleInput.value = "";
  usageOpen.value = false;
  legacyRetry.value = null;
  clearInteractionTimer();
  settledInteractions.clear();
  expiredInteractionReads.clear();
  interactionBusy.value = false;
  cancelBusy.value = false;
  clearScreenshot();
  retryRequests.clear();
  clearApprovalTimer();
  settledApprovals.clear();
  expiredApprovalReads.clear();
  approvalBusy.value = false;
  connection.value = "connecting";
}

async function handleFailure(cause: unknown, publish: (message: string) => void): Promise<void> {
  if (cause instanceof ApiError && cause.status === 401) {
    resetSelection();
    listRequest++;
    sessions.value = [];
    nextOffset.value = null;
    listLoading.value = moreLoading.value = false;
    auth.clear();
    await router.replace({ name: "login" });
    return;
  }
  const messages: Record<string, string> = {
    generation_disabled: "智能助手未启用，请联系管理员检查 SITEFLOW_CONFIG_AI_ENABLED。",
    model_unavailable: "模型不可用，请检查支持工具调用的模型配置。",
    assistant_busy: "助手服务繁忙，请稍后手动重试。",
    assistant_stopping: "助手服务正在停止，请稍后刷新。",
    session_running: "会话正在运行，请等待完成或明确停止后再操作。",
    session_not_found: "会话不存在或已删除，请刷新会话列表。",
    secret_input: "输入可能包含敏感信息，请手动脱敏后再发送。",
    invalid_request: "请求格式无效，请检查输入长度、公共网址及授权选项。",
    request_limit: "请求超过服务端大小限制，请缩短输入。",
    request_timeout: "请求超时，请先刷新核对会话状态。",
    provider_timeout: "模型服务超时，请先刷新核对会话状态。",
    provider_network: "无法连接模型服务，请稍后手动重试。",
    provider_http: "模型服务拒绝请求，请检查服务配置。",
    invalid_response: "模型响应不符合工具调用协议，请检查模型配置。",
    context_limit: contextLimitText,
    invalid_execution_features: "旧单步功能授权无效，请让助手发起交互式授权请求。",
    execution_features_required: "旧单步功能授权已失效，请让助手发起交互式授权请求。",
    credential_site_required: "使用已保存凭据需要绑定已有站点，请先在站点管理中保存凭据，或改用 Bitwarden。",
    credential_site_mismatch: "所选站点与目标网址不同源或已不存在，请重新选择同源站点。",
    authorization_required: "请让助手发起交互式授权请求。",
    execution_consent_required: "请让助手发起交互式授权请求，以允许单步测试。",
    sign_consent_required: "请让助手发起交互式授权请求，签到、点击或验证可能产生实际业务操作。",
    credential_consent_required: "请让助手发起交互式授权请求；凭据由服务端私下提供，不会进入聊天或模型。",
    credentials_unavailable: "无法取得可用凭据，请检查所绑定站点的已保存凭据或 Bitwarden 域名匹配配置。",
    screenshot_consent_required: "请让助手发起交互式图像授权请求，核对后决定是否发送。",
    browser_reopen_required: "请先通过聊天让助手打开浏览器。",
    cookie_site_required: "清除 Cookie 需要明确绑定已保存站点，不会按主机查找其他站点。",
    cookie_binding_changed: "Cookie 站点绑定已失效，请重新确认目标；不会回退到其他站点。",
    cookie_restore_unavailable: "无法恢复已保存 Cookie，浏览器未继续导航，请核对绑定站点的 Cookie 配置。",
    cookie_clear_binding_changed: "Cookie 清除范围或绑定已改变，原批准不可复用，请重新发起完整确认。",
    cookie_clear_approval_required: "清除 Cookie 需要核对服务端提供的范围并人工批准。",
    cookie_clear_internal_only: "Cookie 清除确认只能由专用工具发起，请让助手重新请求。",
    cookie_clear_operation_conflict: "此 Cookie 清除操作已处理，请核对原结果，不要重复执行。",
    browser_outcome_unknown: "已清除站点保存的 Cookie；浏览器 Cookie 清除结果未知，不代表全部成功，不要自动重试。",
    browser_scope_changed: "已清除站点保存的 Cookie；浏览器范围已改变，浏览器 Cookie 清除结果未知，不要自动重试。",
    site_run_active: "该站点有排队或运行中的任务，暂不能清除 Cookie。",
    retry_step_unavailable: "此旧记录没有可安全重试的冻结步骤，无法授权重试。",
    retry_binding_changed: "目标或凭据绑定已改变，不能重试原操作。",
    retry_not_uncertain: "此操作已不在结果不确定的记录中，请刷新会话核对。",
    retry_already_used: "此操作的一次重试授权已使用，请刷新会话核对。",
  };
  const code = cause instanceof ApiError && cause.code && /^[a-z_]{1,128}$/.test(cause.code) ? cause.code : "";
  const message = code && Object.hasOwn(messages, code) ? messages[code] : "操作失败，请刷新核对会话状态后手动重试。";
  publish(`${message}${code ? `（${code}）` : ""} 未自动重试或重发消息。`);
}

type TitleFields = Pick<AssistantSession, "title" | "title_locked" | "title_revision">;

function preserveTitle<T extends TitleFields>(value: T, current?: TitleFields | null): T {
  if (!current || value.title_revision >= current.title_revision) return value;
  return { ...value, title: current.title, title_locked: current.title_locked, title_revision: current.title_revision };
}

function preserveContext<T extends Pick<AssistantSession, "context">>(value: T, current?: Pick<AssistantSession, "context"> | null): T {
  // Equal revisions use the incoming frame: target changes can clear the summary without a revision bump.
  if ((value.context?.revision ?? 0) >= (current?.context?.revision ?? 0)) return value;
  return { ...value, context: current?.context };
}

function preserveUsage(value: AssistantDetail, current: AssistantDetail | null): AssistantDetail {
  if ((value.usage?.revision ?? 0) >= (current?.usage?.revision ?? 0)) return value;
  return { ...value, usage: current?.usage };
}

function applySnapshot(value: unknown, id: string, preserveProgress = false): void {
  if (!isAssistantDetail(value) || value.id !== id) throw new Error("Invalid assistant snapshot");
  const current = detail.value;
  const listed = sessions.value.find(item => item.id === id);
  let next = preserveUsage(preserveContext(preserveContext(preserveTitle(preserveTitle(value, listed), current), listed), current), current);
  if (current) {
    const last = current.messages.at(-1)?.id ?? 0;
    const statuses = new Map(value.messages.map(message => [message.id, message.status]));
    // Title, context and usage revisions are independent of message/tool progress.
    if (preserveProgress || (value.messages.at(-1)?.id ?? 0) < last ||
      current.messages.some(message => message.status && message.status !== "running" &&
        statuses.get(message.id) === "running") ||
      ((value.messages.at(-1)?.id ?? 0) === last && current.status === "idle" && value.status === "running")) {
      next = { ...current, title: next.title, title_locked: next.title_locked, title_revision: next.title_revision, context: next.context, usage: next.usage };
    }
    if (next.pending_approval && settledApprovals.has(`${id}:${next.pending_approval.id}`) && current.pending_approval?.id !== next.pending_approval.id) {
      next = { ...next, pending_approval: current.pending_approval };
    }
    if (next.pending_interaction && settledInteractions.has(`${id}:${next.pending_interaction.id}`) && current.pending_interaction?.id !== next.pending_interaction.id) {
      next = { ...next, pending_interaction: current.pending_interaction };
    }
  }
  detail.value = next;
  messageCache.prune(next.messages);
  const messageIds = new Set(next.messages.map(message => message.id));
  for (const id of openMessageIds) if (!messageIds.has(id)) openMessageIds.delete(id);
  snapshot++;
  const contextUnchanged = listed?.context?.revision === next.context?.revision &&
    listed?.context?.compressed === next.context?.compressed &&
    listed?.context?.summary_through_message_id === next.context?.summary_through_message_id;
  if (listed && (listed.title !== next.title || listed.title_locked !== next.title_locked ||
    listed.title_revision !== next.title_revision || listed.status !== next.status || !contextUnchanged)) {
    sessions.value = sessions.value.map(item => item.id === id ? { ...item, title: next.title,
      title_locked: next.title_locked, title_revision: next.title_revision, status: next.status, context: next.context } : item);
  }
}

function connect(id: string, currentEpoch: number): void {
  closeStream();
  connection.value = "connecting";
  const source = new EventSource(`/api/v1/assistant/sessions/${encodeURIComponent(id)}/events`, { withCredentials: true });
  stream = source;
  let lastData: string | undefined;
  const current = () => alive && epoch === currentEpoch && stream === source;
  source.addEventListener("update", event => {
    if (!current()) return;
    const data = (event as MessageEvent<string>).data;
    if (data === lastData) { connection.value = "live"; return; }
    try {
      applySnapshot(JSON.parse(data), id);
      lastData = data;
      connection.value = "live";
    } catch {
      error.value = "收到的会话更新格式无效，未显示该更新。请刷新核对。";
    }
  });
  source.onopen = () => { if (current()) connection.value = "live"; };
  source.onerror = () => {
    if (!current()) return;
    connection.value = "offline";
    // EventSource hides HTTP status. A read checks auth; native SSE alone reconnects.
    void refreshDetail(true);
  };
}

async function refreshDetail(preserveProgress = false): Promise<void> {
  const id = selectedId.value;
  if (!alive || !id) return;
  const currentEpoch = epoch;
  const request = ++detailRequest;
  const revision = snapshot;
  detailLoading.value = true;
  try {
    const value = await apiRequest<unknown>(`/assistant/sessions/${encodeURIComponent(id)}`);
    if (!alive || epoch !== currentEpoch || request !== detailRequest) return;
    applySnapshot(value, id, preserveProgress || revision !== snapshot);
    if (!stream && detail.value) connect(id, currentEpoch);
  } catch (cause) {
    if (!alive || epoch !== currentEpoch || request !== detailRequest) return;
    await handleFailure(cause, message => { error.value = message; });
  } finally {
    if (alive && epoch === currentEpoch && request === detailRequest) detailLoading.value = false;
  }
}

async function selectSession(id: string): Promise<void> {
  if (!alive || creating.value) return;
  resetSelection(id);
  await refreshDetail();
}

async function loadSessions(append = false): Promise<void> {
  if (!alive || listLoading.value || (append && (moreLoading.value || nextOffset.value === null))) return;
  const request = ++listRequest;
  const currentEpoch = epoch;
  const offset = append ? nextOffset.value! : 0;
  listLoading.value = !append;
  moreLoading.value = append;
  listError.value = "";
  try {
    const value = await apiRequest<unknown>(append
      ? `/assistant/sessions?offset=${offset}&limit=100` : "/assistant/sessions");
    if (!alive || request !== listRequest) return;
    if (!isAssistantSessionList(value) ||
      (value.offset !== undefined && (value.offset !== offset || value.limit !== 100))) {
      throw new Error("Invalid assistant sessions");
    }
    const previousItems = new Map(sessions.value.map(item => [item.id.toLowerCase(), item]));
    const incoming = value.items.map(item => {
      const previous = previousItems.get(item.id.toLowerCase());
      const next = preserveContext(preserveTitle(item, previous), previous);
      return detail.value?.id === item.id
         ? { ...preserveContext(preserveTitle(next, detail.value), detail.value), status: detail.value.status } : next;
    });
    // Only explicit exhaustion proves the head is the whole list; legacy heads were capped.
    const exhaustive = !append && value.next_offset === null;
    const merged = exhaustive ? incoming : append ? [...sessions.value, ...incoming] : [...incoming, ...sessions.value];
    const unique = new Map<string, AssistantSession>();
    for (const item of merged) {
      const key = item.id.toLowerCase();
      if (append || !unique.has(key)) unique.set(key, item);
    }
    sessions.value = [...unique.values()];
    if (exhaustive) historyPaged = false;
    if (append || !historyPaged) nextOffset.value = value.next_offset ?? null;
    if (append) historyPaged = true;
    if (detail.value) {
      const listed = sessions.value.find(item => item.id === detail.value?.id);
      detail.value = { ...preserveTitle(detail.value, listed), context: listed?.context ?? detail.value.context };
    }
    if (!append && !selectedId.value && currentEpoch === epoch && !creating.value && value.items[0]) {
      await selectSession(value.items[0].id);
    }
  } catch (cause) {
    if (!alive || request !== listRequest) return;
    await handleFailure(cause, message => { listError.value = message; });
  } finally {
    if (alive && request === listRequest) listLoading.value = moreLoading.value = false;
  }
}

async function createSession(): Promise<void> {
  if (!alive || creating.value) return;
  const currentEpoch = epoch;
  creating.value = true;
  createError.value = "";
  try {
    const value = await apiRequest<unknown>("/assistant/sessions", {
      method: "POST", body: JSON.stringify({}),
    });
    if (!alive || epoch !== currentEpoch) return;
    if (!isAssistantDetail(value)) throw new Error("Invalid assistant session");
    resetSelection(value.id);
    applySnapshot(value, value.id);
    connect(value.id, epoch);
    listRequest++;
    listLoading.value = moreLoading.value = false;
    void loadSessions();
  } catch (cause) {
    if (!alive || epoch !== currentEpoch) return;
    await handleFailure(cause, message => { createError.value = message; });
  } finally {
    if (alive) creating.value = false;
  }
}

// Mutation responses may predate SSE; title revisions and the fresh read guard prevent regression.
async function mutate(kind: Mutation, payload: unknown, latched = false): Promise<boolean> {
  if (!alive || !detail.value || (latched ? action.value !== kind : Boolean(action.value))) return false;
  const id = selectedId.value;
  const currentEpoch = epoch;
  action.value = kind;
  if (kind === "send") sendAccepted = false;
  error.value = "";
  try {
    const path = kind === "send" ? "messages" : kind === "refresh" ? "browser/refresh" : kind === "retry" ? "retry-authorizations" : kind;
    const revision = snapshot;
    const value = await apiRequest<unknown>(`/assistant/sessions/${encodeURIComponent(id)}/${path}`, {
      method: kind === "title" ? "PUT" : "POST", body: JSON.stringify(payload),
    });
    if (!alive || epoch !== currentEpoch) return false;
    if (kind === "send") {
      content.value = "";
      // HTTP acceptance is settled; a running tool may already be waiting for approval via SSE.
      sendAccepted = true;
      if (running.value) action.value = "";
    }
    if (kind === "title") {
      applySnapshot(value, id);
      titleDialog.value = false;
    }
    if (kind === "refresh" || kind === "retry") applySnapshot(value, id, revision !== snapshot);
    if (kind === "retry" && payload && typeof payload === "object" && "operation_id" in payload && typeof payload.operation_id === "string") {
      retryRequests.delete(payload.operation_id.toLowerCase());
    }
    await refreshDetail();
    if (alive && epoch === currentEpoch) void loadSessions();
    return alive && epoch === currentEpoch;
  } catch (cause) {
    if (!alive || epoch !== currentEpoch) return false;
    await handleFailure(cause, message => { error.value = message; });
    return false;
  } finally {
    if (alive && epoch === currentEpoch && action.value === kind) action.value = "";
  }
}

async function takeScreenshot(): Promise<void> {
  if (!alive || !canScreenshot.value) return;
  const id = selectedId.value;
  const currentEpoch = epoch;
  clearScreenshot();
  const request = ++screenshotRequest;
  action.value = "screenshot";
  error.value = "";
  screenshotOpen.value = true;
  try {
    const value = await apiRequest<unknown>(`/assistant/sessions/${encodeURIComponent(id)}/browser/screenshot`, {
      method: "POST", body: JSON.stringify({}),
    });
    if (!alive || epoch !== currentEpoch || request !== screenshotRequest || !screenshotOpen.value) return;
    if (!isAssistantScreenshot(value)) throw new Error("Invalid screenshot");
    screenshotPreview.value = { src: `data:image/jpeg;base64,${value.image_base64}`, width: value.width, height: value.height };
  } catch (cause) {
    if (!alive || epoch !== currentEpoch || request !== screenshotRequest) return;
    clearScreenshot();
    await handleFailure(cause, message => { error.value = message; });
  } finally {
    if (alive && epoch === currentEpoch) action.value = "";
  }
}

async function refreshBrowser(): Promise<void> {
  if (!alive || !canUseBrowser.value) return;
  const currentEpoch = epoch;
  const target = JSON.stringify(detail.value?.target);
  action.value = "refresh";
  try {
    await ElMessageBox.confirm("将以 GET 重新加载当前网页，不会重提 POST 表单。但 GET 请求也可能产生业务副作用。刷新或截图不能证明之前的操作失败。确认刷新网页？", "刷新网页", {
      type: "warning", confirmButtonText: "刷新网页", cancelButtonText: "取消",
    });
  } catch {
    if (alive && epoch === currentEpoch) action.value = "";
    return;
  }
  if (!alive || epoch !== currentEpoch) return;
  if (running.value || !browserReady.value || target !== JSON.stringify(detail.value?.target)) { action.value = ""; return; }
  clearScreenshot();
  await mutate("refresh", {}, true);
}

async function authorizeRetry(operationId: string): Promise<void> {
  const item = recoveryOperations.value.find(item => item.operation_id === operationId);
  if (!alive || detail.value?.pending_approval || pendingInteraction.value || !canUseBrowser.value || !item?.canRequestAuthorization || legacyRetry.value) return;
  legacyRetry.value = { operationId, target: JSON.stringify(detail.value?.target) };
}

async function confirmLegacyRetry(): Promise<void> {
  const prompt = legacyRetry.value;
  const item = legacyRetryOperation.value;
  if (!alive || !prompt || !canUseBrowser.value || detail.value?.pending_approval || pendingInteraction.value ||
    !item?.canRequestAuthorization || prompt.target !== JSON.stringify(detail.value?.target)) return;
  const operationId = prompt.operationId;
  const key = operationId.toLowerCase();
  const requestId = retryRequests.get(key) ?? crypto.randomUUID();
  retryRequests.set(key, requestId);
  legacyRetry.value = null;
  await mutate("retry", { operation_id: operationId, confirmed: true, request_id: requestId });
}

async function respondInteraction(decision: "approve" | "deny"): Promise<void> {
  if (!alive || !pendingInteraction.value || interactionBusy.value || action.value) return;
  updateInteractionClock();
  if (interactionExpired.value || (decision === "approve" && !interactionValid.value)) return;
  const item = pendingInteraction.value;
  const key = interactionKey.value;
  const id = selectedId.value;
  const currentEpoch = epoch;
  const revision = snapshot;
  const payload = decision === "deny" ? { decision } : { decision,
    ...(item.options?.length ? { selections: [...interactionSelections.value] } : {}),
    ...(item.fields?.length ? { values: Object.fromEntries(item.fields.map(field => [field.id, interactionValues.value[field.id] ?? ""])) } : {}) };
  interactionBusy.value = true;
  error.value = "";
  try {
    const value = await apiRequest<unknown>(`/assistant/sessions/${encodeURIComponent(id)}/interactions/${encodeURIComponent(item.id)}/response`, {
      method: "POST", body: JSON.stringify(payload),
    });
    if (!alive || epoch !== currentEpoch) return;
    if (!isAssistantDetail(value) || value.id !== id) throw new Error("Invalid interaction response");
    if (revision === snapshot && interactionKey.value === key) applySnapshot(value, id);
    settledInteractions.add(key);
    await refreshDetail();
  } catch (cause) {
    if (!alive || epoch !== currentEpoch) return;
    if (interactionKey.value === key || (cause instanceof ApiError && cause.status === 401)) {
      await handleFailure(cause, message => { error.value = message; });
    }
  } finally {
    if (alive && epoch === currentEpoch) interactionBusy.value = false;
  }
}

async function decideApproval(decision: "approve" | "deny"): Promise<void> {
  if (!alive || !pendingApproval.value || approvalBusy.value || action.value) return;
  updateApprovalClock();
  if (approvalExpired.value) return;
  const approval = pendingApproval.value;
  const key = approvalKey.value;
  const id = selectedId.value;
  const currentEpoch = epoch;
  const revision = snapshot;
  approvalBusy.value = true;
  error.value = "";
  try {
    const value = await apiRequest<unknown>(`/assistant/sessions/${encodeURIComponent(id)}/retry-approvals/${encodeURIComponent(approval.id)}/decision`, {
      method: "POST", body: JSON.stringify({ decision }),
    });
    if (!alive || epoch !== currentEpoch) return;
    if (!isAssistantDetail(value) || value.id !== id) throw new Error("Invalid approval response");
    // SSE can resolve or replace the approval before this write returns, even with unchanged message IDs.
    if (revision === snapshot && approvalKey.value === key) applySnapshot(value, id);
    settledApprovals.add(key);
    await refreshDetail();
  } catch (cause) {
    if (!alive || epoch !== currentEpoch) return;
    if (approvalKey.value === key || (cause instanceof ApiError && cause.status === 401)) {
      await handleFailure(cause, message => { error.value = message; });
    }
  } finally {
    if (alive && epoch === currentEpoch) approvalBusy.value = false;
  }
}

async function send(): Promise<void> {
  if (!canSend.value) return;
  await mutate("send", { content: content.value.trim(), consent_to_send: true, request_id: crypto.randomUUID() });
}

async function cancel(): Promise<void> {
  if (!alive || !running.value || cancelBusy.value) return;
  const id = selectedId.value;
  const currentEpoch = epoch;
  cancelBusy.value = true;
  try {
    const revision = snapshot;
    const value = await apiRequest<unknown>(`/assistant/sessions/${encodeURIComponent(id)}/cancel`, { method: "POST", body: JSON.stringify({}) });
    if (!alive || epoch !== currentEpoch) return;
    applySnapshot(value, id, revision !== snapshot);
    await refreshDetail();
    if (alive && epoch === currentEpoch) void loadSessions();
  } catch (cause) {
    if (alive && epoch === currentEpoch) await handleFailure(cause, message => { error.value = message; });
  } finally {
    if (alive && epoch === currentEpoch) cancelBusy.value = false;
  }
}

function openTitle(): void {
  if (!detail.value || detailLoading.value || action.value || running.value) return;
  titleInput.value = detail.value.title;
  titleDialog.value = true;
}

async function saveTitle(): Promise<void> {
  if (!titleDialog.value || !canSaveTitle.value) return;
  await mutate("title", { title: titleInput.value.trim() });
}

async function deleteSession(): Promise<void> {
  if (!detail.value || running.value || action.value) return;
  const currentEpoch = epoch;
  const id = selectedId.value;
  action.value = "delete";
  try {
    await ElMessageBox.confirm("删除后无法恢复聊天记录。确认删除此会话？", "删除会话", {
      type: "warning", confirmButtonText: "删除", cancelButtonText: "保留",
    });
  } catch {
    if (alive && epoch === currentEpoch) action.value = "";
    return;
  }
  if (!alive || epoch !== currentEpoch) return;
  if (running.value) { action.value = ""; return; }
  try {
    await apiRequest(`/assistant/sessions/${encodeURIComponent(id)}`, { method: "DELETE" });
    if (!alive || epoch !== currentEpoch) return;
    resetSelection();
    // A known removal before the next live offset shifts all remaining rows left.
    if (nextOffset.value !== null && sessions.value.some(item => item.id === id)) {
      nextOffset.value = Math.max(0, nextOffset.value - 1);
    }
    sessions.value = sessions.value.filter(item => item.id !== id);
    listRequest++;
    listLoading.value = moreLoading.value = false;
    void loadSessions();
  } catch (cause) {
    if (!alive || epoch !== currentEpoch) return;
    await handleFailure(cause, message => { error.value = message; });
  } finally {
    if (alive && epoch === currentEpoch) action.value = "";
  }
}

function toolResult(message: AssistantMessage): string {
  return messageCache.pretty(message);
}

function uncertaintyStatus(message: AssistantMessage): "pending" | "historical" | null {
  if (message.role !== "tool" || !["execute_step", "retry_step"].includes(message.tool_name ?? "")) return null;
  try {
    const result = messageCache.parse(message);
    if (result?.status !== "unknown" || result?.code !== "outcome_uncertain") return null;
    const unresolved = detail.value?.unresolved_operations;
    return unresolved === undefined || unresolved.some(item => item.operation_id.toLowerCase() === result.operation_id?.toLowerCase())
      ? "pending" : "historical";
  } catch { return null; }
}

function isContextLimit(message: AssistantMessage): boolean {
  return message.role === "system" && message.content.includes("[context_limit]");
}

function trustResult(message: AssistantMessage): Record<string, unknown> | null {
  if (message.role !== "tool" || !["browser_open", "browser_observe", "browser_navigate"].includes(message.tool_name ?? "")) return null;
  try {
    const result = messageCache.parse(message);
    if (!result || typeof result !== "object" || Array.isArray(result)) return null;
    if (result.ok === false && result.code === "browser_trust_required") return result;
    return Array.isArray(result.warnings) ? result.warnings.slice(0, 4).find((warning: unknown): warning is Record<string, unknown> =>
      warning !== null && typeof warning === "object" && !Array.isArray(warning) &&
      "code" in warning && warning.code === "browser_trust_required" &&
      "source" in warning && warning.source === "blocked_request") ?? null : null;
  } catch { return null; }
}

function isProposal(message: AssistantMessage): boolean {
  if (message.role !== "tool" || message.tool_name !== "submit_workflow_draft") return false;
  try {
    const result = messageCache.parse(message);
    return result?.ok === true && result?.submitted === true && Boolean(result.proposal);
  } catch { return false; }
}

function workflowResult(message: AssistantMessage): { kind: "import"; site_id: number; name: string; operation: "created" | "updated" } |
  { kind: "run"; run_id: number; status: string; lane: string } | null {
  if (message.role !== "tool" || message.status === "running" || message.status === "error" ||
    !["import_workflow_draft", "run_site"].includes(message.tool_name ?? "")) return null;
  try {
    const result = messageCache.parse(message);
    if (result?.ok !== true) return null;
    if (message.tool_name === "import_workflow_draft" && Number.isSafeInteger(result.site_id) && result.site_id > 0 &&
      typeof result.name === "string" && ["created", "updated"].includes(result.operation) &&
      typeof result.history_key === "string" && Number.isSafeInteger(result.config_revision) && result.config_revision > 0 &&
      assistantRunLanePolicies.includes(result.lane_policy)) {
      return { kind: "import", site_id: result.site_id, name: result.name, operation: result.operation };
    }
    if (message.tool_name === "run_site" && Number.isSafeInteger(result.run_id) && result.run_id > 0 &&
      ["queued", "running", "success", "fail", "error", "deferred", "unknown", "skipped", "waiting_human", "canceled", "interrupted"].includes(result.status) &&
      assistantRunLanePolicies.some(lane => lane !== "auto" && lane === result.lane)) {
      return { kind: "run", run_id: result.run_id, status: result.status, lane: result.lane };
    }
  } catch { /* Tool JSON remains available even when the result cannot form a safe link. */ }
  return null;
}

function messageMetadata(message: AssistantMessage) {
  const parse = () => messageCache.parse(message);
  return { uncertainty: uncertaintyStatus(message), trust: trustResult(message), proposal: isProposal(message),
    workflow: workflowResult(message), config: siteConfigResult(message, parse), operation: operationResultLabel(message, parse) };
}

onMounted(() => {
  try { safetyVisible.value = localStorage.getItem(safetyDismissalKey) !== "dismissed"; } catch { /* Storage may be blocked. */ }
  void loadSessions();
});
onBeforeUnmount(() => {
  alive = false;
  listRequest++;
  resetSelection();
});
</script>

<template>
  <div class="assistant-page">
    <PageHeader class="assistant-header" eyebrow="AI ASSISTANT" title="智能助手" description="对话梳理需求，观察网站，按授权单步验证，生成待人工审阅的配置提案。">
      <template #actions>
        <el-button @click="takeoverOpen = true">助手接管</el-button>
        <el-button @click="settingsOpen = true">助手设置</el-button>
        <el-button type="primary" :loading="creating" :disabled="creating" @click="createSession">新建会话</el-button>
      </template>
    </PageHeader>
    <p v-if="createError" class="error-text" role="alert">{{ createError }}</p>
    <div class="assistant-grid">
      <aside class="sessions-panel" aria-label="助手会话">
        <div class="panel-heading"><strong>会话</strong><el-button text :loading="listLoading" :disabled="listLoading" @click="loadSessions()">刷新列表</el-button></div>
        <p v-if="!sessions.length && !listLoading" class="muted">暂无会话。新建会话后开始对话。</p>
        <nav class="session-list" aria-label="选择会话">
          <button v-for="session in sessions" :key="session.id" type="button" class="session-item"
            :class="{ selected: selectedId === session.id }" :aria-current="selectedId === session.id ? 'true' : undefined"
            :disabled="creating" @click="selectSession(session.id)">
            <strong>{{ session.title }}</strong>
            <span><i :class="{ active: session.status === 'running' }" />{{ session.status === 'running' ? '运行中' : '空闲' }}</span>
            <small v-if="session.context?.compressed" :title="`上下文修订 ${session.context.revision}。${contextTip}`">较早上下文已整理，完整记录仍保留</small>
            <time :datetime="session.updated_at">{{ new Date(session.updated_at).toLocaleString() }}</time>
          </button>
          <button v-if="nextOffset !== null" type="button" class="session-more"
            :disabled="listLoading || moreLoading" :aria-busy="moreLoading" @click="loadSessions(true)"
            title="按当前排序加载；会话更新可能使分页重叠或遗漏，非历史快照。">{{ moreLoading ? '正在加载会话…' : '加载更多会话' }}</button>
          <p v-if="listError" class="error-text" role="alert">{{ listError }}</p>
        </nav>
      </aside>
      <section class="chat-panel" aria-label="助手对话" :aria-busy="detailLoading || running">
        <header class="workspace-heading">
          <div class="workspace-title"><div class="title-line"><h2>{{ detail?.title || (detailLoading ? '正在加载会话' : '开始新的对话') }}</h2>
            <button v-if="detail" type="button" class="context-button" :style="{ '--context-progress': contextCircle }"
              :aria-label="contextUsageTooltip" :title="contextUsageTooltip" aria-haspopup="dialog" @click="usageOpen = true"><span aria-hidden="true" /></button></div>
            <div v-if="detail" class="state-line">
              <el-tag :type="running ? 'warning' : 'info'">{{ running ? '运行中' : '空闲' }}</el-tag>
              <span class="muted">{{ connection === 'live' ? '实时连接' : connection === 'offline' ? '连接中断，正在重连' : '正在连接' }}</span>
              <small class="muted">{{ detail.title_locked ? '已手动命名，自动标题已停止' : '根据前 3 条用户消息自动命名' }}</small>
              <small v-if="detail.context?.compressed" class="muted" role="status"
                :title="`上下文修订 ${detail.context.revision}。${contextTip}`">较早上下文已整理，完整记录仍保留</small>
            </div>
          </div>
          <div v-if="selectedId" class="workspace-actions">
            <el-button :disabled="detailLoading || !!action" @click="refreshDetail()">刷新会话</el-button>
            <el-button :disabled="!canUseBrowser" :loading="action === 'refresh'" @click="refreshBrowser">刷新网页</el-button>
            <el-button :disabled="!canScreenshot" :loading="action === 'screenshot'" @click="takeScreenshot">截图预览</el-button>
            <el-button :disabled="!detail || detailLoading || running || !!action" @click="openTitle">重命名</el-button>
            <el-button type="danger" plain :disabled="!detail || running || !!action" @click="deleteSession">删除</el-button>
          </div>
        </header>
        <template v-if="detail">
          <div class="timeline" aria-label="消息与工具日志" tabindex="0">
          <section v-if="recoveryOperations.length && !running && !detail.pending_approval" class="recovery-section" aria-label="结果不确定的操作">
            <h3>操作结果不确定</h3>
            <p>原操作可能已成功，截图或刷新不能证明失败。模型重试时会自动请求授权。以下为旧流程的提前授权入口，不会立即执行或发送消息。</p>
            <article v-for="operation in recoveryOperations" :key="operation.operation_id" class="recovery-operation">
              <strong>{{ operation.feature_type || '功能未提供' }}</strong>
              <p class="mono">原操作 ID：{{ operation.operation_id }}</p>
              <template v-if="operation.authorization">
                <p v-if="operation.authorization.status === 'approved'" role="status">已允许重试一次，发送消息让助手继续</p>
                <p v-else>{{ operation.authorization.status === 'used' ? '先前一次重试授权已使用；能否再次申请以服务端判断为准' : '一次重试授权已过期或浏览器已更换，需重新确认' }}</p>
                <p>服务端授权有效期 5 分钟 · 到期 {{ new Date(operation.authorization.expires_at).toLocaleString() }} · 目标修订 {{ operation.authorization.target_revision }}</p>
              </template>
              <p v-if="!browserReady">请先通过聊天让助手打开浏览器，再确认重试。</p>
              <p v-if="!operation.can_authorize && !operation.authorization">此记录没有可安全重试的冻结步骤，不能授权重试。</p>
              <el-button v-if="!operation.authorization || operation.authorization.status === 'expired' || operation.canRequestAuthorization" type="warning" plain
                :disabled="!canUseBrowser || !operation.canRequestAuthorization" :loading="action === 'retry'"
                @click="authorizeRetry(operation.operation_id)">提前授权一次</el-button>
            </article>
          </section>
            <p v-if="!detail.messages.length" class="empty-chat">描述希望配置的网站流程。尚未确认目标也可以先交流。</p>
            <article v-for="message in detail.messages" :key="message.id" class="message-card" :class="`role-${message.role}`">
              <template v-for="meta in [messageMetadata(message)]" :key="message.id">
              <div class="message-heading">
                <strong>{{ message.role === 'tool' ? (message.title || '工具调用') : message.role === 'user' ? '你' : message.role === 'assistant' ? '智能助手' : '系统' }}</strong>
                <span v-if="message.tool_name" class="mono muted">{{ message.tool_name }}</span>
                <el-tag v-if="message.status" size="small" :type="meta.uncertainty === 'historical' ? 'info' : meta.uncertainty ? 'warning' : message.status === 'error' ? 'danger' : message.status === 'running' ? 'warning' : 'success'">
                  {{ meta.uncertainty === 'historical' ? '历史结果不确定，已处理' : meta.uncertainty ? '结果不确定' : message.status === 'running' ? '进行中' : message.status === 'success' ? '成功' : '失败' }}
                </el-tag>
                <time class="muted" :datetime="message.created_at">{{ new Date(message.created_at).toLocaleTimeString() }}</time>
              </div>
              <template v-if="message.role === 'tool'">
                <section v-if="meta.trust" class="trust-notice" role="alert">
                  <strong>跨来源访问已阻止，需用户核对并确认可信来源。</strong>
                  <p v-if="meta.trust.source === 'blocked_request'">已阻止页面发起的跨来源请求，当前页面仍保留，可继续观察；不会自动重试。</p>
                  <p>当前完整 URL：{{ meta.trust.source_url }}</p>
                  <p>目标完整 URL：{{ meta.trust.target_url }}</p>
                  <p>待核对 origin：{{ meta.trust.target_origin }}</p>
                  <p v-if="meta.trust.source_url_redacted === true || meta.trust.target_url_redacted === true || meta.trust.url_redacted === true">脱敏参数不可直接作为可运行地址。</p>
                  <p>不会自动添加可信来源、导航或重试。请在聊天中要求助手发起交互式可信来源确认，核对服务端提供的授权面板后决定；不要提供凭据。</p>
                </section>
                <p v-if="meta.proposal" class="proposal-notice">配置提案已生成，供人工审阅；此提案提交调用本身未保存或执行，实际导入与运行以对应工具结果为准。</p>
                <template v-for="result in [meta.workflow]" :key="message.id">
                  <p v-if="result?.kind === 'import'" class="proposal-notice">已{{ result.operation === 'created' ? '新建' : '更新' }}站点「{{ result.name }}」（#{{ result.site_id }}）。<router-link to="/sites">查看站点配置</router-link></p>
                  <p v-else-if="result?.kind === 'run'" class="proposal-notice">已触发完整测试任务，创建人：助手 · {{ result.status }} · {{ result.lane }}。<router-link :to="{ path: '/runs', query: { run: String(result.run_id) } }">查看运行 #{{ result.run_id }}</router-link></p>
                </template>
                <template v-for="result in [meta.config]" :key="`config-${message.id}`">
                  <p v-if="result" class="proposal-notice">已更新站点<span v-if="result.name">「{{ result.name }}」</span>（#{{ result.site_id }}）。<span v-if="result.fields.length">变更字段：{{ result.fields.join('、') }}。</span><router-link to="/sites">查看站点配置</router-link></p>
                </template>
                <p v-if="message.tool_name === 'browser_screenshot'" class="muted">仅展示图像证据元数据，不加载模型图像地址。需要查看当前网页时，请使用上方“截图预览”。截图不证明原操作失败。</p>
                <p v-if="meta.operation" class="proposal-notice">{{ meta.operation }}</p>
                <details @toggle="toggleMessage(message.id, $event)"><summary>{{ meta.proposal ? '展开完整配置提案 JSON' : '展开工具结果' }}</summary><pre v-if="openMessageIds.has(message.id)">{{ toolResult(message) }}</pre></details>
              </template>
              <template v-else-if="isContextLimit(message)">
                <p class="message-content">{{ contextLimitText }}</p>
                <details @toggle="toggleMessage(message.id, $event)"><summary>展开原始系统记录</summary><pre v-if="openMessageIds.has(message.id)">{{ message.content }}</pre></details>
              </template>
              <AssistantMarkdown v-else-if="message.role === 'assistant' && message.status !== 'error'" :content="message.content" />
              <p v-else class="message-content">{{ message.content }}</p>
              </template>
            </article>
            <p v-if="running || action === 'send'" class="activity" role="status"><span class="activity-dot" />助手正在处理，请等待。可停止后台任务。</p>
          </div>
        </template>
        <p v-else class="empty-chat timeline">{{ detailLoading ? '正在读取聊天记录...' : '选择左侧会话，或新建一个会话。' }}</p>
        <p v-if="error" class="error-text workspace-error" role="alert">{{ error }}</p>
        <section v-if="pendingInteraction" :key="interactionKey" class="inline-prompt interaction-prompt" aria-label="助手交互请求" :aria-busy="interactionBusy">
          <div class="prompt-body">
            <h3>{{ pendingInteraction.title }}</h3><p class="message-content">{{ pendingInteraction.description }}</p>
            <p class="muted">{{ interactionPurposeLabel(pendingInteraction.purpose) }} · {{ interactionExpired ? '交互等待已超时' : `剩余 ${interactionSeconds} 秒` }}</p>
            <p class="muted">请只填写非秘密信息。不要提供密码、Cookie 或令牌。批准后服务端继续处理，拒绝将终止此请求。</p>
            <AssistantInteractionReview v-if="pendingInteraction.review" :purpose="pendingInteraction.purpose" :review="pendingInteraction.review" />
            <fieldset v-if="pendingInteraction.options?.length" :disabled="interactionBusy || interactionExpired || !!action">
              <legend>{{ pendingInteraction.multiple ? '可多选' : '请选择一项' }}</legend>
              <label v-for="option in pendingInteraction.options" :key="option.id" class="prompt-option">
                <input v-if="pendingInteraction.multiple" v-model="interactionSelections" type="checkbox" :value="option.id" />
                <input v-else type="radio" :name="interactionKey" :value="option.id" :checked="interactionSelections.includes(option.id)" @change="interactionSelections = [option.id]" />{{ option.label }}
              </label>
            </fieldset>
            <label v-for="field in pendingInteraction.fields" :key="field.id" class="prompt-field">{{ field.label }}{{ field.required ? '（必填）' : '（可选）' }}
              <input v-model="interactionValues[field.id]" type="text" :required="field.required" maxlength="2000" autocomplete="off" :disabled="interactionBusy || interactionExpired || !!action" />
            </label>
          </div>
          <div class="prompt-actions"><el-button :disabled="interactionBusy || interactionExpired || !!action" @click="respondInteraction('deny')">拒绝</el-button>
            <el-button type="primary" :loading="interactionBusy" :disabled="!interactionValid || interactionBusy || interactionExpired || !!action" @click="respondInteraction('approve')">确认并继续</el-button></div>
        </section>
        <section v-if="pendingApproval" :key="approvalKey" class="inline-prompt approval-prompt" aria-label="重复操作需要授权">
          <div class="prompt-body"><h3>{{ pendingApproval.title }}</h3><p>功能：{{ pendingApproval.feature_type }}</p>
            <p class="mono">原操作 ID：{{ pendingApproval.operation_id }}</p>
            <p>原操作结果不确定，可能已经成功。再次执行可能重复登录、提交、签到、点击、产生费用或其他副作用，截图或刷新不能证明原操作失败。</p>
            <p>批准后，服务端将立即继续正在等待的同一次工具调用，无需另发消息；拒绝将终止此操作。</p>
            <p role="status">{{ approvalExpired ? '授权等待已超时' : `剩余授权时间 ${approvalSeconds} 秒` }}</p>
            <p>截止时间：<time :datetime="pendingApproval.expires_at">{{ new Date(pendingApproval.expires_at).toLocaleString() }}</time></p></div>
          <div class="prompt-actions"><el-button :disabled="approvalBusy || approvalExpired || !!action" @click="decideApproval('deny')">拒绝并终止此操作</el-button>
            <el-button type="primary" :loading="approvalBusy" :disabled="approvalBusy || approvalExpired || !!action" @click="decideApproval('approve')">批准并继续</el-button></div>
        </section>
        <section v-if="legacyRetry && legacyRetryOperation" class="inline-prompt legacy-retry-prompt" aria-label="允许重试一次">
          <div class="prompt-body"><h3>允许重试一次</h3><p>功能：{{ legacyRetryOperation.feature_type || '未提供' }} · 原操作 ID：{{ legacyRetryOperation.operation_id }}</p>
            <p>原操作结果不确定，可能已经成功。再次执行可能重复登录、提交、签到、点击、产生费用或其他副作用，截图或刷新不能证明原操作失败。</p>
            <p v-if="legacyRetryOperation.authorization?.status === 'used'">先前重试授权已使用，再次申请可能造成更多重复操作；是否允许再次授权由服务端判断。</p>
            <p v-if="retryRequests.has(legacyRetryOperation.operation_id.toLowerCase())">上次授权请求结果未确认，本次仅使用同一请求 ID 核对，不新建或续期授权。</p>
            <p>仅允许服务端冻结的原步骤重试一次，授权由服务端签发、5 分钟有效；确认不会立即执行，仍需发送消息让助手继续。</p></div>
          <div class="prompt-actions"><el-button :disabled="!!action" @click="legacyRetry = null">取消</el-button><el-button type="warning" :disabled="!canUseBrowser" @click="confirmLegacyRetry">允许重试一次</el-button></div>
        </section>
        <form v-if="detail" class="composer" @submit.prevent="send">
          <el-input id="assistant-message" v-model="content" type="textarea" aria-label="消息" :rows="2" :maxlength="12000" show-word-limit
            :disabled="running || !!action || detailLoading" placeholder="描述需求或回复助手的问题，不要提供任何凭据。"
            @keydown.ctrl.enter.prevent="!$event.isComposing && send()" />
          <div class="composer-actions"><span class="muted">最多 12000 字符 · Ctrl+Enter 发送 · 运行中请先停止再删除</span>
            <el-button v-if="running" type="danger" :loading="cancelBusy" :disabled="cancelBusy" @click="cancel">停止</el-button>
            <el-button type="primary" native-type="submit" :loading="action === 'send'" :disabled="!canSend">发送</el-button>
          </div>
        </form>
      </section>
    </div>
    <div v-if="safetyVisible" class="safety-rail" role="note"><p>不要粘贴密码、Cookie、令牌或其他秘密。发送即同意将消息、有限历史和授权观察结果发送给模型，可能包含个人资料。聊天记录加密保存；提案本身不保存，授权后的工具可实际保存配置、触发任务并产生费用或其他副作用。切换页面不会停止后台任务。</p><button type="button" aria-label="关闭安全提示" @click="dismissSafety">关闭</button></div>
    <AssistantSettingsDialog v-model="settingsOpen" />
    <AssistantTakeoverDialog v-model="takeoverOpen" @session="selectSession" />
    <el-dialog v-model="usageOpen" title="上下文与累计用量" width="min(900px, 94vw)" class="usage-dialog" destroy-on-close>
      <p>{{ contextTip }}</p><p>{{ contextUsageLabel }}占用 {{ contextPercentText }}。对话请求的输入 + 输出，不含摘要、标题请求。分母为对应请求的预算快照。</p>
      <p v-if="contextUsageStale" class="usage-warning" role="status">最近调用未返回完整用量，以下保留上次有效的计数和预算，不代表当前上下文；没有有效记录时显示“--”。下次有效测量会更新，整理上下文后会清除旧测量。</p>
      <dl class="usage-totals" :aria-label="`${contextUsageLabel}用量`">
        <div><dt>{{ contextUsageLabel }}输入 + 输出 Token</dt><dd>{{ tokenText(detail?.usage?.last_context_tokens) }}</dd></div>
        <div><dt>预算 Token</dt><dd>{{ tokenText(detail?.usage?.last_context_budget_tokens) }}</dd></div>
        <div><dt>输入 Token</dt><dd>{{ tokenText(detail?.usage?.last_context_prompt_tokens) }}</dd></div>
        <div><dt>输出 Token</dt><dd>{{ tokenText(detail?.usage?.last_context_completion_tokens) }}</dd></div>
        <div><dt>缓存命中 Token</dt><dd>{{ tokenText(detail?.usage?.last_context_cached_tokens) }}</dd></div>
      </dl>
      <template v-if="detail?.usage">
        <p class="muted">以下为会话累计消耗，不是当前上下文大小；Token 均为服务端返回的已知值。</p>
        <p v-if="detail.usage.unreported_usage_count" class="usage-warning" role="status">部分调用未返回用量，已知值统计；未返回用量的调用数：{{ detail.usage.unreported_usage_count }}</p>
        <dl class="usage-totals" aria-label="会话累计用量"><div><dt>累计请求数</dt><dd>{{ detail.usage.totals.request_count }}</dd></div>
          <div v-for="metric in usageMetrics" :key="metric.key"><dt>累计{{ metric.label }} Token</dt><dd>{{ detail.usage.totals[metric.key] }}</dd></div></dl>
        <p>缓存命中是输入的子集，思考是输出的子集，不要重复相加；总计使用返回值，不自行求和。未返回的单项显示“--”，不是 0。</p>
        <p>最近调用（最多 100 条，累计统计包含更早调用）</p>
        <el-table :data="detail.usage.calls" size="small" empty-text="暂无调用" class="usage-calls">
          <el-table-column label="类型" min-width="80"><template #default="{ row }">{{ usageKinds[row.kind as keyof typeof usageKinds] }}</template></el-table-column>
          <el-table-column label="时间" min-width="180"><template #default="{ row }"><time :datetime="row.created_at">{{ new Date(row.created_at).toLocaleString() }}</time></template></el-table-column>
          <el-table-column v-for="metric in usageMetrics" :key="metric.key" :label="`${metric.label} Token`" min-width="110"><template #default="{ row }">{{ tokenText(row[metric.key]) }}</template></el-table-column>
        </el-table>
      </template>
    </el-dialog>
    <el-dialog v-model="screenshotOpen" title="当前网页截图预览" width="min(1000px, 94vw)" destroy-on-close @close="clearScreenshot">
      <p>仅在本页内存预览，关闭即清除。密码表单已遮罩，但可见个人资料或其他敏感内容仍可能出现在图像中；不保证没有秘密。截图不证明之前的操作失败。</p>
      <img v-if="screenshotPreview" class="screenshot-preview" :src="screenshotPreview.src" :width="screenshotPreview.width"
        :height="screenshotPreview.height" alt="当前网页截图，密码表单已遮罩，但仍可能包含敏感信息" />
      <p v-else role="status">正在读取截图...</p>
    </el-dialog>
    <el-dialog v-model="titleDialog" title="重命名会话" width="min(520px, 94vw)" :close-on-click-modal="action !== 'title'"
      :close-on-press-escape="action !== 'title'" :show-close="action !== 'title'">
      <el-form label-position="top" @submit.prevent="saveTitle">
        <el-form-item label="会话标题（最多 128 字符）"><el-input v-model="titleInput" :maxlength="128" show-word-limit
          :disabled="running || !!action || detailLoading" /></el-form-item>
        <p class="muted">手动命名后将停止自动更新标题。</p>
        <p v-if="error" class="error-text" role="alert">{{ error }}</p>
      </el-form>
      <template #footer><el-button :disabled="!!action" @click="titleDialog = false">取消</el-button><el-button type="primary" :loading="action === 'title'" :disabled="!canSaveTitle" @click="saveTitle">保存标题</el-button></template>
    </el-dialog>
  </div>
</template>

<style scoped>
.assistant-page { width: 100%; max-width: 1600px; height: 100%; min-height: 0; margin: 0 auto; display: flex; flex-direction: column; overflow: hidden; }
.panel-heading, .workspace-heading, .composer-actions { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.assistant-header { margin-bottom: 12px; }
.assistant-grid { display: grid; grid-template-columns: 220px minmax(0, 1fr); gap: 4px; flex: 1; min-height: 0; }
.sessions-panel, .chat-panel { min-width: 0; min-height: 0; border: 1px solid #e4e9f0; border-radius: 12px; background: #fff; overflow: hidden; display: flex; flex-direction: column; }
.sessions-panel { padding: 14px; }
.session-list { display: grid; align-content: start; gap: 4px; min-height: 0; overflow: auto; margin-top: 12px; }
.session-item { display: grid; gap: 7px; width: 100%; text-align: left; padding: 12px; border: 1px solid transparent; border-radius: 8px; background: #f8fafc; color: inherit; cursor: pointer; overflow-wrap: anywhere; }
.session-item.selected { border-color: #b8d9ff; background: #eaf3ff; }
.session-item:focus-visible { outline: 2px solid #409eff; }
.session-item span, .session-item time { font-size: 12px; color: #7a8794; }
.session-item i { display: inline-block; width: 6px; height: 6px; border-radius: 50%; background: #a7b3c0; margin-right: 6px; }
.session-item i.active { background: #e6a23c; }
.session-more { flex: none; padding: 10px; border: 1px solid #c5cfdb; border-radius: 8px; background: #fff; color: #2f7ed8; cursor: pointer; }
.session-more:disabled { cursor: default; color: #7a8794; }
.workspace-heading { padding: 14px 18px; border-bottom: 1px solid #e4e9f0; align-items: flex-start; flex: none; }
.workspace-title { min-width: 0; }
.title-line { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }
h2 { font-size: 18px; margin: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.context-button { flex: none; width: 24px; height: 24px; border: 0; border-radius: 50%; padding: 3px; background: conic-gradient(#409eff var(--context-progress), #e4e9f0 0); cursor: pointer; }
.context-button span { display: block; width: 100%; height: 100%; border-radius: 50%; background: #fff; }
.context-button:focus-visible { outline: 2px solid #245a92; outline-offset: 3px; }
.state-line, .workspace-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; font-size: 12px; }
.workspace-actions .el-button { margin-left: 0; }
.usage-totals { display: grid; grid-template-columns: repeat(auto-fit, minmax(125px, 1fr)); gap: 12px; }
.usage-totals dt { color: #596c80; }
.usage-totals dd { margin: 4px 0 0; font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
.usage-warning { color: #854d0e; }
.usage-calls { width: 100%; font-variant-numeric: tabular-nums; }
.recovery-section { padding: 14px 20px; background: #fdf6ec; color: #854d0e; font-size: 13px; overflow-wrap: anywhere; }
.recovery-section h3 { margin: 0; font-size: 14px; }
.recovery-operation { padding: 12px 0; border-top: 1px solid #e6c994; }
.screenshot-preview { display: block; max-width: 100%; height: auto; }
.inline-prompt { flex: 0 1 auto; min-height: 90px; max-height: min(38dvh, 360px); margin: 0 14px 8px; border: 1px solid #e6c994; border-radius: 10px; background: #fffbf3; overflow: hidden; display: flex; flex-direction: column; font-size: 13px; overflow-wrap: anywhere; }
.prompt-body { min-height: 0; overflow: auto; overscroll-behavior: contain; padding: 12px 14px; }
.prompt-body h3 { margin: 0; font-size: 15px; }
.prompt-actions { display: flex; justify-content: flex-end; flex-wrap: wrap; gap: 8px; flex: none; padding: 8px 12px; border-top: 1px solid #efdfbf; }
.prompt-actions .el-button { margin: 0; }
.prompt-option { display: flex; align-items: start; gap: 8px; padding: 6px 0; }
.prompt-field { display: grid; gap: 6px; margin-top: 12px; }
.prompt-field input { width: 100%; border: 1px solid #c5cfdb; border-radius: 6px; padding: 8px; font: inherit; }
.timeline { flex: 1; min-height: 0; overflow: auto; overscroll-behavior: contain; padding: 20px; display: flex; flex-direction: column; gap: 2px; }
.timeline > * { flex-shrink: 0; }
.message-card { border: 1px solid #e4e9f0; border-radius: 10px; padding: 14px; margin: 0; overflow-wrap: anywhere; }
.role-user { margin-left: 8%; background: #f0f7ff; border-color: #d9eaff; }
.role-tool { border-left: 3px solid #409eff; background: #fafbfd; }
.role-system { background: #fdf6ec; }
.message-heading { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; font-size: 12px; }
.message-heading time { margin-left: auto; }
.message-content { white-space: pre-wrap; margin: 12px 0 0; line-height: 1.7; font-size: 14px; }
details { margin-top: 12px; font-size: 12px; }
summary { cursor: pointer; color: #2f7ed8; }
pre { max-height: 440px; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; padding: 12px; border-radius: 8px; background: #f0f3f7; font-size: 12px; }
.proposal-notice { color: #529b2e; font-size: 13px; }
.trust-notice { margin-top: 12px; padding: 14px; border: 1px solid #e6a23c; border-radius: 8px; background: #fdf6ec; color: #854d0e; font-size: 13px; }
.trust-notice p { white-space: pre-wrap; overflow-wrap: anywhere; }
.empty-chat { color: #7a8794; padding: 32px 20px; line-height: 1.8; }
.activity { display: flex; align-items: center; gap: 8px; color: #7a8794; font-size: 13px; }
.activity-dot { width: 8px; height: 8px; border-radius: 50%; background: #409eff; animation: pulse 1.3s ease-in-out infinite alternate; }
.composer { border-top: 1px solid #e4e9f0; padding: 12px 18px; flex: none; }
.composer :deep(textarea) { resize: none; }
.composer-actions { flex-wrap: wrap; font-size: 12px; margin-top: 8px; }
.composer-actions > span { flex: 1; }
.error-text { color: #c45656; font-size: 13px; white-space: pre-wrap; overflow-wrap: anywhere; }
.workspace-error { margin: 6px 18px; max-height: 48px; overflow: auto; flex: none; }
.safety-rail { display: flex; align-items: start; gap: 12px; flex: none; max-height: 70px; margin: 8px auto 0; padding: 8px 12px; border: 1px solid #eadbbd; border-radius: 10px; background: #fffbf3; box-shadow: 0 3px 12px #27394d0d; font-size: 11px; color: #7c622d; }
.safety-rail p { margin: 0; min-height: 0; max-height: 48px; overflow: auto; line-height: 1.5; }
.safety-rail button { flex: none; border: 0; background: transparent; color: #72551e; cursor: pointer; padding: 2px; }
@keyframes pulse { to { opacity: 0.3; } }
@media (prefers-reduced-motion: reduce) { .activity-dot { animation: none; } }
@media (max-width: 1100px) { .assistant-grid { grid-template-columns: 180px minmax(0, 1fr); gap: 2px; } .workspace-heading { flex-direction: column; } }
@media (max-width: 720px) {
  .assistant-grid { grid-template-columns: minmax(0, 1fr); grid-template-rows: 48px minmax(0, 1fr); gap: 1px; }
  .sessions-panel { flex-direction: row; align-items: center; padding: 4px 8px; gap: 8px; }
  .panel-heading { flex: none; gap: 4px; }
  .panel-heading strong, .sessions-panel > p { display: none; }
  .session-list { display: flex; margin: 0; gap: 2px; overflow: auto; }
  .session-list .error-text { flex: 0 0 220px; max-height: 36px; overflow: auto; margin: 0; }
  .session-item { display: block; width: auto; flex: 0 0 100px; padding: 6px; }
  .session-item strong { display: block; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; font-size: 12px; }
  .session-item span, .session-item time, .session-item small { display: none; }
  .assistant-header { margin-bottom: 6px; }
  .assistant-header :deep(.page-header__title) { font-size: 20px; }
  .assistant-header :deep(.page-header__description), .state-line small, .composer-actions > span { display: none; }
  .assistant-header :deep(.page-header__actions) { gap: 4px; }
  .assistant-header :deep(.el-button) { padding: 6px 8px; }
  .timeline { padding: 12px; }
  .workspace-heading { padding: 8px 10px; gap: 6px; }
  .workspace-title { width: 100%; }
  .title-line { margin-bottom: 4px; }
  .workspace-actions { flex-wrap: nowrap; overflow-x: auto; max-width: 100%; gap: 6px; }
  .workspace-actions .el-button { padding: 5px 8px; font-size: 11px; height: 26px; }
  .composer { padding: 8px 10px; }
  .composer-actions { justify-content: flex-end; }
  .inline-prompt { margin: 0 8px 6px; max-height: 28dvh; min-height: 75px; }
  .safety-rail { max-height: 50px; padding: 5px 8px; margin-top: 6px; }
  .safety-rail p { max-height: 36px; }
}
@media (max-width: 720px) and (max-height: 640px) {
  .state-line { display: none; }
  .title-line { margin-bottom: 0; }
  .timeline { padding: 4px 10px; }
  .workspace-heading { padding: 6px 10px; gap: 4px; }
  .composer :deep(textarea) { min-height: 36px !important; height: 36px; }
  .safety-rail { max-height: 32px; }
  .safety-rail p { max-height: 20px; }
  .inline-prompt { max-height: 24dvh; }
}
</style>
