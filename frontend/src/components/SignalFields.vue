<script setup lang="ts">
import FloatingField from "@/components/FloatingField.vue";
import StringListField from "@/components/StringListField.vue";
import type { SignalForm } from "@/types/workflow";

const props = defineProps<{
  modelValue: SignalForm;
  used?: boolean;
  pickerBaseUrl?: string;
}>();
</script>

<template>
  <div class="signal-fields">
    <div class="signal-row two">
      <FloatingField label="信号名称" required :has-value="Boolean(modelValue.label)">
        <input
          :value="modelValue.label"
          @input="(event) => (modelValue.label = (event.target as HTMLInputElement).value)"
        />
      </FloatingField>
      <FloatingField label="匹配方式" :has-value="true">
        <select v-model="modelValue.match_mode">
          <option value="any">任一命中</option>
          <option value="all">全部命中</option>
        </select>
      </FloatingField>
    </div>

    <div class="signal-row two">
      <FloatingField label="说明" :has-value="Boolean(modelValue.description)">
        <input
          :value="modelValue.description"
          @input="(event) => (modelValue.description = (event.target as HTMLInputElement).value)"
        />
      </FloatingField>
      <FloatingField label="启用信号" :has-value="true">
        <div class="signal-switch">
          <el-switch v-model="modelValue.enabled" />
          <span class="signal-switch-state">{{ modelValue.enabled ? "启用" : "禁用" }}</span>
        </div>
      </FloatingField>
    </div>

    <div class="signal-row two">
      <StringListField
        v-model="modelValue.include_texts"
        label="包含文本"
        description="页面文本命中时视为命中"
        add-label="添加包含文本"
      />
      <StringListField
        v-model="modelValue.include_selectors"
        label="包含选择器"
        add-label="添加包含选择器"
        :picker-base-url="pickerBaseUrl"
      />
    </div>

    <div class="signal-row two">
      <StringListField
        v-model="modelValue.exclude_texts"
        label="排除文本"
        description="命中排除规则时信号为否"
        add-label="添加排除文本"
      />
      <StringListField
        v-model="modelValue.exclude_selectors"
        label="排除选择器"
        add-label="添加排除选择器"
        :picker-base-url="pickerBaseUrl"
      />
    </div>

    <p class="signal-info">
      信号选择器默认检查元素是否存在，隐藏元素也可能命中；需要可见证据可在 Playwright 选择器中显式使用 :visible。表单登录成功元素要求可见。
    </p>

    <p v-if="used" class="signal-used">
      该信号已被步骤条件使用，不能删除，但可以继续修改判断规则。
    </p>
  </div>
</template>

<style scoped>
.signal-fields {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.signal-row {
  display: grid;
  gap: 10px 12px;
}

.signal-row.two {
  grid-template-columns: repeat(2, minmax(0, 1fr));
}

.signal-switch {
  display: flex;
  align-items: center;
  gap: 9px;
  min-height: 29px;
}

.signal-switch-state {
  color: #606266;
  font-size: 13px;
}

.signal-info,
.signal-used {
  margin: 0;
  font-size: 11px;
}

.signal-info {
  color: #606266;
}

.signal-used {
  color: #b88230;
}

@media (max-width: 760px) {
  .signal-row.two {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
