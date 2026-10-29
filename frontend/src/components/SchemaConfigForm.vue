<script setup lang="ts">
import { computed, ref } from "vue";

import ElementPickerDialog from "@/components/ElementPickerDialog.vue";
import FloatingField from "@/components/FloatingField.vue";
import SecretProviderSelect from "@/components/SecretProviderSelect.vue";
import {
  type ConfigObject,
  type JsonSchema,
  effectiveValue,
  itemDefault,
} from "@/utils/schema";

const props = defineProps<{
  schema: JsonSchema;
  modelValue: ConfigObject;
  inherited?: ConfigObject | null;
  pickerBaseUrl?: string;
  stepOptions?: { value: string; label: string }[];
}>();

const emit = defineEmits<{
  (event: "update:modelValue", value: ConfigObject): void;
}>();

interface PropertyEntry {
  key: string;
  property: JsonSchema;
}

const pickerOpen = ref(false);
const pickerTargetKey = ref("");
const pickerTargetIndex = ref<number | null>(null);
const pickerTitle = ref("元素拾取器");

const entries = computed<PropertyEntry[]>(() =>
  Object.entries(props.schema.properties ?? {}).map(([key, property]) => ({
    key,
    property,
  })),
);

function emitConfig(next: ConfigObject): void {
  emit("update:modelValue", next);
}

function hasOwn(target: ConfigObject | null | undefined, key: string): boolean {
  return Boolean(target && Object.prototype.hasOwnProperty.call(target, key));
}

function isOverridden(key: string): boolean {
  return hasOwn(props.modelValue, key);
}

function isInherited(key: string): boolean {
  return hasOwn(props.inherited, key);
}

function isRequired(key: string): boolean {
  if (key === "service_hint" && props.schema.properties?.totp_provider_id) {
    return true;
  }
  return (props.schema.required ?? []).includes(key);
}

function valueOf(key: string, property: JsonSchema): unknown {
  return effectiveValue(key, props.modelValue, props.inherited, property);
}

function setField(key: string, value: unknown): void {
  emitConfig({ ...props.modelValue, [key]: value });
}

function clearField(key: string): void {
  const next = { ...props.modelValue };
  delete next[key];
  emitConfig(next);
}

function stringValue(key: string, property: JsonSchema): string {
  const value = valueOf(key, property);
  return value === undefined || value === null ? "" : String(value);
}

function numberValue(key: string, property: JsonSchema): string {
  const value = valueOf(key, property);
  return typeof value === "number" ? String(value) : "";
}

function booleanValue(key: string, property: JsonSchema): boolean {
  return valueOf(key, property) === true;
}

function isFloatingBoolean(property: JsonSchema): boolean {
  return property["ui:widget"] === "switch-floating";
}

function booleanLabel(property: JsonSchema, value: boolean): string {
  if (value) return String(property["ui:trueLabel"] ?? property.title ?? "启用");
  return String(property["ui:falseLabel"] ?? property.title ?? "禁用");
}

function enumOptions(property: JsonSchema): { label: string; value: string }[] {
  return (property.enum ?? []).map((item) => {
    const value = String(item);
    return {
      label: property.enum_titles?.[value] ?? value,
      value,
    };
  });
}

function enumDescription(key: string, property: JsonSchema): string {
  return property.enum_descriptions?.[stringValue(key, property)] ?? "";
}

function arrayValue(key: string, property: JsonSchema): unknown[] {
  const value = valueOf(key, property);
  return Array.isArray(value) ? value : [];
}

function arrayItemValue(key: string, property: JsonSchema, index: number): unknown {
  return arrayValue(key, property)[index];
}

function arrayItemText(key: string, property: JsonSchema, index: number): string {
  const value = arrayItemValue(key, property, index);
  return value === undefined || value === null ? "" : String(value);
}

function updateArrayItem(
  key: string,
  property: JsonSchema,
  index: number,
  value: unknown,
): void {
  const next = [...arrayValue(key, property)];
  next[index] = value;
  setField(key, next);
}

function addArrayItem(key: string, property: JsonSchema): void {
  setField(key, [...arrayValue(key, property), itemDefault(property.items)]);
}

function removeArrayItem(key: string, property: JsonSchema, index: number): void {
  const next = [...arrayValue(key, property)];
  next.splice(index, 1);
  setField(key, next);
}

function objectValue(key: string, property: JsonSchema): Record<string, string> {
  const value = valueOf(key, property);
  if (value && typeof value === "object" && !Array.isArray(value)) {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>).map(([itemKey, item]) => [
        itemKey,
        String(item),
      ]),
    );
  }
  return {};
}

function objectEntries(key: string, property: JsonSchema): { key: string; value: string }[] {
  return Object.entries(objectValue(key, property)).map(([itemKey, item]) => ({
    key: itemKey,
    value: item,
  }));
}

function addObjectEntry(key: string, property: JsonSchema): void {
  const current = objectValue(key, property);
  let index = Object.keys(current).length + 1;
  let itemKey = `field_${index}`;
  while (itemKey in current) {
    index += 1;
    itemKey = `field_${index}`;
  }
  setField(key, { ...current, [itemKey]: "" });
}

function renameObjectKey(
  key: string,
  property: JsonSchema,
  oldKey: string,
  newKey: string,
): void {
  if (oldKey === newKey) return;
  const current = objectValue(key, property);
  const next: Record<string, string> = {};
  for (const [itemKey, item] of Object.entries(current)) {
    next[itemKey === oldKey ? newKey : itemKey] = item;
  }
  setField(key, next);
}

function setObjectValue(
  key: string,
  property: JsonSchema,
  itemKey: string,
  value: string,
): void {
  setField(key, { ...objectValue(key, property), [itemKey]: value });
}

function removeObjectKey(key: string, property: JsonSchema, itemKey: string): void {
  const next = { ...objectValue(key, property) };
  delete next[itemKey];
  setField(key, next);
}

function hasValue(key: string, property: JsonSchema): boolean {
  if (property.type === "array" || property.type === "object") return true;
  const value = valueOf(key, property);
  if (value === undefined || value === null || value === "") return false;
  if (Array.isArray(value)) return value.length > 0;
  if (typeof value === "object") return Object.keys(value).length > 0;
  return true;
}

function isSelectorField(key: string, property: JsonSchema): boolean {
  return (
    property.type === "array" &&
    (key.endsWith("_selector") || key.endsWith("_selectors"))
  );
}

function openPicker(key: string, property: JsonSchema, index: number | null = null): void {
  pickerTargetKey.value = key;
  pickerTargetIndex.value = index;
  pickerTitle.value = `拾取元素 · ${property.title ?? key}`;
  pickerOpen.value = true;
}

function applyPicked(selector: string): void {
  const key = pickerTargetKey.value;
  if (!key) return;
  const entry = entries.value.find((item) => item.key === key);
  if (!entry) return;
  const next = [...arrayValue(key, entry.property)];
  const index = pickerTargetIndex.value;
  if (index !== null && index >= 0 && index < next.length) {
    next[index] = selector;
  } else {
    next.push(selector);
  }
  setField(key, next);
}

function inheritedText(key: string, property: JsonSchema): string {
  const value = props.inherited ? props.inherited[key] : undefined;
  if (Array.isArray(value)) return value.join(", ") || "空数组";
  if (value && typeof value === "object") return JSON.stringify(value);
  return String(value ?? "");
}
</script>

<template>
  <div class="schema-grid">
    <div
      v-for="entry in entries"
      :key="entry.key"
      class="schema-field"
      :class="{
        'is-wide': entry.property.type === 'array' || entry.property.type === 'object',
      }"
    >
      <div v-if="isInherited(entry.key)" class="field-tools">
        <el-tag v-if="!isOverridden(entry.key)" size="small" type="info" effect="plain">
          继承
        </el-tag>
        <template v-else>
          <el-tag size="small" type="warning" effect="plain">覆盖</el-tag>
          <button type="button" class="restore-btn" @click="clearField(entry.key)">
            恢复继承
          </button>
        </template>
      </div>

      <div
        v-if="entry.property.type === 'boolean' && isFloatingBoolean(entry.property)"
        class="switch-float-field"
      >
        <span class="switch-float-label">
          {{ entry.property.description ?? entry.property.title ?? entry.key }}
        </span>
        <div class="switch-float-body">
          <el-switch
            :model-value="booleanValue(entry.key, entry.property)"
            @update:model-value="(value: string | number | boolean) => setField(entry.key, Boolean(value))"
          />
          <span class="switch-state">
            {{ booleanLabel(entry.property, booleanValue(entry.key, entry.property)) }}
          </span>
        </div>
      </div>

      <div v-else-if="entry.property.type === 'boolean'" class="switch-field">
        <el-switch
          :model-value="booleanValue(entry.key, entry.property)"
          @update:model-value="(value: string | number | boolean) => setField(entry.key, Boolean(value))"
        />
        <span class="switch-label">
          {{ entry.property.title ?? entry.key }}
          <span v-if="isRequired(entry.key)" class="required-mark">*</span>
        </span>
      </div>

      <p
        v-if="
          entry.property.description &&
          entry.property.type === 'boolean' &&
          !isFloatingBoolean(entry.property)
        "
        class="switch-hint"
      >
        {{ entry.property.description }}
      </p>

      <FloatingField
        v-if="entry.property.type !== 'boolean'"
        :label="entry.property.title ?? entry.key"
        :description="entry.property.description"
        :required="isRequired(entry.key)"
        :has-value="hasValue(entry.key, entry.property)"
      >
        <div v-if="entry.property.type === 'array'" class="inline-list">
          <div
            v-for="(_, index) in arrayValue(entry.key, entry.property)"
            :key="index"
            class="inline-row"
          >
            <select
              v-if="entry.property.items?.enum"
              :value="arrayItemText(entry.key, entry.property, index)"
              @change="
                (event) =>
                  updateArrayItem(
                    entry.key,
                    entry.property,
                    index,
                    (event.target as HTMLSelectElement).value,
                  )
              "
            >
              <option
                v-for="option in enumOptions(entry.property.items)"
                :key="option.value"
                :value="option.value"
              >
                {{ option.label }}
              </option>
            </select>
            <input
              v-else
              :value="arrayItemText(entry.key, entry.property, index)"
              :placeholder="entry.property.items?.description ?? '输入一项'"
              @input="
                (event) =>
                  updateArrayItem(
                    entry.key,
                    entry.property,
                    index,
                    (event.target as HTMLInputElement).value,
                  )
              "
            />
            <button
              v-if="isSelectorField(entry.key, entry.property)"
              type="button"
              class="row-pick"
              title="从页面拾取选择器"
              @click="openPicker(entry.key, entry.property, index)"
            >
              拾取
            </button>
            <button
              type="button"
              class="row-remove"
              title="删除这一项"
              @click="removeArrayItem(entry.key, entry.property, index)"
            >
              ×
            </button>
          </div>
          <div class="inline-actions">
            <button type="button" class="inline-add" @click="addArrayItem(entry.key, entry.property)">
              + 添加一项
            </button>
            <button
              v-if="isSelectorField(entry.key, entry.property)"
              type="button"
              class="inline-add"
              @click="openPicker(entry.key, entry.property)"
            >
              + 拾取并添加
            </button>
          </div>
        </div>

        <div v-else-if="entry.property.type === 'object'" class="inline-list">
          <div
            v-for="item in objectEntries(entry.key, entry.property)"
            :key="item.key"
            class="inline-row"
          >
            <input
              :value="item.key"
              placeholder="字段名"
              class="key-input"
              @input="
                (event) =>
                  renameObjectKey(
                    entry.key,
                    entry.property,
                    item.key,
                    (event.target as HTMLInputElement).value,
                  )
              "
            />
            <input
              :value="item.value"
              placeholder="值"
              @input="
                (event) =>
                  setObjectValue(
                    entry.key,
                    entry.property,
                    item.key,
                    (event.target as HTMLInputElement).value,
                  )
              "
            />
            <button
              type="button"
              class="row-remove"
              title="删除字段"
              @click="removeObjectKey(entry.key, entry.property, item.key)"
            >
              ×
            </button>
          </div>
          <button type="button" class="inline-add" @click="addObjectEntry(entry.key, entry.property)">
            + 添加字段
          </button>
        </div>

        <SecretProviderSelect
          v-else-if="entry.key === 'totp_provider_id'"
          :model-value="stringValue(entry.key, entry.property)"
          capability="secret.totp"
          @update:model-value="setField(entry.key, $event)"
        />

        <select
          v-else-if="entry.property['ui:widget'] === 'step-key'"
          :value="stringValue(entry.key, entry.property)"
          @change="
            (event) =>
              setField(entry.key, (event.target as HTMLSelectElement).value)
          "
        >
          <option value="">请选择步骤</option>
          <option
            v-for="option in stepOptions ?? []"
            :key="option.value"
            :value="option.value"
          >
            {{ option.label }}
          </option>
          <option
            v-if="
              stringValue(entry.key, entry.property) &&
              !(stepOptions ?? []).some(
                (option) => option.value === stringValue(entry.key, entry.property),
              )
            "
            :value="stringValue(entry.key, entry.property)"
          >
            {{ stringValue(entry.key, entry.property) }}（当前值）
          </option>
        </select>

        <select
          v-else-if="entry.property.enum"
          :value="stringValue(entry.key, entry.property)"
          @change="
            (event) =>
              setField(entry.key, (event.target as HTMLSelectElement).value)
          "
        >
          <option
            v-for="option in enumOptions(entry.property)"
            :key="option.value"
            :value="option.value"
          >
            {{ option.label }}
          </option>
        </select>

        <textarea
          v-else-if="entry.property['ui:widget'] === 'textarea'"
          :value="stringValue(entry.key, entry.property)"
          rows="3"
          :placeholder="entry.property.description ?? ''"
          @input="
            (event) =>
              setField(entry.key, (event.target as HTMLTextAreaElement).value)
          "
        />

        <input
          v-else-if="entry.property.type === 'number' || entry.property.type === 'integer'"
          type="number"
          :value="numberValue(entry.key, entry.property)"
          :min="entry.property.minimum"
          :max="entry.property.maximum"
          @input="
            (event) => {
              const value = (event.target as HTMLInputElement).value;
              if (value === '') clearField(entry.key);
              else setField(entry.key, Number(value));
            }
          "
        />

        <input
          v-else
          :value="stringValue(entry.key, entry.property)"
          :placeholder="entry.property.description ?? ''"
          @input="
            (event) =>
              setField(entry.key, (event.target as HTMLInputElement).value)
          "
        />
        <p
          v-if="entry.property.enum && enumDescription(entry.key, entry.property)"
          class="enum-hint"
        >
          {{ enumDescription(entry.key, entry.property) }}
        </p>
      </FloatingField>
    </div>
  </div>

  <ElementPickerDialog
    v-model="pickerOpen"
    :base-url="pickerBaseUrl"
    :title="pickerTitle"
    @picked="applyPicked"
  />
</template>

<style scoped>
.schema-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 14px 12px;
}

.schema-field {
  position: relative;
  min-width: 0;
}

.schema-field.is-wide {
  grid-column: 1 / -1;
}

.field-tools {
  position: absolute;
  right: 8px;
  top: -9px;
  z-index: 2;
  display: flex;
  align-items: center;
  gap: 5px;
  padding: 0 4px;
  background: #fff;
}

.restore-btn {
  border: none;
  padding: 0;
  background: transparent;
  color: #2f7ed8;
  font-size: 11px;
  cursor: pointer;
}

.restore-btn:hover {
  text-decoration: underline;
}

.switch-float-field {
  position: relative;
  min-height: 42px;
  padding: 7px 10px 0;
  border: 1px solid #dcdfe6;
  border-radius: 8px;
  background: #fff;
}

.switch-float-label {
  position: absolute;
  left: 7px;
  top: 0;
  transform: translateY(-50%);
  padding: 0 4px;
  color: #606266;
  font-size: 11px;
  line-height: 1.2;
  background: #fff;
  white-space: nowrap;
  max-width: calc(100% - 14px);
  overflow: hidden;
  text-overflow: ellipsis;
}

.switch-float-body {
  min-height: 29px;
  display: flex;
  align-items: center;
  gap: 9px;
}

.switch-state {
  color: #606266;
  font-size: 13px;
}

.switch-field {
  min-height: 42px;
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 0 10px;
  border: 1px solid #dcdfe6;
  border-radius: 8px;
  background: #fff;
}

.switch-label {
  color: #606266;
  font-size: 13px;
}

.required-mark {
  color: #f56c6c;
}

.switch-hint {
  margin: 3px 0 0;
  color: #909399;
  font-size: 11px;
  line-height: 1.4;
}

.enum-hint {
  flex-basis: 100%;
  margin: 2px 0 4px;
  color: #7a8794;
  font-size: 11px;
  line-height: 1.45;
}

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

.inline-row input,
.inline-row select {
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

.inline-row .key-input {
  flex: 0 0 38%;
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
