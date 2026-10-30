<script setup lang="ts">
import { computed, nextTick, onMounted, reactive, ref } from "vue";
import { onBeforeRouteLeave, useRouter } from "vue-router";
import { ElMessage, ElMessageBox } from "element-plus";

import { ApiError, apiRequest } from "@/api/client";
import CopyConfigDialog from "@/components/CopyConfigDialog.vue";
import ExportSelectionDialog from "@/components/ExportSelectionDialog.vue";
import SignalEditor from "@/components/SignalEditor.vue";
import WorkflowStepEditor from "@/components/WorkflowStepEditor.vue";
import { useAuthStore } from "@/stores/auth";
import { usePersistedSearch } from "@/utils/persistedSearch";
import type { ExportItem } from "@/types/export";
import type {
  Feature,
  GroupForm,
  GroupView,
  ResolvedWorkflow,
  ResolvedSignalView,
  SignalForm,
  StepForm,
  TemplateDetail,
  TemplateSummary,
} from "@/types/workflow";
import {
  importArray,
  importText,
  parseExportFile,
  type ImportRecord,
} from "@/utils/exportFile";
import { missingRequiredFields } from "@/utils/schema";
import {
  inheritedConfigFor,
  mergeSignalOptions,
  collectConditionKeys,
  parseConditionExpr,
  signalToForm,
  stepToForm,
  stepUsesLlm,
  usedSignalKeys,
  withSystemSignals,
} from "@/utils/workflow";

interface EditorForm {
  name: string;
  description: string;
  parent_id: number | undefined;
  enabled: boolean;
  steps: StepForm[];
  groups: GroupForm[];
  signals: SignalForm[];
}

const auth = useAuthStore();
const router = useRouter();
const templates = ref<TemplateSummary[]>([]);
const templateSearch = usePersistedSearch("siteflow:templates:search");
const features = ref<Feature[]>([]);
const cfboxEnabled = ref(false);
const loading = ref(true);
const error = ref("");
const editorOpen = ref(false);
const editingId = ref<number | null>(null);
const saving = ref(false);
const editorError = ref("");
const formBaseline = ref("");
const loadingForm = ref(false);
const interactionStarted = ref(false);
const groupsLocked = ref(false);
const form = reactive<EditorForm>(emptyForm());
const chain = ref<TemplateDetail[]>([]);
const editorRef = ref<{
  addStep: () => void;
  expandAll: () => void;
  collapseAll: () => void;
} | null>(null);
const previewOpen = ref(false);
const previewLoading = ref(false);
const preview = ref<ResolvedWorkflow | null>(null);
const previewError = ref("");
const previewTitle = ref("");
const exportOpen = ref(false);
const copyOpen = ref(false);
const copying = ref(false);
const templateImportInput = ref<HTMLInputElement | null>(null);
const templateImporting = ref(false);

const isEditing = computed(() => editingId.value !== null);
const title = computed(() => (isEditing.value ? "编辑模板" : "新建模板"));
const chainSignals = computed<ResolvedSignalView[]>(
  () => chain.value[chain.value.length - 1]?.resolved_signals ?? [],
);
const inheritedGroups = computed(
  () => chain.value[chain.value.length - 1]?.resolved?.groups ?? [],
);
const signalOptions = computed(() => withSystemSignals(chainSignals.value));
const availableSignals = computed(() =>
  mergeSignalOptions(signalOptions.value, form.signals),
);
const usedSignalKeysValue = computed(() => Array.from(usedSignalKeys(form.steps)));
const copyTemplateItems = computed(() =>
  templates.value.map((item) => ({
    id: item.id,
    label: item.name,
    description: [
      item.parent_id !== null ? `父模板：${templateName(item.parent_id)}` : "无父模板",
      `v${item.version}`,
      `${item.step_count} 个步骤`,
    ].join(" · "),
  })),
);
const exportItems = computed<ExportItem[]>(() =>
  templates.value.map((item) => ({
    id: item.id,
    label: item.name,
    description: [
      `${item.step_count} 个步骤`,
      `v${item.version}`,
      item.parent_id !== null ? `父模板：${templateName(item.parent_id)}` : "",
      item.valid ? "" : "配置无效",
    ]
      .filter(Boolean)
      .join(" · "),
  })),
);
const filteredTemplates = computed(() => {
  const query = templateSearch.value.trim().toLowerCase();
  if (!query) return templates.value;
  return templates.value.filter((item) => {
    const parentName =
      item.parent_id === null ? "" : templateName(item.parent_id).toLowerCase();
    return (
      item.name.toLowerCase().includes(query) || parentName.includes(query)
    );
  });
});
const dirty = computed(
  () =>
    interactionStarted.value &&
    Boolean(formBaseline.value) &&
    JSON.stringify(form) !== formBaseline.value,
);

async function closeEditor(): Promise<void> {
  if (dirty.value) {
    try {
      await ElMessageBox.confirm(
        "有修改未保存，直接离开页面将会丢弃刚刚的修改。",
        "未保存的修改",
        {
          confirmButtonText: "直接离开",
          cancelButtonText: "继续编辑",
          type: "warning",
        },
      );
    } catch {
      return;
    }
  }
  editorOpen.value = false;
}

onBeforeRouteLeave(async () => {
  if (!editorOpen.value || !dirty.value) return true;
  try {
    await ElMessageBox.confirm(
      "有修改未保存，直接离开页面将会丢弃刚刚的修改。",
      "未保存的修改",
      {
        confirmButtonText: "直接离开",
        cancelButtonText: "继续编辑",
        type: "warning",
      },
    );
    return true;
  } catch {
    return false;
  }
});

function emptyForm(): EditorForm {
  return {
    name: "",
    description: "",
    parent_id: undefined,
    enabled: true,
    steps: [],
    groups: [],
    signals: [],
  };
}

function groupToForm(group: GroupView): GroupForm {
  return { uid: `group-${group.group_key}`, ...group };
}

function selectedParentId(): number | null {
  return typeof form.parent_id === "number" ? form.parent_id : null;
}

function templateName(id: number | null): string {
  if (id === null) return "-";
  return templates.value.find((item) => item.id === id)?.name ?? `#${id}`;
}

async function handleUnauthorized(cause: unknown): Promise<boolean> {
  if (cause instanceof ApiError && cause.status === 401) {
    auth.clear();
    await router.replace({ name: "login" });
    return true;
  }
  return false;
}

function exportStep(step: TemplateDetail["steps"][number]) {
  return {
    step_key: step.step_key,
    feature_type: step.feature_type,
    label: step.label,
    group_key: step.group_key,
    position: step.position,
    enabled: step.enabled,
    config: step.config,
    condition: step.condition,
    on_error: step.on_error,
    timeout_seconds: step.timeout_seconds,
  };
}

async function buildTemplateExport(items: ExportItem[]): Promise<unknown> {
  try {
    const details = await Promise.all(
      items.map((item) => apiRequest<TemplateDetail>(`/templates/${item.id}`)),
    );
    return {
      format: "siteflow.export",
      version: 1,
      kind: "templates",
      exported_at: new Date().toISOString(),
      templates: details.map((detail) => ({
        name: detail.name,
        description: detail.description,
        parent_name: detail.parent_id === null ? null : templateName(detail.parent_id),
        enabled: detail.enabled,
        version: detail.version,
        groups: detail.groups.map((group) => ({
          group_key: group.group_key,
          name: group.name,
          position: group.position,
        })),
        steps: detail.steps.map(exportStep),
        signals: detail.signals.map((signal) => ({
          signal_key: signal.signal_key,
          label: signal.label,
          description: signal.description,
          include_texts: signal.include_texts,
          include_selectors: signal.include_selectors,
          exclude_texts: signal.exclude_texts,
          exclude_selectors: signal.exclude_selectors,
          match_mode: signal.match_mode,
          enabled: signal.enabled,
        })),
      })),
    };
  } catch (cause) {
    if (await handleUnauthorized(cause)) throw cause;
    throw cause;
  }
}

interface PendingTemplate {
  name: string;
  parentName: string | null;
  item: ImportRecord;
}

function openTemplateImport(): void {
  templateImportInput.value?.click();
}

async function importTemplatesFromFile(file: File): Promise<void> {
  const rows = parseExportFile(await file.text(), "templates", "模板");
  const current = await apiRequest<TemplateSummary[]>("/templates");
  const existing = new Map(current.map((item) => [item.name, item.id]));
  const seen = new Set<string>();
  const parsed: PendingTemplate[] = rows.map((item, index) => {
    const name = importText(item.name);
    if (!name) throw new Error(`第 ${index + 1} 个模板缺少名称`);
    if (seen.has(name)) throw new Error(`导入文件中模板名称重复：${name}`);
    seen.add(name);
    const rawParent = item.parent_name;
    const parentName =
      rawParent === null || rawParent === undefined ? null : importText(rawParent) || null;
    if (parentName === name) throw new Error(`模板不能继承自身：${name}`);
    return { name, parentName, item };
  });
  const skipped = parsed.filter((entry) => existing.has(entry.name)).map((entry) => entry.name);
  const pending = parsed.filter((entry) => !existing.has(entry.name));
  if (!pending.length) {
    ElMessage.info("所有模板均已存在，未导入任何模板");
    return;
  }
  const pendingNames = new Set(pending.map((entry) => entry.name));
  for (const entry of pending) {
    if (
      entry.parentName &&
      !existing.has(entry.parentName) &&
      !pendingNames.has(entry.parentName)
    ) {
      throw new Error(`模板 ${entry.name} 的父模板不存在：${entry.parentName}`);
    }
  }

  const available = new Set(existing.keys());
  const ordered: PendingTemplate[] = [];
  while (pending.length) {
    const index = pending.findIndex(
      (entry) => !entry.parentName || available.has(entry.parentName),
    );
    if (index < 0) {
      const names = pending.map((entry) => entry.name).join("、");
      throw new Error(`无法确定导入顺序，父模板缺失或存在循环：${names}`);
    }
    const [entry] = pending.splice(index, 1);
    ordered.push(entry);
    available.add(entry.name);
  }

  const skippedText = skipped.length
    ? `；将跳过 ${skipped.length} 个同名模板（${skipped.slice(0, 5).join("、")}${
        skipped.length > 5 ? "…" : ""
      }）`
    : "";
  try {
    await ElMessageBox.confirm(
      `确认导入 ${ordered.length} 个模板${skippedText}？`,
      "导入模板",
      {
        confirmButtonText: "导入",
        cancelButtonText: "取消",
        type: skipped.length ? "warning" : "info",
      },
    );
  } catch {
    return;
  }

  const nameToId = new Map(existing);
  const created: number[] = [];
  try {
    for (const entry of ordered) {
      const detail = await apiRequest<TemplateDetail>("/templates", {
        method: "POST",
        body: JSON.stringify({
          name: entry.name,
          description: importText(entry.item.description),
          parent_id: entry.parentName ? nameToId.get(entry.parentName) ?? null : null,
          enabled: entry.item.enabled !== false,
          groups: importArray(entry.item.groups),
          steps: importArray(entry.item.steps),
          signals: importArray(entry.item.signals),
        }),
      });
      created.push(detail.id);
      nameToId.set(detail.name, detail.id);
    }
  } catch (cause) {
    for (const id of [...created].reverse()) {
      try {
        await apiRequest(`/templates/${id}`, { method: "DELETE" });
      } catch {
        // Ignore rollback failures and report the original import error.
      }
    }
    throw cause;
  }

  ElMessage.success(
    `已导入 ${ordered.length} 个模板${skipped.length ? `，跳过 ${skipped.length} 个同名模板` : ""}`,
  );
  await load();
}

async function onTemplateImportFile(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement;
  const file = input.files?.[0];
  if (!file) {
    input.value = "";
    return;
  }
  templateImporting.value = true;
  try {
    await importTemplatesFromFile(file);
  } catch (cause) {
    if (!(await handleUnauthorized(cause))) {
      ElMessage.error(cause instanceof Error ? cause.message : "导入模板失败");
    }
  } finally {
    templateImporting.value = false;
    input.value = "";
  }
}

async function load(): Promise<void> {
  loading.value = true;
  error.value = "";
  try {
    const [templateList, featureList, integrations] = await Promise.all([
      apiRequest<TemplateSummary[]>("/templates"),
      apiRequest<Feature[]>("/features"),
      apiRequest<{ cfbox: { enabled: boolean } }>("/integrations"),
    ]);
    templates.value = templateList;
    features.value = featureList;
    cfboxEnabled.value = integrations.cfbox.enabled;
  } catch (cause) {
    if (await handleUnauthorized(cause)) return;
    error.value = cause instanceof Error ? cause.message : "加载模板失败";
  } finally {
    loading.value = false;
  }
}

async function loadChain(templateId: number | undefined): Promise<TemplateDetail[]> {
  const result: TemplateDetail[] = [];
  const seen = new Set<number>();
  let current = templateId;
  while (current && !seen.has(current)) {
    seen.add(current);
    const detail = await apiRequest<TemplateDetail>(`/templates/${current}`);
    result.unshift(detail);
    current = detail.parent_id ?? undefined;
  }
  return result;
}

async function loadParentChain(): Promise<void> {
  if (!form.parent_id) {
    chain.value = [];
    return;
  }
  try {
    chain.value = await loadChain(form.parent_id);
  } catch (cause) {
    if (await handleUnauthorized(cause)) return;
    chain.value = [];
  }
}

function resetEditor(): void {
  Object.assign(form, emptyForm());
  editorError.value = "";
  chain.value = [];
  groupsLocked.value = false;
}

function openCopyTemplate(): void {
  copyOpen.value = true;
}

async function copyFromTemplate(id: number): Promise<void> {
  copying.value = true;
  editorError.value = "";
  try {
    const detail = await apiRequest<TemplateDetail>(`/templates/${id}`);
    form.description = detail.description;
    form.parent_id = detail.parent_id ?? undefined;
    form.enabled = detail.enabled;
    form.steps = detail.steps.map(stepToForm);
    form.groups = detail.groups.map(groupToForm);
    form.signals = detail.signals.map(signalToForm);
    chain.value = form.parent_id ? await loadChain(form.parent_id) : [];
    groupsLocked.value = false;
    interactionStarted.value = true;
    copyOpen.value = false;
    await nextTick();
    ElMessage.success(`已复制模板 ${detail.name} 的配置`);
  } catch (cause) {
    if (await handleUnauthorized(cause)) return;
    ElMessage.error(cause instanceof Error ? cause.message : "复制模板配置失败");
  } finally {
    copying.value = false;
  }
}

function openCreate(): void {
  loadingForm.value = true;
  interactionStarted.value = false;
  resetEditor();
  editingId.value = null;
  editorOpen.value = true;
  void nextTick(() => {
    formBaseline.value = JSON.stringify(form);
    interactionStarted.value = false;
    loadingForm.value = false;
  });
}

async function openEdit(id: number): Promise<void> {
  loadingForm.value = true;
  interactionStarted.value = false;
  resetEditor();
  editorOpen.value = true;
  editingId.value = id;
  try {
    const detail = await apiRequest<TemplateDetail>(`/templates/${id}`);
    form.name = detail.name;
    form.description = detail.description;
    form.parent_id = detail.parent_id ?? undefined;
    form.enabled = detail.enabled;
    const loadedChain = form.parent_id ? await loadChain(form.parent_id) : [];
    chain.value = loadedChain;
    form.steps = detail.steps.map(stepToForm);
    form.groups = detail.groups.map(groupToForm);
    form.signals = detail.signals.map(signalToForm);
    groupsLocked.value = detail.groups_locked && detail.groups.length > 0;
  } catch (cause) {
    if (await handleUnauthorized(cause)) return;
    editorError.value = cause instanceof Error ? cause.message : "加载模板失败";
  } finally {
    await nextTick();
    loadingForm.value = false;
    await nextTick();
    formBaseline.value = JSON.stringify(form);
    interactionStarted.value = false;
  }
}

async function changeParent(): Promise<void> {
  await loadParentChain();
}

function addStep(): void {
  editorRef.value?.addStep();
}

function expandAllSteps(): void {
  editorRef.value?.expandAll();
}

function collapseAllSteps(): void {
  editorRef.value?.collapseAll();
}

function validate(): void {
  if (!form.name.trim()) throw new Error("请填写模板名称");
  if (!form.parent_id && !form.steps.length) {
    throw new Error("根模板至少需要一个功能步骤");
  }
  const signalKeys = new Set<string>();
  for (const signal of form.signals) {
    if (signalKeys.has(signal.signal_key)) throw new Error(`信号 key 重复: ${signal.signal_key}`);
    signalKeys.add(signal.signal_key);
    if (!signal.label.trim()) throw new Error("信号名称不能为空");
    if (!signal.include_texts.length && !signal.include_selectors.length) {
      throw new Error(`信号 ${signal.label || signal.signal_key} 至少需要一个包含文本或选择器`);
    }
  }
  const available = new Set(availableSignals.value.map((signal) => signal.signal_key));
  const used = new Set<string>();
  for (const step of form.steps) {
    if (used.has(step.step_key)) throw new Error(`步骤 key 重复: ${step.step_key}`);
    used.add(step.step_key);
    const feature = features.value.find((item) => item.key === step.feature_type);
    if (!feature) throw new Error(`步骤 ${step.step_key} 的功能类型无效`);
    const parsed = parseConditionExpr(step.condition);
    if (step.condition && step.condition !== "always" && !parsed) {
      throw new Error(`步骤 ${step.step_key} 的执行条件无效`);
    }
    for (const key of collectConditionKeys(parsed)) {
      if (!available.has(key)) {
        throw new Error(`步骤 ${step.step_key} 引用了不存在的信号 ${key}`);
      }
    }
    const inherited = inheritedConfigFor(chain.value, step.step_key, step.feature_type);
    const missing = missingRequiredFields(feature.config_schema, step.config, inherited);
    if (missing.length) {
      throw new Error(`步骤 ${step.step_key} 缺少必填字段：${missing.join("、")}`);
    }
  }
}

function buildPayload(): Record<string, unknown> {
  return {
    name: form.name.trim(),
    description: form.description,
    parent_id: selectedParentId(),
    enabled: form.enabled,
    groups: form.groups.map((group) => ({
      group_key: group.group_key,
      name: group.name.trim(),
      position: group.position,
      step_anchor: group.step_anchor,
    })),
    signals: form.signals.map((signal) => ({
      signal_key: signal.signal_key,
      label: signal.label.trim(),
      description: signal.description,
      include_texts: signal.include_texts,
      include_selectors: signal.include_selectors,
      exclude_texts: signal.exclude_texts,
      exclude_selectors: signal.exclude_selectors,
      match_mode: signal.match_mode,
      enabled: signal.enabled,
    })),
    steps: form.steps.map((step) => ({
      step_key: step.step_key,
      feature_type: step.feature_type,
      label: step.label.trim() || null,
      group_key: step.group_key || null,
      position: step.position ?? null,
      enabled: step.enabled === "inherit" ? null : step.enabled === "on",
      config: step.config,
      condition: step.condition || null,
      on_error: step.on_error || null,
      timeout_seconds: step.timeout_seconds ?? null,
    })),
  };
}

async function save(): Promise<void> {
  editorError.value = "";
  try {
    validate();
  } catch (cause) {
    editorError.value = cause instanceof Error ? cause.message : "表单校验失败";
    return;
  }
  saving.value = true;
  try {
    const url = editingId.value === null ? "/templates" : `/templates/${editingId.value}`;
    await apiRequest(url, {
      method: editingId.value === null ? "POST" : "PUT",
      body: JSON.stringify(buildPayload()),
    });
    formBaseline.value = JSON.stringify(form);
    ElMessage.success("模板已保存");
    editorOpen.value = false;
    await load();
  } catch (cause) {
    if (await handleUnauthorized(cause)) return;
    editorError.value = cause instanceof Error ? cause.message : "保存模板失败";
  } finally {
    saving.value = false;
  }
}

async function remove(row: TemplateSummary): Promise<void> {
  try {
    await ElMessageBox.confirm(`确认删除模板 ${row.name}？`, "删除模板", {
      confirmButtonText: "删除",
      cancelButtonText: "取消",
      type: "warning",
    });
  } catch {
    return;
  }
  try {
    await apiRequest(`/templates/${row.id}`, { method: "DELETE" });
    ElMessage.success("模板已删除");
    await load();
  } catch (cause) {
    if (await handleUnauthorized(cause)) return;
    ElMessage.error(cause instanceof Error ? cause.message : "删除模板失败");
  }
}

async function showPreview(row: TemplateSummary): Promise<void> {
  previewOpen.value = true;
  previewLoading.value = true;
  previewError.value = "";
  preview.value = null;
  previewTitle.value = `${row.name} · 解析预览`;
  try {
    const detail = await apiRequest<TemplateDetail>(`/templates/${row.id}`);
    preview.value = detail.resolved;
    if (!detail.resolved) previewError.value = detail.validation_error ?? "模板无法解析";
  } catch (cause) {
    if (await handleUnauthorized(cause)) return;
    previewError.value = cause instanceof Error ? cause.message : "解析失败";
  } finally {
    previewLoading.value = false;
  }
}

onMounted(load);
</script>

<template>
  <section v-if="!editorOpen">
    <div class="page-head">
      <div>
        <p class="eyebrow">TEMPLATES</p>
        <h1>模板管理</h1>
        <p class="muted">模板由有序功能步骤组成；可继承父模板，并按 step_key 覆盖。</p>
      </div>
      <div class="head-actions">
        <input
          ref="templateImportInput"
          type="file"
          accept=".json,application/json"
          class="hidden-file"
          @change="onTemplateImportFile"
        />
        <el-button :loading="templateImporting" @click="openTemplateImport">
          导入模板
        </el-button>
        <el-button @click="exportOpen = true">导出模板</el-button>
        <el-button type="primary" @click="openCreate">新建模板</el-button>
      </div>
    </div>

    <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon class="notice" />

    <el-card shadow="never" class="panel">
      <div class="list-toolbar">
        <el-input
          v-model="templateSearch"
          clearable
          placeholder="搜索模板名称或父模板名称"
          class="list-search"
        />
      </div>
      <el-table :data="filteredTemplates" v-loading="loading" row-key="id">
        <el-table-column prop="name" label="名称" min-width="170" />
        <el-table-column label="父模板" min-width="150">
          <template #default="{ row }">{{ templateName(row.parent_id) }}</template>
        </el-table-column>
        <el-table-column label="步骤" width="70" prop="step_count" />
        <el-table-column label="版本" width="70" prop="version" />
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tag :type="row.valid ? 'success' : 'danger'" size="small" effect="plain">
              {{ row.valid ? "有效" : "无效" }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="210" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" @click="openEdit(row.id)">编辑</el-button>
            <el-button link @click="showPreview(row)">预览</el-button>
            <el-button link type="danger" @click="remove(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <ExportSelectionDialog
      v-model="exportOpen"
      title="导出模板配置"
      file-name-prefix="siteflow-templates"
      :items="exportItems"
      :build-payload="buildTemplateExport"
      hint="导出所选模板的分组、步骤和信号配置为 JSON；父模板引用会保留。"
    />
  </section>

  <section
    v-else
    class="editor-page"
    @pointerdown="interactionStarted = true"
    @keydown="interactionStarted = true"
  >
    <div class="page-head">
      <div>
        <el-button link @click="closeEditor">← 返回模板列表</el-button>
        <h1>{{ title }}</h1>
        <p class="muted">步骤按列表从上到下执行；模板步骤固定，新增步骤使用上下移动调整顺序。</p>
      </div>
      <div class="head-actions">
        <el-button @click="closeEditor">取消</el-button>
        <el-button type="primary" :loading="saving" @click="save">保存模板</el-button>
      </div>
    </div>

    <el-alert
      v-if="editorError"
      :title="editorError"
      type="error"
      :closable="false"
      show-icon
      class="notice"
    />

    <el-card shadow="never" class="panel">
      <template #header>
        <div class="card-head">
          <div class="title-row">
            <strong>模板配置</strong>
            <span class="muted">名称、继承关系和描述</span>
            <el-button
              v-if="!isEditing"
              size="small"
              plain
              :disabled="!templates.length"
              @click="openCopyTemplate"
            >
              复制模板配置
            </el-button>
          </div>
        </div>
      </template>
      <el-form label-position="top">
        <el-row :gutter="14">
          <el-col :xs="24" :md="12">
            <el-form-item label="模板名称" required>
              <el-input v-model="form.name" maxlength="128" />
            </el-form-item>
          </el-col>
          <el-col :xs="24" :md="8">
            <el-form-item label="父模板">
              <el-select
                v-model="form.parent_id"
                clearable
                placeholder="无父模板"
                style="width: 100%"
                @change="changeParent"
              >
                <el-option
                  v-for="item in templates.filter((tpl) => tpl.id !== editingId)"
                  :key="item.id"
                  :label="item.name"
                  :value="item.id"
                />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :xs="24" :md="4">
            <el-form-item label="启用">
              <el-switch v-model="form.enabled" />
            </el-form-item>
          </el-col>
        </el-row>
        <el-form-item label="描述">
          <el-input v-model="form.description" type="textarea" :rows="2" maxlength="4000" />
        </el-form-item>
      </el-form>
    </el-card>

    <SignalEditor
      v-model="form.signals"
      :chain-signals="signalOptions"
      :used-keys="usedSignalKeysValue"
      picker-base-url=""
    />

    <el-card shadow="never" class="panel">
      <template #header>
        <div class="card-head">
          <div class="title-row">
            <strong>功能步骤</strong>
            <span class="muted">共 {{ form.steps.length }} 个本地步骤</span>
          </div>
          <div class="head-actions">
            <el-button size="small" @click="expandAllSteps">展开</el-button>
            <el-button size="small" @click="collapseAllSteps">收起</el-button>
            <el-button type="primary" plain @click="addStep">添加步骤</el-button>
          </div>
        </div>
      </template>
      <WorkflowStepEditor
        v-if="!loadingForm"
        ref="editorRef"
        v-model="form.steps"
        :features="features"
        :chain="chain"
        :signals="availableSignals"
        :cfbox-enabled="cfboxEnabled"
        :groups="form.groups"
        :inherited-groups="inheritedGroups"
        :groups-locked="groupsLocked"
        key-namespace="temp"
        @update:groups="form.groups = $event"
      />
    </el-card>
  </section>

  <CopyConfigDialog
    v-model="copyOpen"
    title="复制模板配置"
    :items="copyTemplateItems"
    :loading="copying"
    hint="将复制父模板、步骤、分组和信号；模板名称不复制。"
    @confirm="copyFromTemplate"
  />

  <el-drawer v-model="previewOpen" :title="previewTitle" size="min(840px, 96vw)">
    <div v-loading="previewLoading">
      <el-alert v-if="previewError" :title="previewError" type="error" :closable="false" show-icon />
      <template v-if="preview">
        <div class="preview-meta">
          <el-tag v-for="item in preview.requires" :key="item" size="small" effect="plain">
            依赖：{{ item }}
          </el-tag>
        </div>
        <div v-for="(step, index) in preview.steps" :key="step.step_key" class="preview-step">
          <div class="preview-step-head">
            <strong>#{{ index + 1 }} · {{ step.label }}</strong>
            <el-tag v-if="stepUsesLlm(features.find((item) => item.key === step.feature_type), step.config)" size="small" type="success" effect="plain">大模型</el-tag>
            <el-tag size="small" :type="step.enabled ? 'success' : 'info'" effect="plain">
              {{ step.enabled ? "启用" : "禁用" }}
            </el-tag>
          </div>
          <p class="muted mono">
            {{ step.step_key }} · {{ step.feature_type }} · {{ step.source }} ·
            {{ step.condition }} → 无法判断时人工接管 / {{ step.on_error }}
          </p>
          <pre class="config-preview">{{ JSON.stringify(step.config, null, 2) }}</pre>
        </div>
      </template>
    </div>
  </el-drawer>
</template>

<style scoped>
.page-head {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 18px;
  margin-bottom: 20px;
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

.head-actions {
  display: flex;
  gap: 8px;
}

.hidden-file {
  display: none;
}

.panel {
  border-radius: 16px;
  border-color: #e4e9f0;
  margin-bottom: 16px;
}

.list-toolbar {
  display: flex;
  justify-content: flex-start;
  margin-bottom: 12px;
}

.list-search {
  width: min(340px, 100%);
}

.editor-page :deep(.el-card) {
  display: block;
  overflow: visible;
}

.editor-page :deep(.el-card__header) {
  position: sticky;
  top: -26px;
  z-index: 6;
  background: #fff;
  border-radius: 16px 16px 0 0;
  box-shadow: 0 1px 0 #edf1f6;
}

.notice {
  margin-bottom: 14px;
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

.card-head > .title-row {
  align-items: center;
  flex-wrap: wrap;
}


.preview-meta {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 12px;
}

.preview-step {
  border: 1px solid #e4e9f0;
  border-radius: 10px;
  padding: 12px;
  margin-bottom: 10px;
}

.preview-step-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.preview-step p {
  margin: 6px 0;
  font-size: 12px;
}

.config-preview {
  margin: 8px 0 0;
  padding: 10px;
  border-radius: 8px;
  background: #f4f6f9;
  font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
  font-size: 12px;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

@media (max-width: 760px) {
  .editor-page :deep(.el-card__header) {
    top: -16px;
  }

  .page-head {
    align-items: flex-start;
    flex-direction: column;
  }
}
</style>
