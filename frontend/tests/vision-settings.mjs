import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { createServer } from "vite";
import { createRenderer, createSSRApp, nextTick, reactive, ssrContextKey } from "vue";
import { renderToString } from "vue/server-renderer";
import ElementPlus, { ID_INJECTION_KEY, ZINDEX_INJECTION_KEY } from "element-plus";

const source = await readFile(new URL("../src/components/VisionSettingsCard.vue", import.meta.url), "utf8");
const calls = [];
const messages = [];
let responseVision;
let failure;
const testResult = { ok: true, message: "已保存配置测试成功" };
globalThis.__visionSettingsMocks = {
  ElMessage: {
    info: message => messages.push(message),
    success: message => messages.push(message),
  },
  async saveVisionSettings(payload) {
    calls.push({ action: "save", payload: structuredClone(payload) });
    if (failure) throw failure;
    return { vision: responseVision };
  },
  async clearVisionSettings() {
    calls.push({ action: "clear" });
    if (failure) throw failure;
    return { vision: responseVision };
  },
  async testVisionSettings(...args) {
    calls.push({ action: "test", args });
    if (failure) throw failure;
    return testResult;
  },
};
const server = await createServer({
  optimizeDeps: { noDiscovery: true, include: [] },
  server: { middlewareMode: true, hmr: false },
  plugins: [{
    name: "vision-settings-test-expose", enforce: "pre",
    transform(code, id) {
      if (!id.endsWith("/VisionSettingsCard.vue")) return;
      return code
        .replace('import { ElMessage } from "element-plus";', 'const { ElMessage } = globalThis.__visionSettingsMocks;')
        .replace('import { clearVisionSettings, saveVisionSettings, testVisionSettings } from "@/api/client";',
          'const { clearVisionSettings, saveVisionSettings, testVisionSettings } = globalThis.__visionSettingsMocks;')
        .replace("</script>", "defineExpose({ form, tokenTouched, busy, result, allLocked, clearDisabled, run });\n</script>");
    },
  }],
});
const vision = (maxTokens = { value: 4096, source: "db", editable: true, error: null }) => ({
  base_url: { value: "https://example.com/v1", source: "db", editable: true, error: null },
  model: { value: "reasoning-model", source: "db", editable: true, error: null },
  api_key: { configured: true, source: "db", editable: true, masked: "****abcd" },
  supports_vision: { value: true, source: "unset", editable: true },
  max_tokens: maxTokens,
  available: true,
});
const renderer = createRenderer({
  createComment: () => ({}), insert() {}, remove() {}, parentNode() {}, nextSibling() {},
});
let app;
try {
  const { default: Card } = await server.ssrLoadModule("/src/components/VisionSettingsCard.vue");
  const props = reactive({ vision: vision() });
  const events = [];
  let ui;
  app = renderer.createApp({ setup() {
    Card.setup(props, { expose(value) { ui = value; }, emit(event, value) { events.push({ event, value }); } });
    return () => null;
  } });
  app.provide(ssrContextKey, { modules: new Set() });
  app.mount({});
  const hydrate = async value => { props.vision = value; await nextTick(); };
  const renderCard = value => renderToString(createSSRApp(Card, { vision: value })
    .use(ElementPlus).provide(ID_INJECTION_KEY, { prefix: 0, current: 0 })
    .provide(ZINDEX_INJECTION_KEY, { current: 0 }));

  assert.equal(ui.form.value.max_tokens, "4096");
  assert.equal(ui.form.value.api_key, "", "never hydrate the masked key into the editable secret");
  assert.equal(ui.allLocked.value, false);
  assert.equal(ui.clearDisabled.value, false);
  const html = await renderCard(vision());
  assert.match(html, /输出 Token 上限（max_tokens）/);
  assert.match(html, /留空或0不发送max_tokens，使用服务端默认上限；思考模型请预留足够预算/);
  const tokenInput = html.match(/<input(?=[^>]*aria-label="max_tokens")[^>]*>/)?.[0];
  assert.ok(tokenInput);
  assert.match(tokenInput, /inputmode="numeric"/);
  // Element Plus SSR omits input values; the exposed live form verifies hydration.
  assert.doesNotMatch(tokenInput, /disabled|max=|maxlength=|type="number"/);
  assert.match(source, /v-model="form.max_tokens"[^>]*clearable/);
  assert.match(source, /@input="tokenTouched = true"/);
  assert.match(source, /@clear="tokenTouched = true"/);

  responseVision = vision({ value: null, source: "unset", editable: true, error: null });
  ui.form.value.model = "new-model";
  await ui.run("save");
  assert.deepEqual(calls.at(-1).payload, { model: "new-model" }, "unrelated saves do not resend an unchanged valid cap");
  ui.form.value.max_tokens = "8192";
  ui.form.value.max_tokens = "4096";
  await ui.run("save");
  assert.deepEqual(calls.at(-1).payload, { model: "new-model" }, "programmatic edits restored to the hydrated string do not write tokens");
  await hydrate(vision());
  for (const value of ["", " ", "0", " 0 ", "1", " 8192 ", "00032", "2147483648"]) {
    ui.form.value.max_tokens = value;
    const count = calls.length;
    await ui.run("save");
    assert.equal(calls.length, count + 1);
    assert.deepEqual(calls.at(-1), { action: "save", payload: { max_tokens: Number(value) } });
    assert.equal(ui.result.value, null);
    assert.equal(ui.busy.value, false);
    assert.equal(events.at(-1).event, "updated");
    assert.equal(events.at(-1).value, responseVision);
  }
  for (const value of ["-1", "1.5", "0.5", "1.0", "NaN", "Infinity", "true", "false", "12tokens", "1e3", "0x10", NaN, true, false, -1, 1.5]) {
    ui.form.value.max_tokens = value;
    const count = calls.length;
    const eventCount = events.length;
    await ui.run("save");
    assert.equal(calls.length, count, `invalid token input must not save: ${String(value)}`);
    assert.equal(events.length, eventCount);
    assert.equal(ui.result.value.ok, false);
    assert.match(ui.result.value.message, /max_tokens.*非负整数/);
    assert.equal(ui.busy.value, false);
  }

  await hydrate(vision());
  ui.form.value.max_tokens = "16384";
  ui.form.value.base_url = " https://new.example/v1 ";
  ui.form.value.model = " new-model ";
  ui.form.value.supports_vision = false;
  await ui.run("save");
  assert.deepEqual(calls.at(-1).payload, {
    max_tokens: 16384, base_url: "https://new.example/v1", model: "new-model", supports_vision: false,
  });
  ui.form.value.api_key = " replacement-secret ";
  await ui.run("save");
  assert.equal(calls.at(-1).payload.api_key, "replacement-secret", "only explicitly entered secrets are sent");
  assert.ok(messages.every(message => !/replacement-secret|abcd/.test(message)));
  assert.doesNotMatch(source, /console\.|localStorage|sessionStorage/);

  const envZero = vision({ value: 0, source: "env", editable: false, error: null });
  envZero.supports_vision = { value: false, source: "env", editable: false };
  await hydrate(envZero);
  assert.equal(ui.form.value.max_tokens, "0");
  assert.equal(ui.allLocked.value, false, "locked supports_vision and tokens do not disable editable strings");
  assert.equal(ui.clearDisabled.value, true, "any lock blocks whole-config deletion");
  ui.form.value.model = " editable-model ";
  ui.form.value.supports_vision = true;
  ui.form.value.max_tokens = "invalid-but-locked";
  await ui.run("save");
  assert.deepEqual(calls.at(-1).payload, { model: "editable-model" });
  const lockedHtml = await renderCard(envZero);
  assert.match(lockedHtml, /环境变量锁定/);
  assert.match(lockedHtml, /<input(?=[^>]*aria-label="max_tokens")(?=[^>]*disabled)[^>]*>/);
  const saveButton = lockedHtml.match(/<button[^>]*>[\s\S]*?保存模型配置[\s\S]*?<\/button>/)?.[0];
  assert.ok(saveButton);
  assert.doesNotMatch(saveButton, /\sdisabled(?:\s|=|>)/);

  const locked = vision();
  for (const key of ["base_url", "model", "api_key", "supports_vision", "max_tokens"]) {
    locked[key].editable = false;
    locked[key].source = "env";
  }
  await hydrate(locked);
  assert.equal(ui.allLocked.value, true);
  for (const editable of ["base_url", "model", "api_key", "supports_vision", "max_tokens"]) {
    const partial = structuredClone(locked);
    partial[editable].editable = true;
    partial[editable].source = "db";
    await hydrate(partial);
    assert.equal(ui.allLocked.value, false, `${editable} alone enables save`);
    assert.equal(ui.clearDisabled.value, true);
  }

  const unset = vision({ value: null, source: "unset", editable: true, error: null });
  for (const key of ["base_url", "model", "api_key", "supports_vision"]) unset[key].source = "unset";
  await hydrate(unset);
  assert.equal(ui.form.value.max_tokens, "");
  assert.equal(ui.clearDisabled.value, true);
  const tokenOnlyDb = structuredClone(unset);
  tokenOnlyDb.max_tokens = { value: 8192, source: "db", editable: true, error: null };
  await hydrate(tokenOnlyDb);
  assert.equal(ui.clearDisabled.value, false, "a token-only DB override can be cleared");
  assert.equal(ui.form.value.max_tokens, "8192");
  responseVision = unset;
  await ui.run("clear");
  assert.deepEqual(calls.at(-1), { action: "clear" });
  assert.equal(events.at(-1).value, unset);
  await hydrate(events.at(-1).value);
  assert.equal(ui.form.value.max_tokens, "");
  assert.equal(ui.form.value.api_key, "");
  assert.equal(ui.clearDisabled.value, true);

  await hydrate(vision());
  ui.form.value.max_tokens = "";
  await ui.run("save");
  assert.deepEqual(calls.at(-1).payload, { max_tokens: 0 }, "blank explicitly clears the old DB cap");
  await hydrate(events.at(-1).value);
  assert.equal(ui.form.value.max_tokens, "");
  await hydrate(vision({ value: 32768, source: "db", editable: true, error: null }));
  assert.equal(ui.form.value.max_tokens, "32768", "updated props restore positive saved values");
  ui.form.value.max_tokens = "bad unsaved input";
  const beforeTest = events.length;
  await ui.run("test");
  assert.deepEqual(calls.at(-1), { action: "test", args: [] }, "testing uses only saved settings");
  assert.deepEqual(ui.result.value, testResult);
  assert.equal(events.length, beforeTest);

  failure = new Error("保存配置失败");
  ui.form.value.max_tokens = "8192";
  const beforeFailure = events.length;
  await ui.run("save");
  assert.deepEqual(ui.result.value, { ok: false, message: "保存配置失败" });
  assert.equal(ui.busy.value, false);
  assert.equal(events.length, beforeFailure);
  failure = null;
  const invalidView = vision({ value: null, source: "db", editable: true, error: "max_tokens 配置无效" });
  const errorHtml = await renderCard(invalidView);
  assert.match(errorHtml, /max_tokens 配置无效/);
  assert.match(errorHtml, /数据库/);
  await hydrate(invalidView);
  assert.equal(ui.result.value, null, "refresh clears stale operation errors");
  assert.equal(ui.form.value.max_tokens, "");
  ui.form.value.model = "new-model";
  await ui.run("save");
  assert.deepEqual(calls.at(-1).payload, { model: "new-model" }, "untouched invalid cap remains fail-closed when another field changes");
  ui.form.value.max_tokens = "0";
  await ui.run("save");
  assert.deepEqual(calls.at(-1).payload, { model: "new-model", max_tokens: 0 }, "explicit zero clears an invalid stored cap");
  for (const event of ["input", "clear"]) {
    await hydrate(structuredClone(invalidView));
    // Mirror the template event handler, including explicit input of an already blank string.
    ui.tokenTouched.value = true;
    await ui.run("save");
    assert.deepEqual(calls.at(-1).payload, { max_tokens: 0 }, `${event} explicitly clears an already blank invalid view`);
    await hydrate(structuredClone(invalidView));
    assert.equal(ui.tokenTouched.value, false, "hydration resets explicit interaction tracking");
    const count = calls.length;
    await ui.run("save");
    assert.equal(calls.length, count, "hydration alone must not clear an invalid cap");
  }

  // Verify the existing API boundary still sends the exact numeric payload via PUT.
  const client = await server.ssrLoadModule("/src/api/client.ts");
  const originalFetch = globalThis.fetch;
  const originalDocument = globalThis.document;
  const requests = [];
  globalThis.document = { cookie: "" };
  globalThis.fetch = async (url, options) => {
    requests.push({ url, method: options.method, body: options.body });
    return new Response(JSON.stringify({ vision: unset }));
  };
  try {
    for (const max_tokens of [0, 8192]) {
      await client.saveVisionSettings({ max_tokens });
      assert.deepEqual(requests.at(-1), {
        url: "/api/v1/integrations/vision", method: "PUT", body: JSON.stringify({ max_tokens }),
      });
    }
    await client.clearVisionSettings();
    assert.deepEqual(requests.at(-1), { url: "/api/v1/integrations/vision", method: "DELETE", body: undefined });
    await client.testVisionSettings();
    assert.deepEqual(requests.at(-1), { url: "/api/v1/integrations/vision/test", method: "POST", body: undefined });
  } finally {
    globalThis.fetch = originalFetch;
    if (originalDocument === undefined) delete globalThis.document;
    else globalThis.document = originalDocument;
  }
  console.log("Vision settings regression passed");
} finally {
  app?.unmount();
  delete globalThis.__visionSettingsMocks;
  await server.close();
}
