<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { useRouter } from "vue-router";

import { ApiError, apiRequest } from "@/api/client";
import { useAuthStore } from "@/stores/auth";
import { formatServerDateTime } from "@/utils/datetime";
import type {
  SiteDetail, TemplateDetail, WorkflowHistoryTarget,
  WorkflowVersionDetail, WorkflowVersionList, WorkflowVersionSummary,
} from "@/types/workflow";

const props = defineProps<{
  modelValue: boolean;
  target: WorkflowHistoryTarget | null;
}>();
const emit = defineEmits<{
  (event: "update:modelValue", value: boolean): void;
  (event: "restored"): void;
}>();
const auth = useAuthStore();
const router = useRouter();
const versions = ref<WorkflowVersionList | null>(null);
const snapshot = ref<WorkflowVersionDetail | null>(null);
const selectedRevision = ref<number | null>(null);
const page = ref(1);
const pageSize = 20;
const loading = ref(false);
const snapshotLoading = ref(false);
const restoring = ref(false);
const error = ref("");
const snapshotError = ref("");
const restoreError = ref("");
const acceptCurrentDependencies = ref(false);
let session = 0;
let listRequest = 0;
let detailRequest = 0;
let restoreRequest = 0;

const title = computed(() => `${props.target?.name ?? ""} · 配置历史`);
const canRestore = computed(() => Boolean(
  props.modelValue && props.target && versions.value && snapshot.value &&
  props.target.historyKey && versions.value.history_key === props.target.historyKey &&
  snapshot.value.revision === selectedRevision.value &&
  snapshot.value.history_key === versions.value.history_key &&
  snapshot.value.owner_type === props.target.ownerType &&
  snapshot.value.operation !== "delete" &&
  !loading.value && !snapshotLoading.value && !restoring.value,
));

function basePath(target: WorkflowHistoryTarget): string {
  return `/${target.ownerType === "site" ? "sites" : "templates"}/${target.id}/versions`;
}

async function handleUnauthorized(cause: unknown): Promise<boolean> {
  if (cause instanceof ApiError && cause.status === 401) {
    auth.clear();
    await router.replace({ name: "login" });
    return true;
  }
  return false;
}

function clearSnapshot(): void {
  detailRequest++;
  restoreRequest++;
  snapshot.value = null;
  selectedRevision.value = null;
  snapshotLoading.value = false;
  restoring.value = false;
  snapshotError.value = "";
  restoreError.value = "";
  acceptCurrentDependencies.value = false;
}

async function loadVersions(): Promise<void> {
  const target = props.target;
  if (!props.modelValue || !target) return;
  const currentSession = session;
  const request = ++listRequest;
  clearSnapshot();
  versions.value = null;
  loading.value = true;
  error.value = "";
  try {
    if (!target.historyKey) throw new Error("缺少对象身份，请关闭历史并刷新列表后重试");
    const result = await apiRequest<WorkflowVersionList>(
      `${basePath(target)}?limit=${pageSize}&offset=${(page.value - 1) * pageSize}`,
    );
    if (currentSession !== session || request !== listRequest) return;
    if (result.history_key !== target.historyKey) {
      error.value = "对象已替换，请关闭历史并刷新列表后重新打开";
      return;
    }
    versions.value = result;
  } catch (cause) {
    if (await handleUnauthorized(cause)) return;
    if (currentSession !== session || request !== listRequest) return;
    error.value = cause instanceof Error ? cause.message : "加载历史失败";
  } finally {
    if (currentSession === session && request === listRequest) loading.value = false;
  }
}

function changePage(value: number): void {
  page.value = value;
  void loadVersions();
}

async function viewSnapshot(revision: number): Promise<void> {
  const target = props.target;
  if (!props.modelValue || !target || !versions.value || loading.value || restoring.value) return;
  clearSnapshot();
  const currentSession = session;
  const request = ++detailRequest;
  selectedRevision.value = revision;
  snapshotLoading.value = true;
  try {
    const result = await apiRequest<WorkflowVersionDetail>(`${basePath(target)}/${revision}`);
    if (currentSession !== session || request !== detailRequest) return;
    if (result.history_key !== target.historyKey) {
      snapshotError.value = "对象已替换，请关闭历史并刷新列表后重新打开";
      return;
    }
    snapshot.value = result;
  } catch (cause) {
    if (await handleUnauthorized(cause)) return;
    if (currentSession !== session || request !== detailRequest) return;
    snapshotError.value = cause instanceof Error ? cause.message : "加载快照失败";
  } finally {
    if (currentSession === session && request === detailRequest) snapshotLoading.value = false;
  }
}

async function restore(): Promise<void> {
  if (!canRestore.value || !props.target || !versions.value || !snapshot.value) return;
  // Capture everything before confirmation; never retarget a pending confirmation.
  const target = { ...props.target };
  const revision = snapshot.value.revision;
  const expectedRevision = versions.value.current_revision;
  const expectedHistoryKey = versions.value.history_key;
  const acceptDependencies = acceptCurrentDependencies.value;
  const currentSession = session;
  const currentDetail = detailRequest;
  const request = ++restoreRequest;
  restoring.value = true;
  restoreError.value = "";
  try {
    try {
      await ElMessageBox.confirm(
        `确认将 ${target.name} 的 v${revision} 本地配置恢复为新版本？当前版本为 v${expectedRevision}。` +
        (acceptDependencies ? "已选择接受当前依赖，将使用当前模板和全局策略校验。" : "依赖发生漂移时将拒绝恢复。") +
        "恢复不含登录凭据或 Cookie，不递归回滚模板，也不撤销已执行的业务操作。",
        "恢复为新版本",
        { confirmButtonText: "确认恢复", cancelButtonText: "取消", type: "warning" },
      );
    } catch {
      return;
    }
    if (currentSession !== session || currentDetail !== detailRequest || request !== restoreRequest) return;
    await apiRequest<SiteDetail | TemplateDetail>(`${basePath(target)}/${revision}/restore`, {
      method: "POST",
      body: JSON.stringify({
        expected_revision: expectedRevision,
        expected_history_key: expectedHistoryKey,
        accept_current_dependencies: acceptDependencies,
      }),
    });
    // A completed mutation still requires the parent list to refresh, even after closing.
    emit("restored");
    if (currentSession !== session || currentDetail !== detailRequest || request !== restoreRequest) return;
    ElMessage.success("已恢复为新版本");
    page.value = 1;
    await loadVersions();
  } catch (cause) {
    if (await handleUnauthorized(cause)) return;
    if (currentSession !== session || currentDetail !== detailRequest || request !== restoreRequest) return;
    const message = cause instanceof Error ? cause.message : "恢复失败";
    restoreError.value = cause instanceof ApiError && cause.status === 409
      ? `${message}。未自动重试；请检查依赖或刷新历史后重新确认。`
      : message;
  } finally {
    if (currentSession === session && request === restoreRequest) restoring.value = false;
  }
}

function operationLabel(item: WorkflowVersionSummary): string {
  const labels = { baseline: "基线", create: "创建", update: "更新", restore: "恢复", delete: "删除" };
  return labels[item.operation];
}

watch(
  () => [props.modelValue, props.target?.ownerType, props.target?.id, props.target?.historyKey],
  () => {
    session++;
    listRequest++;
    clearSnapshot();
    versions.value = null;
    error.value = "";
    loading.value = false;
    page.value = 1;
    if (props.modelValue && props.target) void loadVersions();
  },
  { immediate: true, flush: "sync" },
);

onBeforeUnmount(() => {
  session++;
  listRequest++;
  clearSnapshot();
});
</script>

<template>
  <el-dialog
    :model-value="modelValue"
    :title="title"
    width="min(1000px, 96vw)"
    top="4vh"
    @update:model-value="emit('update:modelValue', $event)"
  >
    <el-alert
      title="历史 JSON 为管理员可见的完整普通配置。恢复仅使用 local，并按当前模板和全局策略校验；不含登录凭据或 Cookie，不递归回滚模板，不撤销业务操作。"
      type="info" :closable="false" show-icon class="notice"
    />
    <div class="history-toolbar">
      <span class="muted">当前版本：{{ versions ? `v${versions.current_revision}` : "-" }}</span>
      <el-button :loading="loading" :disabled="restoring" @click="loadVersions">刷新历史</el-button>
    </div>
    <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon class="notice" />
    <el-table :data="versions?.items ?? []" v-loading="loading" row-key="revision" max-height="300">
      <el-table-column label="版本" width="80">
        <template #default="{ row }">v{{ row.revision }}</template>
      </el-table-column>
      <el-table-column label="操作" min-width="130">
        <template #default="{ row }">
          {{ operationLabel(row) }}
          <span v-if="row.restored_from_revision !== null">（来自 v{{ row.restored_from_revision }}）</span>
        </template>
      </el-table-column>
      <el-table-column label="时间" min-width="180">
        <template #default="{ row }">{{ formatServerDateTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column prop="created_by" label="操作人" min-width="120" show-overflow-tooltip />
      <el-table-column label="操作" width="100" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" :disabled="loading || restoring" @click="viewSnapshot(row.revision)">查看快照</el-button>
        </template>
      </el-table-column>
    </el-table>
    <div class="history-pagination">
      <el-pagination
        :current-page="page" :page-size="pageSize" :total="versions?.total ?? 0"
        layout="prev, pager, next" :pager-count="5" small
        :disabled="loading || restoring" @current-change="changePage"
      />
    </div>
    <div v-loading="snapshotLoading" class="snapshot-panel">
      <el-alert v-if="snapshotError" :title="snapshotError" type="error" :closable="false" show-icon class="notice" />
      <template v-if="snapshot">
        <h3>v{{ snapshot.revision }} 快照</h3>
        <p class="muted snapshot-meta">Schema {{ snapshot.schema_version }} · SHA-256 {{ snapshot.content_hash }}</p>
        <el-alert v-if="snapshot.snapshot.resolution_error" :title="snapshot.snapshot.resolution_error" type="warning" :closable="false" show-icon />
        <el-tabs>
          <el-tab-pane label="本地配置 local">
            <pre class="config-preview">{{ JSON.stringify(snapshot.snapshot.local, null, 2) }}</pre>
          </el-tab-pane>
          <el-tab-pane label="依赖 dependencies">
            <pre class="config-preview">{{ JSON.stringify(snapshot.snapshot.dependencies, null, 2) }}</pre>
          </el-tab-pane>
          <el-tab-pane label="解析结果 resolved">
            <pre class="config-preview">{{ JSON.stringify(snapshot.snapshot.resolved, null, 2) }}</pre>
          </el-tab-pane>
        </el-tabs>
        <el-checkbox v-model="acceptCurrentDependencies" :disabled="restoring" class="dependency-choice">
          接受当前依赖（允许历史依赖漂移，使用当前模板及全局策略；缺失依赖或版本不兼容仍会拒绝）
        </el-checkbox>
        <el-alert v-if="restoreError" :title="restoreError" type="error" :closable="false" show-icon class="notice" />
        <el-button type="primary" :disabled="!canRestore" :loading="restoring" @click="restore">恢复为新版本</el-button>
      </template>
    </div>
    <template #footer>
      <el-button @click="emit('update:modelValue', false)">关闭</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.notice { margin-bottom: 14px; }
.history-toolbar { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 12px; }
.history-pagination { display: flex; justify-content: flex-end; margin: 14px 0; }
.snapshot-panel { min-height: 32px; }
.snapshot-meta { overflow-wrap: anywhere; font-size: 12px; }
.config-preview {
  max-height: 320px;
  overflow: auto;
  margin: 8px 0 14px;
  padding: 10px;
  border-radius: 8px;
  background: #f4f6f9;
  font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
  font-size: 12px;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.dependency-choice { height: auto; margin-bottom: 14px; align-items: flex-start; }
.dependency-choice :deep(.el-checkbox__label) { white-space: normal; overflow-wrap: anywhere; }
</style>
