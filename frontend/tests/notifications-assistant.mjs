import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { createServer } from "vite";
import { createRenderer, ssrContextKey } from "vue";

const source = await readFile(new URL("../src/views/NotificationsView.vue", import.meta.url), "utf8");
assert.match(source, /<el-switch v-model="emailForm\.notify_assistant" \/>/);
assert.match(source, /<el-switch v-model="httpForm\.notify_assistant" \/>/);
assert.equal((source.match(/接收助手调查通知/g) ?? []).length, 2);
assert.match(source, /原任务完成通知仍按上方成功和手动筛选发送/);
assert.match(source, /未发生接管时不会声称已调查/);

const field = value => ({ value, source: "db", editable: true, error: null });
const initialSettings = () => ({
  layout: {
    title_template: "Title",
    body_template: "Body",
    format: "text",
    editable: true,
  },
  email: {
    enabled: true,
    notify_success: true,
    notify_manual: true,
    notify_assistant: true,
    available: true,
    smtp_host: field("smtp.example.com"),
    smtp_port: field(587),
    security: field("starttls"),
    username: field(""),
    password: { configured: true, source: "db", editable: true, masked: "****abcd" },
    from_address: field("SiteFlow <siteflow@example.com>"),
    to_addresses: field(["one@example.com"]),
    error: null,
  },
  http: {
    enabled: true,
    notify_success: true,
    notify_manual: true,
    notify_assistant: true,
    available: true,
    url: field("https://example.com/notify"),
    method: field("POST"),
    headers: field(null),
    header_names: [],
    timeout_seconds: field(10),
    error: null,
  },
});

let persisted = initialSettings();
const requests = [];
const notices = [];
const mocks = {
  ApiError: class ApiError extends Error {
    constructor(status) {
      super("request failed");
      this.status = status;
    }
  },
  async apiRequest(url, options = {}) {
    const method = options.method ?? "GET";
    const body = options.body === undefined ? undefined : JSON.parse(options.body);
    requests.push({ url, method, body });
    if (url === "/notifications" && method === "GET") return structuredClone(persisted);
    if (url === "/notifications/email" && method === "PUT") {
      persisted = { ...persisted, email: { ...persisted.email, ...body } };
      return structuredClone(persisted);
    }
    if (url === "/notifications/http" && method === "PUT") {
      persisted = { ...persisted, http: { ...persisted.http, ...body } };
      return structuredClone(persisted);
    }
    throw new Error(`unexpected request: ${method} ${url}`);
  },
  useAuthStore: () => ({ clear() {} }),
  useRouter: () => ({ replace: async () => {} }),
  ElMessage: {
    success: message => notices.push(message),
    error: message => notices.push(message),
    warning: message => notices.push(message),
  },
  ElMessageBox: { confirm: async () => {} },
};
globalThis.__notificationsAssistantMocks = mocks;

const server = await createServer({
  optimizeDeps: { noDiscovery: true, include: [] },
  server: { middlewareMode: true, hmr: false },
  plugins: [{
    name: "notifications-assistant-test-expose",
    enforce: "pre",
    transform(code, id) {
      if (!id.endsWith("/NotificationsView.vue")) return;
      return code
        .replace(
          'import { ApiError, apiRequest } from "@/api/client";',
          "const { ApiError, apiRequest } = globalThis.__notificationsAssistantMocks;",
        )
        .replace(
          'import { useAuthStore } from "@/stores/auth";',
          "const { useAuthStore } = globalThis.__notificationsAssistantMocks;",
        )
        .replace(
          'import { useRouter } from "vue-router";',
          "const { useRouter } = globalThis.__notificationsAssistantMocks;",
        )
        .replace(
          'import { ElMessage, ElMessageBox } from "element-plus";',
          "const { ElMessage, ElMessageBox } = globalThis.__notificationsAssistantMocks;",
        )
        .replace(
          "</script>",
          "defineExpose({ emailForm, httpForm, settings, applySettings, emailPayload, httpPayload, saveEmail, saveHttp, load });\n</script>",
        );
    },
  }],
});

const tick = () => new Promise(resolve => setTimeout(resolve, 0));
const renderer = createRenderer({
  createComment: () => ({}),
  insert() {},
  remove() {},
  parentNode() {},
  nextSibling() {},
});

let app;
try {
  const { default: View } = await server.ssrLoadModule("/src/views/NotificationsView.vue");
  let ui;
  app = renderer.createApp({
    setup() {
      View.setup({}, { expose(value) { ui = value; }, emit() {} });
      return () => null;
    },
  });
  app.provide(ssrContextKey, { modules: new Set() });
  app.mount({});
  await tick();

  assert.equal(ui.emailForm.notify_assistant, true);
  assert.equal(ui.httpForm.notify_assistant, true);

  ui.emailForm.notify_assistant = false;
  await ui.saveEmail();
  assert.equal(requests.at(-1).url, "/notifications/email");
  assert.equal(requests.at(-1).body.notify_assistant, false);
  assert.equal(ui.emailForm.notify_assistant, false);
  assert.equal(ui.httpForm.notify_assistant, true, "email setting does not change HTTP");

  ui.httpForm.notify_assistant = false;
  await ui.saveHttp();
  assert.equal(requests.at(-1).url, "/notifications/http");
  assert.equal(requests.at(-1).body.notify_assistant, false);
  assert.equal(ui.httpForm.notify_assistant, false);
  assert.equal(ui.emailForm.notify_assistant, false);
  assert.ok(notices.includes("邮件通知配置已保存"));
  assert.ok(notices.includes("HTTP 通知配置已保存"));

  ui.emailForm.notify_assistant = true;
  ui.httpForm.notify_assistant = true;
  await ui.load();
  assert.equal(ui.emailForm.notify_assistant, false, "reload renders persisted email setting");
  assert.equal(ui.httpForm.notify_assistant, false, "reload renders persisted HTTP setting");

  console.log("Notifications assistant regression passed");
} finally {
  app?.unmount();
  delete globalThis.__notificationsAssistantMocks;
  await server.close();
}
