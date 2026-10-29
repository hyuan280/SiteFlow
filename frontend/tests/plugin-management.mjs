import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { createServer } from "vite";

const server = await createServer({
  optimizeDeps: { noDiscovery: true, include: [] },
  server: { middlewareMode: true },
});
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
  await server.ssrLoadModule("/src/views/PluginsView.vue");
  console.log("Plugin management regression passed");
} finally {
  await server.close();
}
