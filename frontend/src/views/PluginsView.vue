<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref } from "vue";
import { useRouter } from "vue-router";
import { ElMessage, ElMessageBox } from "element-plus";

import { ApiError, apiRequest } from "@/api/client";
import HorizontalTabs from "@/components/HorizontalTabs.vue";
import PageHeader from "@/components/PageHeader.vue";
import { useAuthStore } from "@/stores/auth";
import type { PluginFeatureCategory, PluginRuntimeView } from "@/types/plugin";
import {
  capabilityLabel,
  capabilityType,
  checkStatusLabel,
  checkStatusType,
  pluginStatusLabel,
  pluginStatusType,
} from "@/utils/plugins";
import { formatServerDateTime } from "@/utils/datetime";
import { categoryLabel } from "@/utils/workflow";

const auth = useAuthStore();
const router = useRouter();
const plugins = ref<PluginRuntimeView[]>([]);
const selectedPluginId = ref("");
const pluginTabs = computed(() => plugins.value.map((plugin) => ({
  value: plugin.plugin_id,
  label: plugin.name,
  badge: pluginStatusLabel(plugin.status),
})));
const loading = ref(true);
type References = { reference_count: number; incompatible_count: number; items: { owner_type: string; owner_id: number; name: string; step_key: string | null; enabled: boolean; reference: string; errors: string[] }[] };
const references = reactive<Record<string, References>>({});
const impacts = reactive<Record<string, { changes?: { feature: string; code: string }[] }>>({});
async function inspectPlugin(plugin: PluginRuntimeView): Promise<void> {
  const path = `/plugin-runtime/plugins/${encodeURIComponent(plugin.plugin_id)}`;
  references[plugin.plugin_id] = await apiRequest<References>(`${path}/references`);
  impacts[plugin.plugin_id] = await apiRequest(`${path}/change-impacts`);
}
async function inspectSafely(plugin: PluginRuntimeView): Promise<void> {
  try { await inspectPlugin(plugin); }
  catch { ElMessage.error("加载引用与影响失败"); }
}
async function lifecycle(plugin: PluginRuntimeView, action: "remove" | "restore"): Promise<void> {
  try {
    await inspectPlugin(plugin);
    await ElMessageBox.confirm(action === "remove"
      ? `发现 ${references[plugin.plugin_id]?.reference_count ?? 0} 个引用（包含禁用步骤）。移除的是系统接入，不会停止或删除容器。撤销授权，保留所有配置；相关执行将失败。确认移除？`
      : "恢复后必须重新审批，不会自动恢复执行。确认恢复接入？", action === "remove" ? "移除接入" : "恢复接入", { type: "warning", confirmButtonText: "确认", cancelButtonText: "取消" });
  } catch { return; }
  setBusy(plugin.plugin_id, action, true);
  try {
    replacePlugin(await apiRequest<PluginRuntimeView>(`/plugin-runtime/plugins/${encodeURIComponent(plugin.plugin_id)}/${action}`, { method: "POST" }));
  } catch { ElMessage.error("更新接入状态失败"); }
  finally { setBusy(plugin.plugin_id, action, false); }
}
const error = ref("");
type AuditEvent = { id: number; timestamp: string; plugin_id: string | null; provider_id: string | null; job_id: number | null; site_id: number | null; step_key: string | null; event: string; result: string; reason: string; actor: string | null };
const auditItems = ref<AuditEvent[]>([]);
const auditTotal = ref(0);
const auditPage = ref(1);
const auditFilter = reactive({ plugin_id: "", event: "", result: "" });
const auditLoading = ref(false);
const auditOpen = ref(false);
async function loadAudit(reset = false): Promise<void> {
  if (reset) auditPage.value = 1;
  auditLoading.value = true;
  try {
    const params = new URLSearchParams({ ...auditFilter, page: String(auditPage.value), page_size: "25" });
    const data = await apiRequest<{ items: AuditEvent[]; total: number }>(`/plugin-runtime/audit?${params}`);
    auditItems.value = data.items;
    auditTotal.value = data.total;
  } catch (cause) {
    ElMessage.error(cause instanceof Error ? cause.message : "加载审计失败");
  } finally { auditLoading.value = false; }
}
async function revoke(plugin: PluginRuntimeView): Promise<void> {
  try {
    await ElMessageBox.confirm("撤销所有已审批权限并终止当前授权，保留插件注册信息。确认撤销？", "撤销审批", { type: "warning", confirmButtonText: "撤销审批", cancelButtonText: "取消" });
  } catch { return; }
  setBusy(plugin.plugin_id, "revoke", true);
  try {
    replacePlugin(await apiRequest<PluginRuntimeView>(`/plugin-runtime/plugins/${encodeURIComponent(plugin.plugin_id)}/revoke`, { method: "POST" }));
    ElMessage.success("已撤销审批");
  } catch (cause) { ElMessage.error(cause instanceof Error ? cause.message : "撤销失败"); }
  finally { setBusy(plugin.plugin_id, "revoke", false); }
}
const approvalSelection = reactive<Record<string, string[]>>({});
const featureCategories: PluginFeatureCategory[] = ["flow", "auth", "challenge", "sign", "action", "post"];
type CategoryDraft = {
  categories: Record<string, PluginFeatureCategory | null>;
  assistant_permissions: Record<string, boolean>;
  generation: number;
  permission_epoch: number;
};
const categoryDrafts = reactive<Record<string, CategoryDraft>>({});

function editCategory(plugin: PluginRuntimeView, key: string, category: PluginFeatureCategory | null): void {
  const draft = categoryDrafts[plugin.plugin_id] ??= {
    categories: {}, assistant_permissions: {}, generation: plugin.generation, permission_epoch: plugin.permission_epoch,
  };
  draft.categories[key] = category;
}

function editAssistantPermission(plugin: PluginRuntimeView, key: string, allowed: boolean): void {
  const draft = categoryDrafts[plugin.plugin_id] ??= {
    categories: {}, assistant_permissions: {}, generation: plugin.generation, permission_epoch: plugin.permission_epoch,
  };
  draft.assistant_permissions[key] = allowed;
}

function staleCategoryDraft(plugin: PluginRuntimeView): boolean {
  const draft = categoryDrafts[plugin.plugin_id];
  return !!draft && (draft.generation !== plugin.generation || draft.permission_epoch !== plugin.permission_epoch);
}

async function saveCategories(plugin: PluginRuntimeView): Promise<void> {
  const draft = categoryDrafts[plugin.plugin_id];
  if (!draft || staleCategoryDraft(plugin)) return;
  setBusy(plugin.plugin_id, "categories", true);
  try {
    const updated = await apiRequest<PluginRuntimeView>(
      `/plugin-runtime/plugins/${encodeURIComponent(plugin.plugin_id)}/feature-categories`,
      { method: "PUT", body: JSON.stringify(draft) },
    );
    delete categoryDrafts[plugin.plugin_id];
    replacePlugin(updated);
    ElMessage.success(Object.keys(draft.categories).length
      ? "功能分类与助手授权已保存，旧审批已撤销，请重新审批插件权限"
      : "助手授权已保存，插件通用审批保持不变");
  } catch (cause) {
    if (cause instanceof ApiError && cause.status === 409) await load(true);
    ElMessage.error(cause instanceof Error ? cause.message : "保存分类与助手授权失败");
  } finally {
    setBusy(plugin.plugin_id, "categories", false);
  }
}

const busy = reactive<Record<string, string>>({});
let refreshTimer: ReturnType<typeof setInterval> | null = null;
let loadVersion = 0;

const pendingCount = computed(
  () => plugins.value.filter((plugin) => plugin.status === "unapproved").length,
);
const availableCount = computed(
  () => plugins.value.filter((plugin) => plugin.status === "available").length,
);
const unhealthyCount = computed(
  () => plugins.value.filter((plugin) => plugin.status === "unhealthy").length,
);
const disabledCount = computed(
  () => plugins.value.filter((plugin) => plugin.status === "disabled").length,
);

function isBusy(pluginId: string, action: string): boolean {
  return busy[`${pluginId}:${action}`] === "1";
}

function setBusy(pluginId: string, action: string, value: boolean): void {
  if (value) {
    busy[`${pluginId}:${action}`] = "1";
  } else {
    delete busy[`${pluginId}:${action}`];
  }
}

function syncSelection(plugin: PluginRuntimeView, previous?: PluginRuntimeView): void {
  const existing = approvalSelection[plugin.plugin_id];
  if (!existing) {
    approvalSelection[plugin.plugin_id] = [...plugin.approved_capabilities];
    return;
  }
  approvalSelection[plugin.plugin_id] = existing.filter((item) =>
    plugin.capabilities.includes(item) &&
    (!previous || (previous.generation === plugin.generation && previous.permission_epoch === plugin.permission_epoch) ||
      plugin.approved_capabilities.includes(item)),
  );
}

function applyPlugins(items: PluginRuntimeView[]): void {
  for (const plugin of items) {
    syncSelection(plugin, plugins.value.find((previous) => previous.plugin_id === plugin.plugin_id));
  }
  plugins.value = items;
  if (!items.some((plugin) => plugin.plugin_id === selectedPluginId.value)) {
    selectedPluginId.value = items[0]?.plugin_id ?? "";
  }
}

function replacePlugin(updated: PluginRuntimeView): void {
  loadVersion++;
  syncSelection(updated, plugins.value.find((plugin) => plugin.plugin_id === updated.plugin_id));
  plugins.value = plugins.value.map((plugin) =>
    plugin.plugin_id === updated.plugin_id ? updated : plugin,
  );
}

async function load(silent = false): Promise<void> {
  const version = ++loadVersion;
  if (!silent) loading.value = true;
  error.value = "";
  try {
    const items = await apiRequest<PluginRuntimeView[]>("/plugin-runtime/plugins");
    if (version === loadVersion) applyPlugins(items);
  } catch (cause) {
    if (cause instanceof ApiError && cause.status === 401) {
      auth.clear();
      await router.replace({ name: "login" });
      return;
    }
    if (!silent) {
      error.value = cause instanceof Error ? cause.message : "加载插件列表失败";
    }
  } finally {
    loading.value = false;
  }
}

async function approve(plugin: PluginRuntimeView): Promise<void> {
  if (categoryDrafts[plugin.plugin_id] || isBusy(plugin.plugin_id, "categories")) return;
  setBusy(plugin.plugin_id, "approve", true);
  try {
    const updated = await apiRequest<PluginRuntimeView>(
      `/plugin-runtime/plugins/${encodeURIComponent(plugin.plugin_id)}/approve`,
      {
        method: "POST",
        body: JSON.stringify({
          capabilities: approvalSelection[plugin.plugin_id] ?? [],
          generation: plugin.generation,
          permission_epoch: plugin.permission_epoch,
        }),
      },
    );
    replacePlugin(updated);
    ElMessage.success(
      updated.status === "available" ? "插件已通过审批" : "已批准所选权限，仍有权限待审批",
    );
  } catch (cause) {
    ElMessage.error(cause instanceof Error ? cause.message : "审批插件失败");
  } finally {
    setBusy(plugin.plugin_id, "approve", false);
  }
}

async function disable(plugin: PluginRuntimeView): Promise<void> {
  try {
    await ElMessageBox.confirm(
      "停止后插件仍会保持连接和心跳，但不会接入系统。确认停止？",
      "停止插件",
      {
        confirmButtonText: "停止",
        cancelButtonText: "取消",
        type: "warning",
      },
    );
  } catch {
    return;
  }
  await togglePlugin(plugin, false);
}

async function enable(plugin: PluginRuntimeView): Promise<void> {
  await togglePlugin(plugin, true);
}

async function togglePlugin(plugin: PluginRuntimeView, enabled: boolean): Promise<void> {
  setBusy(plugin.plugin_id, "toggle", true);
  try {
    const updated = await apiRequest<PluginRuntimeView>(
      `/plugin-runtime/plugins/${encodeURIComponent(plugin.plugin_id)}/${
        enabled ? "enable" : "disable"
      }`,
      { method: "POST" },
    );
    replacePlugin(updated);
    ElMessage.success(
      enabled ? "插件已启用接入" : "插件已停止接入，连接和心跳保持正常",
    );
  } catch (cause) {
    ElMessage.error(cause instanceof Error ? cause.message : "更新插件状态失败");
  } finally {
    setBusy(plugin.plugin_id, "toggle", false);
  }
}

async function check(plugin: PluginRuntimeView): Promise<void> {
  setBusy(plugin.plugin_id, "check", true);
  try {
    const updated = await apiRequest<PluginRuntimeView>(
      `/plugin-runtime/plugins/${encodeURIComponent(plugin.plugin_id)}/check`,
      { method: "POST" },
    );
    replacePlugin(updated);
    const failed = updated.checks.some((item) => !item.ok && !item.skipped);
    if (failed) {
      ElMessage.warning("插件检查未通过，请查看检查项");
    } else {
      ElMessage.success("插件检查通过");
    }
  } catch (cause) {
    ElMessage.error(cause instanceof Error ? cause.message : "检查插件失败");
  } finally {
    setBusy(plugin.plugin_id, "check", false);
  }
}

function formatDate(value: string): string {
  return formatServerDateTime(value, "Asia/Shanghai");
}

onMounted(() => {
  void load();
  refreshTimer = setInterval(() => void load(true), 10000);
});

onUnmounted(() => {
  if (refreshTimer !== null) {
    clearInterval(refreshTimer);
    refreshTimer = null;
  }
});
</script>

<template>
  <PageHeader
    eyebrow="PLUGINS"
    title="插件管理"
    description="插件接入后执行协议、端点、能力、健康与 manifest 检查，经管理员审批权限后方可正常工作。"
  >
    <template #actions>
      <el-button @click="auditOpen = true">审计日志</el-button>
      <el-button :loading="loading" @click="load()">刷新</el-button>
    </template>
  </PageHeader>

  <el-alert
    v-if="error"
    :title="error"
    type="error"
    :closable="false"
    show-icon
    class="notice"
  />
  <el-alert
    v-if="pendingCount"
    :title="`有 ${pendingCount} 个插件等待权限审批。审批前不会进入正常工作状态。`"
    type="warning"
    :closable="false"
    show-icon
    class="notice"
  />

  <div class="summary-grid">
    <div class="summary-card">
      <span class="summary-label">已注册</span>
      <strong>{{ plugins.length }}</strong>
    </div>
    <div class="summary-card">
      <span class="summary-label">正常</span>
      <strong class="success">{{ availableCount }}</strong>
    </div>
    <div class="summary-card">
      <span class="summary-label">待审批</span>
      <strong class="warning">{{ pendingCount }}</strong>
    </div>
    <div class="summary-card">
      <span class="summary-label">异常</span>
      <strong class="danger">{{ unhealthyCount }}</strong>
    </div>
    <div class="summary-card">
      <span class="summary-label">已停止</span>
      <strong class="muted-value">{{ disabledCount }}</strong>
    </div>
  </div>

  <el-alert
    title="助手持续授权说明"
    type="warning"
    :closable="false"
    show-icon
    class="notice"
  >
    <p class="approval-note">勾选并保存即持续授权助手发现、使用及单步执行该功能或验证码方法，包括验证码处理与发布（post）。高风险操作可能提交数据、发布内容或产生其他外部副作用；取消勾选可撤回助手授权。插件仍须满足通用审批和可用状态。</p>
    <p class="approval-note">勾选并保存秘密提供商即允许后端在助手操作中私下使用该提供商的凭据或 TOTP，不会向模型透露密码、TOTP 种子或验证码值。凭据与 TOTP 分别授权；取消勾选可撤回助手授权，插件仍须满足通用审批和可用状态。</p>
    <p class="approval-note">仅保存助手授权不会撤销插件通用审批，但会更新权限版本。分类调整保存后会撤销旧审批，必须重新审批；风险指数显示当前已保存分类的风险。</p>
  </el-alert>

  <HorizontalTabs
    v-model="selectedPluginId"
    :items="pluginTabs"
    aria-label="选择插件"
  >
  <div v-loading="loading" class="plugin-list">
    <el-empty
      v-if="!loading && !plugins.length"
      description="暂无已注册插件。启动 demo 插件后会自动注册并出现在这里。"
    />

    <article
      v-for="plugin in plugins"
      v-show="selectedPluginId === plugin.plugin_id"
      :key="plugin.plugin_id"
      class="plugin-card"
    >
      <header class="plugin-head">
        <div class="plugin-title">
          <div class="plugin-mark">{{ plugin.name.slice(0, 1).toUpperCase() }}</div>
          <div>
            <div class="plugin-name-line">
              <h2>{{ plugin.name }}</h2>
              <el-tag :type="pluginStatusType(plugin.status)" size="small">
                {{ pluginStatusLabel(plugin.status) }}
              </el-tag>
            </div>
            <p class="mono">{{ plugin.plugin_id }} · v{{ plugin.version }}</p>
          </div>
        </div>
        <div class="plugin-actions">
          <el-button size="small" @click="inspectSafely(plugin)">引用与变更影响</el-button>
          <el-button v-if="plugin.status === 'removed'" size="small" :loading="isBusy(plugin.plugin_id, 'restore')" @click="lifecycle(plugin, 'restore')">恢复接入（需重新审批）</el-button>
          <el-button v-else size="small" type="danger" :loading="isBusy(plugin.plugin_id, 'remove')" @click="lifecycle(plugin, 'remove')">移除接入</el-button>
          <el-button v-if="plugin.approved_at" size="small" type="warning" :loading="isBusy(plugin.plugin_id, 'revoke')" @click="revoke(plugin)">撤销审批</el-button>
          <el-button
            v-if="plugin.status !== 'removed'"
            size="small"
            :loading="isBusy(plugin.plugin_id, 'check')"
            @click="check(plugin)"
          >
            重新检查
          </el-button>
          <el-button
            v-if="plugin.status === 'unapproved'"
            size="small"
            type="primary"
            :loading="isBusy(plugin.plugin_id, 'approve')"
            :disabled="!!categoryDrafts[plugin.plugin_id] || isBusy(plugin.plugin_id, 'categories')"
            @click="approve(plugin)"
          >
            通过审批
          </el-button>
          <el-button
            v-if="plugin.status === 'disabled'"
            size="small"
            type="success"
            :loading="isBusy(plugin.plugin_id, 'toggle')"
            @click="enable(plugin)"
          >
            启用插件
          </el-button>
          <el-button
            v-else-if="plugin.status !== 'removed'"
            size="small"
            type="danger"
            plain
            :loading="isBusy(plugin.plugin_id, 'toggle')"
            @click="disable(plugin)"
          >
            停止插件
          </el-button>
        </div>
      </header>

      <section v-if="references[plugin.plugin_id]" class="section">
        <h3>引用 {{ references[plugin.plugin_id]?.reference_count }} / 不兼容 {{ references[plugin.plugin_id]?.incompatible_count }}</h3>
        <p class="muted">包含禁用步骤；配置保留不变。以下检查基于当前数据库与继承后的有效配置。</p>
        <p v-for="change in impacts[plugin.plugin_id]?.changes" :key="change.feature">{{ change.feature }}: {{ change.code }}</p>
        <el-table :data="references[plugin.plugin_id]?.items">
          <el-table-column prop="owner_type" label="对象" />
          <el-table-column prop="owner_id" label="ID" />
          <el-table-column prop="name" label="名称" />
          <el-table-column prop="step_key" label="步骤" />
          <el-table-column prop="enabled" label="启用" />
          <el-table-column prop="reference" label="引用" />
          <el-table-column label="兼容性"><template #default="{ row }">{{ row.errors.join(', ') || '兼容' }}</template></el-table-column>
        </el-table>
      </section>

      <div class="meta-grid">
        <div>
          <span class="meta-label">gRPC 端点</span>
          <span class="mono">{{ plugin.endpoint }}</span>
        </div>
        <div>
          <span class="meta-label">协议版本</span>
          <span>v{{ plugin.protocol_version }}</span>
        </div>
        <div>
          <span class="meta-label">generation</span>
          <span>{{ plugin.generation }}</span>
        </div>
        <div>
          <span class="meta-label">最近心跳</span>
          <span>{{ formatDate(plugin.last_heartbeat_at) }}</span>
        </div>
      </div>

      <section class="section">
        <div class="section-head">
          <h3>声明权限</h3>
          <span class="muted">插件 manifest 声明的能力</span>
        </div>
        <div class="tag-row">
          <el-tag
            v-for="capability in plugin.capabilities"
            :key="capability"
            :type="capabilityType(capability)"
            effect="plain"
          >
            {{ capabilityLabel(capability) }}
            <span class="mono tag-key">{{ capability }}</span>
          </el-tag>
          <span v-if="!plugin.capabilities.length" class="muted">无</span>
        </div>
        <div class="metadata-counts">
          <span>功能定义 {{ plugin.feature_count }}</span>
          <span>验证码方法 {{ plugin.captcha_method_count }}</span>
        </div>
      </section>

      <section class="section">
        <div class="section-head">
          <h3>接入检查</h3>
          <span class="muted">首次注册和手动检查的结果</span>
        </div>
        <div class="check-grid">
          <div v-for="item in plugin.checks" :key="item.key" class="check-item">
            <div class="check-line">
              <strong>{{ item.label }}</strong>
              <el-tag :type="checkStatusType(item)" size="small">
                {{ checkStatusLabel(item) }}
              </el-tag>
            </div>
            <p>{{ item.message || "无详细信息" }}</p>
          </div>
        </div>
      </section>

      <section class="section">
        <div class="section-head">
          <h3>功能分类、风险与助手授权</h3>
          <span class="muted">风险指数越高，审批时越需谨慎</span>
        </div>
        <el-table :data="[...plugin.features.map(feature => ({ ...feature, isMethod: false })), ...plugin.captcha_methods.map(method => ({ ...method, isMethod: true })), ...plugin.secret_providers.map(provider => ({ ...provider, isProvider: true }))]" row-key="key" empty-text="无功能、验证码方法或秘密提供商" style="width: 100%">
          <el-table-column label="功能" min-width="180">
            <template #default="{ row }">{{ row.name }} <el-tag v-if="row.isMethod" size="small" type="info">验证码方法</el-tag><el-tag v-if="row.isProvider" size="small" type="danger">{{ row.capability === 'secret.credentials' ? '私有凭据提供商' : 'TOTP 提供商' }}</el-tag><div class="mono muted">{{ row.key }}</div><div v-if="row.isProvider" class="mono muted">{{ row.provider_id }}</div></template>
          </el-table-column>
          <el-table-column label="声明分类" min-width="100">
            <template #default="{ row }">{{ categoryLabel(row.declared_category) }}</template>
          </el-table-column>
          <el-table-column label="生效分类" width="110">
            <template #default="{ row }">
              {{ categoryLabel(row.category) }}
              <el-tag v-if="row.category_overridden" size="small" type="info">已覆盖</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="风险指数" min-width="280">
            <template #default="{ row }">
              <el-tag :type="row.risk_index >= 4 ? 'danger' : row.risk_index >= 3 ? 'warning' : 'info'">
                {{ row.risk_index }}/5 · {{ row.risk_label }}
              </el-tag>
              <div v-if="row.risk_index >= 3" class="approval-note risk-warning">可能产生外部副作用，请谨慎授权。</div>
            </template>
          </el-table-column>
          <el-table-column v-if="auth.isAuthenticated && plugin.status !== 'removed'" label="管理分类" min-width="290">
            <template #default="{ row }">
              <span v-if="row.isProvider" class="muted">提供商分类固定，不可修改</span>
              <span v-else-if="row.isMethod" class="muted">验证码方法固定为验证分类</span>
              <div v-else class="category-editor">
                <el-select
                  :model-value="categoryDrafts[plugin.plugin_id]?.categories[row.key] === null ? row.declared_category : categoryDrafts[plugin.plugin_id]?.categories[row.key] ?? row.category"
                  :aria-label="`${row.name} 分类`"
                  :disabled="isBusy(plugin.plugin_id, 'categories') || isBusy(plugin.plugin_id, 'approve') || staleCategoryDraft(plugin)"
                  @update:model-value="editCategory(plugin, row.key, $event)"
                >
                  <el-option v-for="category in featureCategories" :key="category" :label="categoryLabel(category)" :value="category" />
                </el-select>
                <el-button
                  :disabled="isBusy(plugin.plugin_id, 'categories') || isBusy(plugin.plugin_id, 'approve') || staleCategoryDraft(plugin)"
                  @click="editCategory(plugin, row.key, null)"
                >恢复声明</el-button>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="允许助手使用" width="140" align="center" header-align="center">
            <template #default="{ row }">
              <el-checkbox
                :model-value="categoryDrafts[plugin.plugin_id]?.assistant_permissions[row.key] ?? row.assistant_allowed"
                :aria-label="`${row.name} 允许助手使用`"
                :disabled="!auth.isAuthenticated || plugin.status === 'removed' || isBusy(plugin.plugin_id, 'categories') || isBusy(plugin.plugin_id, 'approve') || staleCategoryDraft(plugin)"
                @update:model-value="editAssistantPermission(plugin, row.key, Boolean($event))"
              />
            </template>
          </el-table-column>
        </el-table>
        <div v-if="categoryDrafts[plugin.plugin_id]" class="category-draft-actions">
          <span v-if="staleCategoryDraft(plugin)" class="danger">插件版本或权限已变化，请放弃草稿后重新编辑。</span>
          <span v-else class="muted">有未保存的分类或助手授权修改</span>
          <el-button type="primary" :loading="isBusy(plugin.plugin_id, 'categories')" :disabled="staleCategoryDraft(plugin) || isBusy(plugin.plugin_id, 'approve')" @click="saveCategories(plugin)">{{ Object.keys(categoryDrafts[plugin.plugin_id]!.categories).length ? '保存并撤销旧审批' : '保存助手授权' }}</el-button>
          <el-button :disabled="isBusy(plugin.plugin_id, 'categories')" @click="delete categoryDrafts[plugin.plugin_id]">放弃修改</el-button>
        </div>
      </section>

      <section v-if="plugin.status === 'unapproved'" class="approval-box">
        <div class="section-head">
          <h3>权限审批</h3>
          <span class="muted">请先核对上方功能分类与风险，再勾选允许使用的能力；新增权限不会自动勾选</span>
        </div>
        <el-checkbox-group v-model="approvalSelection[plugin.plugin_id]" class="capability-select">
          <el-checkbox
            v-for="capability in plugin.capabilities"
            :key="capability"
            :value="capability"
            border
          >
            {{ capabilityLabel(capability) }}
          </el-checkbox>
        </el-checkbox-group>
        <p v-if="plugin.approved_capabilities.length" class="approval-note">
          已批准：{{ plugin.approved_capabilities.map(capabilityLabel).join("、") }}；批准全部能力后插件才会正常工作。
        </p>
        <p v-else class="approval-note">审批通过后插件状态会变为“正常”。</p>
      </section>

      <section v-else-if="plugin.status === 'disabled'" class="stopped-box">
        <span class="stopped-icon">Ⅱ</span>
        <div>
          <strong>已停止接入</strong>
          <p>插件连接和心跳保持正常，但不会接入系统；点击“启用插件”恢复。</p>
        </div>
      </section>

      <section v-else-if="plugin.status === 'removed'" class="stopped-box">已移除系统接入，容器由外部部署管理。恢复后需重新审批。</section>
      <section v-else class="approved-box">
        <span class="approved-icon">✓</span>
        <div>
          <strong>已通过权限审批</strong>
          <p>
            {{ plugin.approved_by || "管理员" }} ·
            {{ plugin.approved_at ? formatDate(plugin.approved_at) : "-" }}
          </p>
        </div>
        <div class="approved-caps">
          <el-tag
            v-for="capability in plugin.approved_capabilities"
            :key="capability"
            size="small"
            type="success"
            effect="plain"
          >
            {{ capabilityLabel(capability) }}
          </el-tag>
        </div>
      </section>
    </article>
  </div>
  </HorizontalTabs>

  <el-dialog v-model="auditOpen" title="审计日志" width="min(1200px, 94vw)" destroy-on-close @open="loadAudit()">
      <el-form inline class="audit-filters" @submit.prevent="loadAudit(true)">
        <el-form-item label="插件"><el-input v-model="auditFilter.plugin_id" placeholder="插件 ID" /></el-form-item>
        <el-form-item label="事件"><el-select v-model="auditFilter.event" clearable style="width: 180px"><el-option v-for="item in ['register', 'approve', 'revoke', 'metadata_change', 'disable', 'enable', 'lease_issue', 'lease_consume', 'lease_reject', 'provider_resolve']" :key="item" :label="item" :value="item" /></el-select></el-form-item>
        <el-form-item label="结果"><el-select v-model="auditFilter.result" clearable style="width: 140px"><el-option v-for="item in ['success', 'denied', 'failed']" :key="item" :label="item" :value="item" /></el-select></el-form-item>
        <el-button :loading="auditLoading" @click="loadAudit(true)">查询</el-button>
      </el-form>
       <el-table :data="auditItems" v-loading="auditLoading" max-height="55vh" style="width: 100%">
        <el-table-column label="时间" min-width="170"><template #default="{ row }">{{ formatDate(row.timestamp) }}</template></el-table-column>
        <el-table-column prop="plugin_id" label="插件" width="110" />
        <el-table-column prop="provider_id" label="提供商" min-width="150" />
        <el-table-column prop="event" label="事件" min-width="150" />
        <el-table-column prop="result" label="结果" width="90" />
        <el-table-column prop="reason" label="原因" min-width="130" />
        <el-table-column prop="job_id" label="任务" width="80" />
        <el-table-column prop="site_id" label="站点" width="80" />
        <el-table-column prop="step_key" label="步骤" min-width="130" />
        <el-table-column prop="actor" label="管理员" width="110" />
      </el-table>
       <el-pagination v-model:current-page="auditPage" :page-size="25" :total="auditTotal" layout="prev, pager, next, total" class="audit-pagination" @current-change="loadAudit()" />
       <template #footer><el-button @click="auditOpen = false">关闭</el-button></template>
  </el-dialog>
</template>

<style scoped>
.risk-warning {
  white-space: nowrap;
}

.notice {
  margin-bottom: 14px;
}

.audit-filters { display: flex; flex-wrap: wrap; gap: 0 12px; }
.audit-filters :deep(.el-form-item) { margin-right: 0; max-width: 100%; }
.audit-pagination { margin-top: 16px; overflow-x: auto; }

.summary-grid {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 8px;
  margin-bottom: 16px;
}

.summary-card {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 10px 12px;
  border: 1px solid #e4e9f0;
  border-radius: 12px;
  background: rgba(255, 255, 255, 0.78);
}

.summary-card strong {
  font-size: 20px;
}

.summary-label {
  color: #7a8794;
  font-size: 12px;
}

.success {
  color: #3f9c35;
}

.warning {
  color: #c58b00;
}

.danger {
  color: #d64545;
}

.muted-value {
  color: #5f6b78;
}

.plugin-list {
  display: grid;
  gap: 14px;
  min-width: 0;
}

.plugin-card {
  min-width: 0;
  border: 1px solid #e4e9f0;
  border-radius: 16px;
  background: rgba(255, 255, 255, 0.88);
  padding: 18px;
}

.plugin-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
}

.plugin-title {
  display: flex;
  align-items: center;
  gap: 12px;
  min-width: 0;
}

.plugin-title > div:last-child {
  min-width: 0;
  overflow-wrap: anywhere;
}

.plugin-mark {
  flex-shrink: 0;
  width: 42px;
  height: 42px;
  border-radius: 12px;
  display: grid;
  place-items: center;
  background: linear-gradient(135deg, #eaf3ff, #d8eaff);
  color: #2f7ed8;
  font-weight: 800;
}

.plugin-name-line {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.plugin-name-line h2 {
  margin: 0;
  font-size: 18px;
}

.plugin-title p {
  margin: 4px 0 0;
  color: #7a8794;
  font-size: 12px;
}

.plugin-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.plugin-actions .el-button,
.category-draft-actions .el-button {
  margin-left: 0;
}

.meta-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 10px;
  margin-top: 16px;
  padding: 12px 14px;
  border-radius: 12px;
  background: #f8fafc;
}

.meta-grid > div {
  display: flex;
  flex-direction: column;
  gap: 3px;
  font-size: 13px;
  min-width: 0;
  overflow-wrap: anywhere;
}

.meta-label {
  color: #8a96a3;
  font-size: 11px;
}

.section {
  margin-top: 18px;
}

.section-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}

.section-head h3 {
  margin: 0;
  font-size: 14px;
}

.section-head span {
  font-size: 12px;
}

.tag-row {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.tag-key {
  margin-left: 6px;
  opacity: 0.65;
  font-size: 10px;
}

.metadata-counts {
  display: flex;
  gap: 16px;
  margin-top: 8px;
  color: #7a8794;
  font-size: 12px;
}

.check-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
}

.check-item {
  padding: 10px 12px;
  border: 1px solid #edf1f6;
  border-radius: 10px;
  background: #fff;
}

.check-line {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.check-line strong {
  font-size: 13px;
}

.check-item p {
  margin: 5px 0 0;
  color: #7a8794;
  font-size: 12px;
}

.approval-box,
.approved-box,
.stopped-box {
  margin-top: 18px;
  padding: 14px;
  border-radius: 12px;
}

.approval-box {
  border: 1px solid #f3dfb2;
  background: #fffbf1;
}

.capability-select {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.category-editor,
.category-draft-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

.category-editor .el-select {
  width: 130px;
}

.category-draft-actions {
  margin-top: 12px;
  flex-wrap: wrap;
}

.approval-note {
  margin: 10px 0 0;
  color: #9a6b00;
  font-size: 12px;
}

.stopped-box {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  border: 1px solid #e2e8ef;
  background: #f7f9fb;
  color: #5f6b78;
}

.stopped-box strong {
  font-size: 13px;
}

.stopped-box p {
  margin: 3px 0 0;
  color: #7a8794;
  font-size: 12px;
}

.stopped-icon {
  width: 22px;
  height: 22px;
  border-radius: 50%;
  display: grid;
  place-items: center;
  background: #e8edf3;
  color: #5f6b78;
  font-size: 11px;
  font-weight: 800;
  margin-top: 1px;
}

.approved-box {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  flex-wrap: wrap;
  border: 1px solid #d9efd4;
  background: #f6fcf4;
  color: #3d7a35;
}

.approved-icon {
  width: 22px;
  height: 22px;
  border-radius: 50%;
  display: grid;
  place-items: center;
  background: #d9efd4;
  font-size: 13px;
  font-weight: 800;
  margin-top: 1px;
}

.approved-box strong {
  font-size: 13px;
}

.approved-box p {
  margin: 3px 0 0;
  color: #6f8a6b;
  font-size: 12px;
}

.approved-caps {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  margin-left: auto;
}

@media (max-width: 900px) {
  .summary-grid,
  .meta-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .check-grid {
    grid-template-columns: 1fr;
  }

  .approved-caps {
    margin-left: 0;
    flex-basis: 100%;
  }
}

@media (max-width: 560px) {
  .summary-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .meta-grid {
    grid-template-columns: 1fr;
  }

  .plugin-card {
    padding: 14px;
  }

  .plugin-actions {
    width: 100%;
  }
}
</style>
