<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { ElMessage } from "element-plus";

import type { ExportItem } from "@/types/export";

const props = defineProps<{
  modelValue: boolean;
  title: string;
  fileNamePrefix: string;
  items: ExportItem[];
  hint?: string;
  buildPayload: (items: ExportItem[]) => Promise<unknown>;
}>();

const emit = defineEmits<{
  (event: "update:modelValue", value: boolean): void;
}>();

const selected = ref<number[]>([]);
const exporting = ref(false);
const error = ref("");

const allSelected = computed(
  () => props.items.length > 0 && selected.value.length === props.items.length,
);
const someSelected = computed(
  () => selected.value.length > 0 && !allSelected.value,
);

watch(
  () => props.modelValue,
  (open) => {
    if (open) {
      selected.value = props.items.map((item) => item.id);
      error.value = "";
    }
  },
);

function toggleAll(value: boolean | string | number): void {
  selected.value = value ? props.items.map((item) => item.id) : [];
}

function close(): void {
  emit("update:modelValue", false);
}

function stamp(): string {
  const now = new Date();
  const pad = (value: number) => String(value).padStart(2, "0");
  return [
    now.getFullYear(),
    pad(now.getMonth() + 1),
    pad(now.getDate()),
    "-",
    pad(now.getHours()),
    pad(now.getMinutes()),
    pad(now.getSeconds()),
  ].join("");
}

async function download(): Promise<void> {
  if (!selected.value.length) {
    ElMessage.warning("请选择至少一项");
    return;
  }
  const chosen = props.items.filter((item) => selected.value.includes(item.id));
  exporting.value = true;
  error.value = "";
  try {
    const payload = await props.buildPayload(chosen);
    const blob = new Blob([JSON.stringify(payload, null, 2)], {
      type: "application/json;charset=utf-8",
    });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `${props.fileNamePrefix}-${stamp()}.json`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
    ElMessage.success(`已导出 ${chosen.length} 项配置`);
    close();
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "导出失败";
  } finally {
    exporting.value = false;
  }
}
</script>

<template>
  <el-dialog
    :model-value="modelValue"
    :title="title"
    width="min(620px, 94vw)"
    @update:model-value="emit('update:modelValue', $event)"
  >
    <div class="export-toolbar">
      <el-checkbox
        :model-value="allSelected"
        :indeterminate="someSelected"
        @change="toggleAll"
      >
        全选
      </el-checkbox>
      <span class="muted">已选择 {{ selected.length }} / {{ items.length }}</span>
    </div>
    <el-checkbox-group v-model="selected" class="export-list">
      <el-checkbox
        v-for="item in items"
        :key="item.id"
        :value="item.id"
        class="export-item"
      >
        <span class="export-name">{{ item.label }}</span>
        <span v-if="item.description" class="export-desc">{{ item.description }}</span>
      </el-checkbox>
    </el-checkbox-group>
    <p v-if="!items.length" class="muted">暂无可导出的配置。</p>
    <el-alert
      v-if="hint"
      :title="hint"
      type="info"
      :closable="false"
      show-icon
      class="export-hint"
    />
    <el-alert
      v-if="error"
      :title="error"
      type="error"
      :closable="false"
      show-icon
      class="export-error"
    />
    <template #footer>
      <el-button @click="close">取消</el-button>
      <el-button
        type="primary"
        :loading="exporting"
        :disabled="!selected.length"
        @click="download"
      >
        导出 JSON
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.export-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding-bottom: 8px;
  margin-bottom: 8px;
  border-bottom: 1px solid #eef1f6;
}

.export-list {
  display: flex;
  flex-direction: column;
  gap: 2px;
  max-height: min(52vh, 460px);
  overflow-y: auto;
}

.export-item {
  display: flex;
  align-items: flex-start;
  width: 100%;
  height: auto;
  min-height: 38px;
  margin: 0;
  padding: 7px 8px;
  border-radius: 8px;
}

.export-item:hover {
  background: #f7fbff;
}

.export-item :deep(.el-checkbox__label) {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
  line-height: 1.35;
  white-space: normal;
}

.export-name {
  color: #303133;
  font-weight: 600;
  overflow-wrap: anywhere;
}

.export-desc {
  color: #8a96a3;
  font-size: 12px;
  overflow-wrap: anywhere;
}

.export-hint {
  margin-top: 12px;
}

.export-error {
  margin-top: 12px;
}
</style>
