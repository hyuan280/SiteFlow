<script setup lang="ts">
import { computed, nextTick, ref, watch } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";

import StepFields from "@/components/StepFields.vue";
import type {
  Feature,
  GroupForm,
  ResolvedGroupView,
  ResolvedSignalView,
  StepForm,
  TemplateDetail,
} from "@/types/workflow";
import { type ConfigObject, missingRequiredFields } from "@/utils/schema";
import {
  buildTemplateGroups,
  newGroupKey,
  newStepKey,
  newStepUid,
  collectConditionKeys,
  parseConditionExpr,
  stepUsesLlm,
  type StepKeyNamespace,
  type TemplateStepGroup,
} from "@/utils/workflow";

interface GroupItem {
  id: string;
  kind: "group";
  position: number;
  group: TemplateStepGroup;
}

interface NewItem {
  id: string;
  kind: "new";
  position: number;
  step: StepForm;
}

type TopItem = GroupItem | NewItem;
type PositionItem = TopItem | { id: string; kind: "boundary"; position: number };

const props = defineProps<{
  modelValue: StepForm[];
  features: Feature[];
  chain: TemplateDetail[];
  keyNamespace: StepKeyNamespace;
  signals: ResolvedSignalView[];
  cfboxEnabled: boolean;
  groups: GroupForm[];
  inheritedGroups: ResolvedGroupView[];
  groupsLocked: boolean;
  pickerBaseUrl?: string;
}>();

const emit = defineEmits<{
  (event: "update:modelValue", value: StepForm[]): void;
  (event: "update:groups", value: GroupForm[]): void;
}>();

const collapsed = ref<Set<string>>(new Set());
const codeOpen = ref(false);
const codeStep = ref<StepForm | null>(null);
const codeText = ref("");
const codeError = ref("");

const GROUP_POSITION_STEP = 1_000_000;
const ERROR_POLICIES = new Set(["stop", "continue", "defer", "escalate_interaction"]);
const CODE_FIELDS = new Set([
  "step_key",
  "feature_type",
  "label",
  "name",
  "group_key",
  "enabled",
  "config",
  "condition",
  "on_error",
  "timeout_seconds",
]);

const featureMap = computed(() => new Map(props.features.map((item) => [item.key, item])));
const stepOptions = computed<{ value: string; label: string }[]>(() => {
  const options = new Map<string, { value: string; label: string }>();
  const add = (key: string, featureType: string, label: string) => {
    if (!key) return;
    if (!label.trim() && options.has(key)) return;
    const name =
      label.trim() || featureMap.value.get(featureType)?.name || featureType || key;
    options.set(key, { value: key, label: `${name}（${key}）` });
  };
  const resolved = props.chain[props.chain.length - 1]?.resolved?.steps ?? [];
  for (const step of resolved) {
    add(step.step_key, step.feature_type, step.label ?? "");
  }
  for (const step of props.modelValue) {
    add(step.step_key, step.feature_type, step.label);
  }
  return Array.from(options.values());
});
const groups = computed(() => buildTemplateGroups(props.chain));
const groupMap = computed(() => new Map(groups.value.map((group) => [group.key, group])));
const newSteps = computed(() =>
  props.modelValue.filter((step) => !groupMap.value.has(step.step_key)),
);
interface DisplayGroup {
  step_anchor?: number | null;
  key: string;
  name: string;
  position: number;
  source: string;
  locked: boolean;
  form?: GroupForm;
}

interface GroupSection {
  id: string;
  group: DisplayGroup;
  items: TopItem[];
  order: number;
}

const displayGroups = computed<DisplayGroup[]>(() => [
  ...props.inheritedGroups.map((group) => ({
    key: group.group_key,
    name: group.name,
    position: [...props.groups].sort((a, b) => a.position - b.position).reduce((position, local) => position >= local.position ? position + GROUP_POSITION_STEP : position, group.position),
    step_anchor: group.step_anchor,
    source: group.source,
    locked: true,
  })),
  ...props.groups.map((group) => ({
    key: group.group_key,
    name: group.name,
    position: group.position,
    step_anchor: group.step_anchor,
    source: "当前配置",
    locked: props.groupsLocked && !group.isNew,
    form: group,
  })),
]);

const topItems = computed<TopItem[]>(() => {
  const items: TopItem[] = [
    ...groups.value.map((group) => ({
      id: `group:${group.key}`,
      kind: "group" as const,
      position: group.position + (props.modelValue.some((step) => step.step_key === group.key) ? 1 : 0),
      group,
    })),
    ...newSteps.value.map((step, index) => ({
      id: `new:${step.uid}`,
      kind: "new" as const,
      position: step.position ?? Number.MAX_SAFE_INTEGER + index,
      step,
    })),
  ];
  return items.sort((left, right) => left.position - right.position);
});

function groupKeyFor(item: TopItem): string {
  return item.kind === "group" ? item.group.group_key : item.step.group_key;
}

const groupSections = computed<GroupSection[]>(() => {
  const known = new Set(displayGroups.value.map((group) => group.key));
  const blocks: {
    order: number;
    kind: "group" | "step";
    group?: DisplayGroup;
    items?: TopItem[];
    item?: TopItem;
  }[] = [];
  const orderedGroups = [...displayGroups.value].sort((a, b) => a.position - b.position || a.key.localeCompare(b.key));
  const anchors = orderedGroups.map((group) => {
    const items = topItems.value.filter((item) => groupKeyFor(item) === group.key);
    return group.step_anchor ?? (items.length ? Math.min(...items.map((item) => item.position)) : 0);
  }).sort((a, b) => a - b);
  for (const [index, group] of orderedGroups.entries()) {
    const items = topItems.value.filter((item) => groupKeyFor(item) === group.key);
    blocks.push({
      order: anchors[index],
      kind: "group",
      group,
      items,
    });
  }
  for (const item of topItems.value) {
    if (!known.has(groupKeyFor(item))) {
      blocks.push({ order: item.position, kind: "step", item });
    }
  }
  blocks.sort(
    (left, right) =>
      left.order - right.order ||
      (left.kind === "group" ? 0 : 1) - (right.kind === "group" ? 0 : 1),
  );
  const sections: GroupSection[] = [];
  let pending: TopItem[] = [];
  let pendingOrder = 0;
  let ungroupedIndex = 0;
  const flush = () => {
    if (!pending.length) return;
    sections.push({
      id: `ungrouped:${ungroupedIndex++}`,
      group: {
        key: "",
        name: "未分组",
        position: pendingOrder,
        source: "当前配置",
        locked: false,
      },
      items: [...pending],
      order: pendingOrder,
    });
    pending = [];
  };
  for (const block of blocks) {
    if (block.kind === "group" && block.group && block.items) {
      flush();
      sections.push({
        id: `group:${block.group.key}`,
        group: block.group,
        items: block.items,
        order: block.order,
      });
    } else if (block.item) {
      if (!pending.length) pendingOrder = block.order;
      pending.push(block.item);
    }
  }
  flush();
  return sections;
});

function stepNumber(item: TopItem): number {
  const ordered = groupSections.value.flatMap((section) => section.items);
  return ordered.findIndex((entry) => entry.id === item.id) + 1;
}

function emitGroups(groups: GroupForm[]): void {
  emit("update:groups", groups);
}

function updateGroupName(group: DisplayGroup, value: string): void {
  if (!group.form || group.locked) return;
  const name = value.trim();
  if (!name) return;
  emitGroups(
    props.groups.map((item) =>
      item.group_key === group.key ? { ...item, name } : item,
    ),
  );
}

function removeGroup(group: DisplayGroup): void {
  if (!group.form || group.locked) return;
  const section = groupSections.value.find((item) => item.group.key === group.key);
  if (section?.items.length) {
    ElMessage.warning("分组内仍有步骤，请先移除或移动步骤");
    return;
  }
  emitGroups(props.groups.filter((item) => item.group_key !== group.key));
}

async function addGroup(afterKey = ""): Promise<void> {
  let name = "";
  try {
    const result = await ElMessageBox.prompt("分组名称", "新建分组", {
      confirmButtonText: "创建",
      cancelButtonText: "取消",
      inputPlaceholder: "例如：登录、验证、签到",
      inputValidator: (value) => Boolean(value.trim()) || "请填写分组名称",
    });
    name = result.value.trim();
  } catch {
    return;
  }
  const used = new Set([
    ...props.inheritedGroups.map((group) => group.group_key),
    ...props.groups.map((group) => group.group_key),
    ...props.modelValue.map((step) => step.step_key),
  ]);
  const groupKey = newGroupKey(props.keyNamespace, used);
  const ordered = [...groupSections.value];
  const index = afterKey
    ? ordered.findIndex((section) => section.group.key === afterKey)
    : ordered.length - 1;
  const current = ordered[index];
  const anchor = [...displayGroups.value].sort((a, b) => a.position - b.position).find((group) => group.key === afterKey)
    ?? [...displayGroups.value].sort((a, b) => b.position - a.position)[0];
  const position = (anchor?.position ?? 0) + GROUP_POSITION_STEP;
  const group: GroupForm = {
    uid: `group-${Date.now()}`,
    group_key: groupKey,
    name,
    position,
    step_anchor: positionBetweenSections(current, ordered[index + 1]),
    isNew: true,
  };
  emitGroups([...props.groups.map((item) => item.position >= position ? { ...item, position: item.position + GROUP_POSITION_STEP } : item), group]);
  nextTick(() => {
    document
      .querySelector(`[data-group-key="${groupKey}"]`)
      ?.scrollIntoView({ block: "center" });
  });
}

function addStepToGroup(key: string): void {
  addStep(key);
}

function positionBetweenSections(
  previous?: GroupSection,
  next?: GroupSection,
): number {
  const lower = previous ? (previous.group.key ? previous.order : sectionMaxPosition(previous)) : 0;
  if (!next) return lower + 1000;
  const upper = next.order;
  if (upper <= lower) {
    throw new Error("分组位置重叠，请先调整相邻分组的顺序");
  }
  return lower + Math.min(1000, (upper - lower) / 2);
}

function setStepGroup(step: StepForm, key: string): void {
  if (step.group_key === key) return;
  step.group_key = key;
  const section = groupSections.value.find((item) => item.group.key === key);
  const position =
    section && section.items.length === 0 && section.group.form
      ? 1000
      : nextStepPosition(key);
  step.position = position;
  emitSteps([...props.modelValue]);
}

function sectionMaxPosition(section: GroupSection): number {
  const positions = sectionStepPositions(section);
  if (positions.length) return Math.max(...positions);
  return section.order;
}

function moveGroup(groupKey: string, direction: -1 | 1): void {
  const sections = groupSections.value;
  const index = sections.findIndex((item) => item.group.key === groupKey);
  if (index < 0) return;
  const section = sections[index];
  if (!section.group.form || section.group.locked) return;
  const adjacent = sections[index + direction];
  if (!adjacent) return;
  if (!adjacent.group.key) return;
  const target = adjacent.group.position;
  emitGroups(
    props.groups.map((item) =>
      item.group_key === groupKey ? { ...item, position: target } :
        item.group_key === adjacent.group.key ? { ...item, position: section.group.position } : item,
    ),
  );
  nextTick(() => {
    document
      .querySelector(`[data-group-key="${groupKey}"]`)
      ?.scrollIntoView({ block: "center" });
  });
}

function currentFor(group: TemplateStepGroup): StepForm | undefined {
  return props.modelValue.find((step) => step.step_key === group.key);
}

function featureFor(step: StepForm): Feature | undefined {
  return featureMap.value.get(step.feature_type);
}

function usesLlm(
  featureType: string,
  config: ConfigObject,
  inherited?: ConfigObject | null,
): boolean {
  return stepUsesLlm(featureMap.value.get(featureType), config, inherited);
}

function inheritedFor(step: StepForm): ConfigObject | null {
  const group = groupMap.value.get(step.step_key);
  if (!group || group.feature_type !== step.feature_type) return null;
  return group.cumulative;
}

function inheritedConditionFor(step: StepForm): string | null {
  const group = groupMap.value.get(step.step_key);
  if (!group || group.feature_type !== step.feature_type) return null;
  return group.condition;
}

function emitSteps(steps: StepForm[]): void {
  emit("update:modelValue", steps);
}

function normalizePositions(force = false): void {
  let changed = false;
  for (const step of newSteps.value) {
    if (step.position !== undefined) continue;
    step.position = nextStepPosition(step.group_key);
    changed = true;
  }
  if (changed) emitSteps([...props.modelValue]);
}

function assignPositions(items: PositionItem[]): boolean {
  const updates: { step: StepForm; position: number }[] = [];
  let index = 0;
  while (index < items.length) {
    const item = items[index];
    if (item.kind !== "new") {
      index += 1;
      continue;
    }
    const start = index;
    while (index < items.length && items[index].kind === "new") index += 1;
    const run = items.slice(start, index) as NewItem[];
    const prev = findGroupPosition(items, start, -1);
    const next = findGroupPosition(items, index - 1, 1);
    const count = run.length;
    run.forEach((entry, offset) => {
      let position: number;
      if (prev !== null && next !== null) {
        position = prev + ((next - prev) * (offset + 1)) / (count + 1);
      } else if (prev !== null) {
        position = prev + 1000 * (offset + 1);
      } else if (next !== null) {
        position = (next * (offset + 1)) / (count + 1);
      } else {
        position = 1000 * (offset + 1);
      }
      updates.push({ step: entry.step, position });
    });
  }
  let previous = -1;
  let previousLocal = false;
  for (const item of items) {
    const position = item.kind !== "new" ? item.position : updates.find((entry) => entry.step === item.step)!.position;
    if (!Number.isFinite(position) || position < 0 || position > 1_000_000_000 ||
        position < previous || (position === previous && (item.kind === "new" || previousLocal))) {
      ElMessage.warning("Not enough position space between fixed workflow blocks");
      return false;
    }
    previous = position;
    previousLocal = item.kind === "new";
  }
  for (const update of updates) update.step.position = update.position;
  return true;
}

function findGroupPosition(items: PositionItem[], from: number, direction: -1 | 1): number | null {
  let index = from + direction;
  while (index >= 0 && index < items.length) {
    const item = items[index];
    if (item.kind !== "new") return item.position;
    index += direction;
  }
  return null;
}

function endPosition(): number {
  const last = groupSections.value[groupSections.value.length - 1];
  return last ? (last.group.key ? last.order : sectionMaxPosition(last)) + 1000 : 1000;
}

function nextStepPosition(groupKey: string): number {
  if (!groupKey) return endPosition();
  const sections = groupSections.value;
  const index = sections.findIndex((item) => item.group.key === groupKey);
  const section = sections[index];
  const items = section?.items ?? [];
  if (!items.length) {
    const group = displayGroups.value.find((item) => item.key === groupKey);
    return 1000;
  }
  const last = sectionMaxPosition(section);
  const next = sections[index + 1];
  return last + 1000;
}

function addStep(groupKey = ""): void {
  const used = new Set(props.modelValue.map((step) => step.step_key));
  for (const group of groups.value) used.add(group.key);
  for (const group of props.inheritedGroups) used.add(group.group_key);
  for (const group of props.groups) used.add(group.group_key);
  const feature =
    props.features.find((item) => item.key === "flow.open") ?? props.features[0];
  if (!feature) return;
  if (!groupKey && !assignPositions(ungroupedPositionItems())) return;
  const position = nextStepPosition(groupKey);
  const step: StepForm = {
    uid: newStepUid(),
    step_key: newStepKey(props.keyNamespace, used),
    feature_type: feature.key,
    label: "",
    group_key: groupKey,
    position,
    enabled: "on",
    config: {},
    condition: "",
    on_error: "",
    timeout_seconds: undefined,
    isNew: true,
  };
  emitSteps([...props.modelValue, step]);
  nextTick(() => scrollStepIntoView(step.uid));
}

function addModification(group: TemplateStepGroup): void {
  if (currentFor(group)) return;
  const step: StepForm = {
    uid: newStepUid(),
    step_key: group.key,
    feature_type: group.feature_type,
    label: "",
    group_key: group.group_key,
    position: undefined,
    enabled: "inherit",
    config: {},
    condition: "",
    on_error: "",
    timeout_seconds: undefined,
  };
  emitSteps([...props.modelValue, step]);
}

function removeStep(step: StepForm): void {
  emitSteps(props.modelValue.filter((item) => item.uid !== step.uid));
}

function baseCardId(key: string): string {
  return `base:${key}`;
}

function modCardId(key: string, detailId: number, stepKey: string): string {
  return `mod:${key}:${detailId}:${stepKey}`;
}

function currentCardId(key: string): string {
  return `current:${key}`;
}

function newCardId(uid: string): string {
  return `new:${uid}`;
}

function isCardCollapsed(id: string): boolean {
  return collapsed.value.has(id);
}

function toggleCard(id: string): void {
  const next = new Set(collapsed.value);
  if (next.has(id)) next.delete(id);
  else next.add(id);
  collapsed.value = next;
}

function expandAll(): void {
  collapsed.value = new Set();
}

function collapseAll(): void {
  const ids = new Set<string>();
  for (const group of groups.value) {
    ids.add(`base:${group.key}`);
    for (const mod of group.mods) {
      ids.add(`mod:${group.key}:${mod.detail.id}:${mod.step.step_key}`);
    }
    if (currentFor(group)) ids.add(`current:${group.key}`);
  }
  for (const step of newSteps.value) ids.add(`new:${step.uid}`);
  collapsed.value = ids;
}

function codeValue(step: StepForm): Record<string, unknown> {
  return {
    feature_type: step.feature_type,
    label: step.label,
    enabled:
      step.enabled === "inherit" ? null : step.enabled === "on",
    group_key: step.group_key,
    condition: step.condition || null,
    on_error: step.on_error,
    timeout_seconds: step.timeout_seconds ?? null,
    config: step.config,
  };
}

function openCodeConfig(step: StepForm): void {
  codeStep.value = step;
  codeText.value = JSON.stringify(codeValue(step), null, 2);
  codeError.value = "";
  codeOpen.value = true;
}

function asObject(value: unknown, label: string): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${label} 必须是对象`);
  }
  return value as Record<string, unknown>;
}

function applyCodeConfig(): void {
  const step = codeStep.value;
  if (!step) return;
  codeError.value = "";
  try {
    let parsed: unknown;
    try {
      parsed = JSON.parse(codeText.value);
    } catch (cause) {
      throw new Error(`JSON 语法错误：${cause instanceof Error ? cause.message : "无法解析"}`);
    }
    const data = asObject(parsed, "配置代码");
    for (const key of Object.keys(data)) {
      if (!CODE_FIELDS.has(key)) {
        throw new Error(`未知字段：${key}`);
      }
    }
    let feature = featureFor(step);
    if (data.feature_type !== undefined) {
      if (typeof data.feature_type !== "string") {
        throw new Error("feature_type 必须是字符串");
      }
      const nextFeature = featureMap.value.get(data.feature_type);
      if (!nextFeature) {
        throw new Error(`未知功能类型：${data.feature_type}`);
      }
      if (groupMap.value.has(step.step_key) && nextFeature.key !== step.feature_type) {
        throw new Error("模板步骤不能修改功能类型");
      }
      feature = nextFeature;
    }
    if (!feature) {
      throw new Error("功能类型无效");
    }
    const rawLabel = data.label !== undefined ? data.label : data.name;
    const nextLabel = rawLabel === undefined
      ? step.label
      : rawLabel === null
        ? ""
        : String(rawLabel);
    let nextEnabled = step.enabled;
    if (data.enabled !== undefined) {
      if (data.enabled === null || data.enabled === "inherit") {
        nextEnabled = "inherit";
      } else if (data.enabled === true || data.enabled === "on") {
        nextEnabled = "on";
      } else if (data.enabled === false || data.enabled === "off") {
        nextEnabled = "off";
      } else {
        throw new Error("enabled 只能是 null、true、false 或 inherit/on/off");
      }
    }
    const nextGroupKey = data.group_key === undefined
      ? step.group_key
      : data.group_key === null
        ? ""
        : String(data.group_key);
    if (nextGroupKey) {
      const knownGroup =
        props.groups.some((group) => group.group_key === nextGroupKey) ||
        props.inheritedGroups.some((group) => group.group_key === nextGroupKey);
      if (!knownGroup) {
        throw new Error(`分组不存在：${nextGroupKey}`);
      }
    }
    const inherited = groupMap.value.has(step.step_key) ? inheritedFor(step) : null;
    if (inherited) {
      const expected = groupMap.value.get(step.step_key)?.group_key ?? "";
      if (nextGroupKey !== expected) {
        throw new Error("模板步骤不能通过代码配置改变分组");
      }
    } else if (props.inheritedGroups.some((group) => group.group_key === nextGroupKey)) {
      throw new Error("本地步骤不能使用模板分组");
    } else if (
      !step.isNew &&
      props.groupsLocked &&
      step.group_key &&
      nextGroupKey !== step.group_key
    ) {
      throw new Error("模板已被使用，只有新增步骤可以改变分组");
    }
    const nextCondition = data.condition === undefined
      ? step.condition
      : data.condition === null || data.condition === ""
        ? ""
        : String(data.condition);
    const condition = parseConditionExpr(nextCondition);
    if (nextCondition && nextCondition !== "always" && !condition) {
      throw new Error(`执行条件格式无效：${nextCondition}`);
    }
    for (const key of collectConditionKeys(condition)) {
      if (!props.signals.some((signal) => signal.signal_key === key)) {
        throw new Error(`执行条件引用了不存在的信号：${key}`);
      }
    }
    const nextOnError = data.on_error === undefined
      ? step.on_error
      : data.on_error === null
        ? ""
        : String(data.on_error);
    if (nextOnError && !ERROR_POLICIES.has(nextOnError)) {
      throw new Error(`错误策略无效：${nextOnError}`);
    }
    let nextTimeout = step.timeout_seconds;
    if (data.timeout_seconds !== undefined) {
      if (data.timeout_seconds === null || data.timeout_seconds === "") {
        nextTimeout = undefined;
      } else {
        const value = Number(data.timeout_seconds);
        if (!Number.isInteger(value) || value < 1 || value > 3600) {
          throw new Error("timeout_seconds 必须是 1-3600 的整数");
        }
        nextTimeout = value;
      }
    }
    const nextConfig = data.config === undefined
      ? step.config
      : asObject(data.config, "config");
    const propertyKeys = new Set(Object.keys(feature.config_schema.properties ?? {}));
    for (const key of Object.keys(nextConfig)) {
      if (!propertyKeys.has(key)) {
        throw new Error(`config 包含未知字段：${key}`);
      }
    }
    const missing = missingRequiredFields(feature.config_schema, nextConfig, inherited);
    if (missing.length) {
      throw new Error(`配置缺少必填字段：${missing.join("、")}`);
    }
    step.feature_type = feature.key;
    step.label = nextLabel;
    step.enabled = nextEnabled;
    if (step.group_key !== nextGroupKey) {
      step.group_key = nextGroupKey;
      step.position = nextStepPosition(nextGroupKey);
    }
    step.condition = nextCondition;
    step.on_error = nextOnError;
    step.timeout_seconds = nextTimeout;
    step.config = nextConfig;
    emitSteps([...props.modelValue]);
    codeOpen.value = false;
    codeStep.value = null;
  } catch (cause) {
    codeError.value = cause instanceof Error ? cause.message : "配置代码无效";
  }
}

function onFeatureChange(step: StepForm): void {
  if (featureFor(step)) {
    step.config = {};
    step.label = "";
  }
}

function scrollStepIntoView(uid: string): void {
  const element = document.querySelector(`[data-step-uid="${uid}"]`);
  if (element instanceof HTMLElement) {
    element.scrollIntoView({ block: "center" });
  }
}

function sectionStepPositions(section: GroupSection): number[] {
  const positions: number[] = [];
  for (const item of section.items) {
    if (item.kind === "group") {
      if (typeof item.group.base.step.position === "number") {
        positions.push(item.group.base.step.position);
      }
      for (const mod of item.group.mods) {
        if (typeof mod.step.position === "number") {
          positions.push(mod.step.position);
        }
      }
    } else if (typeof item.step.position === "number") {
      positions.push(item.step.position);
    }
  }
  return positions;
}

function ungroupedPositionItems(): PositionItem[] {
  // Group slots are fixed step-space boundaries, not their independent ranks
  // or the positions of the steps contained in them.
  return groupSections.value.flatMap((section): PositionItem[] =>
    section.group.key
      ? [{ id: section.id, kind: "boundary", position: section.order }]
      : section.items,
  );
}

function moveStep(step: StepForm, direction: -1 | 1): void {
  if (step.group_key) {
    const items = topItems.value.filter((item) => groupKeyFor(item) === step.group_key);
    const index = items.findIndex((item) => item.kind === "new" && item.step.uid === step.uid);
    const adjacent = items[index + direction];
    if (!adjacent || adjacent.kind !== "new") return;
    const oldPosition = step.position;
    step.position = adjacent.step.position;
    adjacent.step.position = oldPosition;
    emitSteps([...props.modelValue]);
    return;
  }
  const items = ungroupedPositionItems();
  const fromIndex = items.findIndex(
    (item) => item.kind === "new" && item.step.uid === step.uid,
  );
  if (fromIndex < 0) return;
  const target = Math.max(0, Math.min(items.length - 1, fromIndex + direction));
  if (target === fromIndex) return;
  const [moved] = items.splice(fromIndex, 1);
  items.splice(target, 0, moved);
  if (!assignPositions(items)) return;
  emitSteps([...props.modelValue]);
  nextTick(() => scrollStepIntoView(step.uid));
}

watch(
  () => props.chain,
  () => {
    nextTick(() => normalizePositions(true));
  },
);

watch(
  () => props.modelValue.length,
  () => {
    nextTick(() => normalizePositions());
  },
  { immediate: true },
);

defineExpose({ addStep, addGroup, expandAll, collapseAll });
</script>

<template>
  <div class="step-editor-list">
    <div v-if="!topItems.length" class="empty muted">
      暂无步骤，点击“添加步骤”开始配置。
    </div>

    <div
      v-for="section in groupSections"
      :key="section.id"
      class="workflow-group"
      :data-group-key="section.group.key"
    >
      <div class="workflow-group-head">
        <div class="group-title">
          <strong v-if="section.group.locked || !section.group.form">
            {{ section.group.name }}
          </strong>
          <el-input
            v-else
            :model-value="section.group.name"
            size="small"
            class="group-name-input"
            @change="updateGroupName(section.group, $event)"
          />
          <el-tag
            v-if="section.group.locked"
            size="small"
            type="info"
            effect="plain"
          >模板分组</el-tag>
          <el-tag v-else size="small" type="success" effect="plain">当前配置</el-tag>
          <span class="muted">{{ section.items.length }} 个步骤</span>
        </div>
        <div class="group-actions">
          <el-button
            v-if="section.group.form && !section.group.locked"
            size="small"
            plain
            title="上移分组"
            @click="moveGroup(section.group.key, -1)"
          >
            ↑
          </el-button>
          <el-button
            v-if="section.group.form && !section.group.locked"
            size="small"
            plain
            title="下移分组"
            @click="moveGroup(section.group.key, 1)"
          >
            ↓
          </el-button>
          <el-button
            v-if="section.group.form"
            size="small"
            plain
            @click="addStepToGroup(section.group.key)"
          >
            添加步骤
          </el-button>
          <el-button
            v-if="section.group.key"
            size="small"
            plain
            @click="addGroup(section.group.key)"
          >
            在此后新建分组
          </el-button>
          <el-button
            v-if="section.group.form && !section.group.locked"
            size="small"
            type="danger"
            plain
            @click="removeGroup(section.group)"
          >
            删除分组
          </el-button>
        </div>
      </div>

      <div class="workflow-group-body">
        <p v-if="!section.items.length" class="muted group-empty">该分组暂无步骤。</p>
        <template v-for="item in section.items" :key="item.id">
      <div class="top-item">
        <template v-if="item.kind === 'group'">
          <div class="imported-group">
            <div
              class="imported-card base-card"
              :class="{ 'is-collapsed': isCardCollapsed(baseCardId(item.group.key)) }"
            >
              <div class="card-head">
                <div class="step-heading">
                  <span
                    v-if="!isCardCollapsed(baseCardId(item.group.key))"
                    class="step-order"
                  >#{{ stepNumber(item) }}</span>
                  <button
                    type="button"
                    class="name-toggle"
                    @click="toggleCard(baseCardId(item.group.key))"
                  >
                    <span class="caret">
                      {{ isCardCollapsed(baseCardId(item.group.key)) ? "▸" : "▾" }}
                    </span>
                    <strong>{{ item.group.base.step.label || featureMap.get(item.group.feature_type)?.name || item.group.key }}</strong>
                  </button>
                  <template v-if="!isCardCollapsed(baseCardId(item.group.key))">
                    <el-tag size="small" type="info" effect="plain">模板步骤</el-tag>
                    <el-tag v-if="usesLlm(item.group.feature_type, item.group.cumulative)" size="small" type="success" effect="plain">大模型</el-tag>
                    <el-tag size="small" effect="plain">
                      来自模板 {{ item.group.base.source }}
                    </el-tag>
                  </template>
                </div>
                <div v-if="!isCardCollapsed(baseCardId(item.group.key))" class="step-actions">
                  <el-tag size="small" effect="plain" class="mono">{{ item.group.key }}</el-tag>
                  <el-button
                    v-if="!currentFor(item.group)"
                    size="small"
                    type="primary"
                    plain
                    @click="addModification(item.group)"
                  >
                    修改步骤
                  </el-button>
                  <el-tag v-else size="small" type="warning" effect="plain">当前已修改</el-tag>
                </div>
              </div>
              <StepFields
                v-show="!isCardCollapsed(baseCardId(item.group.key))"
                :step="item.group.base.form"
                :feature="featureMap.get(item.group.feature_type)"
                :features="features"
                :inherited="null"
                :signals="signals"
                :cfbox-enabled="cfboxEnabled"
                :picker-base-url="pickerBaseUrl"
                :step-options="stepOptions"
                readonly
              />
            </div>

            <div
              v-for="mod in item.group.mods"
              :key="`${mod.detail.id}:${mod.step.step_key}`"
              class="imported-card child-card"
              :class="{
                'is-collapsed': isCardCollapsed(
                  modCardId(item.group.key, mod.detail.id, mod.step.step_key),
                ),
              }"
            >
              <div class="card-head">
                <div class="step-heading">
                  <span
                    v-if="!isCardCollapsed(modCardId(item.group.key, mod.detail.id, mod.step.step_key))"
                    class="step-order"
                  >子步骤</span>
                  <button
                    type="button"
                    class="name-toggle"
                    @click="toggleCard(modCardId(item.group.key, mod.detail.id, mod.step.step_key))"
                  >
                    <span class="caret">
                      {{
                        isCardCollapsed(modCardId(item.group.key, mod.detail.id, mod.step.step_key))
                          ? "▸"
                          : "▾"
                      }}
                    </span>
                    <strong>{{ mod.step.label || featureMap.get(mod.step.feature_type)?.name || item.group.key }}</strong>
                  </button>
                  <template
                    v-if="!isCardCollapsed(modCardId(item.group.key, mod.detail.id, mod.step.step_key))"
                  >
                    <el-tag size="small" type="warning" effect="plain">模板修改</el-tag>
                    <el-tag v-if="usesLlm(mod.step.feature_type, mod.cumulative)" size="small" type="success" effect="plain">大模型</el-tag>
                    <el-tag size="small" effect="plain">来自模板 {{ mod.source }}</el-tag>
                  </template>
                </div>
                <el-tag
                  v-if="!isCardCollapsed(modCardId(item.group.key, mod.detail.id, mod.step.step_key))"
                  size="small"
                  effect="plain"
                  class="mono"
                >{{ mod.step.step_key }}</el-tag>
              </div>
              <StepFields
                v-show="!isCardCollapsed(modCardId(item.group.key, mod.detail.id, mod.step.step_key))"
                :step="mod.form"
                :feature="featureMap.get(mod.step.feature_type)"
                :features="features"
                :inherited="mod.inherited"
                :signals="signals"
                :cfbox-enabled="cfboxEnabled"
                :picker-base-url="pickerBaseUrl"
                :step-options="stepOptions"
                readonly
              />
            </div>

            <div
              v-if="currentFor(item.group)"
              class="imported-card current-card"
              :class="{ 'is-collapsed': isCardCollapsed(currentCardId(item.group.key)) }"
            >
              <div class="card-head">
                <div class="step-heading">
                  <span
                    v-if="!isCardCollapsed(currentCardId(item.group.key))"
                    class="step-order"
                  >子步骤</span>
                  <button
                    type="button"
                    class="name-toggle"
                    @click="toggleCard(currentCardId(item.group.key))"
                  >
                    <span class="caret">
                      {{ isCardCollapsed(currentCardId(item.group.key)) ? "▸" : "▾" }}
                    </span>
                    <strong>当前配置修改</strong>
                  </button>
                  <el-tag
                    v-if="!isCardCollapsed(currentCardId(item.group.key))"
                    size="small"
                    type="success"
                    effect="plain"
                  >当前配置</el-tag>
                  <el-tag
                    v-if="!isCardCollapsed(currentCardId(item.group.key)) && usesLlm(currentFor(item.group)!.feature_type, currentFor(item.group)!.config, inheritedFor(currentFor(item.group)!))"
                    size="small"
                    type="success"
                    effect="plain"
                  >大模型</el-tag>
                </div>
                <div v-if="!isCardCollapsed(currentCardId(item.group.key))" class="step-actions">
                  <el-tag size="small" effect="plain" class="mono">{{ item.group.key }}</el-tag>
                  <el-button
                    size="small"
                    plain
                    @click="openCodeConfig(currentFor(item.group)!)"
                  >
                    代码配置
                  </el-button>
                  <el-button
                    size="small"
                    type="danger"
                    plain
                    @click="removeStep(currentFor(item.group)!)"
                  >
                    移除步骤
                  </el-button>
                </div>
              </div>
              <StepFields
                v-show="!isCardCollapsed(currentCardId(item.group.key))"
                :step="currentFor(item.group)!"
                :feature="featureMap.get(currentFor(item.group)!.feature_type)"
                :features="features"
                :inherited="inheritedFor(currentFor(item.group)!)"
                :signals="signals"
                :cfbox-enabled="cfboxEnabled"
                :picker-base-url="pickerBaseUrl"
                :step-options="stepOptions"
                :effective-condition="inheritedConditionFor(currentFor(item.group)!)"
                lock-feature
                @feature-change="onFeatureChange(currentFor(item.group)!)"
              />
            </div>
          </div>
        </template>

        <template v-else>
          <div
            class="imported-card new-card"
            :class="{ 'is-collapsed': isCardCollapsed(newCardId(item.step.uid)) }"
            :data-step-uid="item.step.uid"
          >
            <div class="card-head">
              <div class="step-heading">
                <button
                  type="button"
                  class="name-toggle"
                  @click="toggleCard(newCardId(item.step.uid))"
                >
                  <span class="caret">
                    {{ isCardCollapsed(newCardId(item.step.uid)) ? "▸" : "▾" }}
                  </span>
                  <strong>{{ item.step.label || featureMap.get(item.step.feature_type)?.name || item.step.step_key }}</strong>
                </button>
                <el-tag size="small" type="success" effect="plain">当前配置</el-tag>
                <el-tag v-if="item.step.isNew" size="small" type="success" effect="plain">
                  新增
                </el-tag>
                <el-tag v-if="usesLlm(item.step.feature_type, item.step.config)" size="small" type="success" effect="plain">大模型</el-tag>
                <el-select
                  v-if="
                    props.groups.length &&
                    (item.step.isNew || !groupsLocked || !item.step.group_key)
                  "
                  :model-value="item.step.group_key"
                  size="small"
                  class="step-group-select"
                  placeholder="未分组"
                  @change="setStepGroup(item.step, $event)"
                >
                  <el-option label="未分组" value="" />
                  <el-option
                    v-for="group in props.groups"
                    :key="group.group_key"
                    :label="group.name"
                    :value="group.group_key"
                  />
                </el-select>
              </div>
              <div class="step-actions">
                <el-button size="small" plain @click="openCodeConfig(item.step)">
                  代码配置
                </el-button>
                <el-button size="small" plain title="上移" @click="moveStep(item.step, -1)">
                  ↑
                </el-button>
                <el-button size="small" plain title="下移" @click="moveStep(item.step, 1)">
                  ↓
                </el-button>
                <template v-if="!isCardCollapsed(newCardId(item.step.uid))">
                  <el-tag size="small" effect="plain" class="mono">{{ item.step.step_key }}</el-tag>
                  <el-button size="small" type="danger" plain @click="removeStep(item.step)">
                    移除步骤
                  </el-button>
                </template>
              </div>
            </div>
            <StepFields
              v-show="!isCardCollapsed(newCardId(item.step.uid))"
              :step="item.step"
              :feature="featureMap.get(item.step.feature_type)"
              :features="features"
              :inherited="null"
              :signals="signals"
              :cfbox-enabled="cfboxEnabled"
              :picker-base-url="pickerBaseUrl"
              :step-options="stepOptions"
              @feature-change="onFeatureChange(item.step)"
            />
          </div>
        </template>
      </div>
        </template>
      </div>
    </div>

    <div class="step-editor-footer">
      <span class="muted">
        标题栏添加的步骤进入「未分组」；新增步骤可在卡片上选择分组，或先新建分组再分配。模板分组本身只读。
      </span>
      <el-button size="small" plain @click="addGroup()">新建分组</el-button>
    </div>

    <el-dialog
      v-model="codeOpen"
      title="代码配置"
      width="min(760px, 94vw)"
      destroy-on-close
    >
      <div class="code-config">
        <p class="muted code-hint">
          粘贴 JSON 配置。可配置名称（label/name）、功能类型、分组、步骤状态、执行条件、错误策略、超时时间和功能字段。
        </p>
        <el-input
          v-model="codeText"
          type="textarea"
          :rows="22"
          spellcheck="false"
          class="code-input"
        />
        <el-alert
          v-if="codeError"
          :title="codeError"
          type="error"
          :closable="false"
          show-icon
        />
      </div>
      <template #footer>
        <el-button @click="codeOpen = false">取消</el-button>
        <el-button type="primary" @click="applyCodeConfig">应用</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.step-editor-list {
  display: flex;
  flex-direction: column;
  gap: 1px;
}

.workflow-group {
  border: 1px solid #e7edf5;
  border-radius: 14px;
  padding: 9px;
  background: #f7f9fc;
}

.workflow-group-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 2px 4px 8px;
  flex-wrap: wrap;
}

.group-title {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.group-title strong {
  font-size: 14px;
}

.group-actions {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.group-name-input {
  width: 150px;
}

.workflow-group-body {
  display: flex;
  flex-direction: column;
  gap: 1px;
}

.group-empty {
  padding: 8px 4px;
  font-size: 13px;
}

.step-group-select {
  width: 132px;
}

.empty {
  padding: 20px;
  border: 1px dashed #d8e0ea;
  border-radius: 10px;
  text-align: center;
}

.top-item {
  border-radius: 10px;
}

.imported-group {
  display: flex;
  flex-direction: column;
  gap: 1px;
}

.imported-card {
  border: 1px solid #e4e9f0;
  border-radius: 12px;
  padding: 14px;
  background: #fbfcfe;
}

.base-card {
  border-color: #d9e8fb;
  background: #f8fbff;
}

.child-card {
  margin-left: 22px;
  background: #fffdf7;
  border-color: #f0dfb8;
}

.current-card {
  margin-left: 22px;
  background: #f8fffb;
  border-color: #bfe8d2;
}

.new-card {
  border-color: #cfe9d8;
  background: #f9fffb;
  scroll-margin-top: 20px;
}

.imported-card.is-collapsed {
  padding-bottom: 14px;
}

.imported-card.is-collapsed .card-head {
  margin-bottom: 0;
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

.card-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 10px;
}

.step-heading {
  display: flex;
  align-items: center;
  gap: 7px;
  flex-wrap: wrap;
}

.step-order {
  color: #8a96a3;
  font-size: 12px;
}

.step-actions {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  justify-content: flex-end;
}

.code-config {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.code-hint {
  margin: 0;
  font-size: 12px;
  line-height: 1.6;
}

.code-input :deep(textarea) {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px;
  line-height: 1.55;
}

.step-editor-footer {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  padding-top: 4px;
}

.step-editor-footer span {
  font-size: 12px;
}

@media (max-width: 760px) {
  .card-head {
    flex-direction: column;
  }

  .step-actions {
    justify-content: flex-start;
  }

  .child-card,
  .current-card {
    margin-left: 10px;
  }
}
</style>
