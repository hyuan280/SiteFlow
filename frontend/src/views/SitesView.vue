<script setup lang="ts">
import { computed, nextTick, onMounted, reactive, ref } from "vue";
import { onBeforeRouteLeave, useRouter } from "vue-router";
import { ElMessage, ElMessageBox } from "element-plus";

import { ApiError, apiRequest } from "@/api/client";
import CopyConfigDialog from "@/components/CopyConfigDialog.vue";
import ExportSelectionDialog from "@/components/ExportSelectionDialog.vue";
import FloatingField from "@/components/FloatingField.vue";
import SignalEditor from "@/components/SignalEditor.vue";
import StringListField from "@/components/StringListField.vue";
import WorkflowStepEditor from "@/components/WorkflowStepEditor.vue";
import { useAuthStore } from "@/stores/auth";
import { formatServerDateTime } from "@/utils/datetime";
import { usePersistedSearch } from "@/utils/persistedSearch";
import type { ExportItem } from "@/types/export";
import type {
  Feature,
  GroupForm,
  GroupView,
  ResolvedSignalView,
  ResolvedWorkflow,
  RunDetail,
  SignalForm,
  SiteCredential,
  SiteDetail,
  SiteSummary,
  StepForm,
  TemplateDetail,
  TemplateSummary,
} from "@/types/workflow";
import {
  importArray,
  importText,
  parseExportFile,
  type ImportRecord,
} from "@/utils/exportFile";
import { missingRequiredFields } from "@/utils/schema";
import {
  inheritedConfigFor,
  laneLabel,
  mergeSignalOptions,
  collectConditionKeys,
  parseConditionExpr,
  signalToForm,
  stepToForm,
  stepUsesLlm,
  usedSignalKeys,
  withSystemSignals,
} from "@/utils/workflow";

interface EditorForm {
  name: string;
  base_url: string;
  template_id: number | undefined;
  enabled: boolean;
  prefer_interaction: boolean;
  use_proxy: boolean;
  support_resign: boolean;
  resign_max_days: number;
  bitwarden_enabled: boolean;
  credential_provider_id: string | null;
  bitwarden_match_mode: "domain" | "root_domain" | "custom";
  bitwarden_custom_match: string;
  checkin_after: string;
  timezone: string;
  tagsText: string;
  trusted_urls: string[];
  notes: string;
  steps: StepForm[];
  groups: GroupForm[];
  signals: SignalForm[];
}

const auth = useAuthStore();
const router = useRouter();
const sites = ref<SiteSummary[]>([]);
const siteSearch = usePersistedSearch("siteflow:sites:search");
const templates = ref<TemplateSummary[]>([]);
const features = ref<Feature[]>([]);
const cfboxEnabled = ref(false);
const bitwardenAvailable = ref(false);
const loading = ref(true);
const error = ref("");
const editorOpen = ref(false);
const editingId = ref<number | null>(null);
const saving = ref(false);
const editorError = ref("");
const formBaseline = ref("");
const credentialBaseline = ref("");
const loadingForm = ref(false);
const interactionStarted = ref(false);
const form = reactive<EditorForm>(emptyForm());
const chain = ref<TemplateDetail[]>([]);
const editorRef = ref<{
  addStep: () => void;
  expandAll: () => void;
  collapseAll: () => void;
} | null>(null);
const credential = ref<SiteCredential>(emptyCredential());
const credentialUsername = ref("");
const credentialPassword = ref("");
const credentialSaving = ref(false);
const credentialError = ref("");
const cookieInput = ref("");
const cookieSaving = ref(false);
const cookieError = ref("");
const displayTimezone = ref("Asia/Shanghai");
const pluginCredentialProviders = ref<Array<{ id: string; name: string; capability: string }>>([]);
const credentialTab = ref<string>("account");
const externalProviderId = computed(() => credentialTab.value === "bitwarden" ? "builtin.bitwarden" : credentialTab.value);
const externalProviderName = computed(() => credentialTabs.value.find(tab => tab.key === credentialTab.value)?.label ?? credentialTab.value);
const externalProviderEnabled = computed(() => externalProviderId.value === "builtin.bitwarden" ? form.bitwarden_enabled : form.credential_provider_id === externalProviderId.value);
const externalProviderAvailable = computed(() => credentialTab.value === "bitwarden" ? bitwardenAvailable.value : pluginCredentialProviders.value.some(p => p.id === credentialTab.value));
function setExternalProvider(enabled: boolean | string | number): void {
  form.credential_provider_id = enabled ? externalProviderId.value : "builtin.manual";
  form.bitwarden_enabled = Boolean(enabled) && externalProviderId.value === "builtin.bitwarden";
}
const credentialTabs = computed(() => {
  const tabs: Array<{ key: string; label: string }> = [
    { key: "account", label: "账户" },
    { key: "cookie", label: "Cookie" },
  ];
  if (bitwardenAvailable.value || form.bitwarden_enabled) {
    tabs.push({ key: "bitwarden", label: "Bitwarden" });
  }
  for (const provider of pluginCredentialProviders.value) tabs.push({ key: provider.id, label: provider.name });
  if (form.credential_provider_id?.startsWith("plugin.") && !tabs.some(tab => tab.key === form.credential_provider_id)) {
    tabs.push({ key: form.credential_provider_id, label: `${form.credential_provider_id.slice(7)}（不可用）` });
  }
  return tabs;
});
const bitwardenHost = computed(() => {
  try {
    return new URL(form.base_url).hostname;
  } catch {
    return "";
  }
});
const runLoadingId = ref<number | null>(null);
const previewOpen = ref(false);
const previewLoading = ref(false);
const preview = ref<ResolvedWorkflow | null>(null);
const previewError = ref("");
const previewTitle = ref("");
const exportOpen = ref(false);
const copyOpen = ref(false);
const copying = ref(false);
const siteImportInput = ref<HTMLInputElement | null>(null);
const siteImporting = ref(false);

const isEditing = computed(() => editingId.value !== null);
const title = computed(() => (isEditing.value ? "编辑站点" : "新建站点"));
const templateMap = computed(() => new Map(templates.value.map((item) => [item.id, item])));
const chainSignals = computed<ResolvedSignalView[]>(
  () => chain.value[chain.value.length - 1]?.resolved_signals ?? [],
);
const inheritedGroups = computed(
  () => chain.value[chain.value.length - 1]?.resolved?.groups ?? [],
);
const signalOptions = computed(() => withSystemSignals(chainSignals.value));
const availableSignals = computed(() =>
  mergeSignalOptions(signalOptions.value, form.signals),
);
const usedSignalKeysValue = computed(() => Array.from(usedSignalKeys(form.steps)));
const copySiteItems = computed(() =>
  sites.value.map((item) => ({
    id: item.id,
    label: item.name,
    description: [
      item.base_url,
      item.template_name ?? "无模板",
      `${item.step_count} 个步骤`,
    ].join(" · "),
  })),
);
const exportItems = computed<ExportItem[]>(() =>
  sites.value.map((item) => ({
    id: item.id,
    label: item.name,
    description: [
      item.base_url,
      item.template_name ?? "无模板",
      `${item.step_count} 个步骤`,
      item.enabled ? "" : "未启用",
      item.valid ? "" : "配置无效",
    ]
      .filter(Boolean)
      .join(" · "),
  })),
);
const filteredSites = computed(() => {
  const query = siteSearch.value.trim().toLowerCase();
  if (!query) return sites.value;
  return sites.value.filter((site) => {
    let host = "";
    try {
      host = new URL(site.base_url).hostname.toLowerCase();
    } catch {
      host = site.base_url.toLowerCase();
    }
    return (
      site.name.toLowerCase().includes(query) ||
      host.includes(query) ||
      site.base_url.toLowerCase().includes(query)
    );
  });
});
const hasCredentialInputs = computed(
  () => Boolean(credentialUsername.value.trim() || credentialPassword.value),
);
const formDirty = computed(
  () => Boolean(formBaseline.value) && JSON.stringify(form) !== formBaseline.value,
);
const credentialDirty = computed(
  () =>
    Boolean(cookieInput.value.trim()) ||
    (Boolean(credentialBaseline.value) &&
      JSON.stringify([credentialUsername.value, credentialPassword.value]) !==
        credentialBaseline.value),
);
const dirty = computed(
  () => interactionStarted.value && (formDirty.value || credentialDirty.value),
);

async function closeEditor(): Promise<void> {
  if (dirty.value) {
    try {
      await ElMessageBox.confirm(
        "有修改未保存，直接离开页面将会丢弃刚刚的修改。",
        "未保存的修改",
        {
          confirmButtonText: "直接离开",
          cancelButtonText: "继续编辑",
          type: "warning",
        },
      );
    } catch {
      return;
    }
  }
  editorOpen.value = false;
}

onBeforeRouteLeave(async () => {
  if (!editorOpen.value || !dirty.value) return true;
  try {
    await ElMessageBox.confirm(
      "有修改未保存，直接离开页面将会丢弃刚刚的修改。",
      "未保存的修改",
      {
        confirmButtonText: "直接离开",
        cancelButtonText: "继续编辑",
        type: "warning",
      },
    );
    return true;
  } catch {
    return false;
  }
});

function emptyCredential(): SiteCredential {
  return {
    configured: false,
    username_masked: null,
    cookies_configured: false,
    cookie_count: 0,
    updated_at: null,
    cookies_updated_at: null,
    last_verified_at: null,
    last_error: null,
    error: null,
  };
}

function emptyForm(): EditorForm {
  return {
    name: "",
    base_url: "",
    template_id: undefined,
    enabled: true,
    prefer_interaction: false,
    use_proxy: false,
    support_resign: true,
    resign_max_days: 30,
    bitwarden_enabled: false,
    credential_provider_id: null,
    bitwarden_match_mode: "domain",
    bitwarden_custom_match: "",
    checkin_after: "",
    timezone: "Asia/Shanghai",
    tagsText: "",
    trusted_urls: [],
    notes: "",
    steps: [],
    groups: [],
    signals: [],
  };
}

function groupToForm(group: GroupView): GroupForm {
  return { uid: `group-${group.group_key}`, ...group };
}

function selectedTemplateId(): number | null {
  return typeof form.template_id === "number" ? form.template_id : null;
}

function templateName(id: number | null): string {
  if (id === null) return "-";
  return templateMap.value.get(id)?.name ?? `#${id}`;
}

async function handleUnauthorized(cause: unknown): Promise<boolean> {
  if (cause instanceof ApiError && cause.status === 401) {
    auth.clear();
    await router.replace({ name: "login" });
    return true;
  }
  return false;
}

function trustedUrls(value: unknown): string[] {
  if (value === undefined) return [];
  if (!Array.isArray(value) || value.some((pattern) => typeof pattern !== "string")) {
    throw new Error("trusted_urls 必须是字符串数组");
  }
  if (value.length > 20) throw new Error("可信 URL 正则最多 20 条");
  if (value.some((pattern) => !pattern.trim() || [...pattern].length > 1024)) {
    throw new Error("可信 URL 正则不能为空，且每条最多 1024 字符");
  }
  // Preserve patterns verbatim; Python regex validation belongs to the backend.
  return [...value];
}

function importBitwardenMode(value: unknown): "domain" | "root_domain" | "custom" {
  const mode = importText(value);
  return mode === "root_domain" || mode === "custom" ? mode : "domain";
}

function exportStep(step: SiteDetail["steps"][number]) {
  return {
    step_key: step.step_key,
    feature_type: step.feature_type,
    label: step.label,
    group_key: step.group_key,
    position: step.position,
    enabled: step.enabled,
    config: step.config,
    condition: step.condition,
    on_error: step.on_error,
    timeout_seconds: step.timeout_seconds,
  };
}

function exportSignal(signal: SiteDetail["signals"][number]) {
  return {
    signal_key: signal.signal_key,
    label: signal.label,
    description: signal.description,
    include_texts: signal.include_texts,
    include_selectors: signal.include_selectors,
    exclude_texts: signal.exclude_texts,
    exclude_selectors: signal.exclude_selectors,
    match_mode: signal.match_mode,
    enabled: signal.enabled,
  };
}

async function buildSiteExport(items: ExportItem[]): Promise<unknown> {
  try {
    const details = await Promise.all(
      items.map((item) => apiRequest<SiteDetail>(`/sites/${item.id}`)),
    );
    return {
      format: "siteflow.export",
      version: 1,
      kind: "sites",
      exported_at: new Date().toISOString(),
      sites: details.map((detail) => ({
        name: detail.name,
        base_url: detail.base_url,
        enabled: detail.enabled,
        support_resign: detail.support_resign,
        resign_max_days: detail.resign_max_days,
        bitwarden_enabled: detail.bitwarden_enabled,
        credential_provider_id: detail.credential_provider_id ?? null,
        bitwarden_match_mode: detail.bitwarden_match_mode,
        bitwarden_custom_match: detail.bitwarden_custom_match,
        template_name: detail.template_name,
        lane_policy: detail.lane_policy,
        checkin_after: detail.checkin_after,
        timezone: detail.timezone,
        tags: detail.tags,
        trusted_urls: trustedUrls(detail.trusted_urls),
        notes: detail.notes,
        groups: detail.groups.map((group) => ({
          group_key: group.group_key,
          name: group.name,
          position: group.position,
        })),
        steps: detail.steps.map(exportStep),
        signals: detail.signals.map(exportSignal),
      })),
    };
  } catch (cause) {
    if (await handleUnauthorized(cause)) throw cause;
    throw cause;
  }
}

interface PendingSite {
  name: string;
  templateName: string | null;
  item: ImportRecord;
}

function openSiteImport(): void {
  siteImportInput.value?.click();
}

async function importSitesFromFile(file: File): Promise<void> {
  const rows = parseExportFile(await file.text(), "sites", "站点");
  const [currentSites, currentTemplates] = await Promise.all([
    apiRequest<SiteSummary[]>("/sites"),
    apiRequest<TemplateSummary[]>("/templates"),
  ]);
  const existing = new Set(currentSites.map((item) => item.name));
  const templateMap = new Map(currentTemplates.map((item) => [item.name, item.id]));
  const seen = new Set<string>();
  const parsed: PendingSite[] = rows.map((item, index) => {
    const name = importText(item.name);
    if (!name) throw new Error(`第 ${index + 1} 个站点缺少名称`);
    if (seen.has(name)) throw new Error(`导入文件中站点名称重复：${name}`);
    seen.add(name);
    trustedUrls(item.trusted_urls);
    const rawTemplate = item.template_name;
    const templateName =
      rawTemplate === null || rawTemplate === undefined
        ? null
        : importText(rawTemplate) || null;
    return { name, templateName, item };
  });

  const skipped = parsed.filter((entry) => existing.has(entry.name)).map((entry) => entry.name);
  const pending = parsed.filter((entry) => !existing.has(entry.name));
  if (!pending.length) {
    ElMessage.info("所有站点均已存在，未导入任何站点");
    return;
  }
  for (const entry of pending) {
    if (!importText(entry.item.base_url)) {
      throw new Error(`站点 ${entry.name} 缺少站点地址`);
    }
    if (entry.templateName && !templateMap.has(entry.templateName)) {
      throw new Error(`站点 ${entry.name} 引用的模板不存在：${entry.templateName}`);
    }
  }

  const skippedText = skipped.length
    ? `；将跳过 ${skipped.length} 个同名站点（${skipped.slice(0, 5).join("、")}${
        skipped.length > 5 ? "…" : ""
      }）`
    : "";
  try {
    await ElMessageBox.confirm(
      `确认导入 ${pending.length} 个站点${skippedText}？`,
      "导入站点",
      {
        confirmButtonText: "导入",
        cancelButtonText: "取消",
        type: skipped.length ? "warning" : "info",
      },
    );
  } catch {
    return;
  }

  const created: number[] = [];
  try {
    for (const entry of pending) {
      const detail = await apiRequest<SiteDetail>("/sites", {
        method: "POST",
        body: JSON.stringify({
          name: entry.name,
          base_url: importText(entry.item.base_url),
          enabled: entry.item.enabled !== false,
          support_resign: entry.item.support_resign !== false,
          resign_max_days: Number(entry.item.resign_max_days) || 30,
          bitwarden_enabled: entry.item.bitwarden_enabled === true,
          credential_provider_id: importText(entry.item.credential_provider_id) || null,
          bitwarden_match_mode: importBitwardenMode(entry.item.bitwarden_match_mode),
          bitwarden_custom_match: importText(entry.item.bitwarden_custom_match) || null,
          template_id: entry.templateName
            ? templateMap.get(entry.templateName) ?? null
            : null,
          lane_policy: importText(entry.item.lane_policy) || "auto",
          checkin_after: importText(entry.item.checkin_after) || null,
          timezone: importText(entry.item.timezone) || "Asia/Shanghai",
          tags: importArray(entry.item.tags),
          trusted_urls: trustedUrls(entry.item.trusted_urls),
          notes: importText(entry.item.notes),
          groups: importArray(entry.item.groups),
          steps: importArray(entry.item.steps),
          signals: importArray(entry.item.signals),
        }),
      });
      created.push(detail.id);
    }
  } catch (cause) {
    for (const id of [...created].reverse()) {
      try {
        await apiRequest(`/sites/${id}`, { method: "DELETE" });
      } catch {
        // Ignore rollback failures and report the original import error.
      }
    }
    throw cause;
  }

  ElMessage.success(
    `已导入 ${pending.length} 个站点${skipped.length ? `，跳过 ${skipped.length} 个同名站点` : ""}`,
  );
  await load();
}

async function onSiteImportFile(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement;
  const file = input.files?.[0];
  if (!file) {
    input.value = "";
    return;
  }
  siteImporting.value = true;
  try {
    await importSitesFromFile(file);
  } catch (cause) {
    if (!(await handleUnauthorized(cause))) {
      ElMessage.error(cause instanceof Error ? cause.message : "导入站点失败");
    }
  } finally {
    siteImporting.value = false;
    input.value = "";
  }
}

async function load(): Promise<void> {
  loading.value = true;
  error.value = "";
  try {
    const [siteList, templateList, featureList, integrations, providers] = await Promise.all([
      apiRequest<SiteSummary[]>("/sites"),
      apiRequest<TemplateSummary[]>("/templates"),
      apiRequest<Feature[]>("/features"),
      apiRequest<{
        cfbox: { enabled: boolean };
        bitwarden: { enabled: boolean };
      }>("/integrations"),
      apiRequest<Array<{ id: string; name: string; capability: string }>>("/secret-providers"),
    ]);
    sites.value = siteList;
    templates.value = templateList;
    features.value = featureList;
    cfboxEnabled.value = integrations.cfbox.enabled;
    bitwardenAvailable.value = integrations.bitwarden.enabled;
    pluginCredentialProviders.value = providers.filter(p => p.capability === "secret.credentials" && p.id.startsWith("plugin."));
  } catch (cause) {
    if (await handleUnauthorized(cause)) return;
    error.value = cause instanceof Error ? cause.message : "加载站点失败";
  } finally {
    loading.value = false;
  }
}

async function loadChain(templateId: number | undefined): Promise<TemplateDetail[]> {
  const result: TemplateDetail[] = [];
  const seen = new Set<number>();
  let current = templateId;
  while (current && !seen.has(current)) {
    seen.add(current);
    const detail = await apiRequest<TemplateDetail>(`/templates/${current}`);
    result.unshift(detail);
    current = detail.parent_id ?? undefined;
  }
  return result;
}

async function loadTemplateChain(): Promise<void> {
  if (!form.template_id) {
    chain.value = [];
    return;
  }
  try {
    chain.value = await loadChain(form.template_id);
  } catch (cause) {
    if (await handleUnauthorized(cause)) return;
    chain.value = [];
  }
}

function resetEditor(): void {
  Object.assign(form, emptyForm());
  editorError.value = "";
  chain.value = [];
  credential.value = emptyCredential();
  credentialUsername.value = "";
  credentialPassword.value = "";
  credentialError.value = "";
  cookieInput.value = "";
  cookieError.value = "";
  credentialTab.value = "account";
}

function openCopySite(): void {
  copyOpen.value = true;
}

async function copyFromSite(id: number): Promise<void> {
  copying.value = true;
  editorError.value = "";
  try {
    const detail = await apiRequest<SiteDetail>(`/sites/${id}`);
    const name = form.name;
    const baseUrl = form.base_url;
    form.template_id = detail.template_id ?? undefined;
    form.enabled = detail.enabled;
    form.prefer_interaction = ["interaction", "proxy_interaction"].includes(
      detail.lane_policy,
    );
    form.use_proxy = ["proxy_general", "proxy_interaction"].includes(
      detail.lane_policy,
    );
    form.support_resign = detail.support_resign;
    form.resign_max_days = detail.resign_max_days;
    form.bitwarden_enabled = detail.bitwarden_enabled;
    form.credential_provider_id = detail.credential_provider_id ?? null;
    form.bitwarden_match_mode = detail.bitwarden_match_mode;
    form.bitwarden_custom_match = detail.bitwarden_custom_match ?? "";
    form.checkin_after = detail.checkin_after ?? "";
    form.timezone = detail.timezone;
    form.tagsText = detail.tags.join(", ");
    form.trusted_urls = trustedUrls(detail.trusted_urls);
    form.notes = detail.notes;
    form.steps = detail.steps.map(stepToForm);
    form.groups = detail.groups.map(groupToForm);
    form.signals = detail.signals.map(signalToForm);
    form.name = name;
    form.base_url = baseUrl;
    chain.value = form.template_id ? await loadChain(form.template_id) : [];
    interactionStarted.value = true;
    copyOpen.value = false;
    await nextTick();
    ElMessage.success(`已复制站点 ${detail.name} 的配置`);
  } catch (cause) {
    if (await handleUnauthorized(cause)) return;
    ElMessage.error(cause instanceof Error ? cause.message : "复制站点配置失败");
  } finally {
    copying.value = false;
  }
}

function openCreate(): void {
  loadingForm.value = true;
  interactionStarted.value = false;
  resetEditor();
  editingId.value = null;
  editorOpen.value = true;
  void nextTick(() => {
    formBaseline.value = JSON.stringify(form);
    credentialBaseline.value = JSON.stringify(["", ""]);
    interactionStarted.value = false;
    loadingForm.value = false;
  });
}

async function openEdit(id: number): Promise<void> {
  loadingForm.value = true;
  interactionStarted.value = false;
  resetEditor();
  editorOpen.value = true;
  editingId.value = id;
  try {
    const detail = await apiRequest<SiteDetail>(`/sites/${id}`);
    form.name = detail.name;
    form.base_url = detail.base_url;
    form.template_id = detail.template_id ?? undefined;
    form.enabled = detail.enabled;
    form.support_resign = detail.support_resign;
    form.resign_max_days = detail.resign_max_days;
    form.bitwarden_enabled = detail.bitwarden_enabled;
    form.credential_provider_id = detail.credential_provider_id ?? null;
    form.bitwarden_match_mode = detail.bitwarden_match_mode;
    form.bitwarden_custom_match = detail.bitwarden_custom_match ?? "";
    form.prefer_interaction = ["interaction", "proxy_interaction"].includes(
      detail.lane_policy,
    );
    form.use_proxy = ["proxy_general", "proxy_interaction"].includes(detail.lane_policy);
    form.checkin_after = detail.checkin_after ?? "";
    form.timezone = detail.timezone;
    form.tagsText = detail.tags.join(", ");
    form.trusted_urls = trustedUrls(detail.trusted_urls);
    form.notes = detail.notes;
    credential.value = detail.credential;
    cookieInput.value = "";
    cookieError.value = "";
    const loadedChain = form.template_id ? await loadChain(form.template_id) : [];
    chain.value = loadedChain;
    form.steps = detail.steps.map(stepToForm);
    form.groups = detail.groups.map(groupToForm);
    form.signals = detail.signals.map(signalToForm);
  } catch (cause) {
    if (await handleUnauthorized(cause)) return;
    editorError.value = cause instanceof Error ? cause.message : "加载站点失败";
  } finally {
    await nextTick();
    loadingForm.value = false;
    await nextTick();
    formBaseline.value = JSON.stringify(form);
    credentialBaseline.value = JSON.stringify(["", ""]);
    interactionStarted.value = false;
  }
}

async function changeTemplate(): Promise<void> {
  await loadTemplateChain();
}

function onTemplateChange(event: Event): void {
  const value = (event.target as HTMLSelectElement).value;
  form.template_id = value ? Number(value) : undefined;
  void changeTemplate();
}

function addStep(): void {
  editorRef.value?.addStep();
}

function expandAllSteps(): void {
  editorRef.value?.expandAll();
}

function collapseAllSteps(): void {
  editorRef.value?.collapseAll();
}

function validateCredentialInputs(): void {
  if (!hasCredentialInputs.value) return;
  if (!credential.value.configured && (!credentialUsername.value.trim() || !credentialPassword.value)) {
    throw new Error("首次配置登录凭据需要同时填写用户名和密码");
  }
}

function validate(): void {
  if (!form.name.trim()) throw new Error("请填写站点名称");
  if (!form.base_url.trim()) throw new Error("请填写站点地址");
  trustedUrls(form.trusted_urls);
  if (form.checkin_after.trim() && !/^([01]\d|2[0-3]):[0-5]\d$/.test(form.checkin_after.trim())) {
    throw new Error("签到时间格式应为 HH:MM");
  }
  validateCredentialInputs();
  if (
    (form.bitwarden_enabled || form.credential_provider_id?.startsWith("plugin.")) &&
    form.bitwarden_match_mode === "custom" &&
    !form.bitwarden_custom_match.trim()
  ) {
    throw new Error("凭据提供商自定义匹配方式需要填写匹配值");
  }
  if (!form.template_id && !form.steps.length) {
    throw new Error("未选择模板时至少需要一个功能步骤");
  }
  const signalKeys = new Set<string>();
  for (const signal of form.signals) {
    if (signalKeys.has(signal.signal_key)) throw new Error(`信号 key 重复: ${signal.signal_key}`);
    signalKeys.add(signal.signal_key);
    if (!signal.label.trim()) throw new Error("信号名称不能为空");
    if (!signal.include_texts.length && !signal.include_selectors.length) {
      throw new Error(`信号 ${signal.label || signal.signal_key} 至少需要一个包含文本或选择器`);
    }
  }
  const available = new Set(availableSignals.value.map((signal) => signal.signal_key));
  const used = new Set<string>();
  for (const step of form.steps) {
    if (used.has(step.step_key)) throw new Error(`步骤 key 重复: ${step.step_key}`);
    used.add(step.step_key);
    const feature = features.value.find((item) => item.key === step.feature_type);
    if (!feature) throw new Error(`步骤 ${step.step_key} 的功能类型无效`);
    const parsed = parseConditionExpr(step.condition);
    if (step.condition && step.condition !== "always" && !parsed) {
      throw new Error(`步骤 ${step.step_key} 的执行条件无效`);
    }
    for (const key of collectConditionKeys(parsed)) {
      if (!available.has(key)) {
        throw new Error(`步骤 ${step.step_key} 引用了不存在的信号 ${key}`);
      }
    }
    const inherited = inheritedConfigFor(chain.value, step.step_key, step.feature_type);
    const missing = missingRequiredFields(feature.config_schema, step.config, inherited);
    if (missing.length) {
      throw new Error(`步骤 ${step.step_key} 缺少必填字段：${missing.join("、")}`);
    }
  }
}

function lanePolicy(): string {
  if (form.use_proxy) {
    return form.prefer_interaction ? "proxy_interaction" : "proxy_general";
  }
  return form.prefer_interaction ? "interaction" : "general";
}

function buildPayload(): Record<string, unknown> {
  return {
    name: form.name.trim(),
    base_url: form.base_url.trim(),
    enabled: form.enabled,
    template_id: selectedTemplateId(),
    lane_policy: lanePolicy(),
    support_resign: form.support_resign,
    resign_max_days: form.resign_max_days,
    bitwarden_enabled: form.bitwarden_enabled,
    credential_provider_id: form.credential_provider_id,
    bitwarden_match_mode: form.bitwarden_match_mode,
    bitwarden_custom_match: form.bitwarden_custom_match.trim() || null,
    checkin_after: form.checkin_after.trim() || null,
    timezone: form.timezone.trim() || "Asia/Shanghai",
    tags: form.tagsText
      .split(",")
      .map((item) => item.trim())
      .filter(Boolean),
    trusted_urls: trustedUrls(form.trusted_urls),
    notes: form.notes,
    groups: form.groups.map((group) => ({
      group_key: group.group_key,
      name: group.name.trim(),
      position: group.position,
      step_anchor: group.step_anchor,
    })),
    signals: form.signals.map((signal) => ({
      signal_key: signal.signal_key,
      label: signal.label.trim(),
      description: signal.description,
      include_texts: signal.include_texts,
      include_selectors: signal.include_selectors,
      exclude_texts: signal.exclude_texts,
      exclude_selectors: signal.exclude_selectors,
      match_mode: signal.match_mode,
      enabled: signal.enabled,
    })),
    steps: form.steps.map((step) => ({
      step_key: step.step_key,
      feature_type: step.feature_type,
      label: step.label.trim() || null,
      group_key: step.group_key || null,
      position: step.position ?? null,
      enabled: step.enabled === "inherit" ? null : step.enabled === "on",
      config: step.config,
      condition: step.condition || null,
      on_error: step.on_error || null,
      timeout_seconds: step.timeout_seconds ?? null,
    })),
  };
}

function credentialPayload(): Record<string, string> {
  const payload: Record<string, string> = {};
  if (credentialUsername.value.trim()) payload.username = credentialUsername.value.trim();
  if (credentialPassword.value) payload.password = credentialPassword.value;
  return payload;
}

async function saveCredentialFor(siteId: number, notify = true): Promise<boolean> {
  if (!hasCredentialInputs.value) return true;
  credentialSaving.value = true;
  credentialError.value = "";
  try {
    credential.value = await apiRequest<SiteCredential>(`/sites/${siteId}/credential`, {
      method: "PUT",
      body: JSON.stringify(credentialPayload()),
    });
    credentialUsername.value = "";
    credentialPassword.value = "";
    credentialBaseline.value = JSON.stringify(["", ""]);
    if (notify) ElMessage.success("登录凭据已保存");
    return true;
  } catch (cause) {
    if (await handleUnauthorized(cause)) return false;
    credentialError.value = cause instanceof Error ? cause.message : "保存登录凭据失败";
    return false;
  } finally {
    credentialSaving.value = false;
  }
}

async function saveCookiesFor(siteId: number, notify = true): Promise<boolean> {
  if (!cookieInput.value.trim()) return true;
  cookieSaving.value = true;
  cookieError.value = "";
  try {
    credential.value = await apiRequest<SiteCredential>(`/sites/${siteId}/cookies`, {
      method: "PUT",
      body: JSON.stringify({ cookies: cookieInput.value }),
    });
    cookieInput.value = "";
    if (notify) ElMessage.success(`已保存 ${credential.value.cookie_count} 条 Cookie`);
    return true;
  } catch (cause) {
    if (await handleUnauthorized(cause)) return false;
    cookieError.value = cause instanceof Error ? cause.message : "保存 Cookie 失败";
    return false;
  } finally {
    cookieSaving.value = false;
  }
}

async function saveAccount(): Promise<void> {
  if (editingId.value === null) {
    ElMessage.warning("站点保存后再单独保存凭据");
    return;
  }
  if (!hasCredentialInputs.value) {
    ElMessage.warning("请输入用户名或密码");
    return;
  }
  if (await saveCredentialFor(editingId.value, false)) {
    ElMessage.success("账户凭据已保存");
    await load();
  }
}

async function saveCookieSettings(): Promise<void> {
  if (editingId.value === null) {
    ElMessage.warning("站点保存后再单独保存 Cookie");
    return;
  }
  if (!cookieInput.value.trim()) {
    ElMessage.warning("请粘贴 Cookie");
    return;
  }
  if (await saveCookiesFor(editingId.value, false)) {
    ElMessage.success(`已保存 ${credential.value.cookie_count} 条 Cookie`);
    await load();
  }
}

async function removeCredential(): Promise<void> {
  if (editingId.value === null) return;
  try {
    await ElMessageBox.confirm("确认移除该站点的用户名和密码？Cookie 会保留。", "移除凭据", {
      confirmButtonText: "移除",
      cancelButtonText: "取消",
      type: "warning",
    });
  } catch {
    return;
  }
  try {
    await apiRequest(`/sites/${editingId.value}/credential`, { method: "DELETE" });
    credential.value = await apiRequest<SiteCredential>(
      `/sites/${editingId.value}/credential`,
    );
    credentialUsername.value = "";
    credentialPassword.value = "";
    credentialError.value = "";
    credentialBaseline.value = JSON.stringify(["", ""]);
    ElMessage.success("登录凭据已移除");
    await load();
  } catch (cause) {
    if (await handleUnauthorized(cause)) return;
    credentialError.value = cause instanceof Error ? cause.message : "移除登录凭据失败";
  }
}

async function removeCookies(): Promise<void> {
  if (editingId.value === null) return;
  try {
    await ElMessageBox.confirm("确认清除该站点已导入的 Cookie？", "清除 Cookie", {
      confirmButtonText: "清除",
      cancelButtonText: "取消",
      type: "warning",
    });
  } catch {
    return;
  }
  cookieSaving.value = true;
  cookieError.value = "";
  try {
    await apiRequest(`/sites/${editingId.value}/cookies`, { method: "DELETE" });
    credential.value = await apiRequest<SiteCredential>(
      `/sites/${editingId.value}/credential`,
    );
    ElMessage.success("Cookie 已清除");
    await load();
  } catch (cause) {
    if (await handleUnauthorized(cause)) return;
    cookieError.value = cause instanceof Error ? cause.message : "清除 Cookie 失败";
  } finally {
    cookieSaving.value = false;
  }
}

function siteLoginConfigured(site: SiteSummary): boolean {
  return site.credential.configured || site.bitwarden_enabled || Boolean(site.credential_provider_id && site.credential_provider_id !== "builtin.manual");
}

function bitwardenModeLabel(mode: "domain" | "root_domain" | "custom"): string {
  if (mode === "root_domain") return "主域名匹配";
  if (mode === "custom") return "自定义匹配";
  return "完整域名匹配";
}

function formatDate(value: string | null | undefined): string {
  return formatServerDateTime(value, displayTimezone.value);
}

async function loadTimezone(): Promise<void> {
  try {
    const settings = await apiRequest<{ timezone: string }>("/scheduler-settings");
    displayTimezone.value = settings.timezone || "Asia/Shanghai";
  } catch {
    // Keep the default display timezone.
  }
}

async function save(closeAfter = true, successMessage = "站点已保存"): Promise<void> {
  editorError.value = "";
  try {
    validate();
  } catch (cause) {
    editorError.value = cause instanceof Error ? cause.message : "表单校验失败";
    return;
  }
  saving.value = true;
  try {
    const url = editingId.value === null ? "/sites" : `/sites/${editingId.value}`;
    const saved = await apiRequest<SiteDetail>(url, {
      method: editingId.value === null ? "POST" : "PUT",
      body: JSON.stringify(buildPayload()),
    });
    editingId.value = saved.id;
    credential.value = saved.credential;
    if (hasCredentialInputs.value) {
      const credentialSaved = await saveCredentialFor(saved.id, false);
      if (!credentialSaved) {
        await load();
        editorError.value = `站点已保存，但登录凭据未保存：${credentialError.value}`;
        return;
      }
    }
    if (cookieInput.value.trim()) {
      const cookieSaved = await saveCookiesFor(saved.id, false);
      if (!cookieSaved) {
        await load();
        editorError.value = `站点已保存，但 Cookie 未保存：${cookieError.value}`;
        return;
      }
    }
    formBaseline.value = JSON.stringify(form);
    credentialBaseline.value = JSON.stringify(["", ""]);
    ElMessage.success(successMessage);
    if (closeAfter) editorOpen.value = false;
    await load();
  } catch (cause) {
    if (await handleUnauthorized(cause)) return;
    editorError.value = cause instanceof Error ? cause.message : "保存站点失败";
  } finally {
    saving.value = false;
  }
}

async function saveBitwardenSettings(): Promise<void> {
  if (editingId.value === null) {
    ElMessage.warning("站点保存后再单独保存设置");
    return;
  }
  await save(false, "凭据提供商设置已保存");
}

async function runSite(row: SiteSummary): Promise<void> {
  if (!row.enabled || !row.valid) return;
  runLoadingId.value = row.id;
  try {
    const run = await apiRequest<RunDetail>("/runs", {
      method: "POST",
      body: JSON.stringify({ site_ids: [row.id] }),
    });
    ElMessage.success(`已创建运行 #${run.id}`);
    await router.push({ name: "runs", query: { run: String(run.id) } });
  } catch (cause) {
    if (await handleUnauthorized(cause)) return;
    ElMessage.error(cause instanceof Error ? cause.message : "启动运行失败");
  } finally {
    runLoadingId.value = null;
  }
}

async function remove(row: SiteSummary): Promise<void> {
  try {
    await ElMessageBox.confirm(`确认删除站点 ${row.name}？`, "删除站点", {
      confirmButtonText: "删除",
      cancelButtonText: "取消",
      type: "warning",
    });
  } catch {
    return;
  }
  try {
    await apiRequest(`/sites/${row.id}`, { method: "DELETE" });
    ElMessage.success("站点已删除");
    await load();
  } catch (cause) {
    if (await handleUnauthorized(cause)) return;
    ElMessage.error(cause instanceof Error ? cause.message : "删除站点失败");
  }
}

async function showPreview(row: SiteSummary): Promise<void> {
  previewOpen.value = true;
  previewLoading.value = true;
  previewError.value = "";
  preview.value = null;
  previewTitle.value = `${row.name} · 解析预览`;
  try {
    const detail = await apiRequest<SiteDetail>(`/sites/${row.id}`);
    preview.value = detail.resolved;
    if (!detail.resolved) previewError.value = detail.validation_error ?? "站点无法解析";
  } catch (cause) {
    if (await handleUnauthorized(cause)) return;
    previewError.value = cause instanceof Error ? cause.message : "解析失败";
  } finally {
    previewLoading.value = false;
  }
}

onMounted(() => {
  void loadTimezone();
  void load();
});
</script>

<template>
  <section v-if="!editorOpen">
    <div class="page-head">
      <div>
        <p class="eyebrow">SITES</p>
        <h1>站点管理</h1>
        <p class="muted">站点引用模板，并按 step_key 覆盖、禁用或追加功能步骤。</p>
      </div>
      <div class="head-actions">
        <input
          ref="siteImportInput"
          type="file"
          accept=".json,application/json"
          class="hidden-file"
          @change="onSiteImportFile"
        />
        <el-button :loading="siteImporting" @click="openSiteImport">导入站点</el-button>
        <el-button @click="exportOpen = true">导出站点</el-button>
        <el-button type="primary" @click="openCreate">新建站点</el-button>
      </div>
    </div>

    <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon class="notice" />

    <el-card shadow="never" class="panel">
      <div class="list-toolbar">
        <el-input
          v-model="siteSearch"
          clearable
          placeholder="搜索站点名称或域名"
          class="list-search"
        />
      </div>
      <el-table :data="filteredSites" v-loading="loading" row-key="id">
        <el-table-column prop="name" label="名称" min-width="150" />
        <el-table-column label="地址" min-width="220" show-overflow-tooltip>
          <template #default="{ row }">
            <a
              class="site-link"
              :href="row.base_url"
              target="_blank"
              rel="noopener noreferrer"
            >
              {{ row.base_url }}
            </a>
          </template>
        </el-table-column>
        <el-table-column label="模板" min-width="150">
          <template #default="{ row }">{{ row.template_name ?? "-" }}</template>
        </el-table-column>
        <el-table-column label="车道" width="110">
          <template #default="{ row }">
            <el-tag size="small" effect="plain">{{ laneLabel(row.lane) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="步骤" width="70" prop="step_count" />
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tag :type="row.valid ? 'success' : 'danger'" size="small" effect="plain">
              {{ row.valid ? "有效" : "无效" }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="凭据" width="90">
          <template #default="{ row }">
            <el-tag
              :type="siteLoginConfigured(row) ? 'success' : 'info'"
              size="small"
              effect="plain"
            >
              {{ siteLoginConfigured(row) ? "已配置" : "未配置" }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="启用" width="80">
          <template #default="{ row }">
            <el-tag :type="row.enabled ? 'success' : 'info'" size="small" effect="plain">
              {{ row.enabled ? "是" : "否" }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="250" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" @click="openEdit(row.id)">编辑</el-button>
            <el-button link @click="showPreview(row)">预览</el-button>
            <el-button
              link
              type="success"
              :loading="runLoadingId === row.id"
              :disabled="!row.enabled || !row.valid"
              @click="runSite(row)"
            >
              执行
            </el-button>
            <el-button link type="danger" @click="remove(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <ExportSelectionDialog
      v-model="exportOpen"
      title="导出站点配置"
      file-name-prefix="siteflow-sites"
      :items="exportItems"
      :build-payload="buildSiteExport"
      hint="导出所选站点的分组、步骤、信号和站点字段为 JSON；登录凭据不会导出。"
    />
  </section>

  <section
    v-else
    class="editor-page"
    @pointerdown="interactionStarted = true"
    @keydown="interactionStarted = true"
  >
    <div class="page-head">
      <div>
        <el-button link @click="closeEditor">← 返回站点列表</el-button>
        <h1>{{ title }}</h1>
        <p class="muted">模板步骤固定；修改会生成子步骤卡片，新增步骤使用上下移动调整顺序。</p>
      </div>
      <div class="head-actions">
        <el-button @click="closeEditor">取消</el-button>
        <el-button type="primary" :loading="saving" @click="() => save()">保存站点</el-button>
      </div>
    </div>

    <el-alert
      v-if="editorError"
      :title="editorError"
      type="error"
      :closable="false"
      show-icon
      class="notice"
    />

    <el-card shadow="never" class="panel">
      <template #header>
        <div class="card-head">
          <div class="title-row">
            <strong>站点配置</strong>
            <span class="muted">基础信息、模板和运行设置</span>
            <el-button
              v-if="!isEditing"
              size="small"
              plain
              :disabled="!sites.length"
              @click="openCopySite"
            >
              复制站点配置
            </el-button>
          </div>
        </div>
      </template>
      <div class="site-meta">
        <el-row :gutter="14">
          <el-col :xs="24" :md="8">
            <FloatingField label="站点名称" required :has-value="Boolean(form.name)">
              <input
                :value="form.name"
                maxlength="128"
                @input="(event) => (form.name = (event.target as HTMLInputElement).value)"
              />
            </FloatingField>
          </el-col>
          <el-col :xs="24" :md="10">
            <FloatingField label="站点地址" required :has-value="Boolean(form.base_url)">
              <input
                :value="form.base_url"
                placeholder="https://pt.example.com"
                @input="(event) => (form.base_url = (event.target as HTMLInputElement).value)"
              />
            </FloatingField>
          </el-col>
          <el-col :xs="24" :md="6">
            <FloatingField label="时区" :has-value="Boolean(form.timezone)">
              <input
                :value="form.timezone"
                @input="(event) => (form.timezone = (event.target as HTMLInputElement).value)"
              />
            </FloatingField>
          </el-col>
        </el-row>

        <el-row :gutter="14">
          <el-col :xs="24" :md="8">
            <FloatingField label="模板选择" :has-value="true">
              <select :value="form.template_id ?? ''" @change="onTemplateChange">
                <option value="">不使用模板</option>
                <option v-for="item in templates" :key="item.id" :value="item.id">
                  {{ item.name }}
                </option>
              </select>
            </FloatingField>
          </el-col>
          <el-col :xs="24" :md="8">
            <FloatingField label="签到时间" :has-value="Boolean(form.checkin_after)">
              <input
                :value="form.checkin_after"
                placeholder="HH:MM，可留空"
                @input="(event) => (form.checkin_after = (event.target as HTMLInputElement).value)"
              />
            </FloatingField>
          </el-col>
          <el-col :xs="24" :md="8">
            <FloatingField label="标签（逗号分隔）" :has-value="Boolean(form.tagsText)">
              <input
                :value="form.tagsText"
                @input="(event) => (form.tagsText = (event.target as HTMLInputElement).value)"
              />
            </FloatingField>
          </el-col>
        </el-row>

        <el-row :gutter="14">
          <el-col :xs="24" :md="8">
            <FloatingField label="启用站点" :has-value="true">
              <div class="switch-inline">
                <el-switch v-model="form.enabled" />
                <span class="switch-state">{{ form.enabled ? "启用" : "禁用" }}</span>
              </div>
            </FloatingField>
          </el-col>
          <el-col :xs="24" :md="8">
            <FloatingField label="选择交互" :has-value="true">
              <div class="switch-inline">
                <el-switch v-model="form.prefer_interaction" />
                <span class="switch-state">
                  {{ form.prefer_interaction ? "需要交互" : "无需交互" }}
                </span>
              </div>
            </FloatingField>
          </el-col>
          <el-col :xs="24" :md="8">
            <FloatingField label="选择代理" :has-value="true">
              <div class="switch-inline">
                <el-switch v-model="form.use_proxy" />
                <span class="switch-state">{{ form.use_proxy ? "需要代理" : "无需代理" }}</span>
              </div>
            </FloatingField>
          </el-col>
        </el-row>

        <el-row :gutter="14">
          <el-col :xs="24" :md="12">
            <FloatingField label="支持补签" :has-value="true">
              <div class="switch-inline">
                <el-switch v-model="form.support_resign" />
                <span class="switch-state">
                  {{ form.support_resign ? "支持补签" : "不支持补签" }}
                </span>
              </div>
            </FloatingField>
          </el-col>
          <el-col :xs="24" :md="12">
            <FloatingField label="支持补签最大天数" :has-value="true">
              <el-input-number
                v-model="form.resign_max_days"
                :min="1"
                :max="365"
                :disabled="!form.support_resign"
                controls-position="right"
              />
            </FloatingField>
          </el-col>
        </el-row>

        <div>
          <StringListField
            v-model="form.trusted_urls"
            label="可信 URL 正则（仅本站点）"
            add-label="添加可信 URL 正则"
          />
          <p class="muted trusted-url-help">
            仅扩展登录凭据和 TOTP 的使用范围，不改变其他权限；原站点同源 URL 默认可信，留空不扩展。
            每条 Python 正则对浏览器当前完整 HTTP(S) URL（含路径、查询和片段）进行全匹配，最多 20 条，每条 1-1024 字符。
            正则有效性由后端校验。域名中的 <code>.</code> 应写为 <code>\.</code>；宽泛的 <code>.*</code> 不安全，可能向不可信页面泄露凭据。
            示例：<code>^https://openid\.13a\.com/login(?:[/?#].*)?$</code>
          </p>
        </div>

        <FloatingField label="备注" :has-value="Boolean(form.notes)">
          <textarea
            :value="form.notes"
            rows="2"
            maxlength="4000"
            @input="(event) => (form.notes = (event.target as HTMLTextAreaElement).value)"
          />
        </FloatingField>
      </div>
    </el-card>

    <el-card shadow="never" class="panel">
      <template #header>
        <div class="card-head">
          <div>
            <strong>登录凭据</strong>
            <span class="muted">本地 AES-256-GCM 加密</span>
          </div>
          <el-tag
            :type="
               credential.configured || form.bitwarden_enabled || form.credential_provider_id?.startsWith('plugin.') ? 'success' : 'info'
            "
            size="small"
            effect="plain"
          >
            {{
               credential.configured || form.bitwarden_enabled || form.credential_provider_id?.startsWith('plugin.') ? "已配置" : "未配置"
            }}
          </el-tag>
        </div>
      </template>

      <div class="credential-tabs">
        <button
          v-for="tab in credentialTabs"
          :key="tab.key"
          type="button"
          class="credential-tab"
          :class="{ active: credentialTab === tab.key }"
          @click="credentialTab = tab.key"
        >
          {{ tab.label }}
        </button>
      </div>

      <template v-if="credentialTab === 'account'">
        <el-alert
          v-if="form.bitwarden_enabled || form.credential_provider_id?.startsWith('plugin.')"
          title="已启用外部凭据提供商，本地账户密码不会用于登录。"
          type="info"
          :closable="false"
          show-icon
          class="notice"
        />
        <p class="muted credential-help">
          用户名、密码独立加密保存，仅在任务执行时解密，不会回显，留空保持不变。
        </p>
        <el-alert
          v-if="credential.error"
          :title="credential.error"
          type="error"
          :closable="false"
          show-icon
          class="notice"
        />
        <el-alert
          v-if="credential.last_error"
          :title="credential.last_error"
          type="warning"
          :closable="false"
          show-icon
          class="notice"
        />
        <el-alert
          v-if="credentialError"
          :title="credentialError"
          type="error"
          :closable="false"
          show-icon
          class="notice"
        />
        <el-row :gutter="14">
          <el-col :xs="24" :md="12">
            <FloatingField
              label="用户名"
              :required="!credential.configured"
              :has-value="Boolean(credentialUsername) || credential.configured"
            >
              <input
                v-model="credentialUsername"
                maxlength="512"
                autocomplete="off"
                :placeholder="credential.configured ? '留空保持原用户名' : ''"
              />
            </FloatingField>
          </el-col>
          <el-col :xs="24" :md="12">
            <FloatingField
              label="密码"
              :required="!credential.configured"
              :has-value="Boolean(credentialPassword) || credential.configured"
            >
              <input
                v-model="credentialPassword"
                type="password"
                maxlength="2048"
                autocomplete="new-password"
                :placeholder="credential.configured ? '留空保持原密码' : ''"
              />
            </FloatingField>
          </el-col>
        </el-row>
        <div class="credential-status-row">
          <span class="muted">
            <template v-if="credential.configured">
              已保存：{{ credential.username_masked ?? "无法读取用户名" }} · 更新于
              {{ formatDate(credential.updated_at) }}
            </template>
            <template v-else-if="!isEditing">凭据将会随站点一起保存</template>
            <template v-else>尚未配置账户</template>
          </span>
          <div v-if="isEditing" class="credential-buttons">
            <el-button
              v-if="credential.configured"
              link
              type="danger"
              @click="removeCredential"
            >
              移除
            </el-button>
            <el-button
              type="primary"
              :loading="credentialSaving"
              :disabled="!hasCredentialInputs"
              @click="saveAccount"
            >
              保存账户
            </el-button>
          </div>
        </div>
      </template>

      <template v-else-if="credentialTab === 'bitwarden' || credentialTab.startsWith('plugin.')">
        <p class="muted credential-help">
          {{ externalProviderName }} 凭据不会保存到 SiteFlow。任务执行时由提供商按匹配设置获取登录条目。
        </p>
        <el-alert
          v-if="!externalProviderAvailable"
          title="凭据提供商未启用、未批准或服务未就绪，当前不能启用该凭据来源。"
          type="warning"
          :closable="false"
          show-icon
          class="notice"
        />
        <el-row :gutter="14">
          <el-col :xs="24" :md="10">
            <FloatingField :label="`启用 ${externalProviderName}`" :has-value="true">
              <div class="switch-inline">
                <el-switch
                  :model-value="externalProviderEnabled"
                  @update:model-value="setExternalProvider"
                  :disabled="!externalProviderAvailable && !externalProviderEnabled"
                />
                <span class="switch-state">
                  {{
                    externalProviderEnabled
                      ? `从 ${externalProviderName} 获取凭据`
                      : externalProviderAvailable
                        ? "不启用"
                        : "不支持"
                  }}
                </span>
              </div>
            </FloatingField>
          </el-col>
          <el-col :xs="24" :md="14">
            <FloatingField label="匹配方式" :has-value="true">
              <select
                v-model="form.bitwarden_match_mode"
                :disabled="!externalProviderEnabled"
              >
                <option value="domain">完整域名匹配</option>
                <option value="root_domain">主域名匹配</option>
                <option value="custom">自定义</option>
              </select>
            </FloatingField>
          </el-col>
        </el-row>
        <el-row
          v-if="externalProviderEnabled && form.bitwarden_match_mode === 'custom'"
          :gutter="14"
          class="bitwarden-custom-row"
        >
          <el-col :xs="24" :md="14">
            <FloatingField
              label="自定义匹配值"
              :has-value="Boolean(form.bitwarden_custom_match)"
            >
              <input
                v-model="form.bitwarden_custom_match"
                maxlength="255"
                placeholder="条目名称、域名或 URL 片段"
              />
            </FloatingField>
          </el-col>
        </el-row>
        <div class="credential-status-row">
          <span class="muted">
            <template v-if="externalProviderEnabled">
              已启用：从 {{ externalProviderName }} 获取凭据 ·
              {{ bitwardenModeLabel(form.bitwarden_match_mode) }}
              <template v-if="bitwardenHost"> · {{ bitwardenHost }}</template>
            </template>
            <template v-else-if="!isEditing">凭据将会随站点一起保存</template>
            <template v-else>尚未启用 {{ externalProviderName }}</template>
          </span>
          <div v-if="isEditing" class="credential-buttons">
            <el-button
              type="primary"
              :loading="saving"
              @click="saveBitwardenSettings"
            >
              保存设置
            </el-button>
          </div>
        </div>
      </template>

      <template v-else>
        <p class="muted credential-help">
          Cookie 独立加密保存，仅在任务执行时解密，不会回显，留空保持不变。
        </p>
        <el-alert
          v-if="cookieError"
          :title="cookieError"
          type="error"
          :closable="false"
          show-icon
          class="notice"
        />
        <FloatingField label="Cookie" :has-value="Boolean(cookieInput)" class="cookie-field">
          <textarea
            v-model="cookieInput"
            rows="3"
            maxlength="200000"
            placeholder="例如：uid=123; pass=abc&#10;或粘贴浏览器导出的 JSON Cookie 数组"
          />
        </FloatingField>
        <div class="credential-status-row">
          <span class="muted">
            <template v-if="credential.cookies_configured">
              已设置：{{ credential.cookie_count }} 条 Cookie · 更新于
              {{ formatDate(credential.cookies_updated_at) }}
            </template>
            <template v-else-if="!isEditing">凭据将会随站点一起保存</template>
            <template v-else>尚未配置 Cookie</template>
          </span>
          <div v-if="isEditing" class="credential-buttons">
            <el-button
              v-if="credential.cookies_configured"
              link
              type="danger"
              :loading="cookieSaving"
              @click="removeCookies"
            >
              移除
            </el-button>
            <el-button
              type="primary"
              :loading="cookieSaving"
              :disabled="!cookieInput.trim()"
              @click="saveCookieSettings"
            >
              保存 Cookie
            </el-button>
          </div>
        </div>
      </template>
    </el-card>

    <SignalEditor
      v-model="form.signals"
      :chain-signals="signalOptions"
      :used-keys="usedSignalKeysValue"
      :picker-base-url="form.base_url"
    />

    <el-card shadow="never" class="panel">
      <template #header>
        <div class="card-head">
          <div class="title-row">
            <strong>功能步骤</strong>
            <span class="muted">共 {{ form.steps.length }} 个本地步骤</span>
          </div>
          <div class="head-actions">
            <el-button size="small" @click="expandAllSteps">展开</el-button>
            <el-button size="small" @click="collapseAllSteps">收起</el-button>
            <el-button type="primary" plain @click="addStep">添加步骤</el-button>
          </div>
        </div>
      </template>
      <WorkflowStepEditor
        v-if="!loadingForm"
        ref="editorRef"
        v-model="form.steps"
        :features="features"
        :chain="chain"
        :signals="availableSignals"
        :cfbox-enabled="cfboxEnabled"
        :groups="form.groups"
        :inherited-groups="inheritedGroups"
        :groups-locked="false"
        :picker-base-url="form.base_url"
        key-namespace="site"
        @update:groups="form.groups = $event"
      />
    </el-card>
  </section>

  <CopyConfigDialog
    v-model="copyOpen"
    title="复制站点配置"
    :items="copySiteItems"
    :loading="copying"
    hint="将复制模板、步骤、分组、信号、车道、Bitwarden 和运行设置；站点名称、地址、本地凭据和 Cookie 不复制。"
    @confirm="copyFromSite"
  />

  <el-drawer v-model="previewOpen" :title="previewTitle" size="min(840px, 96vw)">
    <div v-loading="previewLoading">
      <el-alert v-if="previewError" :title="previewError" type="error" :closable="false" show-icon />
      <template v-if="preview">
        <div class="preview-meta">
          <el-tag v-if="preview.lane" effect="plain">浏览器：{{ laneLabel(preview.lane) }}</el-tag>
          <el-tag v-for="item in preview.requires" :key="item" size="small" effect="plain">
            依赖：{{ item }}
          </el-tag>
        </div>
        <div v-for="(step, index) in preview.steps" :key="step.step_key" class="preview-step">
          <div class="preview-step-head">
            <strong>#{{ index + 1 }} · {{ step.label }}</strong>
            <el-tag v-if="stepUsesLlm(features.find((item) => item.key === step.feature_type), step.config)" size="small" type="success" effect="plain">大模型</el-tag>
            <el-tag size="small" :type="step.enabled ? 'success' : 'info'" effect="plain">
              {{ step.enabled ? "启用" : "禁用" }}
            </el-tag>
          </div>
          <p class="muted mono">
            {{ step.step_key }} · {{ step.feature_type }} · {{ step.source }} ·
            {{ step.condition }} → 无法判断时人工接管 / {{ step.on_error }}
          </p>
          <pre class="config-preview">{{ JSON.stringify(step.config, null, 2) }}</pre>
        </div>
      </template>
    </div>
  </el-drawer>
</template>

<style scoped>
.page-head {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 18px;
  margin-bottom: 20px;
}

.eyebrow {
  margin: 0 0 6px;
  color: #409eff;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.16em;
}

.page-head h1 {
  margin: 0 0 6px;
  font-size: 26px;
}

.page-head p {
  margin: 0;
}

.head-actions {
  display: flex;
  gap: 8px;
}

.hidden-file {
  display: none;
}

.panel {
  border-radius: 16px;
  border-color: #e4e9f0;
  margin-bottom: 16px;
}

.list-toolbar {
  display: flex;
  justify-content: flex-start;
  margin-bottom: 12px;
}

.list-search {
  width: min(340px, 100%);
}

.site-link {
  color: #409eff;
  text-decoration: none;
  overflow-wrap: anywhere;
}

.site-link:hover {
  text-decoration: underline;
}

.editor-page :deep(.el-card) {
  display: block;
  overflow: visible;
}

.editor-page :deep(.el-card__header) {
  position: sticky;
  top: -26px;
  z-index: 6;
  background: #fff;
  border-radius: 16px 16px 0 0;
  box-shadow: 0 1px 0 #edf1f6;
}

.notice {
  margin-bottom: 14px;
}

.site-meta {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.trusted-url-help {
  margin: 8px 0 0;
  font-size: 12px;
  line-height: 1.7;
  overflow-wrap: anywhere;
}

.credential-tabs {
  display: flex;
  gap: 4px;
  overflow-x: auto;
  margin-bottom: 14px;
  border-bottom: 1px solid #edf1f6;
}

.credential-tab {
  flex: none;
  padding: 8px 16px;
  border: none;
  border-bottom: 2px solid transparent;
  background: transparent;
  color: #606266;
  cursor: pointer;
  font: inherit;
  font-size: 13px;
}

.credential-tab:hover {
  color: #409eff;
}

.credential-tab.active {
  border-bottom-color: #409eff;
  color: #409eff;
  font-weight: 600;
}

.credential-help {
  margin: 0 0 14px;
  font-size: 12px;
  line-height: 1.7;
}

.cookie-field {
  width: 100%;
}

.bitwarden-custom-row {
  margin-top: 14px;
}

.credential-status-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-top: 14px;
  flex-wrap: wrap;
}

.credential-buttons {
  display: flex;
  align-items: center;
  gap: 10px;
}

.switch-inline {
  display: flex;
  align-items: center;
  gap: 9px;
  min-height: 29px;
}

.switch-state {
  color: #606266;
  font-size: 13px;
}

.card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.card-head > div {
  display: flex;
  align-items: baseline;
  gap: 8px;
}

.card-head > .title-row {
  align-items: center;
  flex-wrap: wrap;
}


.preview-meta {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 12px;
}

.preview-step {
  border: 1px solid #e4e9f0;
  border-radius: 10px;
  padding: 12px;
  margin-bottom: 10px;
}

.preview-step-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.preview-step p {
  margin: 6px 0;
  font-size: 12px;
}

.config-preview {
  margin: 8px 0 0;
  padding: 10px;
  border-radius: 8px;
  background: #f4f6f9;
  font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
  font-size: 12px;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

@media (max-width: 760px) {
  .editor-page :deep(.el-card__header) {
    top: -16px;
  }

  .page-head {
    align-items: flex-start;
    flex-direction: column;
  }
}
</style>
