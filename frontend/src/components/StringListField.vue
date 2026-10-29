<script setup lang="ts">
import { ref } from "vue";

import ElementPickerDialog from "@/components/ElementPickerDialog.vue";
import FloatingField from "@/components/FloatingField.vue";

const props = defineProps<{
  label: string;
  description?: string;
  modelValue: string[];
  addLabel?: string;
  pickerBaseUrl?: string;
}>();

const pickerOpen = ref(false);
const pickerIndex = ref<number | null>(null);

const emit = defineEmits<{
  (event: "update:modelValue", value: string[]): void;
}>();

function update(index: number, value: string): void {
  const next = [...props.modelValue];
  next[index] = value;
  emit("update:modelValue", next);
}

function add(): void {
  emit("update:modelValue", [...props.modelValue, ""]);
}

function remove(index: number): void {
  const next = [...props.modelValue];
  next.splice(index, 1);
  emit("update:modelValue", next);
}

function openPicker(index: number | null = null): void {
  pickerIndex.value = index;
  pickerOpen.value = true;
}

function applyPicked(selector: string): void {
  const next = [...props.modelValue];
  const index = pickerIndex.value;
  if (index !== null && index >= 0 && index < next.length) {
    next[index] = selector;
  } else {
    next.push(selector);
  }
  emit("update:modelValue", next);
}
</script>

<template>
  <FloatingField :label="label" :description="description" :has-value="true">
    <div class="inline-list">
      <div v-for="(item, index) in modelValue" :key="index" class="inline-row">
        <input
          :value="item"
          @input="(event) => update(index, (event.target as HTMLInputElement).value)"
        />
        <button
          v-if="pickerBaseUrl !== undefined"
          type="button"
          class="row-pick"
          title="从页面拾取选择器"
          @click="openPicker(index)"
        >
          拾取
        </button>
        <button type="button" class="row-remove" title="删除这一项" @click="remove(index)">
          ×
        </button>
      </div>
      <div class="inline-actions">
        <button type="button" class="inline-add" @click="add">
          + {{ addLabel ?? "添加一项" }}
        </button>
        <button
          v-if="pickerBaseUrl !== undefined"
          type="button"
          class="inline-add"
          @click="openPicker()"
        >
          + 拾取并添加
        </button>
      </div>
    </div>
  </FloatingField>

  <ElementPickerDialog
    v-if="pickerBaseUrl !== undefined"
    v-model="pickerOpen"
    :base-url="pickerBaseUrl"
    :title="`拾取元素 · ${label}`"
    @picked="applyPicked"
  />
</template>

<style scoped>
.inline-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  width: 100%;
  padding-bottom: 5px;
}

.inline-row {
  display: flex;
  align-items: center;
  gap: 6px;
  width: 100%;
}

.inline-row input {
  flex: 1;
  min-width: 0;
  height: 28px;
  padding: 0 6px;
  border: 1px solid #e4e9f0;
  border-radius: 6px;
  background: #fbfcfe;
  font: inherit;
  font-size: 12px;
}

.row-pick {
  flex: none;
  height: 24px;
  padding: 0 7px;
  border: 1px solid #b9d7f5;
  border-radius: 6px;
  background: #f2f8ff;
  color: #2f7ed8;
  cursor: pointer;
  font-size: 11px;
}

.row-pick:hover {
  background: #e4f1ff;
}

.inline-actions {
  display: flex;
  align-items: center;
  gap: 12px;
}

.row-remove {
  flex: none;
  width: 24px;
  height: 24px;
  border: none;
  border-radius: 6px;
  background: transparent;
  color: #c45656;
  cursor: pointer;
  font-size: 16px;
  line-height: 1;
}

.row-remove:hover {
  background: #fef0f0;
}

.inline-add {
  align-self: flex-start;
  border: none;
  padding: 2px 0;
  background: transparent;
  color: #2f7ed8;
  font-size: 12px;
  cursor: pointer;
}

.inline-add:hover {
  text-decoration: underline;
}
</style>
