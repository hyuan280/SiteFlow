<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue";
import { ElMessageBox } from "element-plus";
import { ApiError, getModelRegistry, saveModelRegistry, testModel } from "@/api/client";
import type { VisionUpdate } from "@/types/integrations";
import type { ModelMetadata, ModelRegistryView, ModelTestConfig, ModelUsage, ModelView } from "@/types/model-registry";
import { probeModelInBrowser, safeModelTestResult, validateModelProbe } from "@/utils/model-test";

const emit = defineEmits<{ unauthorized: [error: ApiError] }>();
const saved = ref<ModelRegistryView | null>(null);
const items = ref<ModelView[]>([]);
const busy = ref(false);
const conflict = ref(false);
const notice = ref<{ ok: boolean; message: string } | null>(null);
const editingId = ref<string | null>(null);
const editingConfig = ref<ModelView["config"] | null>(null);
const dialogOpen = ref(false);
const formError = ref("");
const tokenTouched = ref(false);
const emptyForm = () => ({ name: "", base_url: "", model: "", api_key: "", enabled: true,
  usages: [] as ModelUsage[], supports_vision: true, max_tokens: "" });
const form = ref(emptyForm());
const fields = ["base_url", "model", "api_key", "supports_vision", "max_tokens"] as const;
const usageOptions: { value: ModelUsage; label: string }[] = [
  { value: "feature_cards", label: "功能卡片" },
  { value: "assistant", label: "智能助手" },
  { value: "mcp", label: "MCP" },
];
let active = true;
let generation = 0;
let browserProbe: AbortController | null = null;
const metadata = (item: ModelMetadata): ModelMetadata => ({
  id: item.id, name: item.name, enabled: item.enabled, usages: [...item.usages],
});
const dirty = computed(() => JSON.stringify(items.value.map(metadata)) !==
  JSON.stringify((saved.value?.items ?? []).map(metadata)));
const blocked = computed(() => busy.value || !saved.value);
const dbCount = computed(() => items.value.filter(item => item.source === "db").length);

function report(error: unknown, mutation?: "save") {
  if (!active) return;
  if (error instanceof ApiError && error.status === 401) {
    closeEditor();
    emit("unauthorized", error);
    notice.value = { ok: false, message: "登录已失效，请重新登录。" };
    return;
  }
  if (mutation === "save" &&
    (!(error instanceof ApiError) || error.status >= 500 || error.status === 408)) {
    conflict.value = true;
    notice.value = { ok: false, message: "保存可能已完成，但未能确认结果。草稿已保留，请刷新确认后再操作；不会自动重试。" };
    return;
  }
  const messages: Record<string, string> = {
    model_registry_conflict: "模型设置已被其他页面修改。草稿已保留，请刷新后重新编辑；不会自动重试。",
    model_config_locked: "配置已被环境变量锁定。草稿已保留，请刷新后重新编辑。",
    model_registry_unavailable: "模型设置暂不可用，请检查服务端配置后重试。",
    model_registry_limit: "模型配置超出容量限制，请减少配置内容。",
    model_config_incomplete: "新增模型需要完整的地址、模型名称和 API Key。",
    invalid_model_registry: "模型配置无效，请检查输入内容。",
    invalid_model_registry_response: "模型设置响应格式无效，已保留当前草稿。请刷新确认最新状态。",
    model_not_found: "该模型已不存在，请刷新模型列表。",
    invalid_model_usage: "模型用途无效，请刷新后重新编辑。",
    request_limit: "模型配置请求过大，请减少配置内容。",
    request_timeout: "模型设置请求超时，请稍后重试。",
  };
  if (error instanceof ApiError && error.status === 409) conflict.value = true;
  notice.value = { ok: false, message: error instanceof ApiError
    ? messages[error.code ?? ""] ?? (error.status === 403 ? "没有操作权限或安全凭据已失效，请刷新页面。" :
      error.status === 409 ? messages.model_registry_conflict! : "模型设置请求失败，请稍后重试。")
    : conflict.value ? "无法刷新模型设置，仍需刷新确认最新状态后才能保存。" : "无法连接模型设置服务，请稍后重试。" };
}

function hydrate(value: ModelRegistryView) {
  saved.value = value;
  items.value = value.items.map(item => ({ ...item, usages: [...item.usages] }));
  conflict.value = false;
}

async function load() {
  if (busy.value) return;
  if (dirty.value || dialogOpen.value) {
    try {
      await ElMessageBox.confirm("刷新将丢弃排序、启用状态、删除和弹窗中的未保存编辑，是否继续？", "刷新模型设置", {
        type: "warning", confirmButtonText: "丢弃草稿并刷新", cancelButtonText: "保留草稿",
      });
    } catch { return; }
    if (!active || busy.value) return;
  }
  busy.value = true;
  const request = ++generation;
  try {
    const value = await getModelRegistry();
    if (!active || request !== generation) return;
    closeEditor();
    hydrate(value);
    notice.value = null;
  } catch (error) { if (request === generation) report(error); }
  finally { if (active && request === generation) busy.value = false; }
}

function closeEditor() {
  if (browserProbe) {
    generation++;
    browserProbe.abort();
    browserProbe = null;
    busy.value = false;
  }
  dialogOpen.value = false;
  editingId.value = null;
  editingConfig.value = null;
  form.value = emptyForm();
  formError.value = "";
  tokenTouched.value = false;
}

function edit(id?: string) {
  if (blocked.value || dialogOpen.value || (!id && dbCount.value >= 20)) return;
  const item = id ? items.value.find(item => item.id === id) : undefined;
  if (id && (!item || !item.config_editable)) return;
  editingId.value = item?.id ?? crypto.randomUUID();
  editingConfig.value = item?.config ?? null;
  const config = item?.config;
  form.value = {
    name: item?.name ?? "", enabled: item?.enabled ?? true, usages: [...(item?.usages ?? [])],
    base_url: config?.base_url.value ?? "", model: config?.model.value ?? "", api_key: "",
    supports_vision: config?.supports_vision.value ?? true,
    max_tokens: config?.max_tokens.value == null ? "" : String(config.max_tokens.value),
  };
  tokenTouched.value = false;
  formError.value = "";
  dialogOpen.value = true;
}

function move(id: string, direction: number) {
  if (blocked.value || dialogOpen.value) return;
  const index = items.value.findIndex(item => item.id === id);
  const target = index + direction;
  if (index < 0 || target < 0 || target >= items.value.length) return;
  const [item] = items.value.splice(index, 1);
  items.value.splice(target, 0, item!);
}

async function remove(id: string) {
  if (blocked.value || dialogOpen.value || !items.value.some(item => item.id === id && item.source === "db")) return;
  try {
    await ElMessageBox.confirm("将从草稿中移除此模型，保存列表后不再用于调用。旧密钥可能加密保留，仅用于历史日志脱敏，不再参与模型选择。", "删除模型", {
      type: "warning", confirmButtonText: "从草稿移除", cancelButtonText: "取消",
    });
  } catch { return; }
  if (!active || blocked.value || dialogOpen.value) return;
  items.value = items.value.filter(item => item.id !== id);
}

async function save(withConfig = false) {
  if (blocked.value || conflict.value || (!withConfig && (dialogOpen.value || !dirty.value))) return;
  const payload = { expected_revision: saved.value!.revision, items: items.value.map(metadata) as
    (ModelMetadata & { config?: VisionUpdate })[] };
  if (withConfig) {
    if (!dialogOpen.value || !editingId.value) return;
    if (items.value.some(item => item.id === editingId.value && !item.config_editable)) return;
    const current = editingConfig.value;
    const config: VisionUpdate = {};
    const name = form.value.name.trim();
    if (!name || name.length > 128) { formError.value = "请填写名称（最多 128 个字符）。"; return; }
    for (const key of ["base_url", "model", "api_key"] as const) {
      const value = form.value[key].trim();
      if (key === "api_key" && !value && current) continue;
      if (key !== "api_key" && current && value === (current[key].value ?? "")) continue;
      if (!value) { formError.value = "地址、模型名称和新增模型的 API Key 不能为空。"; return; }
      config[key] = value;
    }
    if (!current || form.value.supports_vision !== current.supports_vision.value) {
      config.supports_vision = form.value.supports_vision;
    }
    const initialCap = current?.max_tokens.value == null ? "" : String(current.max_tokens.value);
    if (form.value.max_tokens !== initialCap || tokenTouched.value && form.value.max_tokens === "") {
      const value = form.value.max_tokens;
      if (typeof value !== "string" || !/^\d*$/.test(value.trim()) || !Number.isSafeInteger(Number(value)) || Number(value) > 2147483647) {
        formError.value = "max_tokens 必须为 0 到 2147483647 的非负整数。"; return;
      }
      config.max_tokens = value.trim() === "" ? null : Number(value);
    }
    const item = { id: editingId.value, name, enabled: form.value.enabled, usages: [...form.value.usages],
      ...(Object.keys(config).length ? { config } : {}) };
    const index = payload.items.findIndex(item => item.id === editingId.value);
    if (index < 0) {
      if (dbCount.value >= 20) return;
      payload.items.push(item);
    } else payload.items[index] = item;
  }
  busy.value = true;
  const request = ++generation;
  formError.value = "";
  try {
    const value = await saveModelRegistry(payload);
    if (!active || request !== generation) return;
    hydrate(value);
    closeEditor();
    notice.value = { ok: true, message: "模型设置已保存。" };
  } catch (error) { if (request === generation) report(error, "save"); }
  finally { if (active && request === generation) busy.value = false; }
}

async function test(id: string) {
  if (blocked.value || !saved.value?.items.some(item => item.id === id && item.config.available)) return;
  if (dirty.value || dialogOpen.value) {
    try {
      await ElMessageBox.confirm("存在未保存编辑。本次只测试该 ID 的已保存配置，不会保存或测试草稿。", "测试已保存配置", {
        confirmButtonText: "仅测试已保存配置", cancelButtonText: "取消",
      });
    } catch { return; }
    if (!active || blocked.value) return;
  }
  busy.value = true;
  const request = ++generation;
  try {
    const result = await testModel(id);
    if (!active || request !== generation) return;
    const config = saved.value!.items.find(item => item.id === id)!.config;
    const safe = safeModelTestResult(result, [config.base_url.value ?? "", config.model.value ?? "",
      form.value.api_key.trim(), form.value.base_url.trim(), form.value.model.trim()]);
    notice.value = { ok: safe.ok, message: `后端复用已保存密钥 · 仅测试已保存配置（不保存）：${safe.message}` };
  } catch (error) { if (request === generation) report(error); }
  finally { if (active && request === generation) busy.value = false; }
}

async function testCurrent() {
  if (!active || blocked.value || !dialogOpen.value || !editingId.value) return;
  if (items.value.some(item => item.id === editingId.value && !item.config_editable)) return;
  const current = editingConfig.value;
  const values = { ...form.value };
  const config = validateModelProbe(values, !current?.api_key.configured, current?.max_tokens.value);
  if (typeof config === "string") { formError.value = config; return; }
  const direct = !!config.api_key;
  const patch: ModelTestConfig = {};
  if (current && !direct) {
    for (const key of ["base_url", "model"] as const) {
      const initial = (current[key].value ?? "").trim();
      if (config[key] !==
        (key === "base_url" ? initial.replace(/\/+$/, "") : initial)) patch[key] = config[key];
    }
    if (config.supports_vision !== current.supports_vision.value) patch.supports_vision = config.supports_vision;
    if (config.max_tokens !== current.max_tokens.value || tokenTouched.value && values.max_tokens.trim() === "" && current.max_tokens.error) patch.max_tokens = config.max_tokens;
  }
  const source = direct ? "浏览器直连" : "后端复用已保存密钥";
  busy.value = true;
  const request = ++generation;
  const id = editingId.value;
  const revision = saved.value!.revision;
  formError.value = "";
  notice.value = null;
  try {
    if (!direct && patch.base_url !== undefined) {
      try {
        await ElMessageBox.confirm(`使用已保存密钥向当前地址发送测试请求（不保存），核对目标：${config.base_url}`, "核对测试目标", {
          type: "warning", confirmButtonText: "向当前地址测试", cancelButtonText: "取消",
        });
      } catch { return; }
    }
    if (!active || request !== generation || !dialogOpen.value) return;
    if (direct) browserProbe = new AbortController();
    const result = direct ? await probeModelInBrowser(config, browserProbe!.signal)
      : await testModel(id, { expected_revision: revision, config: patch });
    if (!active || request !== generation || !dialogOpen.value) return;
    const safe = safeModelTestResult(result, [config.api_key, config.base_url, config.model]);
    notice.value = { ok: safe.ok, message: `${source} · 当前配置（不保存）：${safe.message}` };
  } catch (error) {
    if (request === generation && active) {
      report(error);
      if (dialogOpen.value && notice.value) notice.value.message = `${source} · 当前配置（不保存）：${notice.value.message}`;
    }
  } finally {
    if (active && request === generation) { browserProbe = null; busy.value = false; }
  }
}

onMounted(() => { void load(); });
onUnmounted(() => { active = false; generation++; closeEditor(); });
</script>

<template>
  <el-card shadow="never" class="model-card" :aria-busy="busy">
    <template #header>
      <div class="card-head">
        <div class="actions"><strong>大模型</strong><el-tag size="small" type="info">模型 {{ items.length }}</el-tag></div>
        <div class="actions">
          <el-button size="small" :disabled="busy" @click="load">刷新</el-button>
          <el-button size="small" type="primary" plain :disabled="blocked || dbCount >= 20 || dialogOpen" @click="edit()">添加模型</el-button>
        </div>
      </div>
    </template>
    <p class="hint">按列表顺序选择符合用途及能力的已启用模型。已配置仅表示配置完整，不代表连接健康。</p>
    <p v-if="busy && !saved" role="status" class="hint">正在加载模型设置...</p>
    <div role="list" aria-label="模型优先顺序">
      <div v-for="(item, index) in items" :key="item.id" class="model-row" role="listitem">
        <div class="order-controls">
          <span class="order-number">{{ index + 1 }}</span>
          <el-button size="small" :aria-label="`上移 ${item.name}`" :disabled="blocked || dialogOpen || index === 0" @click="move(item.id, -1)">上移</el-button>
          <el-button size="small" :aria-label="`下移 ${item.name}`" :disabled="blocked || dialogOpen || index === items.length - 1" @click="move(item.id, 1)">下移</el-button>
        </div>
        <div class="model-name">
          <strong :title="item.name">{{ item.name }}</strong>
          <span :title="item.config.model.value ?? ''">{{ item.config.model.value || '未配置模型' }}</span>
        </div>
        <el-tag v-if="item.source === 'env'" size="small" type="warning" effect="plain">环境变量</el-tag>
        <el-switch v-model="item.enabled" :aria-label="`启用 ${item.name}`" :disabled="blocked || dialogOpen" />
        <div class="usages">
          <el-tag v-for="usage in item.usages" :key="usage" size="small" type="info">{{ usageOptions.find(option => option.value === usage)?.label }}</el-tag>
          <span v-if="!item.usages.length" class="hint">未指定用途</span>
        </div>
        <el-tag class="capability" :type="item.config.supports_vision.value ? 'success' : 'info'" size="small" effect="plain">{{ item.config.supports_vision.value ? '视觉' : '仅文本' }}</el-tag>
        <el-tag :type="item.config.available ? 'success' : 'info'" size="small">{{ item.config.available ? '已配置' : '未配置' }}</el-tag>
        <div class="actions row-actions">
          <el-button size="small" :disabled="blocked || dialogOpen || !item.config_editable" @click="edit(item.id)">配置</el-button>
          <el-button size="small" :disabled="blocked || !item.config.available" @click="test(item.id)">测试</el-button>
          <el-button v-if="item.source === 'db'" size="small" type="danger" plain :disabled="blocked || dialogOpen" @click="remove(item.id)">删除</el-button>
        </div>
      </div>
    </div>
    <el-alert v-if="notice" class="notice" :title="notice.message" :type="notice.ok ? 'success' : 'error'" :closable="false" show-icon />
    <div class="card-footer">
      <span class="hint">{{ dirty ? '有未保存的列表更改。' : '按顺序选择符合用途及能力的已启用模型。' }}</span>
      <el-button type="primary" :loading="busy" :disabled="blocked || !dirty || conflict || dialogOpen" @click="save()">保存排序与启用状态</el-button>
    </div>

    <el-dialog :model-value="dialogOpen" :title="editingConfig ? '配置模型' : '添加模型'" width="min(640px, calc(100vw - 32px))"
      :close-on-click-modal="false" :close-on-press-escape="!busy" :show-close="!busy" destroy-on-close
      @update:model-value="!$event && !busy && closeEditor()">
      <el-form label-position="top" :disabled="busy" @submit.prevent="save(true)">
        <el-form-item label="显示名称"><el-input v-model="form.name" maxlength="128" autocomplete="off" /></el-form-item>
        <el-form-item v-for="key in fields" :key="key">
          <template #label>
            {{ key === 'base_url' ? 'OpenAI 兼容 Base URL（以 /v1 结尾）' : key === 'model' ? '模型名称' : key === 'api_key' ? 'API Key' : key === 'supports_vision' ? '支持视觉（识别图片）' : '输出 Token 上限（max_tokens）' }}
          </template>
          <el-switch v-if="key === 'supports_vision'" v-model="form.supports_vision" />
          <el-input v-else-if="key === 'max_tokens'" v-model="form.max_tokens" clearable inputmode="numeric" aria-label="max_tokens"
            placeholder="留空或 0 使用服务端默认上限" @input="tokenTouched = true" @clear="tokenTouched = true" />
          <el-input v-else v-model="form[key]" :type="key === 'api_key' ? 'password' : 'text'" :show-password="key === 'api_key'"
            :maxlength="key === 'base_url' ? 2048 : key === 'model' ? 256 : 4096"
            :autocomplete="key === 'api_key' ? 'new-password' : 'off'" :placeholder="key === 'api_key' ? (editingConfig ? '留空保留已保存密钥' : '新增模型必须填写密钥') : ''" />
          <span v-if="editingConfig && 'error' in editingConfig[key] && editingConfig[key].error" class="field-error">此字段的已保存配置无效，请检查。</span>
        </el-form-item>
        <el-form-item label="启用"><el-switch v-model="form.enabled" /></el-form-item>
        <el-form-item label="用途">
          <el-checkbox-group v-model="form.usages"><el-checkbox v-for="option in usageOptions" :key="option.value" :value="option.value">{{ option.label }}</el-checkbox></el-checkbox-group>
        </el-form-item>
        <p class="hint">保存前请测试连通性，测试不代表保存配置</p>
        <el-alert v-if="formError" :title="formError" type="error" :closable="false" />
        <el-alert v-if="notice" :title="notice.message" :type="notice.ok ? 'success' : 'error'" :closable="false" />
      </el-form>
      <template #footer>
        <div class="actions dialog-actions">
          <el-button :disabled="busy" @click="closeEditor">取消</el-button>
          <el-button v-if="conflict" :disabled="busy" @click="load">刷新并重新编辑</el-button>
          <el-button :disabled="blocked" @click="testCurrent">测试当前配置</el-button>
          <el-button type="primary" :loading="busy" :disabled="blocked || conflict" @click="save(true)">保存模型与列表</el-button>
        </div>
      </template>
    </el-dialog>
  </el-card>
</template>

<style scoped>
.model-card { border-radius: 16px; border-color: #e4e9f0; margin-bottom: 18px; min-width: 0; }
.card-head, .actions, .order-controls, .usages, .card-footer { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.card-head, .card-footer { justify-content: space-between; }
.model-row { display: flex; align-items: center; flex-wrap: wrap; gap: 12px; padding: 12px 0; border-top: 1px solid #e4e9f0; min-width: 0; }
.order-controls { flex-shrink: 0; gap: 4px; }
.order-number { width: 18px; color: #7a8794; font-size: 12px; }
.model-name { flex: 1 1 120px; min-width: 0; display: flex; flex-direction: column; gap: 3px; }
.model-name strong, .model-name span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.model-name span, .hint { color: #7a8794; font-size: 12px; overflow-wrap: anywhere; }
.hint { margin: 0 0 12px; }
span.hint { margin: 4px 0 0; }
.usages { max-width: 270px; }
.row-actions { flex-shrink: 0; }
.actions :deep(.el-button + .el-button), .order-controls :deep(.el-button + .el-button) { margin-left: 0; }
.notice { margin-top: 12px; }
.card-footer { margin-top: 16px; }
.dialog-actions { justify-content: flex-end; }
.capability { flex-shrink: 0; }
.field-error { font-size: 12px; color: #d9534f; }
@media (max-width: 1100px) {
  .model-row { flex-wrap: wrap; }
  .model-name { flex-basis: calc(100% - 170px); }
  .row-actions { margin-left: auto; }
}
@media (max-width: 520px) {
  .model-row { gap: 8px; }
  .model-name { flex-basis: calc(100% - 150px); }
  .usages { max-width: 100%; }
  .row-actions { flex-wrap: wrap; margin-left: 0; }
  .card-footer { align-items: flex-start; }
}
</style>
