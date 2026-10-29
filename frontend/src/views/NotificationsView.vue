<script setup lang="ts">
import { computed, onMounted, reactive, ref } from "vue";
import { useRouter } from "vue-router";
import { ElMessage, ElMessageBox } from "element-plus";

import { ApiError, apiRequest } from "@/api/client";
import { useAuthStore } from "@/stores/auth";
import type {
  EmailNotificationView,
  HTTPNotificationView,
  NotificationFieldView,
  NotificationSettings,
  NotificationTestResult,
} from "@/types/notifications";

const auth = useAuthStore();
const router = useRouter();
const loading = ref(true);
const loadError = ref("");
const settings = ref<NotificationSettings | null>(null);
const savingLayout = ref(false);
const savingEmail = ref(false);
const savingHttp = ref(false);
const testingEmail = ref(false);
const testingHttp = ref(false);

const layoutForm = reactive({
  title_template: "",
  body_template: "",
  format: "text" as "text" | "html",
});
const emailForm = reactive({
  enabled: false,
  notify_success: true,
  notify_manual: true,
  smtp_host: "",
  smtp_port: 587 as number | undefined,
  security: "starttls" as "ssl" | "starttls" | "none",
  username: "",
  password: "",
  from_address: "",
  to_addresses: "",
});
const httpForm = reactive({
  enabled: false,
  notify_success: true,
  notify_manual: true,
  url: "",
  method: "POST" as "POST" | "PUT" | "PATCH",
  headers: "",
  timeout_seconds: 10,
});

const placeholders = [
  "run_id",
  "status",
  "status_label",
  "trigger_label",
  "site_count",
  "success_count",
  "fail_count",
  "waiting_count",
  "sites",
  "summary",
  "started_at",
  "finished_at",
];

const sampleValues: Record<string, string | number> = {
  run_id: 128,
  status: "fail",
  status_label: "失败",
  trigger_label: "定时调度",
  mode: "scheduled",
  created_by: "admin",
  site_count: 3,
  success_count: 2,
  fail_count: 1,
  waiting_count: 0,
  unknown_count: 0,
  deferred_count: 0,
  canceled_count: 0,
  summary: "成功 2，失败 1，等待人工 0",
  started_at: "2026-10-25 10:00:12 CST",
  finished_at: "2026-10-25 10:01:38 CST",
  sites: "示例站点 A：成功 · 签到成功\n示例站点 B：成功 · 签到成功\n示例站点 C：失败 · 登录失败",
};

const layoutError = computed(() => {
  if (!layoutForm.title_template.trim()) return "通知标题不能为空";
  if (!layoutForm.body_template.trim()) return "通知布局正文不能为空";
  return "";
});

const emailSource = computed(() => channelSource(settings.value?.email));
const httpSource = computed(() => channelSource(settings.value?.http));
const emailLockedFields = computed(() => lockCount(settings.value?.email));
const httpLockedFields = computed(() => lockCount(settings.value?.http));
const emailHasDb = computed(() => channelHasDb(settings.value?.email));
const httpHasDb = computed(() => channelHasDb(settings.value?.http));

const layoutPreviewTitle = computed(() => renderTemplate(layoutForm.title_template));
const layoutPreviewBody = computed(() => renderTemplate(layoutForm.body_template));
const layoutTitlePlaceholder = "[SiteFlow] 运行 #{{ run_id }} {{ status_label }}";
const layoutBodyPlaceholder =
  "支持 {{run_id}}、{{status_label}}、{{sites}} 等变量";
const headerPlaceholder = computed(() =>
  settings.value?.http.header_names.length
    ? `当前请求头：${settings.value.http.header_names.join(", ")}；留空保持，输入 {} 清除`
    : '{"Authorization": "Bearer ..."}',
);

function sourceLabel(source: string): string {
  if (source === "env") return "环境变量";
  if (source === "db") return "数据库";
  return "未配置";
}

function sourceTagType(source: string): "warning" | "success" | "info" {
  if (source === "env") return "warning";
  if (source === "db") return "success";
  return "info";
}

function statusTagType(channel: { enabled: boolean; available: boolean } | undefined) {
  if (!channel) return "info" as const;
  if (channel.enabled && channel.available) return "success" as const;
  if (channel.enabled) return "warning" as const;
  return "info" as const;
}

function statusLabel(channel: { enabled: boolean; available: boolean } | undefined): string {
  if (!channel) return "未配置";
  if (channel.enabled && channel.available) return "已启用";
  if (channel.enabled) return "配置不完整";
  return "未启用";
}

function channelSource(channel: EmailNotificationView | HTTPNotificationView | undefined): string {
  if (!channel) return "unset";
  const fields = Object.values(channel).filter(
    (value): value is NotificationFieldView =>
      typeof value === "object" &&
      value !== null &&
      "source" in value &&
      typeof (value as NotificationFieldView).source === "string",
  );
  if (fields.some((field) => field.source === "env")) return "env";
  if (fields.some((field) => field.source === "db")) return "db";
  return "unset";
}

function lockCount(channel: EmailNotificationView | HTTPNotificationView | undefined): number {
  if (!channel) return 0;
  return Object.values(channel).filter(
    (value) =>
      typeof value === "object" &&
      value !== null &&
      "source" in value &&
      (value as NotificationFieldView).source === "env",
  ).length;
}

function channelHasDb(channel: EmailNotificationView | HTTPNotificationView | undefined): boolean {
  if (!channel) return false;
  return Object.values(channel).some(
    (value) =>
      typeof value === "object" &&
      value !== null &&
      "source" in value &&
      (value as NotificationFieldView).source === "db",
  );
}

function fieldString(field: NotificationFieldView | undefined): string {
  return typeof field?.value === "string" ? field.value : "";
}

function fieldNumber(field: NotificationFieldView | undefined, fallback: number): number {
  return typeof field?.value === "number" ? field.value : fallback;
}

function fieldList(field: NotificationFieldView | undefined): string[] {
  return Array.isArray(field?.value) ? field.value.map(String) : [];
}

function renderTemplate(template: string): string {
  return template.replace(/\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}/g, (match, key: string) => {
    const value = sampleValues[key];
    return value === undefined ? match : String(value);
  });
}

function applySettings(data: NotificationSettings): void {
  settings.value = data;
  layoutForm.title_template = data.layout.title_template;
  layoutForm.body_template = data.layout.body_template;
  layoutForm.format = data.layout.format;

  emailForm.enabled = data.email.enabled;
  emailForm.notify_success = data.email.notify_success;
  emailForm.notify_manual = data.email.notify_manual;
  emailForm.smtp_host = fieldString(data.email.smtp_host);
  emailForm.smtp_port = fieldNumber(data.email.smtp_port, 587);
  const security = fieldString(data.email.security);
  emailForm.security = ["ssl", "starttls", "none"].includes(security)
    ? (security as "ssl" | "starttls" | "none")
    : "starttls";
  emailForm.username = fieldString(data.email.username);
  emailForm.password = "";
  emailForm.from_address = fieldString(data.email.from_address);
  emailForm.to_addresses = fieldList(data.email.to_addresses).join(", ");

  httpForm.enabled = data.http.enabled;
  httpForm.notify_success = data.http.notify_success;
  httpForm.notify_manual = data.http.notify_manual;
  httpForm.url = fieldString(data.http.url);
  const method = fieldString(data.http.method).toUpperCase();
  httpForm.method = ["POST", "PUT", "PATCH"].includes(method)
    ? (method as "POST" | "PUT" | "PATCH")
    : "POST";
  httpForm.headers = "";
  httpForm.timeout_seconds = fieldNumber(data.http.timeout_seconds, 10);
}

async function handleUnauthorized(error: unknown): Promise<boolean> {
  if (error instanceof ApiError && error.status === 401) {
    auth.clear();
    await router.replace({ name: "login" });
    return true;
  }
  return false;
}

async function load(): Promise<void> {
  loading.value = true;
  loadError.value = "";
  try {
    applySettings(await apiRequest<NotificationSettings>("/notifications"));
  } catch (error) {
    if (await handleUnauthorized(error)) return;
    loadError.value = error instanceof Error ? error.message : "加载通知设置失败";
  } finally {
    loading.value = false;
  }
}

async function saveLayout(): Promise<void> {
  if (layoutError.value) {
    ElMessage.warning(layoutError.value);
    return;
  }
  savingLayout.value = true;
  try {
    applySettings(
      await apiRequest<NotificationSettings>("/notifications/layout", {
        method: "PUT",
        body: JSON.stringify({
          title_template: layoutForm.title_template,
          body_template: layoutForm.body_template,
          format: layoutForm.format,
        }),
      }),
    );
    ElMessage.success("通知布局已保存");
  } catch (error) {
    if (await handleUnauthorized(error)) return;
    ElMessage.error(error instanceof Error ? error.message : "保存通知布局失败");
  } finally {
    savingLayout.value = false;
  }
}

function splitAddresses(value: string): string[] {
  return value
    .split(/[,;\n]/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function emailPayload(): Record<string, unknown> {
  const email = settings.value?.email;
  if (!email) return { enabled: emailForm.enabled };
  const payload: Record<string, unknown> = {
    enabled: emailForm.enabled,
    notify_success: emailForm.notify_success,
    notify_manual: emailForm.notify_manual,
  };
  if (email.smtp_host.editable) payload.smtp_host = emailForm.smtp_host.trim();
  if (email.smtp_port.editable) payload.smtp_port = emailForm.smtp_port ?? null;
  if (email.security.editable) payload.security = emailForm.security;
  if (email.username.editable) payload.username = emailForm.username.trim();
  if (email.password.editable && emailForm.password) payload.password = emailForm.password;
  if (email.from_address.editable) payload.from_address = emailForm.from_address.trim();
  if (email.to_addresses.editable) payload.to_addresses = splitAddresses(emailForm.to_addresses);
  return payload;
}

async function saveEmail(): Promise<void> {
  savingEmail.value = true;
  try {
    applySettings(
      await apiRequest<NotificationSettings>("/notifications/email", {
        method: "PUT",
        body: JSON.stringify(emailPayload()),
      }),
    );
    ElMessage.success("邮件通知配置已保存");
  } catch (error) {
    if (await handleUnauthorized(error)) return;
    ElMessage.error(error instanceof Error ? error.message : "保存邮件通知失败");
  } finally {
    savingEmail.value = false;
  }
}

function httpPayload(): Record<string, unknown> {
  const http = settings.value?.http;
  if (!http) return { enabled: httpForm.enabled };
  const payload: Record<string, unknown> = {
    enabled: httpForm.enabled,
    notify_success: httpForm.notify_success,
    notify_manual: httpForm.notify_manual,
  };
  if (http.url.editable) payload.url = httpForm.url.trim();
  if (http.method.editable) payload.method = httpForm.method;
  if (http.timeout_seconds.editable) payload.timeout_seconds = httpForm.timeout_seconds;
  if (http.headers.editable && httpForm.headers.trim()) {
    let parsed: unknown;
    try {
      parsed = JSON.parse(httpForm.headers);
    } catch {
      throw new Error("请求头必须是合法 JSON 对象");
    }
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
      throw new Error("请求头必须是 JSON 对象");
    }
    payload.headers = parsed;
  }
  return payload;
}

async function saveHttp(): Promise<void> {
  savingHttp.value = true;
  try {
    const payload = httpPayload();
    applySettings(
      await apiRequest<NotificationSettings>("/notifications/http", {
        method: "PUT",
        body: JSON.stringify(payload),
      }),
    );
    ElMessage.success("HTTP 通知配置已保存");
  } catch (error) {
    if (await handleUnauthorized(error)) return;
    ElMessage.error(error instanceof Error ? error.message : "保存 HTTP 通知失败");
  } finally {
    savingHttp.value = false;
  }
}

async function clearChannel(channel: "email" | "http"): Promise<void> {
  try {
    await ElMessageBox.confirm(
      "将清除数据库中的配置；环境变量配置保持不变。是否继续？",
      "清除通知配置",
      {
        confirmButtonText: "清除",
        cancelButtonText: "取消",
        type: "warning",
      },
    );
  } catch {
    return;
  }
  const saving = channel === "email" ? savingEmail : savingHttp;
  saving.value = true;
  try {
    applySettings(
      await apiRequest<NotificationSettings>(`/notifications/${channel}`, {
        method: "DELETE",
      }),
    );
    ElMessage.success("数据库配置已清除");
  } catch (error) {
    if (await handleUnauthorized(error)) return;
    ElMessage.error(error instanceof Error ? error.message : "清除配置失败");
  } finally {
    saving.value = false;
  }
}

async function testChannel(channel: "email" | "http"): Promise<void> {
  const testing = channel === "email" ? testingEmail : testingHttp;
  testing.value = true;
  try {
    const result = await apiRequest<NotificationTestResult>(
      `/notifications/${channel}/test`,
      { method: "POST" },
    );
    if (result.ok) {
      ElMessage.success(result.message);
    } else {
      const errors = result.results
        .filter((item) => !item.ok)
        .map((item) => item.message)
        .join("；");
      ElMessage.error(errors || result.message);
    }
  } catch (error) {
    if (await handleUnauthorized(error)) return;
    ElMessage.error(error instanceof Error ? error.message : "发送测试通知失败");
  } finally {
    testing.value = false;
  }
}

onMounted(() => {
  void load();
});
</script>

<template>
  <section class="page-head">
    <div>
      <p class="eyebrow">NOTIFICATIONS</p>
      <h1>通知设置</h1>
      <p class="muted">任务结束后发送邮件或 HTTP 通知，并自定义通知标题和正文布局。</p>
    </div>
  </section>

  <el-alert
    v-if="loadError"
    :title="loadError"
    type="error"
    :closable="false"
    show-icon
    class="notice"
  />

  <div v-loading="loading">
    <el-card shadow="never" class="panel layout-panel">
      <template #header>
        <div class="card-head">
          <div>
            <strong>通知布局</strong>
            <span class="muted">统一用于邮件和 HTTP 通知，支持双大括号变量占位符</span>
          </div>
          <el-button type="primary" :loading="savingLayout" @click="saveLayout">
            保存布局
          </el-button>
        </div>
      </template>

      <div class="layout-grid">
        <div>
          <el-form label-position="top" class="config-form">
            <el-form-item label="通知标题">
              <el-input
                v-model="layoutForm.title_template"
                maxlength="200"
                show-word-limit
                :placeholder="layoutTitlePlaceholder"
              />
            </el-form-item>
            <el-form-item label="正文布局">
              <el-input
                v-model="layoutForm.body_template"
                type="textarea"
                :rows="9"
                maxlength="20000"
                show-word-limit
                :placeholder="layoutBodyPlaceholder"
              />
            </el-form-item>
            <el-form-item label="正文格式">
              <el-radio-group v-model="layoutForm.format">
                <el-radio-button label="text">纯文本</el-radio-button>
                <el-radio-button label="html">HTML</el-radio-button>
              </el-radio-group>
              <p class="muted field-hint">
                邮件按所选格式发送；HTTP 通知同时发送纯文本 body 和 format 字段。
              </p>
            </el-form-item>
          </el-form>
        </div>
        <div class="preview-box">
          <div class="preview-label">预览</div>
          <strong class="preview-title">{{ layoutPreviewTitle }}</strong>
          <pre class="preview-body">{{ layoutPreviewBody }}</pre>
          <p v-if="layoutForm.format === 'html'" class="muted field-hint">
            预览按原文显示；实际邮件会以 HTML 渲染。
          </p>
        </div>
      </div>

      <div class="placeholder-row">
        <span class="muted">可用变量：</span>
        <el-tag v-for="item in placeholders" :key="item" size="small" effect="plain">
          {{ item }}
        </el-tag>
      </div>
      <p v-if="layoutError" class="error">{{ layoutError }}</p>
    </el-card>

    <el-row :gutter="18">
      <el-col :xs="24" :lg="12">
        <el-card shadow="never" class="panel">
          <template #header>
            <div class="card-head">
              <div>
                <strong>邮件通知</strong>
                <el-tag :type="statusTagType(settings?.email)" size="small">
                  {{ statusLabel(settings?.email) }}
                </el-tag>
              </div>
              <el-tag :type="sourceTagType(emailSource)" size="small">
                {{ sourceLabel(emailSource) }}
              </el-tag>
            </div>
          </template>

          <el-alert
            v-if="settings?.email.error"
            :title="settings.email.error"
            type="warning"
            :closable="false"
            show-icon
            class="notice"
          />

          <div class="channel-bar">
            <el-tooltip
              :content="emailForm.enabled ? '关闭邮件通知' : '启用邮件通知'"
              placement="top"
            >
              <button
                type="button"
                class="channel-toggle"
                :class="{ 'is-on': emailForm.enabled }"
                role="switch"
                :aria-checked="emailForm.enabled"
                @click="emailForm.enabled = !emailForm.enabled"
              >
                <span class="toggle-track" aria-hidden="true"><span class="toggle-knob"></span></span>
                <span>{{ emailForm.enabled ? "邮件通知已启用" : "邮件通知已关闭" }}</span>
              </button>
            </el-tooltip>
            <span class="muted">环境变量配置下也可切换启用状态</span>
          </div>

          <div class="filter-grid">
            <div class="filter-row">
              <div>
                <strong class="filter-label">
                  {{ emailForm.notify_success ? "接收成功通知" : "只接收失败通知" }}
                </strong>
                <span class="muted filter-hint">
                  {{
                    emailForm.notify_success
                      ? "成功和失败运行都会发送邮件。"
                      : "只在运行失败或异常时发送邮件。"
                  }}
                </span>
              </div>
              <el-switch v-model="emailForm.notify_success" />
            </div>
            <div class="filter-row">
              <div>
                <strong class="filter-label">
                  {{
                    emailForm.notify_manual
                      ? "接受手动运行结果通知"
                      : "只接受自动调度运行结果通知"
                  }}
                </strong>
                <span class="muted filter-hint">
                  {{
                    emailForm.notify_manual
                      ? "手动和自动调度运行都会发送邮件。"
                      : "忽略手动运行，只发送自动调度运行结果。"
                  }}
                </span>
              </div>
              <el-switch v-model="emailForm.notify_manual" />
            </div>
          </div>

          <el-form label-position="top" class="config-form">
            <el-form-item>
              <template #label>
                SMTP 主机
                <el-tag
                  :type="sourceTagType(settings?.email.smtp_host.source ?? 'unset')"
                  size="small"
                >
                  {{ sourceLabel(settings?.email.smtp_host.source ?? "unset") }}
                </el-tag>
              </template>
              <el-input
                v-model="emailForm.smtp_host"
                :disabled="settings ? !settings.email.smtp_host.editable : false"
                placeholder="smtp.example.com"
              />
              <span v-if="settings?.email.smtp_host.error" class="error">
                {{ settings.email.smtp_host.error }}
              </span>
            </el-form-item>
            <div class="field-grid">
              <el-form-item>
                <template #label>
                  SMTP 端口
                  <el-tag
                    :type="sourceTagType(settings?.email.smtp_port.source ?? 'unset')"
                    size="small"
                  >
                    {{ sourceLabel(settings?.email.smtp_port.source ?? "unset") }}
                  </el-tag>
                </template>
                <el-input-number
                  v-model="emailForm.smtp_port"
                  :min="1"
                  :max="65535"
                  :disabled="settings ? !settings.email.smtp_port.editable : false"
                  controls-position="right"
                />
                <span v-if="settings?.email.smtp_port.error" class="error">
                  {{ settings.email.smtp_port.error }}
                </span>
              </el-form-item>
              <el-form-item>
                <template #label>
                  加密方式
                  <el-tag
                    :type="sourceTagType(settings?.email.security.source ?? 'unset')"
                    size="small"
                  >
                    {{ sourceLabel(settings?.email.security.source ?? "unset") }}
                  </el-tag>
                </template>
                <el-select
                  v-model="emailForm.security"
                  :disabled="settings ? !settings.email.security.editable : false"
                >
                  <el-option label="SSL（通常 465）" value="ssl" />
                  <el-option label="STARTTLS（通常 587/25）" value="starttls" />
                  <el-option label="不加密" value="none" />
                </el-select>
                <span class="muted field-hint">
                  465 通常用 SSL，587/25 通常用 STARTTLS；连接超时请检查服务器端口和网络出口。
                </span>
              </el-form-item>
            </div>
            <div class="field-grid">
              <el-form-item>
                <template #label>
                  用户名
                  <el-tag
                    :type="sourceTagType(settings?.email.username.source ?? 'unset')"
                    size="small"
                  >
                    {{ sourceLabel(settings?.email.username.source ?? "unset") }}
                  </el-tag>
                </template>
                <el-input
                  v-model="emailForm.username"
                  :disabled="settings ? !settings.email.username.editable : false"
                  autocomplete="off"
                  placeholder="可选"
                />
              </el-form-item>
              <el-form-item>
                <template #label>
                  密码
                  <el-tag
                    :type="sourceTagType(settings?.email.password.source ?? 'unset')"
                    size="small"
                  >
                    {{ sourceLabel(settings?.email.password.source ?? "unset") }}
                  </el-tag>
                </template>
                <el-input
                  v-model="emailForm.password"
                  type="password"
                  show-password
                  :disabled="settings ? !settings.email.password.editable : false"
                  autocomplete="new-password"
                  :placeholder="
                    settings?.email.password.configured
                      ? '留空保留已保存的密码'
                      : 'SMTP 密码'
                  "
                />
                <span class="muted field-hint">
                  当前：{{
                    settings?.email.password.configured
                      ? settings.email.password.masked ?? "已配置（不显示原文）"
                      : "未配置"
                  }}
                </span>
              </el-form-item>
            </div>
            <el-form-item>
              <template #label>
                发件人
                <el-tag
                  :type="sourceTagType(settings?.email.from_address.source ?? 'unset')"
                  size="small"
                >
                  {{ sourceLabel(settings?.email.from_address.source ?? "unset") }}
                </el-tag>
              </template>
              <el-input
                v-model="emailForm.from_address"
                :disabled="settings ? !settings.email.from_address.editable : false"
                placeholder="SiteFlow <siteflow@example.com>"
              />
              <span v-if="settings?.email.from_address.error" class="error">
                {{ settings.email.from_address.error }}
              </span>
            </el-form-item>
            <el-form-item>
              <template #label>
                收件人
                <el-tag
                  :type="sourceTagType(settings?.email.to_addresses.source ?? 'unset')"
                  size="small"
                >
                  {{ sourceLabel(settings?.email.to_addresses.source ?? "unset") }}
                </el-tag>
              </template>
              <el-input
                v-model="emailForm.to_addresses"
                :disabled="settings ? !settings.email.to_addresses.editable : false"
                placeholder="one@example.com, two@example.com"
              />
              <span class="muted field-hint">多个地址用逗号、分号或换行分隔。</span>
              <span v-if="settings?.email.to_addresses.error" class="error">
                {{ settings.email.to_addresses.error }}
              </span>
            </el-form-item>
          </el-form>

          <div class="form-actions">
            <el-button type="primary" :loading="savingEmail" @click="saveEmail">
              保存邮件通知
            </el-button>
            <el-button :loading="testingEmail" @click="testChannel('email')">发送测试</el-button>
            <el-button
              type="danger"
              plain
              :loading="savingEmail"
              :disabled="!emailHasDb"
              @click="clearChannel('email')"
            >
              清除数据库配置
            </el-button>
          </div>
          <p v-if="emailLockedFields" class="muted">
            {{ emailLockedFields }} 个字段由环境变量锁定，不能修改；启用开关仍可调整。
          </p>
        </el-card>
      </el-col>

      <el-col :xs="24" :lg="12">
        <el-card shadow="never" class="panel">
          <template #header>
            <div class="card-head">
              <div>
                <strong>HTTP 通知</strong>
                <el-tag :type="statusTagType(settings?.http)" size="small">
                  {{ statusLabel(settings?.http) }}
                </el-tag>
              </div>
              <el-tag :type="sourceTagType(httpSource)" size="small">
                {{ sourceLabel(httpSource) }}
              </el-tag>
            </div>
          </template>

          <el-alert
            v-if="settings?.http.error"
            :title="settings.http.error"
            type="warning"
            :closable="false"
            show-icon
            class="notice"
          />

          <div class="channel-bar">
            <el-tooltip
              :content="httpForm.enabled ? '关闭 HTTP 通知' : '启用 HTTP 通知'"
              placement="top"
            >
              <button
                type="button"
                class="channel-toggle"
                :class="{ 'is-on': httpForm.enabled }"
                role="switch"
                :aria-checked="httpForm.enabled"
                @click="httpForm.enabled = !httpForm.enabled"
              >
                <span class="toggle-track" aria-hidden="true"><span class="toggle-knob"></span></span>
                <span>{{ httpForm.enabled ? "HTTP 通知已启用" : "HTTP 通知已关闭" }}</span>
              </button>
            </el-tooltip>
            <span class="muted">环境变量配置下也可切换启用状态</span>
          </div>

          <div class="filter-grid">
            <div class="filter-row">
              <div>
                <strong class="filter-label">
                  {{ httpForm.notify_success ? "接收成功通知" : "只接收失败通知" }}
                </strong>
                <span class="muted filter-hint">
                  {{
                    httpForm.notify_success
                      ? "成功和失败运行都会发送 HTTP 通知。"
                      : "只在运行失败或异常时发送 HTTP 通知。"
                  }}
                </span>
              </div>
              <el-switch v-model="httpForm.notify_success" />
            </div>
            <div class="filter-row">
              <div>
                <strong class="filter-label">
                  {{
                    httpForm.notify_manual
                      ? "接受手动运行结果通知"
                      : "只接受自动调度运行结果通知"
                  }}
                </strong>
                <span class="muted filter-hint">
                  {{
                    httpForm.notify_manual
                      ? "手动和自动调度运行都会发送 HTTP 通知。"
                      : "忽略手动运行，只发送自动调度运行结果。"
                  }}
                </span>
              </div>
              <el-switch v-model="httpForm.notify_manual" />
            </div>
          </div>

          <el-form label-position="top" class="config-form">
            <el-form-item>
              <template #label>
                请求地址
                <el-tag
                  :type="sourceTagType(settings?.http.url.source ?? 'unset')"
                  size="small"
                >
                  {{ sourceLabel(settings?.http.url.source ?? "unset") }}
                </el-tag>
              </template>
              <el-input
                v-model="httpForm.url"
                :disabled="settings ? !settings.http.url.editable : false"
                placeholder="https://example.com/api/siteflow"
              />
              <span v-if="settings?.http.url.error" class="error">
                {{ settings.http.url.error }}
              </span>
            </el-form-item>
            <div class="field-grid">
              <el-form-item>
                <template #label>
                  请求方法
                  <el-tag
                    :type="sourceTagType(settings?.http.method.source ?? 'unset')"
                    size="small"
                  >
                    {{ sourceLabel(settings?.http.method.source ?? "unset") }}
                  </el-tag>
                </template>
                <el-select
                  v-model="httpForm.method"
                  :disabled="settings ? !settings.http.method.editable : false"
                >
                  <el-option label="POST" value="POST" />
                  <el-option label="PUT" value="PUT" />
                  <el-option label="PATCH" value="PATCH" />
                </el-select>
              </el-form-item>
              <el-form-item>
                <template #label>
                  超时时间（秒）
                  <el-tag
                    :type="sourceTagType(settings?.http.timeout_seconds.source ?? 'unset')"
                    size="small"
                  >
                    {{ sourceLabel(settings?.http.timeout_seconds.source ?? "unset") }}
                  </el-tag>
                </template>
                <el-input-number
                  v-model="httpForm.timeout_seconds"
                  :min="1"
                  :max="120"
                  :disabled="settings ? !settings.http.timeout_seconds.editable : false"
                  controls-position="right"
                />
              </el-form-item>
            </div>
            <el-form-item>
              <template #label>
                请求头
                <el-tag
                  :type="sourceTagType(settings?.http.headers.source ?? 'unset')"
                  size="small"
                >
                  {{ sourceLabel(settings?.http.headers.source ?? "unset") }}
                </el-tag>
              </template>
              <el-input
                v-model="httpForm.headers"
                type="textarea"
                :rows="4"
                :disabled="settings ? !settings.http.headers.editable : false"
                :placeholder="headerPlaceholder"
              />
              <span class="muted field-hint">
                请求头值只保存在后端，不回显。填写 JSON 后会整体替换现有请求头；留空保持原值。
              </span>
              <span v-if="settings?.http.headers.error" class="error">
                {{ settings.http.headers.error }}
              </span>
            </el-form-item>
          </el-form>

          <div class="form-actions">
            <el-button type="primary" :loading="savingHttp" @click="saveHttp">
              保存 HTTP 通知
            </el-button>
            <el-button :loading="testingHttp" @click="testChannel('http')">发送测试</el-button>
            <el-button
              type="danger"
              plain
              :loading="savingHttp"
              :disabled="!httpHasDb"
              @click="clearChannel('http')"
            >
              清除数据库配置
            </el-button>
          </div>
          <p v-if="httpLockedFields" class="muted">
            {{ httpLockedFields }} 个字段由环境变量锁定，不能修改；启用开关仍可调整。
          </p>
          <p class="muted">
            请求体为 JSON，包含 <span class="mono">event</span>、
            <span class="mono">title</span>、<span class="mono">body</span>、
            <span class="mono">format</span> 和 <span class="mono">run</span> 详情。
          </p>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<style scoped>
.page-head {
  margin-bottom: 22px;
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

.panel {
  border-radius: 16px;
  border-color: #e4e9f0;
  margin-bottom: 18px;
}

.layout-panel {
  background: linear-gradient(180deg, #ffffff 0%, #fbfdff 100%);
}

.card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.card-head > div {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.layout-grid {
  display: grid;
  grid-template-columns: minmax(0, 1.15fr) minmax(0, 0.85fr);
  gap: 18px;
}

.preview-box {
  min-height: 260px;
  padding: 16px;
  border: 1px solid #e2eaf4;
  border-radius: 12px;
  background:
    radial-gradient(circle at 100% 0%, rgba(64, 158, 255, 0.1), transparent 14rem),
    #f8fbff;
}

.preview-label {
  margin-bottom: 10px;
  color: #8a96a3;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.12em;
  text-transform: uppercase;
}

.preview-title {
  display: block;
  color: #1f2d3d;
  font-size: 16px;
  overflow-wrap: anywhere;
}

.preview-body {
  margin: 12px 0 0;
  color: #4d5b69;
  font-family: inherit;
  font-size: 13px;
  line-height: 1.7;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.placeholder-row {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  margin-top: 4px;
}

.config-form {
  margin-top: 6px;
}

.field-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0 14px;
}

.channel-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 12px;
  font-size: 13px;
}

.channel-toggle {
  display: inline-flex;
  align-items: center;
  gap: 9px;
  min-height: 34px;
  padding: 4px 12px 4px 6px;
  border: 1px solid #dbe4ef;
  border-radius: 999px;
  background: #f7f9fc;
  color: #66727f;
  font: inherit;
  font-weight: 600;
  cursor: pointer;
  transition:
    background 0.18s ease,
    border-color 0.18s ease,
    color 0.18s ease,
    box-shadow 0.18s ease;
}

.channel-toggle:hover {
  border-color: #b9d5f3;
  background: #f1f7ff;
  color: #337ecc;
}

.channel-toggle:focus-visible {
  outline: 2px solid #93c5fd;
  outline-offset: 2px;
}

.channel-toggle.is-on {
  border-color: #b7e0c2;
  background: #f1fbf4;
  color: #2f7d48;
  box-shadow: 0 4px 12px rgba(60, 150, 92, 0.08);
}

.toggle-track {
  position: relative;
  width: 34px;
  height: 20px;
  flex: 0 0 34px;
  border-radius: 999px;
  background: #c3ccd7;
  transition: background 0.18s ease;
}

.toggle-knob {
  position: absolute;
  top: 3px;
  left: 3px;
  width: 14px;
  height: 14px;
  border-radius: 50%;
  background: #fff;
  box-shadow: 0 1px 3px rgba(20, 35, 50, 0.2);
  transition: transform 0.18s ease;
}

.channel-toggle.is-on .toggle-track {
  background: #57b877;
}

.channel-toggle.is-on .toggle-knob {
  transform: translateX(14px);
}

.filter-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
  margin-bottom: 14px;
}

.filter-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 12px;
  border: 1px solid #e7edf5;
  border-radius: 10px;
  background: #fbfcfe;
}

.filter-row > div {
  display: flex;
  flex-direction: column;
  gap: 3px;
  min-width: 0;
}

.filter-label {
  color: #354250;
  font-size: 13px;
}

.filter-hint {
  font-size: 11px;
  line-height: 1.4;
}

.form-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  margin-top: 4px;
}

.notice {
  margin-bottom: 14px;
}

.field-hint {
  display: block;
  margin-top: 4px;
  font-size: 12px;
  line-height: 1.5;
}

.error {
  display: block;
  margin-top: 4px;
  color: #d9534f;
  font-size: 12px;
  overflow-wrap: anywhere;
}

.muted {
  font-size: 12px;
}

@media (max-width: 900px) {
  .layout-grid,
  .field-grid,
  .filter-grid {
    grid-template-columns: 1fr;
  }
}
</style>
