<script setup lang="ts">
import StringListField from "@/components/StringListField.vue";

interface PurchaseClick {
  scope: "product" | "page";
  selectors: string[];
}

const props = defineProps<{
  modelValue: PurchaseClick[];
  minItems?: number;
  maxItems?: number;
  pickerBaseUrl?: string;
}>();

const emit = defineEmits<{
  (event: "update:modelValue", value: PurchaseClick[]): void;
}>();

function change(index: number, patch: Partial<PurchaseClick> | null): void {
  const next = props.modelValue.map((item) => ({ ...item, selectors: [...item.selectors] }));
  if (patch === null) {
    if (next.length <= (props.minItems ?? 1)) return;
    next.splice(index, 1);
  } else if (index === next.length) {
    if (next.length >= (props.maxItems ?? 10)) return;
    next.push({ scope: "product", selectors: [""] });
  } else if (next[index]) {
    next[index] = { ...next[index], ...patch, selectors: [...(patch.selectors ?? next[index].selectors)] };
  }
  emit("update:modelValue", next);
}
</script>

<template>
  <div class="purchase-clicks">
    <p class="hint">按列表顺序点击。候选选择器仅在定位时依次尝试，点击后不再尝试其他候选。</p>
    <div v-for="(item, index) in modelValue" :key="index" class="click-row">
      <div class="click-heading">
        <span>点击 {{ index + 1 }}</span>
        <select
          :value="item.scope"
          :aria-label="`点击 ${index + 1} 范围`"
          @change="change(index, { scope: ($event.target as HTMLSelectElement).value as PurchaseClick['scope'] })"
        >
          <option value="product">商品内</option>
          <option value="page">当前页面</option>
        </select>
        <button
          type="button"
          :disabled="modelValue.length <= (minItems ?? 1)"
          :aria-label="`删除点击 ${index + 1}`"
          @click="change(index, null)"
        >删除点击</button>
      </div>
      <StringListField
        label="候选选择器"
        :model-value="item.selectors"
        add-label="添加候选"
        :picker-base-url="pickerBaseUrl"
        @update:model-value="change(index, { selectors: $event })"
      />
    </div>
    <button
      type="button"
      class="add-click"
      :disabled="modelValue.length >= (maxItems ?? 10)"
      @click="change(modelValue.length, {})"
    >+ 添加点击</button>
  </div>
</template>

<style scoped>
.purchase-clicks {
  width: 100%;
  min-width: 0;
  padding-bottom: 5px;
}

.hint {
  margin: 0 0 8px;
  color: #7a8794;
  font-size: 11px;
  line-height: 1.45;
}

.click-row + .click-row {
  margin-top: 10px;
}

.click-heading {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 9px;
  font-size: 12px;
}

.click-heading select {
  min-width: 0;
  padding: 3px 6px;
  border: 1px solid #e4e9f0;
  border-radius: 6px;
  background: #fbfcfe;
  font: inherit;
}

button {
  border: none;
  padding: 2px 0;
  background: transparent;
  color: #c45656;
  font-size: 12px;
  cursor: pointer;
}

.add-click {
  margin-top: 6px;
  color: #2f7ed8;
}

button:disabled {
  color: #a8abb2;
  cursor: not-allowed;
}
</style>
