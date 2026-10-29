<script setup lang="ts">
import { computed, ref } from "vue";

import SignalFields from "@/components/SignalFields.vue";
import type { ResolvedSignalView, SignalForm } from "@/types/workflow";
import { newSignalKey, signalToForm } from "@/utils/workflow";

const props = defineProps<{
  modelValue: SignalForm[];
  chainSignals: ResolvedSignalView[];
  usedKeys: string[];
  pickerBaseUrl?: string;
}>();

const emit = defineEmits<{
  (event: "update:modelValue", value: SignalForm[]): void;
}>();

const collapsed = ref<Set<string>>(new Set());

const localMap = computed(
  () => new Map(props.modelValue.map((signal) => [signal.signal_key, signal])),
);
const chainKeys = computed(
  () => new Set(props.chainSignals.map((signal) => signal.signal_key)),
);
const localOnly = computed(() =>
  props.modelValue.filter((signal) => !chainKeys.value.has(signal.signal_key)),
);

function emitSignals(signals: SignalForm[]): void {
  emit("update:modelValue", signals);
}

function isUsed(key: string): boolean {
  return props.usedKeys.includes(key);
}

function isCollapsed(key: string): boolean {
  return collapsed.value.has(key);
}

function toggleCard(key: string): void {
  const next = new Set(collapsed.value);
  if (next.has(key)) next.delete(key);
  else next.add(key);
  collapsed.value = next;
}

function expandAll(): void {
  collapsed.value = new Set();
}

function collapseAll(): void {
  collapsed.value = new Set([
    ...props.chainSignals.map((signal) => signal.signal_key),
    ...localOnly.value.map((signal) => signal.signal_key),
  ]);
}

function addSignal(): void {
  const used = new Set([
    ...props.chainSignals.map((signal) => signal.signal_key),
    ...props.modelValue.map((signal) => signal.signal_key),
  ]);
  const signal: SignalForm = {
    uid: `signal-${Date.now()}`,
    signal_key: newSignalKey(used),
    label: "",
    description: "",
    include_texts: [],
    include_selectors: [],
    exclude_texts: [],
    exclude_selectors: [],
    match_mode: "any",
    enabled: true,
  };
  emitSignals([...props.modelValue, signal]);
}

function overrideSignal(signal: ResolvedSignalView): void {
  if (localMap.value.has(signal.signal_key)) return;
  const next = new Set(collapsed.value);
  next.delete(signal.signal_key);
  collapsed.value = next;
  emitSignals([...props.modelValue, signalToForm(signal)]);
}

function removeSignal(signal: SignalForm): void {
  if (isUsed(signal.signal_key)) return;
  emitSignals(props.modelValue.filter((item) => item.uid !== signal.uid));
}

function sourceLabel(source: string): string {
  if (source === "current") return "当前配置";
  if (source === "site") return "站点覆盖";
  if (source === "system") return "系统信号";
  return source.replace("template:", "模板 ");
}

function ruleSummary(signal: ResolvedSignalView): string {
  if (signal.source === "system") return signal.description || "系统自动计算";
  const include = [
    ...signal.include_texts.map((item) => `文本“${item}”`),
    ...signal.include_selectors.map((item) => `选择器 ${item}`),
  ];
  const exclude = [
    ...signal.exclude_texts.map((item) => `文本“${item}”`),
    ...signal.exclude_selectors.map((item) => `选择器 ${item}`),
  ];
  return `包含：${include.join(" 或 ") || "无"}；排除：${exclude.join("、") || "无"}`;
}
</script>

<template>
  <el-card shadow="never" class="signal-panel">
    <template #header>
      <div class="card-head">
        <div class="title-row">
          <strong>信号识别</strong>
          <span class="muted">用于步骤执行条件；信号无法判断时自动升级为人工接管。</span>
        </div>
        <div class="head-actions">
          <el-button size="small" @click="expandAll">展开</el-button>
          <el-button size="small" @click="collapseAll">收起</el-button>
          <el-button type="primary" plain @click="addSignal">添加信号</el-button>
        </div>
      </div>
    </template>

    <div v-if="!chainSignals.length && !modelValue.length" class="signal-empty muted">
      还没有信号。添加后，步骤的执行条件才可以使用“信号 = 是/否”。
    </div>

    <div
      v-for="signal in chainSignals"
      :key="`inherited-${signal.signal_key}`"
      class="signal-card inherited"
      :class="{ 'is-collapsed': isCollapsed(signal.signal_key) }"
    >
      <template v-if="localMap.get(signal.signal_key)">
        <div class="signal-card-head">
          <div class="signal-title">
            <button type="button" class="name-toggle" @click="toggleCard(signal.signal_key)">
              <span class="caret">{{ isCollapsed(signal.signal_key) ? "▸" : "▾" }}</span>
              <strong>{{ localMap.get(signal.signal_key)?.label || signal.label }}</strong>
            </button>
            <template v-if="!isCollapsed(signal.signal_key)">
              <el-tag size="small" type="warning" effect="plain">覆盖模板</el-tag>
              <el-tag size="small" effect="plain">{{ sourceLabel(signal.source) }}</el-tag>
              <span class="mono signal-key">{{ signal.signal_key }}</span>
            </template>
          </div>
          <div v-if="!isCollapsed(signal.signal_key)" class="signal-card-actions">
            <el-button
              size="small"
              type="danger"
              plain
              :disabled="isUsed(signal.signal_key)"
              @click="removeSignal(localMap.get(signal.signal_key)!)"
            >
              移除覆盖
            </el-button>
          </div>
        </div>
        <div v-show="!isCollapsed(signal.signal_key)">
          <SignalFields
            :model-value="localMap.get(signal.signal_key)!"
            :used="isUsed(signal.signal_key)"
            :picker-base-url="pickerBaseUrl"
          />
        </div>
      </template>
      <template v-else>
        <div class="signal-card-head">
          <div class="signal-title">
            <button type="button" class="name-toggle" @click="toggleCard(signal.signal_key)">
              <span class="caret">{{ isCollapsed(signal.signal_key) ? "▸" : "▾" }}</span>
              <strong>{{ signal.label }}</strong>
            </button>
            <template v-if="!isCollapsed(signal.signal_key)">
              <el-tag size="small" type="info" effect="plain">{{ sourceLabel(signal.source) }}</el-tag>
              <span class="mono signal-key">{{ signal.signal_key }}</span>
            </template>
          </div>
          <div v-if="!isCollapsed(signal.signal_key)" class="signal-card-actions">
            <el-button
              v-if="signal.source !== 'system'"
              size="small"
              plain
              @click="overrideSignal(signal)"
            >
              覆盖信号
            </el-button>
          </div>
        </div>
        <div v-show="!isCollapsed(signal.signal_key)">
          <p class="muted signal-desc">{{ signal.description || "无描述" }}</p>
          <p class="signal-rules">{{ ruleSummary(signal) }}</p>
        </div>
      </template>
    </div>

    <div
      v-for="signal in localOnly"
      :key="signal.uid"
      class="signal-card"
      :class="{ 'is-collapsed': isCollapsed(signal.signal_key) }"
    >
      <div class="signal-card-head">
        <div class="signal-title">
          <button type="button" class="name-toggle" @click="toggleCard(signal.signal_key)">
            <span class="caret">{{ isCollapsed(signal.signal_key) ? "▸" : "▾" }}</span>
            <strong>{{ signal.label || "未命名信号" }}</strong>
          </button>
          <template v-if="!isCollapsed(signal.signal_key)">
            <el-tag size="small" type="success" effect="plain">当前配置</el-tag>
            <span class="mono signal-key">{{ signal.signal_key }}</span>
          </template>
        </div>
        <div v-if="!isCollapsed(signal.signal_key)" class="signal-card-actions">
          <el-button
            size="small"
            type="danger"
            plain
            :disabled="isUsed(signal.signal_key)"
            @click="removeSignal(signal)"
          >
            移除信号
          </el-button>
        </div>
      </div>
      <div v-show="!isCollapsed(signal.signal_key)">
        <SignalFields
          :model-value="signal"
          :used="isUsed(signal.signal_key)"
          :picker-base-url="pickerBaseUrl"
        />
      </div>
    </div>
  </el-card>
</template>

<style scoped>
.signal-panel {
  border-radius: 16px;
  border-color: #e4e9f0;
  margin-bottom: 16px;
}

.card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.title-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.head-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  justify-content: flex-end;
}

.signal-empty {
  padding: 14px;
  border: 1px dashed #d8e0ea;
  border-radius: 8px;
  font-size: 12px;
}

.signal-card {
  border: 1px solid #e4e9f0;
  border-radius: 12px;
  background: #fbfcfe;
  padding: 12px 14px;
  margin-bottom: 1px;
}

.signal-card:last-child {
  margin-bottom: 0;
}

.signal-card.inherited {
  background: #fffdf7;
  border-color: #f0dfb8;
}

.signal-card-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 8px;
}

.signal-card.is-collapsed .signal-card-head {
  margin-bottom: 0;
}

.signal-title {
  display: flex;
  align-items: center;
  gap: 7px;
  flex-wrap: wrap;
}

.name-toggle {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  border: none;
  padding: 0;
  background: transparent;
  color: inherit;
  font: inherit;
  text-align: left;
  cursor: pointer;
}

.name-toggle strong {
  font-size: 14px;
}

.name-toggle:hover strong {
  color: #2f7ed8;
}

.caret {
  color: #8a96a3;
  font-size: 11px;
  line-height: 1;
}

.signal-key {
  color: #8a96a3;
  font-size: 11px;
}

.signal-card-actions {
  display: flex;
  align-items: center;
  gap: 6px;
  flex: none;
}

.signal-desc {
  margin: 2px 0 4px;
  font-size: 12px;
}

.signal-rules {
  margin: 0;
  color: #7a8794;
  font-size: 12px;
  line-height: 1.5;
}

@media (max-width: 760px) {
  .card-head {
    align-items: flex-start;
    flex-direction: column;
  }
}
</style>
