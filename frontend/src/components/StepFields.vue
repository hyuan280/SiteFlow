<script setup lang="ts">
import { computed, ref } from "vue";

import FloatingField from "@/components/FloatingField.vue";
import SchemaConfigForm from "@/components/SchemaConfigForm.vue";
import type { Feature, ResolvedSignalView, StepForm } from "@/types/workflow";
import { type ConfigObject, type JsonSchema, effectiveValue } from "@/utils/schema";
import {
  categoryLabel,
  collectConditionLeaves,
  parseConditionExpr,
} from "@/utils/workflow";

const ERROR_POLICIES: { value: string; label: string }[] = [
  { value: "stop", label: "停止" },
  { value: "continue", label: "继续" },
  { value: "defer", label: "延后" },
  { value: "escalate_interaction", label: "升级为交互" },
];

const props = defineProps<{
  step: StepForm;
  feature: Feature | undefined;
  features: Feature[];
  inherited?: ConfigObject | null;
  readonly?: boolean;
  lockFeature?: boolean;
  signals: ResolvedSignalView[];
  effectiveCondition?: string | null;
  cfboxEnabled: boolean;
  pickerBaseUrl?: string;
  stepOptions?: { value: string; label: string }[];
}>();

const emit = defineEmits<{
  (event: "feature-change"): void;
}>();

const schema = computed<JsonSchema>(() => props.feature?.config_schema ?? {});
const conditionOpen = ref(false);
const conditionMode = ref<"all" | "any">("all");
const conditionRows = ref<
  { key: string; label: string; description: string; expected: boolean; selected: boolean }[]
>([]);
const conditionError = ref("");
const selectedConditionCount = computed(
  () => conditionRows.value.filter((row) => row.selected).length,
);

const entries = computed(() =>
  Object.entries(schema.value.properties ?? {}).map(([key, property]) => ({
    key,
    property,
  })),
);
function valueOf(key: string, property: JsonSchema): unknown {
  return effectiveValue(key, props.step.config, props.inherited, property);
}

function formatValue(value: unknown): string {
  if (value === undefined || value === null) return "未设置";
  if (typeof value === "boolean") return value ? "是" : "否";
  if (Array.isArray(value)) return value.length ? value.join("、") : "空";
  if (typeof value === "object") return JSON.stringify(value);
  if (value === "") return "空字符串";
  return String(value);
}

function statusLabel(): string {
  if (props.step.enabled === "inherit") return "继承父级";
  return props.step.enabled === "on" ? "强制启用" : "禁用步骤";
}

function conditionValue(): string {
  return props.step.condition || props.effectiveCondition || "always";
}

function signalLabel(key: string): string {
  return props.signals.find((signal) => signal.signal_key === key)?.label ?? key;
}

function conditionLabel(): string {
  const raw = conditionValue();
  const parsed = parseConditionExpr(raw);
  if (!parsed) return `无效条件：${raw}`;
  if (parsed.kind === "always") return "总是执行";
  const leaves = collectConditionLeaves(parsed);
  const parts = leaves.map(
    (item) => `${signalLabel(item.key)}=${item.expected ? "是" : "否"}`,
  );
  if (parsed.kind === "all") return `全部满足：${parts.join("、")}`;
  if (parsed.kind === "any") return `任一满足：${parts.join("、")}`;
  return parts.join("、");
}

function initializeConditionPanel(): void {
  const raw = conditionValue();
  const parsed = parseConditionExpr(raw);
  conditionError.value = "";
  if (!parsed) {
    conditionError.value = `当前条件无法解析：${raw}`;
  }
  conditionMode.value =
    parsed?.kind === "any" ? "any" : "all";
  const leaves = collectConditionLeaves(parsed);
  const leafMap = new Map(leaves.map((item) => [item.key, item]));
  conditionRows.value = props.signals.map((signal) => {
    const leaf = leafMap.get(signal.signal_key);
    return {
      key: signal.signal_key,
      label: signal.label,
      description: signal.description,
      expected: leaf?.expected ?? true,
      selected: leaf !== undefined,
    };
  });
}

function applyCondition(): void {
  const selected = conditionRows.value.filter((row) => row.selected);
  if (!selected.length) {
    props.step.condition = "always";
    conditionOpen.value = false;
    return;
  }
  const parts = selected.map(
    (row) => `signal:${row.key}:${row.expected ? "true" : "false"}`,
  );
  if (parts.length === 1) {
    props.step.condition = parts[0];
  } else {
    props.step.condition = `${conditionMode.value}(${parts.join(",")})`;
  }
  conditionOpen.value = false;
}

function clearCondition(): void {
  props.step.condition = "always";
  conditionRows.value = conditionRows.value.map((row) => ({ ...row, selected: false }));
  conditionOpen.value = false;
}

function errorValue(): string {
  return props.step.on_error || "stop";
}
</script>

<template>
  <div v-if="readonly" class="readonly-fields">
    <div class="readonly-meta">
      <span>功能：{{ feature?.name ?? step.feature_type }}</span>
      <span>分类：{{ feature ? categoryLabel(feature.category) : "-" }}</span>
      <span>步骤状态：{{ statusLabel() }}</span>
      <span>执行条件：{{ conditionLabel() }}</span>
      <span>错误策略：{{ ERROR_POLICIES.find((item) => item.value === errorValue())?.label }}</span>
      <span v-if="step.timeout_seconds">超时时间：{{ step.timeout_seconds }}s</span>
    </div>
    <div class="readonly-grid">
      <FloatingField
        v-for="entry in entries"
        :key="entry.key"
        :label="entry.property.title ?? entry.key"
        :has-value="true"
      >
        <span class="readonly-value">{{ formatValue(valueOf(entry.key, entry.property)) }}</span>
      </FloatingField>
    </div>
  </div>

  <div v-else class="editable-fields">
    <div v-if="!lockFeature" class="step-row two">
      <FloatingField label="功能类型" required :has-value="true">
        <select
          :value="step.feature_type"
          @change="
            (event) => {
              step.feature_type = (event.target as HTMLSelectElement).value;
              emit('feature-change');
            }
          "
        >
          <option v-for="item in features" :key="item.key" :value="item.key">
            {{ item.name }} · {{ item.key }}
          </option>
        </select>
      </FloatingField>
      <FloatingField label="显示名称" :has-value="Boolean(step.label)">
        <input
          :value="step.label"
          placeholder=""
          @input="(event) => (step.label = (event.target as HTMLInputElement).value)"
        />
      </FloatingField>
    </div>
    <div class="step-row four">
      <FloatingField label="步骤状态" :has-value="true">
        <select v-model="step.enabled">
          <option value="inherit">继承父级</option>
          <option value="on">强制启用</option>
          <option value="off">禁用步骤</option>
        </select>
      </FloatingField>
      <FloatingField label="执行条件" :has-value="true">
        <el-popover
          v-model:visible="conditionOpen"
          placement="bottom-start"
          :width="460"
          trigger="click"
          @show="initializeConditionPanel"
        >
          <template #reference>
            <button type="button" class="condition-trigger">
              <span class="condition-summary">{{ conditionLabel() }}</span>
              <span class="condition-caret">▾</span>
            </button>
          </template>
          <div class="condition-panel">
            <el-alert
              v-if="conditionError"
              :title="conditionError"
              type="warning"
              :closable="false"
              show-icon
              class="condition-error"
            />
            <div class="condition-list">
              <div
                v-for="row in conditionRows"
                :key="row.key"
                class="condition-row"
                :class="{ 'is-selected': row.selected }"
              >
                <el-checkbox v-model="row.selected" />
                <div class="condition-name">
                  <strong>{{ row.label }}</strong>
                  <small v-if="row.description">{{ row.description }}</small>
                </div>
                <div class="expected-toggle">
                  <button
                    type="button"
                    :class="{ active: row.expected }"
                    :disabled="!row.selected"
                    @click="row.expected = true"
                  >
                    是
                  </button>
                  <button
                    type="button"
                    :class="{ active: !row.expected }"
                    :disabled="!row.selected"
                    @click="row.expected = false"
                  >
                    否
                  </button>
                </div>
              </div>
              <p v-if="!conditionRows.length" class="muted">暂无可选信号。</p>
            </div>
            <div class="condition-footer">
              <el-radio-group v-model="conditionMode" size="small">
                <el-radio-button value="all">全部满足</el-radio-button>
                <el-radio-button value="any">任一满足</el-radio-button>
              </el-radio-group>
              <div class="condition-actions">
                <el-button size="small" @click="clearCondition">清除</el-button>
                <el-button size="small" type="primary" @click="applyCondition">
                  应用
                </el-button>
              </div>
            </div>
            <p v-if="selectedConditionCount < 2" class="muted condition-hint">
              选择多个信号时可切换全部满足 / 任一满足。
            </p>
          </div>
        </el-popover>
      </FloatingField>
      <FloatingField label="错误策略" :has-value="true">
        <select
          :value="errorValue()"
          @change="
            (event) => {
              const value = (event.target as HTMLSelectElement).value;
              step.on_error = value === 'stop' ? '' : value;
            }
          "
        >
          <option v-for="item in ERROR_POLICIES" :key="item.value" :value="item.value">
            {{ item.label }}
          </option>
        </select>
      </FloatingField>
      <FloatingField
        label="超时时间（秒）"
        :has-value="step.timeout_seconds !== undefined"
      >
        <input
          type="number"
          min="1"
          max="3600"
          :value="step.timeout_seconds ?? ''"
          @input="
            (event) => {
              const value = (event.target as HTMLInputElement).value;
              step.timeout_seconds = value === '' ? undefined : Number(value);
            }
          "
        />
      </FloatingField>
    </div>

    <div v-if="feature" class="config-block">
      <div class="config-head">
        <strong>功能字段</strong>
        <span v-if="inherited" class="muted">灰色“继承”值来自模板，编辑后成为覆盖值。</span>
      </div>
      <SchemaConfigForm
        v-model="step.config"
        :schema="feature.config_schema"
        :inherited="inherited ?? null"
        :picker-base-url="pickerBaseUrl"
        :step-options="stepOptions"
      />
    </div>
  </div>
</template>

<style scoped>
.readonly-fields {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.readonly-meta {
  display: flex;
  gap: 14px;
  flex-wrap: wrap;
  color: #7a8794;
  font-size: 12px;
}

.readonly-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 14px 12px;
}

.readonly-value {
  display: block;
  color: #1f2d3d;
  font-size: 13px;
  line-height: 1.6;
  overflow-wrap: anywhere;
  padding: 2px 0;
}

.editable-fields {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.step-row {
  display: grid;
  gap: 10px 12px;
}

.step-row.two {
  grid-template-columns: repeat(2, minmax(0, 1fr));
}

.step-row.four {
  grid-template-columns: repeat(4, minmax(0, 1fr));
}

.condition-trigger {
  display: flex;
  align-items: center;
  gap: 6px;
  width: 100%;
  min-width: 0;
  padding: 0;
  border: none;
  background: transparent;
  color: #1f2d3d;
  cursor: pointer;
  font: inherit;
  font-size: 13px;
  text-align: left;
}

.condition-summary {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.condition-caret {
  flex: none;
  color: #909399;
  font-size: 10px;
}

.condition-panel {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.condition-error {
  margin-bottom: 2px;
}

.condition-list {
  display: flex;
  flex-direction: column;
  gap: 2px;
  max-height: 260px;
  overflow-y: auto;
}

.condition-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 7px;
  border-radius: 8px;
}

.condition-row:hover,
.condition-row.is-selected {
  background: #f5f9ff;
}

.condition-name {
  display: flex;
  flex: 1;
  flex-direction: column;
  gap: 1px;
  min-width: 0;
}

.condition-name strong {
  color: #303133;
  font-size: 13px;
}

.condition-name small {
  color: #8a96a3;
  font-size: 11px;
  overflow-wrap: anywhere;
}

.expected-toggle {
  display: flex;
  flex: none;
  overflow: hidden;
  border: 1px solid #dcdfe6;
  border-radius: 6px;
}

.expected-toggle button {
  padding: 3px 8px;
  border: none;
  background: #fff;
  color: #606266;
  cursor: pointer;
  font-size: 11px;
}

.expected-toggle button + button {
  border-left: 1px solid #dcdfe6;
}

.expected-toggle button.active {
  background: #409eff;
  color: #fff;
}

.expected-toggle button:disabled {
  color: #c0c4cc;
  cursor: not-allowed;
}

.condition-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  flex-wrap: wrap;
  padding-top: 8px;
  border-top: 1px solid #edf1f6;
}

.condition-actions {
  display: flex;
  gap: 6px;
}

.condition-hint {
  margin: 0;
  font-size: 11px;
}

.config-block {
  margin-top: 2px;
  border-top: 1px solid #edf1f6;
  padding-top: 10px;
}

.config-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 8px;
  flex-wrap: wrap;
}

.config-head span {
  font-size: 12px;
}

@media (max-width: 900px) {
  .step-row.four {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 600px) {
  .step-row.two,
  .step-row.four {
    grid-template-columns: 1fr;
  }
}
</style>
