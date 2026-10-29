<script setup lang="ts">
import { ref } from "vue";

defineProps<{
  label: string;
  description?: string;
  required?: boolean;
  hasValue?: boolean;
}>();

const focused = ref(false);
</script>

<template>
  <div
    class="float-field"
    :class="{ 'is-active': focused || hasValue, 'is-focused': focused }"
    @focusin="focused = true"
    @focusout="focused = false"
  >
    <label class="float-label">
      {{ label }}
      <span v-if="required" class="required-mark">*</span>
    </label>
    <div class="float-control">
      <slot />
    </div>
    <p v-if="description && focused" class="float-hint">{{ description }}</p>
  </div>
</template>

<style scoped>
.float-field {
  position: relative;
  min-height: 42px;
  padding: 7px 10px 0;
  border: 1px solid #dcdfe6;
  border-radius: 8px;
  background: #fff;
  transition: border-color 0.15s ease, box-shadow 0.15s ease;
}

.float-field:hover {
  border-color: #c0c4cc;
}

.float-field.is-focused {
  border-color: #409eff;
  box-shadow: 0 0 0 1px rgba(64, 158, 255, 0.12);
}

.float-label {
  position: absolute;
  left: 7px;
  top: 50%;
  transform: translateY(-50%);
  padding: 0 4px;
  color: #909399;
  font-size: 13px;
  line-height: 1.2;
  background: #fff;
  pointer-events: none;
  transition: top 0.15s ease, font-size 0.15s ease, color 0.15s ease;
  white-space: nowrap;
  max-width: calc(100% - 14px);
  overflow: hidden;
  text-overflow: ellipsis;
}

.float-field.is-active .float-label {
  top: 0;
  font-size: 11px;
  color: #606266;
}

.float-field.is-focused .float-label {
  color: #409eff;
}

.required-mark {
  color: #f56c6c;
}

.float-control {
  min-height: 29px;
  display: flex;
  align-items: center;
  flex-wrap: wrap;
}

.float-control > :deep(input),
.float-control > :deep(select),
.float-control > :deep(textarea) {
  width: 100%;
  min-width: 0;
  border: none;
  outline: none;
  background: transparent;
  padding: 0;
  color: #1f2d3d;
  font: inherit;
  font-size: 13px;
  line-height: 1.5;
}

.float-control > :deep(select) {
  cursor: pointer;
}

.float-field:not(.is-active) .float-control :deep(input::placeholder),
.float-field:not(.is-active) .float-control :deep(textarea::placeholder) {
  color: transparent;
}

.float-control :deep(.el-switch) {
  flex: none;
}

.float-control :deep(.el-input-number) {
  width: 100%;
}

.float-control :deep(.el-button) {
  flex: none;
}

.float-hint {
  margin: 2px 0 6px;
  color: #909399;
  font-size: 11px;
  line-height: 1.4;
}
</style>
