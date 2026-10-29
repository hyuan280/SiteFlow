<script setup lang="ts">
import { ref, watch } from "vue";
import { ElMessage } from "element-plus";

interface CopyItem {
  id: number;
  label: string;
  description?: string;
}

const props = defineProps<{
  modelValue: boolean;
  title: string;
  items: CopyItem[];
  hint?: string;
  loading?: boolean;
}>();

const emit = defineEmits<{
  (event: "update:modelValue", value: boolean): void;
  (event: "confirm", id: number): void;
}>();

const selected = ref<number | null>(null);

watch(
  () => props.modelValue,
  (open) => {
    if (open) selected.value = props.items[0]?.id ?? null;
  },
);

function close(): void {
  emit("update:modelValue", false);
}

function confirm(): void {
  if (selected.value === null) {
    ElMessage.warning("请选择要复制的配置");
    return;
  }
  emit("confirm", selected.value);
}
</script>

<template>
  <el-dialog
    :model-value="modelValue"
    :title="title"
    width="min(560px, 94vw)"
    @update:model-value="emit('update:modelValue', $event)"
  >
    <el-select
      v-model="selected"
      filterable
      clearable
      :disabled="!items.length || loading"
      placeholder="选择要复制的配置"
      class="copy-select"
    >
      <el-option
        v-for="item in items"
        :key="item.id"
        :label="item.label"
        :value="item.id"
      >
        <span class="copy-name">{{ item.label }}</span>
        <span v-if="item.description" class="copy-desc">{{ item.description }}</span>
      </el-option>
    </el-select>
    <p v-if="!items.length" class="muted">暂无可复制的配置。</p>
    <el-alert
      v-if="hint"
      :title="hint"
      type="info"
      :closable="false"
      show-icon
      class="copy-hint"
    />
    <template #footer>
      <el-button @click="close">取消</el-button>
      <el-button
        type="primary"
        :loading="loading"
        :disabled="selected === null || !items.length"
        @click="confirm"
      >
        复制配置
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.copy-select {
  width: 100%;
}

.copy-name {
  display: block;
  font-weight: 600;
}

.copy-desc {
  display: block;
  color: #8a96a3;
  font-size: 12px;
}

.copy-hint {
  margin-top: 12px;
}
</style>
