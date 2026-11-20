import assert from "node:assert/strict";
import { createServer } from "vite";
import { createSSRApp } from "vue";
import { renderToString } from "vue/server-renderer";
import { readFile } from "node:fs/promises";

const server = await createServer({
  optimizeDeps: { noDiscovery: true, include: [] },
  server: { middlewareMode: true, hmr: false },
  plugins: [{ name: "assistant-policy-test", enforce: "pre", transform(code, id) {
    if (!id.endsWith("/FeaturesView.vue")) return;
    return code
      .replace('import { useRouter } from "vue-router";', 'const useRouter = () => ({});')
      .replace('import { useAuthStore } from "@/stores/auth";', 'const useAuthStore = () => ({ isAuthenticated: true });')
      .replace('import { ElMessage } from "element-plus";', 'const ElMessage = globalThis.__policyMocks.messages;')
      .replace('import { ApiError, apiRequest } from "@/api/client";', 'const { ApiError, apiRequest } = globalThis.__policyMocks;')
      .replace('import FeatureSettingsEditor from "@/components/FeatureSettingsEditor.vue";', 'const FeatureSettingsEditor = {};')
      .replace("</script>", "defineExpose({ features, policyBusy, updateFeature, saveAssistantPolicy });\n</script>");
  } }],
});
try {
  const requests = [], errors = [];
  let respond, reject;
  globalThis.__policyMocks = {
    ApiError: class extends Error {},
    apiRequest: (url, options) => { requests.push({ url, ...options }); return new Promise((resolve, fail) => { respond = resolve; reject = fail; }); },
    messages: { success() {}, error(message) { errors.push(message); } },
  };
  const { default: View } = await server.ssrLoadModule("/src/views/FeaturesView.vue");
  let ui;
  await renderToString(createSSRApp({ setup() {
    View.setup({}, { expose(value) { ui = value; } });
    return () => null;
  } }));
  const feature = { key: "post/test", source: "builtin", assistant_allowed: false, assistant_policy_revision: 0,
    risk_index: 5, risk_label: "high", defaults: { message: "keep" } };
  ui.features.value = [feature];
  const original = ui.features.value[0];
  const saving = ui.saveAssistantPolicy(feature, true);
  assert.equal(ui.policyBusy[feature.key], true);
  assert.equal(original.assistant_allowed, false, "not optimistic before server approval");
  await ui.saveAssistantPolicy(feature, false);
  assert.equal(requests.length, 1, "block overlapping policy writes");
  assert.equal(requests[0].url, "/features/post%2Ftest/assistant-policy");
  assert.equal(requests[0].method, "PUT");
  assert.deepEqual(JSON.parse(requests[0].body), { assistant_allowed: true });
  respond({ ...feature, assistant_allowed: true, assistant_policy_revision: 1 });
  await saving;
  assert.equal(ui.features.value[0], original, "authorization update must not reset settings editor drafts");
  assert.equal(original.assistant_allowed, true);
  ui.updateFeature({ ...feature, defaults: { message: "saved" } });
  assert.equal(ui.features.value[0].assistant_allowed, true, "older settings response cannot undo policy");
  assert.equal(ui.features.value[0].defaults.message, "saved");
  const revoking = ui.saveAssistantPolicy(ui.features.value[0], false);
  respond({ ...feature, assistant_allowed: false, assistant_policy_revision: 2 });
  await revoking;
  assert.equal(ui.features.value[0].assistant_allowed, false);
  await ui.saveAssistantPolicy({ ...feature, source: "plugin" }, true);
  assert.equal(requests.length, 2, "plugin policy is managed with generation and epoch on plugin page");
  assert.deepEqual(errors, []);
  const failed = ui.saveAssistantPolicy(ui.features.value[0], true);
  reject(new Error("policy conflict"));
  await failed;
  assert.equal(ui.features.value[0].assistant_allowed, false, "failed save retains server state");
  assert.equal(ui.policyBusy[feature.key], undefined);
  assert.deepEqual(errors, ["policy conflict"]);
  const source = await readFile(new URL("../src/views/FeaturesView.vue", import.meta.url), "utf8");
  for (const text of ["允许助手使用", "feature.risk_index", "持续授权", "单步执行", "验证码处理与发布（post）", "外部副作用"]) assert.ok(source.includes(text), text);
  assert.ok(source.includes(':indeterminate="feature.category'));
  assert.ok(source.includes("确认持续授权"));
  console.log("Assistant policy regression passed");
} finally {
  delete globalThis.__policyMocks;
  await server.close();
}
