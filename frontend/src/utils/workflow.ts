import type {
  Feature,
  ResolvedSignalView,
  SignalForm,
  SignalView,
  StepForm,
  StepView,
  TemplateDetail,
} from "@/types/workflow";
import type { ConfigObject } from "@/utils/schema";

let uidCounter = 0;

export const SYSTEM_RESIGN_SIGNAL: ResolvedSignalView = {
  signal_key: "sys_resign_pending",
  label: "断签",
  description: "站点存在未签到日期时为真；站点不支持补签时恒为假",
  include_texts: [],
  include_selectors: [],
  exclude_texts: [],
  exclude_selectors: [],
  match_mode: "any",
  enabled: true,
  source: "system",
};

export function withSystemSignals(signals: ResolvedSignalView[]): ResolvedSignalView[] {
  const result = new Map<string, ResolvedSignalView>();
  for (const signal of signals) result.set(signal.signal_key, signal);
  result.set(SYSTEM_RESIGN_SIGNAL.signal_key, SYSTEM_RESIGN_SIGNAL);
  return Array.from(result.values());
}

export function newStepUid(): string {
  uidCounter += 1;
  return `step-${uidCounter}`;
}

export function laneLabel(lane: string | null): string {
  if (lane === "general") return "普通通用";
  if (lane === "interaction") return "普通交互";
  if (lane === "proxy_general") return "代理通用";
  if (lane === "proxy_interaction") return "代理交互";
  return lane ?? "-";
}

export function categoryLabel(category: string): string {
  if (category === "flow") return "流程";
  if (category === "auth") return "认证";
  if (category === "challenge") return "验证";
  if (category === "sign") return "签到";
  if (category === "action") return "动作";
  if (category === "post") return "补签";
  return category;
}

export function featureSupportsLlm(feature: Feature | undefined | null): boolean {
  if (!feature) return false;
  if (feature.key === "flow.result") return true;
  if (feature.requires.includes("vision")) return true;
  return (feature.config_schema.properties?.captcha_method?.enum ?? []).includes("vision");
}

export function stepUsesLlm(
  feature: Feature | undefined | null,
  config: ConfigObject,
  inherited?: ConfigObject | null,
): boolean {
  if (!feature) return false;
  if (feature.key === "flow.result") {
    const effective = mergeConfig(inherited ?? {}, config);
    if (feature.execution_settings?.llm_enabled !== true || effective.llm_enabled !== true) return false;
    const outputValue = effective.output_value ??
      feature.defaults.output_value ?? feature.config_schema.properties?.output_value?.default ?? true;
    return feature.execution_settings.judgment_scope === "all" || outputValue === true;
  }
  if (feature.requires.includes("vision")) return true;
  const method =
    config.captcha_method ??
    inherited?.captcha_method ??
    feature.config_schema.properties?.captcha_method?.default;
  return method === "vision";
}

export function mergeConfig(base: ConfigObject, override: ConfigObject): ConfigObject {
  const result: ConfigObject = { ...base };
  for (const [key, value] of Object.entries(override)) {
    if (value === null) {
      delete result[key];
    } else if (
      value !== null &&
      typeof value === "object" &&
      !Array.isArray(value) &&
      result[key] !== null &&
      typeof result[key] === "object" &&
      !Array.isArray(result[key])
    ) {
      result[key] = mergeConfig(
        result[key] as ConfigObject,
        value as ConfigObject,
      );
    } else {
      result[key] = value;
    }
  }
  return result;
}

export function stepToForm(step: StepView): StepForm {
  return {
    uid: newStepUid(),
    step_key: step.step_key,
    feature_type: step.feature_type,
    label: step.label ?? "",
    group_key: step.group_key ?? "",
    position: step.position ?? undefined,
    enabled: step.enabled === null ? "inherit" : step.enabled ? "on" : "off",
    config: { ...step.config },
    condition: step.condition ?? "",
    on_error: step.on_error ?? "",
    timeout_seconds: step.timeout_seconds ?? undefined,
  };
}

export interface ChainStep {
  detail: TemplateDetail;
  step: StepView;
  source: string;
  form: StepForm;
  inherited: ConfigObject;
  cumulative: ConfigObject;
}

export interface TemplateStepGroup {
  key: string;
  feature_type: string;
  group_key: string;
  position: number;
  base: ChainStep;
  mods: ChainStep[];
  cumulative: ConfigObject;
  condition: string;
}

export function buildTemplateGroups(chain: TemplateDetail[]): TemplateStepGroup[] {
  if (!chain.length) return [];
  const rawByKey = new Map<string, ChainStep[]>();
  for (const detail of chain) {
    for (const step of detail.steps) {
      const list = rawByKey.get(step.step_key) ?? [];
      list.push({
        detail,
        step,
        source: detail.name,
        form: stepToForm(step),
        inherited: {},
        cumulative: {},
      });
      rawByKey.set(step.step_key, list);
    }
  }

  const final = chain[chain.length - 1];
  const orderedKeys = (final?.resolved?.steps ?? []).map((step) => step.step_key);
  const resolvedByKey = new Map(
    (final?.resolved?.steps ?? []).map((step) => [step.step_key, step]),
  );

  const groups: TemplateStepGroup[] = [];
  const seen = new Set<string>();
  const keys = [...orderedKeys, ...Array.from(rawByKey.keys())];
  for (const key of keys) {
    if (seen.has(key)) continue;
    seen.add(key);
    const list = rawByKey.get(key);
    if (!list?.length) continue;
    const base = list[0];
    const mods: ChainStep[] = [];
    let cumulative: ConfigObject = mergeConfig({}, base.form.config);
    base.cumulative = cumulative;
    for (const item of list.slice(1)) {
      item.inherited = cumulative;
      cumulative = mergeConfig(cumulative, item.form.config);
      item.cumulative = cumulative;
      mods.push(item);
    }
    const resolved = resolvedByKey.get(key);
    groups.push({
      key,
      feature_type: base.step.feature_type,
      group_key: resolved?.group_key ?? base.step.group_key ?? "",
      position: resolved?.position ?? base.step.position ?? (groups.length + 1) * 1000,
      base,
      mods,
      cumulative,
      condition: resolved?.condition ?? base.step.condition ?? "always",
    });
  }
  return groups;
}

export function inheritedConfigFor(
  chain: TemplateDetail[],
  stepKey: string,
  featureType: string,
): ConfigObject | null {
  const group = buildTemplateGroups(chain).find((item) => item.key === stepKey);
  if (!group || group.feature_type !== featureType) return null;
  return group.cumulative;
}

export type StepKeyNamespace = "temp" | "site";

const KEY_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789";

function randomSuffix(length = 6): string {
  const bytes = new Uint8Array(length);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (byte) => KEY_ALPHABET[byte % KEY_ALPHABET.length]).join("");
}

export function newSignalKey(used: Set<string>): string {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    const key = `sig_${randomSuffix()}`;
    if (!used.has(key)) return key;
  }
  return `sig_${Date.now().toString(36)}${randomSuffix(4)}`;
}

export function newGroupKey(namespace: StepKeyNamespace, used: Set<string>): string {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    const key = `${namespace === "temp" ? "tgrp" : "sgrp"}_${randomSuffix()}`;
    if (!used.has(key)) return key;
  }
  return `${namespace === "temp" ? "tgrp" : "sgrp"}_${Date.now().toString(36)}`;
}

export function newStepKey(namespace: StepKeyNamespace, used: Set<string>): string {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    const key = `${namespace}_${randomSuffix()}`;
    if (!used.has(key)) return key;
  }
  return `${namespace}_${Date.now().toString(36)}${randomSuffix(4)}`;
}

export function signalToForm(signal: SignalView): SignalForm {
  return {
    uid: newStepUid(),
    ...signal,
    include_texts: [...signal.include_texts],
    include_selectors: [...signal.include_selectors],
    exclude_texts: [...signal.exclude_texts],
    exclude_selectors: [...signal.exclude_selectors],
  };
}

export function mergeSignalOptions(
  chainSignals: ResolvedSignalView[],
  localSignals: SignalForm[],
): ResolvedSignalView[] {
  const result = new Map<string, ResolvedSignalView>();
  for (const signal of chainSignals) result.set(signal.signal_key, signal);
  for (const signal of localSignals) {
    result.set(signal.signal_key, {
      signal_key: signal.signal_key,
      label: signal.label,
      description: signal.description,
      include_texts: signal.include_texts,
      include_selectors: signal.include_selectors,
      exclude_texts: signal.exclude_texts,
      exclude_selectors: signal.exclude_selectors,
      match_mode: signal.match_mode,
      enabled: signal.enabled,
      source: "current",
    });
  }
  return Array.from(result.values());
}

export type ConditionExpr =
  | { kind: "always" }
  | { kind: "signal"; key: string; expected: boolean }
  | { kind: "all" | "any"; children: ConditionExpr[] };

const SIGNAL_KEY_PATTERN = /^[a-z][a-z0-9_.-]*$/;

function splitConditionBody(body: string): string[] | null {
  const parts: string[] = [];
  let depth = 0;
  let start = 0;
  for (let index = 0; index < body.length; index += 1) {
    const char = body[index];
    if (char === "(") depth += 1;
    else if (char === ")") {
      depth -= 1;
      if (depth < 0) return null;
    } else if (char === "," && depth === 0) {
      parts.push(body.slice(start, index).trim());
      start = index + 1;
    }
  }
  parts.push(body.slice(start).trim());
  return parts.every(Boolean) ? parts : null;
}

export function parseConditionExpr(
  condition: string | null | undefined,
): ConditionExpr | null {
  const text = (condition ?? "").trim();
  if (!text || text === "always") return { kind: "always" };
  for (const kind of ["all", "any"] as const) {
    const prefix = `${kind}(`;
    if (!text.startsWith(prefix)) continue;
    if (!text.endsWith(")")) return null;
    const body = text.slice(prefix.length, -1).trim();
    if (!body) return null;
    const parts = splitConditionBody(body);
    if (!parts) return null;
    const children: ConditionExpr[] = [];
    for (const part of parts) {
      const parsed = parseConditionExpr(part);
      if (!parsed) return null;
      children.push(parsed);
    }
    return { kind, children };
  }
  if (!text.startsWith("signal:")) return null;
  const body = text.slice("signal:".length);
  const split = body.lastIndexOf(":");
  if (split <= 0) return null;
  const key = body.slice(0, split);
  const expected = body.slice(split + 1);
  if (!SIGNAL_KEY_PATTERN.test(key) || !["true", "false"].includes(expected)) {
    return null;
  }
  return { kind: "signal", key, expected: expected === "true" };
}

export function collectConditionLeaves(
  expr: ConditionExpr | null,
): { key: string; expected: boolean }[] {
  if (!expr) return [];
  if (expr.kind === "signal") return [{ key: expr.key, expected: expr.expected }];
  if (expr.kind === "always") return [];
  return expr.children.flatMap((child) => collectConditionLeaves(child));
}

export function collectConditionKeys(expr: ConditionExpr | null): string[] {
  if (!expr) return [];
  if (expr.kind === "signal") return [expr.key];
  if (expr.kind === "always") return [];
  return expr.children.flatMap((child) => collectConditionKeys(child));
}

export function usedSignalKeys(steps: StepForm[]): Set<string> {
  const used = new Set<string>();
  for (const step of steps) {
    for (const key of collectConditionKeys(parseConditionExpr(step.condition))) {
      used.add(key);
    }
  }
  return used;
}
