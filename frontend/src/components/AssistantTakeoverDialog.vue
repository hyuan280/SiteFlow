<script setup lang="ts">
import { computed, onBeforeUnmount, reactive, ref, watch } from "vue";
import { useRouter } from "vue-router";
import { ElMessageBox } from "element-plus";

import { ApiError, apiRequest } from "@/api/client";
import { useAuthStore } from "@/stores/auth";
import { isAssistantTakeoverActions, isAssistantTakeoverSettings, isAssistantTakeoverValues, isTakeoverSiteOptions, takeoverStatusLabel } from "@/types/assistant-takeover";
import type { AssistantTakeoverAction, AssistantTakeoverSettings, AssistantTakeoverValues, TakeoverSiteOption } from "@/types/assistant-takeover";

const props = defineProps<{ modelValue: boolean }>();
const emit = defineEmits<{ (event: "update:modelValue", value: boolean): void; (event: "session", value: string): void }>();
const auth = useAuthStore();
const router = useRouter();
const view = ref<AssistantTakeoverSettings | null>(null);
const draft = reactive({ enabled: false, live_access: true, mode: "analysis", site_ids: [] as number[], max_attempts: "1", conclusion_ttl_days: "7" });
const allSites = ref(false);
const sites = ref<TakeoverSiteOption[]>([]);
const sitesReady = ref(false);
const actions = ref<AssistantTakeoverAction[]>([]);
const loading = ref(false);
const busy = ref<"" | "save" | "refresh">("");
const error = ref("");
const actionsError = ref("");
const notice = ref("");
const needsRefresh = ref(false);
let session = 0;
let open = false;
let controller: AbortController | null = null;
const liveAccessWarning = "授权后可无人值守浏览并单步验证，使用私密凭据登录、提交表单、签到或点击，可能产生真实业务副作用；脱敏截图和页面内容可能发送给已配置的模型。浏览器使用站点已保存的代理设置，选择 interaction 或 proxy_interaction 通道。授权仅绑定所选站点身份及获授权时的 URL，不代表授权所有来源或任意 URL。自动接管结束后释放本轮浏览器，不保留原页面或 DOM；同站点、同 URL 的已授予权限保留供用户后续对话，用户可要求重新打开新页面，无需重复授予仍有效的权限。不会抢占人工浏览器或查看连接；其他操作仍需交互授权。";

const values = computed<AssistantTakeoverValues | null>(() => {
  if (typeof allSites.value !== "boolean" || !Array.isArray(draft.site_ids) || typeof draft.max_attempts !== "string" || !/^[1-3]$/.test(draft.max_attempts)) return null;
  if (typeof draft.conclusion_ttl_days !== "string" || !/^(?:[1-9]|[12][0-9]|30)$/.test(draft.conclusion_ttl_days)) return null;
  const value = { enabled: draft.enabled, live_access: draft.live_access, mode: draft.mode, site_ids: allSites.value ? null : [...draft.site_ids], max_attempts: Number(draft.max_attempts), conclusion_ttl_days: Number(draft.conclusion_ttl_days) };
  return isAssistantTakeoverValues(value) ? value : null;
});
const disabled = computed(() => loading.value || !!busy.value || !view.value || !sitesReady.value);
const canSave = computed(() => open && props.modelValue && !disabled.value && !needsRefresh.value && !!values.value);
const scopeLabel = computed(() => allSites.value ? "所有当前及未来启用的站点" : draft.site_ids.length ? `仅所选 ${draft.site_ids.length} 个站点（只接管已启用站点）` : "未选择站点，不接管任何站点");
const missingSites = computed(() => draft.site_ids.filter(id => !sites.value.some(site => site.id === id)));
const current = (id: number) => open && props.modelValue && session === id;

function invalidate(): void {
  session++;
  open = false;
  controller?.abort();
  controller = null;
  loading.value = false;
  busy.value = "";
  view.value = null;
  sites.value = [];
  sitesReady.value = false;
  actions.value = [];
  error.value = "";
  actionsError.value = "";
  notice.value = "";
  needsRefresh.value = false;
}

function apply(value: unknown): void {
  if (!isAssistantTakeoverSettings(value)) throw new Error("Invalid takeover settings");
  view.value = value;
  draft.enabled = value.values.enabled;
  draft.live_access = value.values.live_access;
  draft.mode = value.values.mode;
  draft.site_ids = [...(value.values.site_ids ?? [])];
  allSites.value = value.values.site_ids === null;
  draft.max_attempts = String(value.values.max_attempts);
  draft.conclusion_ttl_days = String(value.values.conclusion_ttl_days);
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
  needsRefresh.value = true;
  error.value = cause instanceof ApiError && cause.status === 409
    ? "接管策略已被其他客户端修改。草稿已保留，未自动重试；请刷新后重新核对。"
    : mutation ? "保存失败或返回格式无效，服务端可能已保存。草稿已保留，未自动重试；请刷新核对后再操作。"
      : "无法加载接管策略或站点选项，或返回格式无效。未使用默认值覆盖，请手动刷新。";
}

async function load(): Promise<void> {
  const id = session;
  const read = new AbortController();
  controller = read;
  loading.value = true;
  error.value = "";
  actionsError.value = "";
  notice.value = "";
  const policy = Promise.all([
    apiRequest<unknown>("/assistant/takeover/settings", { signal: read.signal }),
    apiRequest<unknown>("/sites?view=options", { signal: read.signal }),
  ]).then(([settings, options]) => {
    if (!current(id)) return;
    if (!isTakeoverSiteOptions(options)) throw new Error("Invalid site options");
    apply(settings);
    sites.value = options.map(({ id, name, enabled }) => ({ id, name, enabled }));
    sitesReady.value = true;
  }).catch(async cause => { if (current(id)) await failure(cause); });
  const history = apiRequest<unknown>("/assistant/takeover/actions?limit=20", { signal: read.signal }).then(result => {
    if (!current(id)) return;
    if (!isAssistantTakeoverActions(result)) throw new Error("Invalid takeover actions");
    actions.value = result.items;
  }).catch(async cause => {
    if (!current(id)) return;
    if (cause instanceof ApiError && cause.status === 401) await failure(cause);
    else { actions.value = []; actionsError.value = "无法加载最近接管记录或返回格式无效，请手动刷新。"; }
  });
  await Promise.all([policy, history]);
  if (current(id)) { loading.value = false; controller = null; }
}

async function refresh(): Promise<void> {
  if (!open || !props.modelValue || loading.value || busy.value) return;
  const id = session;
  busy.value = "refresh";
  try {
    if (view.value) await ElMessageBox.confirm("刷新将丢弃当前草稿，并读取最新接管策略与记录。是否继续？", "刷新接管策略", { type: "warning", confirmButtonText: "丢弃草稿并刷新", cancelButtonText: "保留草稿" });
    if (current(id)) await load();
  } catch { /* Keep the draft when confirmation is cancelled. */ }
  finally { if (current(id)) busy.value = ""; }
}

async function save(): Promise<void> {
  if (!canSave.value || !view.value || !values.value) return;
  const id = session;
  const payload = { expected_revision: view.value.revision, values: values.value, confirm: true };
  const scope = scopeLabel.value;
  busy.value = "save";
  try {
    await ElMessageBox.confirm(`保存全局助手接管策略：${payload.values.enabled ? "启用" : "关闭"}；${payload.values.mode === "analysis" ? "仅分析（不保存配置，并非只读）" : "自动修复工作流"}；${scope}；最多 ${payload.values.max_attempts} 次尝试；结论复用有效期 ${payload.values.conclusion_ttl_days} 天。同配置同故障结论仅在有效期内可复用，过期后遇新合格失败重新调查；历史保留。接管失败、错误和需要人工的任务；已推迟不接管。仅限策略生效后创建、已结束的手动或调度任务，且站点已启用并在授权范围内；未知、中断、取消及未完成任务不接管。需要人工的任务仍受动态安全检查：消费结果不确定时只读证据和浏览，不执行变更单步或导入，不自动重放历史部分消费。无人值守浏览与单步验证：${payload.values.live_access ? "授权。" + liveAccessWarning : "未授权，不会访问浏览器或单步执行；仍可读取失败报告、日志和截图。"}失败报告、日志和截图可能发送给已配置的模型。自动修复仅允许修改工作流，不会重新运行完整任务，不会修改凭据、站点元数据、模板或全局设置。不跳过消费确认；结果未知时不自动重试。保存策略本身不会启动浏览器或执行任务。本授权独立于助手设置中的全局默认授权。是否确认？`, "确认助手接管范围", {
      type: "warning", confirmButtonText: "确认并保存策略", cancelButtonText: "取消", dangerouslyUseHTMLString: false,
    });
  } catch {
    if (current(id)) busy.value = "";
    return;
  }
  if (!current(id)) return;
  error.value = "";
  notice.value = "";
  try {
    // A cancelled HTTP request cannot undo a committed policy change.
    const result = await apiRequest<unknown>("/assistant/takeover/settings", { method: "PUT", body: JSON.stringify(payload) });
    if (!current(id)) return;
    apply(result);
    notice.value = "助手接管策略已保存。策略变更不能撤销已产生的业务副作用；已开始的接管请在对应会话核对最终状态。";
  } catch (cause) { if (current(id)) await failure(cause, true); }
  finally { if (current(id)) busy.value = ""; }
}

function close(): void {
  if (busy.value) return;
  invalidate();
  emit("update:modelValue", false);
}

function selectSession(id: string): void {
  if (busy.value) return;
  close();
  emit("session", id);
}

watch(() => props.modelValue, value => {
  invalidate();
  if (value) { open = true; void load(); }
}, { immediate: true, flush: "sync" });
onBeforeUnmount(invalidate);
</script>

<template>
  <el-dialog :model-value="modelValue" title="助手接管" width="min(720px, 94vw)" top="5vh"
    :close-on-click-modal="!busy" :close-on-press-escape="!busy" :show-close="!busy" @update:model-value="close">
    <el-alert type="warning" :closable="false" title="全局接管策略，默认关闭、仅分析、不选择任何站点。新策略默认勾选无人值守浏览与单步验证，核对并保存后才授权；旧策略未授权此能力，需重新勾选并保存。失败报告、日志和截图可能发送给已配置的模型；不会沿用助手设置中的默认授权作为接管同意。" />
    <p data-testid="takeover-status-scope">接管失败、错误和需要人工的任务；已推迟不接管</p>
    <p class="muted">仅限策略生效后创建、已结束的手动或调度任务，且站点已启用并在授权范围内；未知、中断、取消及未完成任务不接管。需要人工的任务仍受动态安全检查，不保证所有挑战都能自动处理。</p>
    <p v-if="loading" role="status">正在加载接管策略与记录...</p>
    <p v-if="view" class="muted">来源：{{ view.source === 'default' || (view.source === undefined && view.revision === 0) ? '默认值' : '数据库' }} · 修订：{{ view.revision }}</p>
    <el-form v-if="view" label-position="top" :disabled="disabled" @submit.prevent="save">
      <el-form-item label="全局接管开关"><el-switch v-model="draft.enabled" active-text="启用助手接管" /></el-form-item>
      <el-form-item label="接管模式">
        <el-select v-model="draft.mode" aria-label="接管模式"><el-option value="analysis" label="仅分析（不保存配置）" /><el-option value="autofix" label="自动修复工作流" /></el-select>
      </el-form-item>
      <p class="muted">仅分析不保存配置，并非只读：授权后仍可浏览、登录和单步验证，可能产生真实业务副作用。自动修复仅修改工作流，不会重新运行完整任务，也不会修改凭据、站点元数据、模板或全局设置。</p>
      <p class="muted">任务结束立即发送原任务通知，接管完成后独立发送简短结论通知；原任务状态不改变，需要人工仍为 waiting_human，不改为成功，修改后不自动重跑。</p>
      <el-form-item label="浏览与验证授权"><el-checkbox v-model="draft.live_access">授权无人值守浏览与单步验证（权限保留供后续对话）</el-checkbox></el-form-item>
      <p v-if="draft.live_access" class="muted" role="alert">{{ liveAccessWarning }}</p>
      <p v-else class="muted">未授权无人值守浏览与单步验证：不会访问浏览器或单步执行，仍可读取失败报告、日志和截图。勾选并核对保存后才授予此能力。</p>
      <p class="muted">勾选仅修改草稿，保存策略本身不会启动浏览器或执行任务。不授权自动完整运行，不跳过消费确认；结果未知时不自动重试。</p>
      <p class="muted">需要人工且无副作用风险的挑战，可在实时授权及宿主检查通过后尝试受限单步；消费结果不确定时只读证据和浏览，不执行变更单步或导入，不自动重放历史部分消费。需要人工的动态挑战不复用旧挑战分析；失败/错误任务仍按同配置、同故障去重。</p>
      <p class="muted">历史失败记录本身不会阻止分析；仅已有有效的同版本同故障调查结论才复用。</p>
      <p class="muted">同一授权用户、同一站点在同一本地日期内复用接管会话，可打开会话继续聊天；次日创建新会话，历史修复摘要仍会保留。</p>
      <el-form-item label="站点范围"><el-checkbox v-model="allSites">明确授权所有当前及未来启用的站点</el-checkbox></el-form-item>
      <el-form-item v-if="!allSites" label="选择接管站点（空列表表示不接管任何站点）">
        <el-select v-model="draft.site_ids" multiple filterable :disabled="disabled" aria-label="接管站点" placeholder="未选择站点">
          <el-option v-for="site in sites" :key="site.id" :value="site.id" :label="`${site.name} (#${site.id})${site.enabled ? '' : '（已停用）'}`" />
          <el-option v-for="id in missingSites" :key="id" :value="id" :label="`#${id}（已不可用，可移除）`" />
        </el-select>
      </el-form-item>
      <p class="muted" data-testid="takeover-scope">{{ scopeLabel }}</p>
      <el-form-item label="最多尝试次数（1..3）"><el-input v-model="draft.max_attempts" inputmode="numeric" aria-label="最多尝试次数" /></el-form-item>
      <el-form-item label="结论复用有效期（1..30 天，默认 7 天）"><el-input v-model="draft.conclusion_ttl_days" type="number" min="1" max="30" step="1" inputmode="numeric" aria-label="结论复用有效期（天）" /></el-form-item>
      <p class="muted">同配置同故障结论仅在有效期内可复用，过期后遇新合格失败重新调查；历史保留。修改有效期仅影响后续复用判断，不自动重查旧任务；复用不会延长原结论有效期。</p>
      <p v-if="!values" class="error-text" role="alert">请选择有效模式、站点、1..3 的整数尝试次数及 1..30 天的整数结论复用有效期。</p>
    </el-form>
    <p v-if="error" class="error-text" role="alert">{{ error }}</p>
    <p v-if="notice" role="status">{{ notice }}</p>
    <h3>最近 20 条接管记录</h3>
    <p class="muted">仅展示服务端授权范围内的记录，不代表所有用户的任务。</p>
    <p v-if="actionsError" class="error-text" role="alert">{{ actionsError }}</p>
    <p v-else-if="!loading && !actions.length" class="muted">暂无接管记录。</p>
    <ol class="takeover-actions" aria-label="最近接管记录">
      <li v-for="item in actions" :key="item.id">
        <span>站点 #{{ item.site_id }} · 任务 #{{ item.source_run_id }} · {{ item.mode === 'analysis' ? '仅分析' : '自动修复工作流' }} · {{ takeoverStatusLabel(item.status) }}</span>
        <p v-if="item.conclusion || item.notification_message" class="takeover-conclusion">{{ item.status === 'reused' ? '历史结论（复用，非本次重新分析）：' : '简短结论：' }}{{ item.conclusion || item.notification_message }}</p>
        <p v-if="item.attempts !== undefined" class="muted">尝试次数：{{ item.attempts }}</p>
        <p v-if="item.status === 'reused' && item.reused_from_run_id" class="muted">复用来源任务 #{{ item.reused_from_run_id }}</p>
        <time>{{ item.created_at }}</time>
        <el-button v-if="item.session_id" link type="primary" :disabled="!!busy" @click="selectSession(item.session_id)">查看接管会话</el-button>
        <span v-else class="muted">尚无可用会话</span>
      </li>
    </ol>
    <template #footer>
      <div class="takeover-footer">
        <el-button :disabled="loading || !!busy" @click="refresh">刷新策略与记录</el-button>
        <el-button :disabled="!!busy" @click="close">关闭</el-button>
        <el-button type="primary" :disabled="!canSave" :loading="busy === 'save'" @click="save">核对并保存</el-button>
      </div>
    </template>
  </el-dialog>
</template>

<style scoped>
.muted, .error-text { font-size: 13px; line-height: 1.6; overflow-wrap: anywhere; }
.error-text { color: var(--el-color-danger, #f56c6c); }
.el-select { width: 100%; }
h3 { margin-top: 24px; font-size: 15px; }
.takeover-actions { display: grid; gap: 12px; padding-left: 22px; max-height: 260px; overflow: auto; }
.takeover-actions li { overflow-wrap: anywhere; }
.takeover-conclusion { margin: 4px 0; font-size: 13px; line-height: 1.6; white-space: pre-wrap; }
.takeover-actions time { display: block; font-size: 12px; color: var(--el-text-color-secondary, #70757e); }
.takeover-footer { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 12px; }
.takeover-footer .el-button { margin-left: 0; }
:deep(.el-checkbox) { height: auto; }
:deep(.el-checkbox__label) { white-space: normal; line-height: 1.6; }
@media (max-width: 540px) { .takeover-footer > * { flex: 1 1 auto; } }
</style>
