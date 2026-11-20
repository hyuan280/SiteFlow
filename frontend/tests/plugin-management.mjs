import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { createServer } from "vite";
import { createRenderer, nextTick, ssrContextKey } from "vue";

const server = await createServer({
  optimizeDeps: { noDiscovery: true, include: [] },
  server: { middlewareMode: true, hmr: false },
  plugins: [{ name: "plugin-management-test", enforce: "pre", transform(code, id) {
    if (!id.endsWith("/PluginsView.vue")) return;
    return code
      .replace('import { useRouter } from "vue-router";', 'const { useRouter } = globalThis.__pluginMocks;')
      .replace('import { ElMessage, ElMessageBox } from "element-plus";', 'const { ElMessage, ElMessageBox } = globalThis.__pluginMocks;')
      .replace('import { ApiError, apiRequest } from "@/api/client";', 'const { ApiError, apiRequest } = globalThis.__pluginMocks;')
      .replace('import { useAuthStore } from "@/stores/auth";', 'const { useAuthStore } = globalThis.__pluginMocks;')
      .replace("</script>", "defineExpose({ applyPlugins, approvalSelection, categoryDrafts, editCategory, editAssistantPermission, staleCategoryDraft, saveCategories, approve });\n</script>");
  } }],
});
const originalSetInterval = globalThis.setInterval;
const originalClearInterval = globalThis.clearInterval;
let app;
try {
  const plugins = await server.ssrLoadModule("/src/utils/plugins.ts");
  assert.equal(plugins.pluginStatusLabel("unapproved"), "待审批");
  assert.equal(plugins.pluginStatusLabel("available"), "正常");
  assert.equal(plugins.pluginStatusType("unapproved"), "warning");
  assert.equal(plugins.pluginStatusLabel("disabled"), "已停止");
  assert.equal(plugins.pluginStatusLabel("removed"), "已移除接入");
  assert.equal(plugins.pluginStatusType("disabled"), "info");
  assert.equal(plugins.capabilityLabel("secret.totp"), "TOTP 提供（高信任）");
  assert.equal(plugins.capabilityType("secret.totp"), "danger");
  assert.equal(plugins.checkStatusLabel({ ok: true, skipped: true }), "已跳过");
  assert.equal(plugins.checkStatusType({ ok: false, skipped: false }), "danger");

  const layout = await readFile(
    new URL("../src/components/layout/MainLayout.vue", import.meta.url),
    "utf8",
  );
  const pluginsMenu = layout.indexOf('label: "插件管理"');
  const settingsMenu = layout.indexOf('label: "系统设置"');
  assert.ok(pluginsMenu > 0);
  assert.ok(pluginsMenu < settingsMenu, "插件管理 must be above 系统设置");

  const router = await readFile(
    new URL("../src/router/index.ts", import.meta.url),
    "utf8",
  );
  const pluginsRoute = router.indexOf('path: "plugins"');
  const settingsRoute = router.indexOf('path: "settings"');
  assert.ok(pluginsRoute > 0);
  assert.ok(pluginsRoute < settingsRoute, "plugins route must be above settings route");

  const view = await readFile(new URL("../src/views/PluginsView.vue", import.meta.url), "utf8");
  assert.ok(view.includes("撤销审批"));
  assert.ok(view.includes("/revoke"));
  assert.ok(view.includes("/plugin-runtime/audit?"));
  assert.ok(view.includes("el-pagination"));
  assert.ok(view.includes("/references"));
  assert.ok(view.includes("/change-impacts"));
  assert.ok(view.includes("不会停止或删除容器"));
  assert.ok(view.includes("metadata_change"));
  assert.ok(view.includes("row.risk_index }}/5"));
  assert.ok(view.includes('label="允许助手使用"'));
  assert.ok(view.indexOf('label="允许助手使用"') > view.indexOf('label="管理分类"'));
  assert.ok(view.includes('width="140" align="center" header-align="center"'));
  assert.ok(view.indexOf("勾选并保存即持续授权") < view.indexOf('class="plugin-list"'));
  assert.equal(view.split("勾选并保存即持续授权").length - 1, 1);
  assert.ok(view.includes('label="生效分类" width="110"'));
  assert.ok(view.includes('label="风险指数" min-width="280"'));
  assert.ok(view.includes("plugin.captcha_methods.map"));
  assert.ok(view.includes('v-else-if="row.isMethod"'));
  assert.ok(view.includes("plugin.secret_providers.map"));
  assert.ok(view.includes('v-if="row.isProvider" class="muted"'));
  assert.ok(view.includes("私有凭据提供商"));
  assert.ok(view.includes("TOTP 提供商"));
  assert.ok(view.includes("不会向模型透露密码、TOTP 种子或验证码值"));
  assert.ok(view.includes("包括验证码处理与发布（post）"));
  const { interactionReviewRows } = await server.ssrLoadModule("/src/utils/assistant-review.ts");
  assert.deepEqual(interactionReviewRows("authorization", { credential_provider_id: "plugin.vault" }), [
    { field: "credential_provider_id", label: "凭据提供商", values: ["plugin.vault"] },
  ]);
  const requests = [], messages = [];
  let response = [];
  globalThis.__pluginMocks = {
    ApiError: class extends Error { constructor(message, status) { super(message); this.status = status; } },
    apiRequest: async (url, options) => { requests.push({ url, ...options }); return response; },
    useRouter: () => ({}),
    useAuthStore: () => ({ isAuthenticated: true }),
    ElMessage: { success: message => messages.push(message), error: message => messages.push(message) },
    ElMessageBox: {},
  };
  let poll;
  globalThis.setInterval = (callback, delay) => { assert.equal(delay, 10000); poll = callback; return 1; };
  globalThis.clearInterval = () => {};
  const { default: View } = await server.ssrLoadModule("/src/views/PluginsView.vue");
  const renderer = createRenderer({ createComment: () => ({}), insert() {}, remove() {}, parentNode() {}, nextSibling() {} });
  let ui;
  app = renderer.createApp({ setup() {
    View.setup({}, { expose(value) { ui = value; } });
    return () => null;
  } });
  app.provide(ssrContextKey, { modules: new Set() });
  app.mount({});
  await nextTick();
  const plugin = {
    plugin_id: "demo/example", generation: 2, permission_epoch: 3,
    capabilities: ["features", "secret.totp"], approved_capabilities: [], status: "unapproved",
    features: [{ key: "login", name: "Login", declared_category: "auth", category: "action", category_overridden: true, risk_index: 4, risk_label: "high" }],
    captcha_methods: [{ key: "ocr", name: "OCR", category: "challenge", declared_category: "challenge", category_overridden: false, risk_index: 3, risk_label: "medium", assistant_allowed: false, assistant_policy_revision: 0 }],
    secret_providers: ["credentials", "totp"].map(kind => ({
      key: `plugin.demo/example.secret.${kind}`, name: "Vault", provider_id: "plugin.demo/example",
      capability: `secret.${kind}`, declared_category: kind === "credentials" ? "auth" : "challenge",
      category: kind === "credentials" ? "auth" : "challenge", category_overridden: false,
      risk_index: 5, risk_label: "critical", assistant_allowed: false, assistant_policy_revision: 0,
    })),
  };
  ui.applyPlugins([plugin]);
  assert.deepEqual([...ui.approvalSelection[plugin.plugin_id]], [], "unapproved capabilities require explicit selection");
  ui.approvalSelection[plugin.plugin_id] = ["features"];
  ui.editCategory(plugin, "login", "sign");
  ui.editAssistantPermission(plugin, "ocr", true);
  response = [{ ...plugin, capabilities: [...plugin.capabilities, "secret.credentials"] }];
  await poll();
  await nextTick();
  assert.deepEqual([...ui.approvalSelection[plugin.plugin_id]], ["features"], "poll preserves selection without adding new permissions");
  assert.equal(ui.categoryDrafts[plugin.plugin_id].categories.login, "sign", "10 second poll preserves category edit");
  assert.equal(ui.categoryDrafts[plugin.plugin_id].assistant_permissions.ocr, true, "poll preserves captcha authorization draft");
  const beforeBlockedApproval = requests.length;
  await ui.approve(plugin);
  assert.equal(requests.length, beforeBlockedApproval, "unsaved category edits block approval");
  ui.editCategory(plugin, "login", null);
  response = { ...plugin, permission_epoch: 4 };
  await ui.saveCategories(plugin);
  assert.equal(requests.at(-1).method, "PUT");
  assert.equal(requests.at(-1).url, "/plugin-runtime/plugins/demo%2Fexample/feature-categories");
  assert.deepEqual(JSON.parse(requests.at(-1).body), { categories: { login: null }, assistant_permissions: { ocr: true }, generation: 2, permission_epoch: 3 });
  assert.equal(ui.categoryDrafts[plugin.plugin_id], undefined);
  assert.match(messages.at(-1), /旧审批已撤销.*重新审批/);
  assert.deepEqual([...ui.approvalSelection[plugin.plugin_id]], [], "epoch change requires explicit reconfirmation");
  const updated = response;
  ui.approvalSelection[plugin.plugin_id] = ["features"];
  await ui.approve(updated);
  assert.deepEqual(JSON.parse(requests.at(-1).body), { capabilities: ["features"], generation: 2, permission_epoch: 4 });
  ui.editCategory(updated, "login", "post");
  ui.applyPlugins([{ ...updated, generation: 3 }]);
  assert.equal(ui.staleCategoryDraft({ ...updated, generation: 3 }), true);
  assert.equal(ui.categoryDrafts[plugin.plugin_id].categories.login, "post", "stale drafts remain visible instead of being lost");
  const beforeStaleSave = requests.length;
  await ui.saveCategories({ ...updated, generation: 3 });
  assert.equal(requests.length, beforeStaleSave, "stale drafts cannot be saved against a new generation");
  delete ui.categoryDrafts[plugin.plugin_id];
  const approved = { ...updated, approved_capabilities: ["features"], status: "available" };
  ui.applyPlugins([approved]);
  ui.approvalSelection[plugin.plugin_id] = ["features"];
  ui.editAssistantPermission(approved, "login", true);
  ui.editAssistantPermission(approved, "ocr", false);
  response = { ...approved, permission_epoch: 5 };
  await ui.saveCategories(approved);
  assert.deepEqual(JSON.parse(requests.at(-1).body), {
    categories: {}, assistant_permissions: { login: true, ocr: false }, generation: 2, permission_epoch: 4,
  });
  assert.match(messages.at(-1), /通用审批保持不变/);
  assert.deepEqual([...ui.approvalSelection[plugin.plugin_id]], ["features"]);
  ui.editAssistantPermission(response, "ocr", true);
  ui.applyPlugins([{ ...response, permission_epoch: 6 }]);
  assert.equal(ui.staleCategoryDraft({ ...response, permission_epoch: 6 }), true);
  assert.equal(ui.categoryDrafts[plugin.plugin_id].assistant_permissions.ocr, true);
  delete ui.categoryDrafts[plugin.plugin_id];
  const providerPlugin = { ...approved, permission_epoch: 6 };
  ui.applyPlugins([providerPlugin]);
  const [credentials, totp] = providerPlugin.secret_providers;
  ui.editAssistantPermission(providerPlugin, credentials.key, true);
  ui.editAssistantPermission(providerPlugin, totp.key, false);
  response = [providerPlugin];
  await poll();
  assert.equal(ui.categoryDrafts[plugin.plugin_id].assistant_permissions[credentials.key], true, "poll preserves provider draft");
  response = { ...providerPlugin, permission_epoch: 7, secret_providers: [
    { ...credentials, assistant_allowed: true, assistant_policy_revision: 1 }, totp,
  ] };
  await ui.saveCategories(providerPlugin);
  assert.deepEqual(JSON.parse(requests.at(-1).body), {
    categories: {}, assistant_permissions: { [credentials.key]: true, [totp.key]: false }, generation: 2, permission_epoch: 6,
  }, "provider permissions save independently without category edits");
  assert.equal(ui.categoryDrafts[plugin.plugin_id], undefined);
  assert.match(messages.at(-1), /通用审批保持不变/);
  const savedProviderPlugin = response;
  ui.editAssistantPermission(savedProviderPlugin, credentials.key, false);
  response = [{ ...savedProviderPlugin, permission_epoch: 8 }];
  await poll();
  assert.equal(ui.staleCategoryDraft(response[0]), true, "poll marks provider drafts stale after permission changes");
  const beforeStaleProviderSave = requests.length;
  await ui.saveCategories(response[0]);
  assert.equal(requests.length, beforeStaleProviderSave, "stale provider drafts cannot save");
  assert.equal(ui.categoryDrafts[plugin.plugin_id].assistant_permissions[credentials.key], false);
  console.log("Plugin management regression passed");
} finally {
  app?.unmount();
  globalThis.setInterval = originalSetInterval;
  globalThis.clearInterval = originalClearInterval;
  delete globalThis.__pluginMocks;
  await server.close();
}
