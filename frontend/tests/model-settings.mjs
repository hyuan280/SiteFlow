import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { randomUUID } from "node:crypto";
import { createServer } from "vite";
import { createRenderer, createSSRApp, h, ssrContextKey } from "vue";
import { renderToString } from "vue/server-renderer";
import ElementPlus, { ID_INJECTION_KEY, ZINDEX_INJECTION_KEY } from "element-plus";
import * as Elements from "element-plus";

const calls = [];
const confirmations = [];
let failure;
let pending;
let confirmAllowed = true;
let seed;
let response;
let ApiError;
let boundaryClient;
let testResponse;
const config = () => ({
  base_url: { value: "https://example.com/v1", source: "db", editable: true, error: null },
  model: { value: "model-long-name", source: "db", editable: true, error: null },
  api_key: { configured: true, source: "db", editable: true, masked: "****never-hydrate" },
  supports_vision: { value: true, source: "unset", editable: true },
  max_tokens: { value: 4096, source: "db", editable: true, error: null },
  available: true,
});
const extraId = randomUUID();
const environment = item => {
  item.source = "env";
  item.config_editable = false;
  for (const [key, field] of Object.entries(item.config)) {
    if (key !== "available") { field.editable = false; field.source = "env"; }
  }
  return item;
};
const registry = () => ({ revision: 7, items: [
  { id: "primary", name: "默认模型", enabled: true, usages: ["feature_cards", "assistant"], source: "db", config_editable: true, config: config() },
  { id: extraId, name: "备用模型", enabled: false, usages: ["mcp"], source: "db", config_editable: true, config: config() },
] });
const capacityRegistry = (dbCount, withEnv = false) => {
  const view = registry();
  view.items = Array.from({ length: dbCount }, (_, index) => ({
    ...structuredClone(view.items[1]), id: index === 0 && !withEnv ? "primary" : randomUUID(),
  }));
  if (withEnv) view.items.unshift(environment(registry().items[0]));
  return view;
};
response = registry();
globalThis.__modelSettingsMocks = {
  ApiError,
  ElMessageBox: { async confirm(...args) {
    confirmations.push(args);
    if (!confirmAllowed) throw new Error("cancel");
  } },
  seed: api => seed?.(api),
  async getModelRegistry() {
    calls.push({ action: "get" });
    if (boundaryClient) return boundaryClient.getModelRegistry();
    if (pending) return pending;
    if (failure) throw failure;
    return structuredClone(response);
  },
  async saveModelRegistry(payload) {
    calls.push({ action: "save", payload: structuredClone(payload) });
    if (boundaryClient) return boundaryClient.saveModelRegistry(payload);
    if (pending) return pending;
    if (failure) throw failure;
    response = { revision: payload.expected_revision + 1, items: payload.items.map(item => {
      const old = response.items.find(old => old.id === item.id);
      const view = structuredClone(old?.config ?? config());
      for (const [key, value] of Object.entries(item.config ?? {})) {
        if (key === "api_key") view.api_key.configured = true;
        else view[key].value = value;
      }
      return { ...item, config: view, source: old?.source ?? "db", config_editable: old?.config_editable ?? true };
    }) };
    return structuredClone(response);
  },
   async testModel(id, draft) {
     calls.push({ action: "test", id, ...(draft ? { draft: structuredClone(draft) } : {}) });
    if (pending) return pending;
    if (failure) throw failure;
     return testResponse ?? { ok: true, message: "文本请求成功（未启用视觉识别）", data: { mode: "text", supports_vision: false } };
  },
};
const server = await createServer({
  optimizeDeps: { noDiscovery: true, include: [] }, server: { middlewareMode: true, hmr: false },
  plugins: [{ name: "model-settings-harness", enforce: "pre", transform(code, id) {
    if (!id.endsWith("/ModelSettingsCard.vue")) return;
    return code.replace('import { ElMessageBox } from "element-plus";', 'const { ElMessageBox } = globalThis.__modelSettingsMocks;')
      .replace('import { ApiError, getModelRegistry, saveModelRegistry, testModel } from "@/api/client";',
        'const { ApiError, getModelRegistry, saveModelRegistry, testModel } = globalThis.__modelSettingsMocks;')
       .replace("</script>", `const exposed = { saved, items, busy, conflict, notice, editingId, editingConfig, dialogOpen, formError, tokenTouched, form, dirty, load, hydrate, closeEditor, edit, move, remove, save, test, testCurrent };
        defineExpose(exposed); globalThis.__modelSettingsMocks.seed(exposed);\n</script>`);
  } }],
});
const renderer = createRenderer({ createComment: () => ({}), insert() {}, remove() {}, parentNode() {}, nextSibling() {} });
const apps = [];
try {
  const client = await server.ssrLoadModule("/src/api/client.ts");
  ApiError = client.ApiError;
  globalThis.__modelSettingsMocks.ApiError = ApiError;
  const { isModelRegistryView } = await server.ssrLoadModule("/src/types/model-registry.ts");
  const { default: Card } = await server.ssrLoadModule("/src/components/ModelSettingsCard.vue");
  const mount = async () => {
    let ui;
    const events = [];
    const app = renderer.createApp({ setup() {
      Card.setup({}, { expose(value) { ui = value; }, emit(...args) { events.push(args); } });
      return () => null;
    } });
    app.provide(ssrContextKey, { modules: new Set() }); app.mount({}); apps.push(app);
    await new Promise(resolve => setImmediate(resolve));
    return { ui, events, app };
  };
  const reset = ui => {
    failure = null; pending = null; confirmAllowed = true;
    testResponse = undefined;
    response = registry(); ui.closeEditor(); ui.hydrate(structuredClone(response));
    ui.notice.value = null;
  };
  const renderCard = async callback => {
    seed = api => { api.hydrate(registry()); callback?.(api); };
    try {
      const context = {};
      // The real dialog defers its slot until onMounted. Render that slot in SSR
      // while retaining the real Element Plus form, inputs, switches and buttons.
      const dialogHarness = { install(app) {
        for (const name of ["ElCard", "ElTag", "ElButton", "ElSwitch", "ElAlert", "ElForm", "ElFormItem", "ElInput", "ElCheckboxGroup", "ElCheckbox"]) {
          app.component(name, Elements[name]);
        }
        app.component("ElDialog", { props: ["modelValue"], setup(props, { slots }) {
          return () => props.modelValue ? h("section", { role: "dialog" }, [slots.default?.(), slots.footer?.()]) : null;
        } });
      } };
      const html = await renderToString(createSSRApp(Card).use(callback ? dialogHarness : ElementPlus)
        .provide(ID_INJECTION_KEY, { prefix: 0, current: 0 }).provide(ZINDEX_INJECTION_KEY, { current: 0 }), context);
      return html + Object.values(context.teleports ?? {}).join("");
    } finally { seed = undefined; }
  };
  const { ui, events } = await mount();
  assert.deepEqual(calls, [{ action: "get" }], "mount only reads registry, never tests a model");
  assert.equal(ui.saved.value.revision, 7);
  assert.equal(ui.dirty.value, false);
  ui.move(extraId, -1);
  assert.equal(ui.items.value[0].id, extraId);
  assert.equal(calls.length, 1, "reorder never auto-saves or tests");
  ui.items.value[0].enabled = true;
  ui.edit(extraId);
  assert.equal(ui.form.value.api_key, "");
  ui.form.value.name = " renamed "; ui.form.value.usages = ["assistant", "mcp"];
  ui.form.value.supports_vision = false;
  ui.form.value.max_tokens = "0";
  ui.move("primary", -1);
  assert.equal(ui.items.value[0].id, extraId, "modal keeps immutable editing ID, reorder blocked while editing");
  await ui.save(true);
  assert.deepEqual(calls.at(-1).payload, { expected_revision: 7, items: [
    { id: extraId, name: "renamed", enabled: true, usages: ["assistant", "mcp"], config: { supports_vision: false, max_tokens: 0 } },
    { id: "primary", name: "默认模型", enabled: true, usages: ["feature_cards", "assistant"] },
  ] });
  assert.equal(ui.dirty.value, false);
  assert.equal(ui.form.value.api_key, "");

  reset(ui); ui.move(extraId, -1); await ui.save();
  assert.equal(calls.at(-1).payload.items[0].id, extraId);
  assert.ok(calls.at(-1).payload.items.every(item => !("config" in item)), "metadata saves preserve every secret");
  reset(ui);
  environment(response.items[0]); ui.hydrate(structuredClone(response));
  ui.items.value[0].config.supports_vision.value = false;
  ui.items.value[0].config.max_tokens.value = 0;
  ui.edit("primary");
  assert.equal(ui.dialogOpen.value, false, "environment models never open an editor");
  const beforeEnvEdit = calls.length; const beforeEnvConfirm = confirmations.length;
  await ui.remove("primary"); await ui.save(true); await ui.testCurrent();
  assert.equal(calls.length, beforeEnvEdit); assert.equal(confirmations.length, beforeEnvConfirm);
  ui.items.value[0].enabled = false; ui.move("primary", 1); await ui.save();
  assert.deepEqual(calls.at(-1).payload.items[1], { id: "primary", name: "默认模型", enabled: false, usages: ["feature_cards", "assistant"] });
  assert.ok(calls.at(-1).payload.items.every(item => Object.keys(item).sort().join() === "enabled,id,name,usages"),
    "metadata writes strip source, config_editable and connection config");

  for (const value of ["-1", "1.5", "1e3", "0x10", "2147483648", "9007199254740992", NaN, true]) {
    reset(ui); ui.edit(extraId); ui.form.value.max_tokens = value;
    const count = calls.length; await ui.save(true);
    assert.equal(calls.length, count, `invalid cap: ${value}`); assert.match(ui.formError.value, /非负整数/);
  }
  for (const [value, expected] of [["", null], [" ", null], ["0", 0], ["2147483647", 2147483647], [" 00032 ", 32]]) {
    reset(ui); ui.edit(extraId); ui.form.value.max_tokens = value; await ui.save(true);
    assert.equal(calls.at(-1).payload.items[1].config.max_tokens, expected);
  }
  reset(ui); ui.items.value[1].config.max_tokens.value = null; ui.edit(extraId);
  ui.tokenTouched.value = true; await ui.save(true);
  assert.deepEqual(calls.at(-1).payload.items[1].config, { max_tokens: null }, "explicit blank clears even an invalid blank saved cap");
  for (const repair of [null, 0]) {
    reset(ui);
    response.items[0].config.max_tokens = { value: null, source: "db", editable: true, error: "unsafe-invalid-cap" };
    response.items[0].config.base_url.error = "unsafe-invalid-url";
    response.items[0].config.available = false;
    ui.hydrate(structuredClone(response));
    ui.edit("primary"); ui.form.value.name = "repair metadata";
    await ui.save(true);
    assert.equal(calls.at(-1).payload.items[0].config, undefined, "invalid saved cap/URL do not block metadata or get silently cleared");
    ui.edit("primary"); ui.form.value.max_tokens = repair === null ? "" : "0";
    ui.tokenTouched.value = true;
    ui.form.value.base_url = "https://repaired.example/v1";
    await ui.save(true);
    assert.deepEqual(calls.at(-1).payload.items[0].config, { base_url: "https://repaired.example/v1", max_tokens: repair },
      "invalid config view allows explicit partial repair");
  }
  reset(ui); ui.edit(extraId); ui.form.value.api_key = "   "; ui.form.value.name = "metadata"; await ui.save(true);
  assert.equal(calls.at(-1).payload.items[1].config, undefined, "blank key and unchanged connections omitted");
  reset(ui); ui.edit(extraId); ui.form.value.max_tokens = "8192"; ui.tokenTouched.value = true;
  ui.form.value.max_tokens = "4096"; await ui.save(true);
  assert.equal(calls.at(-1).payload.items[1].config, undefined, "restored unchanged cap is omitted even after input events");
  reset(ui); ui.edit(extraId); ui.form.value.api_key = " replacement-secret "; await ui.save(true);
  assert.deepEqual(calls.at(-1).payload.items[1].config, { api_key: "replacement-secret" });
  assert.doesNotMatch(JSON.stringify(ui.notice.value), /replacement-secret/);

  reset(ui); ui.edit();
  assert.match(ui.editingId.value, /^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/);
  const newId = ui.editingId.value;
  ui.form.value.name = "新模型";
  const beforeIncomplete = calls.length; await ui.save(true); assert.equal(calls.length, beforeIncomplete);
  Object.assign(ui.form.value, { base_url: "https://new.example/v1", model: "new-model", api_key: "new-secret", supports_vision: false });
  await ui.save(true);
  assert.deepEqual(calls.at(-1).payload.items[2], { id: newId, name: "新模型", enabled: true, usages: [], config: {
    base_url: "https://new.example/v1", model: "new-model", api_key: "new-secret", supports_vision: false,
  } });
  reset(ui); const confirmationCount = confirmations.length; await ui.remove("primary");
  assert.equal(confirmations.length, confirmationCount + 1); assert.equal(ui.items.value.length, 1, "legacy primary is an ordinary deletable DB row");
  reset(ui);
  confirmAllowed = false; await ui.remove(extraId); assert.equal(ui.items.value.length, 2);
  confirmAllowed = true; await ui.remove(extraId); assert.equal(ui.items.value.length, 1);
  const beforeDeleteSave = calls.length; assert.equal(ui.saved.value.items.length, 2);
  await ui.save(); assert.equal(calls.length, beforeDeleteSave + 1);
  assert.deepEqual(calls.at(-1).payload.items.map(item => item.id), ["primary"]);
  await ui.remove("primary"); await ui.save();
  assert.deepEqual(calls.at(-1).payload.items, [], "the final DB row may be deleted");
  assert.deepEqual(ui.saved.value.items, []); assert.equal(ui.dirty.value, false);
  ui.edit(); assert.equal(ui.dialogOpen.value, true, "an empty registry allows adding a model");
  Object.assign(ui.form.value, { name: "首个模型", base_url: "https://first.example/v1", model: "first-model", api_key: "first-secret" });
  await ui.save(true); assert.equal(ui.items.value.length, 1); assert.equal(ui.items.value[0].source, "db");
  reset(ui); response.items = []; await ui.load();
  assert.deepEqual(ui.items.value, []); assert.equal(ui.saved.value.revision, 7);
  ui.edit(); assert.equal(ui.dialogOpen.value, true, "initial empty response allows adding a model");
  for (const withEnv of [false, true]) {
    reset(ui); response = capacityRegistry(20, withEnv); await ui.load();
    assert.equal(ui.items.value.length, withEnv ? 21 : 20);
    ui.edit(); assert.equal(ui.dialogOpen.value, false, "20 DB rows block adding, including a DB primary");
    ui.edit(ui.items.value.at(-1).id); ui.form.value.name = "编辑满额模型";
    await ui.save(true);
    assert.equal(calls.at(-1).payload.items.length, withEnv ? 21 : 20, "editing at capacity preserves every row");
    ui.closeEditor();
    const removedId = ui.items.value.at(-1).id;
    const remaining = ui.items.value.filter(item => item.id !== removedId).map(({ id, name, enabled, usages }) => ({ id, name, enabled, usages: [...usages] }));
    await ui.remove(removedId); await ui.save();
    assert.deepEqual(calls.at(-1).payload.items, remaining, "deleting a DB row preserves all other rows without truncation");
    assert.equal(ui.items.value.length, withEnv ? 20 : 19);
    ui.edit(); assert.equal(ui.dialogOpen.value, true, "19 DB rows allow adding, including with an env row");
    Object.assign(ui.form.value, { name: "新增至满额", base_url: "https://capacity.example/v1", model: "capacity-model", api_key: "capacity-key" });
    const addedId = ui.editingId.value;
    await ui.save(true);
    assert.deepEqual(calls.at(-1).payload.items.slice(0, -1), remaining, "adding at 19 DB plus env preserves existing rows");
    assert.equal(calls.at(-1).payload.items.at(-1).id, addedId);
    assert.equal(ui.items.value.length, withEnv ? 21 : 20);
    assert.equal(isModelRegistryView(response), true);
    if (withEnv) assert.equal(ui.items.value[0].source, "env", "environment row survives every save");

    reset(ui); response = capacityRegistry(19, withEnv); ui.hydrate(structuredClone(response));
    ui.edit(); Object.assign(ui.form.value, { name: "容量守卫", base_url: "https://capacity.example/v1", model: "capacity-model", api_key: "capacity-key" });
    ui.items.value.push({ ...structuredClone(response.items.at(-1)), id: randomUUID() });
    const beforeCapacitySave = calls.length; await ui.save(true);
    assert.equal(calls.length, beforeCapacitySave, "create save guard independently enforces the DB count");
  }

  reset(ui); await ui.test(extraId);
  assert.deepEqual(calls.at(-1), { action: "test", id: extraId }, "disabled entry tests saved exact ID");
  assert.match(ui.notice.value.message, /后端复用已保存密钥.*文本请求成功/);
  ui.move(extraId, -1); ui.edit(extraId); ui.form.value.api_key = "unsaved-secret";
  const beforeTest = calls.length; confirmAllowed = false; await ui.test(extraId);
  assert.equal(calls.length, beforeTest);
  confirmAllowed = true; await ui.test(extraId);
  assert.deepEqual(calls.at(-1), { action: "test", id: extraId });
  assert.equal(ui.form.value.api_key, "unsaved-secret", "test never hydrates or saves draft");
  assert.doesNotMatch(ui.notice.value.message, /raw-secret/);
  ui.closeEditor(); assert.equal(ui.form.value.api_key, "");

  reset(ui); ui.edit(extraId);
  Object.assign(ui.form.value, { name: "", enabled: false, usages: [], model: "draft-model", supports_vision: false, max_tokens: "0" });
  const beforeDraftTest = calls.length;
  await ui.testCurrent();
  assert.deepEqual(calls.slice(beforeDraftTest), [{ action: "test", id: extraId, draft: {
    expected_revision: 7, config: { model: "draft-model", supports_vision: false, max_tokens: 0 },
  } }], "current test submits only changed non-key fields, never metadata or a save");
  assert.equal(ui.saved.value.revision, 7); assert.equal(ui.form.value.model, "draft-model");
  assert.equal(ui.conflict.value, false); assert.match(ui.notice.value.message, /当前配置（不保存）.*文本请求成功/);
  ui.form.value.name = "still editable"; await ui.save(true);
  assert.equal(calls.at(-1).action, "save", "saving remains available after a read-only draft test");

  reset(ui); ui.edit(extraId);
  testResponse = { ok: false, message: "Model model-long-name unavailable at https://example.com/v1", data: {
    mode: "text", http_status: 404, provider_error: { message: "Deployment not found", code: "model_not_found", cookie: "private-cookie" },
    headers: "private-headers", api_key: "private-unknown-key",
  } };
  await ui.testCurrent();
  assert.match(ui.notice.value.message, /后端复用已保存密钥.*HTTP 404.*模型不存在或当前密钥无权访问/);
  assert.doesNotMatch(JSON.stringify(ui.notice.value), /Deployment not found|example.com|model-long-name|private-cookie|private-headers|private-unknown-key/);
  testResponse = { ok: true, message: "unsafe response", api_key: "private-unknown-key" };
  await ui.testCurrent();
  assert.equal(ui.notice.value.ok, false); assert.match(ui.notice.value.message, /响应格式无效/);
  assert.doesNotMatch(JSON.stringify(ui.notice.value), /unsafe response|private-unknown-key/);
  assert.equal(ui.conflict.value, false);

  reset(ui); ui.edit(extraId); await ui.testCurrent();
  assert.deepEqual(calls.at(-1).draft, { expected_revision: 7, config: {} }, "unchanged fields are omitted");
  ui.form.value.max_tokens = ""; await ui.testCurrent();
  assert.deepEqual(calls.at(-1).draft.config, { max_tokens: null });
  ui.form.value.base_url = "https://changed.example/v1";
  confirmAllowed = false; const beforeTarget = calls.length; await ui.testCurrent();
  assert.equal(calls.length, beforeTarget); assert.equal(ui.busy.value, false);
  assert.match(confirmations.at(-1)[0], /使用已保存密钥.*不保存.*核对目标.*https:\/\/changed.example\/v1/);
  confirmAllowed = true; await ui.testCurrent();
  assert.deepEqual(calls.at(-1).draft.config, { base_url: "https://changed.example/v1", max_tokens: null });

  reset(ui); ui.items.value[0].config.available = false;
  ui.items.value[0].config.base_url.value = null; ui.edit("primary");
  ui.form.value.base_url = "https://repaired.example/v1"; await ui.testCurrent();
  assert.deepEqual(calls.at(-1).draft.config, { base_url: "https://repaired.example/v1" }, "incomplete saved config can be repaired and tested");
  reset(ui);
  environment(response.items[0]); response.items[0].config.supports_vision.value = false;
  response.items[0].config.max_tokens.value = 0; ui.hydrate(structuredClone(response));
  ui.items.value[0].enabled = false; ui.move("primary", 1);
  ui.edit("primary"); assert.equal(ui.dialogOpen.value, false);
  ui.edit(extraId); ui.form.value.api_key = "unrelated-draft-key";
  ui.form.value.base_url = "https://unrelated.example/v1";
  const beforeEnvTest = calls.length; await ui.test("primary");
  assert.deepEqual(calls.slice(beforeEnvTest), [{ action: "test", id: "primary" }], "environment row always tests exactly the saved ID, never draft values or a save");
  assert.equal(ui.saved.value.revision, 7); assert.equal(ui.form.value.api_key, "unrelated-draft-key");
  assert.equal(ui.dirty.value, true); assert.equal(ui.dialogOpen.value, true);

  for (const source of ["db", "env"]) {
    for (const cap of [2147483648, Number.MAX_SAFE_INTEGER]) {
      reset(ui);
      response.items[0].config.max_tokens.value = cap;
      if (source === "env") environment(response.items[0]);
      ui.hydrate(structuredClone(response));
      const before = calls.length;
      if (source === "env") {
        await ui.test("primary");
        assert.deepEqual(calls.slice(before), [{ action: "test", id: "primary" }], "environment legacy cap tests saved config without an editor");
        continue;
      }
      ui.edit("primary"); await ui.testCurrent();
      assert.deepEqual(calls.slice(before), [{ action: "test", id: "primary", draft: { expected_revision: 7, config: {} } }], "unchanged DB legacy cap tests without saving or patching the cap");
      assert.equal(ui.form.value.max_tokens, String(cap)); assert.equal(ui.saved.value.revision, 7);
      for (const changed of ["2147483649", ` ${cap} `, String(Number.MAX_SAFE_INTEGER + 1)]) {
        ui.form.value.max_tokens = changed;
        const beforeInvalid = calls.length; await ui.testCurrent();
        assert.equal(calls.length, beforeInvalid); assert.match(ui.formError.value, /新增或修改.*2147483647/);
        await ui.save(true); assert.equal(calls.length, beforeInvalid, "changed oversized caps cannot be saved");
      }
      ui.form.value.max_tokens = String(cap);
      ui.form.value.name = "metadata only"; await ui.save(true);
      assert.equal(calls.at(-1).action, "save");
      assert.equal(calls.at(-1).payload.items[0].config, undefined, "unchanged legacy cap is also omitted from saves");
    }
  }

  for (const [key, value] of [["base_url", "https://example.com/v1?x=1"], ["model", " "], ["model", "a".repeat(257)], ["max_tokens", "-1"], ["max_tokens", "2147483648"]]) {
    reset(ui); ui.edit(extraId); ui.form.value[key] = value;
    const before = calls.length; await ui.testCurrent();
    assert.equal(calls.length, before); assert.ok(ui.formError.value);
  }
  reset(ui); ui.items.value[1].config.api_key.configured = false; ui.edit(extraId);
  const beforeMissingKey = calls.length; await ui.testCurrent();
  assert.equal(calls.length, beforeMissingKey); assert.match(ui.formError.value, /API Key/);

  const originalProbeFetch = globalThis.fetch;
  const providerRequests = [];
  globalThis.fetch = async (url, options) => {
    providerRequests.push({ url, options });
    return new Response(JSON.stringify({ choices: [{ message: { content: "OK" }, finish_reason: "stop" }] }));
  };
  try {
    for (const cap of [2147483648, Number.MAX_SAFE_INTEGER]) {
      reset(ui);
      ui.items.value[0].config.max_tokens.value = cap;
      ui.edit("primary"); ui.form.value.api_key = "current-key-with-legacy-cap";
      const before = calls.length; const beforeProvider = providerRequests.length;
      await ui.testCurrent();
      assert.equal(calls.length, before, "typed key and legacy cap never go to SiteFlow");
      assert.equal(providerRequests.length, beforeProvider + 1);
      assert.equal(JSON.parse(providerRequests.at(-1).options.body).max_tokens, cap);
      assert.match(ui.notice.value.message, /浏览器直连.*请求成功/);
      assert.equal(ui.form.value.api_key, "current-key-with-legacy-cap");
      ui.form.value.max_tokens = "2147483649"; await ui.testCurrent();
      assert.equal(providerRequests.length, beforeProvider + 1, "modified oversized cap cannot reach browser transport");
      assert.equal(calls.length, before);
    }
    for (const cap of [2147483648, Number.MAX_SAFE_INTEGER]) {
      reset(ui); ui.edit();
      Object.assign(ui.form.value, { base_url: "https://new.example/v1", model: "new-model", api_key: "new-key", max_tokens: String(cap) });
      const before = calls.length; const beforeProvider = providerRequests.length;
      await ui.testCurrent();
      assert.equal(calls.length, before); assert.equal(providerRequests.length, beforeProvider);
      assert.match(ui.formError.value, /新增或修改.*2147483647/);
    }
    for (const existing of [false, true]) {
      reset(ui); ui.edit(existing ? extraId : undefined);
      Object.assign(ui.form.value, { name: "", base_url: "https://draft.example/v1", model: "current-model", api_key: "only-provider-key", enabled: false, usages: [], supports_vision: false, max_tokens: "0" });
      const before = calls.length; const confirms = confirmations.length;
      const beforeForm = JSON.stringify(ui.form.value);
      await ui.testCurrent();
      assert.equal(calls.length, before, "typed key never goes to SiteFlow, including for a new model");
      assert.equal(confirmations.length, confirms, "browser explicit button requires no saved-test confirmation");
      assert.equal(JSON.stringify(ui.form.value), beforeForm);
      assert.equal(providerRequests.at(-1).url, "https://draft.example/v1/chat/completions");
      assert.equal(JSON.parse(providerRequests.at(-1).options.body).messages[0].content, "Reply with the single word OK.");
      assert.match(ui.notice.value.message, /浏览器直连.*不保存.*文本请求成功/);
      ui.form.value.supports_vision = true; ui.form.value.max_tokens = "32";
      await ui.testCurrent();
      const body = JSON.parse(providerRequests.at(-1).options.body);
      assert.equal(body.max_tokens, 32); assert.equal(body.messages[0].content[1].type, "image_url");
      assert.match(ui.notice.value.message, /视觉图片请求成功/);
    }
    globalThis.fetch = async () => { throw new TypeError("only-provider-key private endpoint"); };
    const beforeFailure = calls.length; await ui.testCurrent();
    assert.equal(calls.length, beforeFailure, "CORS/network failure never falls back to backend");
    assert.match(ui.notice.value.message, /CORS.*混合内容.*网络/);
    assert.doesNotMatch(ui.notice.value.message, /only-provider-key|private endpoint/);
    assert.equal(ui.conflict.value, false); assert.equal(ui.form.value.api_key, "only-provider-key");

    reset(ui); ui.edit(extraId); failure = new ApiError(409, "private-server-error", "model_registry_conflict");
    await ui.testCurrent(); assert.equal(ui.conflict.value, true);
    assert.match(ui.notice.value.message, /刷新/); assert.doesNotMatch(ui.notice.value.message, /private-server-error/);
    reset(ui); ui.edit(extraId); failure = new ApiError(500, "private-server-error");
    await ui.testCurrent(); assert.equal(ui.conflict.value, false);
    assert.doesNotMatch(ui.notice.value.message, /private-server-error/);

    reset(ui); const cancelled = await mount(); cancelled.ui.edit(extraId);
    cancelled.ui.form.value.api_key = "abort-on-unmount";
    let resolveProbe; let probeSignal;
    globalThis.fetch = (_url, options) => { probeSignal = options.signal; return new Promise(resolve => { resolveProbe = resolve; }); };
    const testing = cancelled.ui.testCurrent();
    const inFlightCount = calls.length;
    await cancelled.ui.testCurrent(); await cancelled.ui.save(true); await cancelled.ui.load();
    assert.equal(calls.length, inFlightCount, "testing is single-flight");
    cancelled.app.unmount(); assert.equal(probeSignal.aborted, true);
    resolveProbe(new Response(JSON.stringify({ choices: [{ message: { content: "OK" } }] })));
    await testing;
    assert.equal(cancelled.ui.notice.value, null, "late probe cannot publish after unmount");
    assert.equal(cancelled.ui.form.value.api_key, "");

    reset(ui); ui.edit(extraId); ui.form.value.api_key = "abort-on-close";
    const closingTest = ui.testCurrent();
    ui.closeEditor(); assert.equal(probeSignal.aborted, true);
    ui.edit(extraId); const nextEditorNotice = ui.notice.value;
    resolveProbe(new Response(JSON.stringify({ choices: [{ message: { content: "OK" } }] })));
    await closingTest;
    assert.equal(ui.notice.value, nextEditorNotice, "late result cannot publish into a reopened editor");
    assert.equal(ui.busy.value, false);
  } finally { globalThis.fetch = originalProbeFetch; }

  reset(ui); ui.move(extraId, -1); ui.edit(extraId); ui.form.value.api_key = "preserved-secret";
  failure = new ApiError(409, undefined, "model_registry_conflict");
  const beforeConflict = calls.length; await ui.save(true);
  assert.equal(calls.length, beforeConflict + 1, "no automatic retry or refresh after conflict");
  assert.equal(ui.saved.value.revision, 7); assert.equal(ui.conflict.value, true);
  assert.equal(ui.form.value.api_key, "preserved-secret"); assert.equal(ui.items.value[0].id, extraId);
  await ui.save(true); assert.equal(calls.length, beforeConflict + 1, "stale CAS cannot be retried");
  failure = null; confirmAllowed = false; await ui.load();
  assert.equal(calls.length, beforeConflict + 1); assert.equal(ui.form.value.api_key, "preserved-secret");
  confirmAllowed = true; failure = new Error("raw-secret-should-never-render"); await ui.load();
  assert.equal(ui.form.value.api_key, "preserved-secret", "failed refresh also preserves draft");
  failure = null; response.revision = 10; await ui.load();
  assert.equal(ui.saved.value.revision, 10); assert.equal(ui.conflict.value, false);
  assert.equal(ui.form.value.api_key, ""); assert.equal(ui.dialogOpen.value, false);
  for (const [status, code] of [[409, "model_config_locked"], [422, "invalid_model_registry"], [503, "model_registry_unavailable"], [403, undefined], [500, "unknown_code"]]) {
    reset(ui); ui.edit(extraId); ui.form.value.api_key = "keep-on-error"; failure = new ApiError(status, undefined, code);
    await ui.save(true); assert.equal(ui.form.value.api_key, "keep-on-error");
    assert.equal(ui.busy.value, false); assert.doesNotMatch(ui.notice.value.message, /raw-secret/);
  }
  reset(ui); ui.edit(extraId); ui.form.value.api_key = "erase-on-unauthorized";
  failure = new ApiError(401); await ui.test(extraId);
  assert.equal(events.at(-1)[0], "unauthorized"); assert.equal(ui.form.value.api_key, "");

  for (const error of [new Error("raw-secret-network"), new ApiError(500), new ApiError(408)]) {
    reset(ui); ui.edit(extraId); ui.form.value.api_key = "preserve-on-unknown-save";
    failure = error;
    await ui.save(true);
    assert.equal(ui.conflict.value, true); assert.match(ui.notice.value.message, /保存可能已完成.*刷新/);
    assert.equal(ui.form.value.api_key, "preserve-on-unknown-save");
    const count = calls.length; await ui.save(true);
    assert.equal(calls.length, count, "uncertain writes cannot be blindly retried");
    failure = null; await ui.load(); assert.equal(ui.conflict.value, false);
  }
  reset(ui); failure = new Error("readonly-network-failure");
  await ui.test(extraId); assert.equal(ui.conflict.value, false, "read-only tests do not block writes on network failure");
  await ui.load(); assert.equal(ui.conflict.value, false, "ordinary GET failure does not create an unknown-write state");

  failure = null; response = registry();
  let resolve;
  pending = new Promise(done => { resolve = done; });
  const late = await mount(); assert.equal(late.ui.busy.value, true);
  late.app.unmount(); resolve(registry()); await new Promise(done => setImmediate(done));
  assert.equal(late.ui.saved.value, null, "late GET cannot hydrate unmounted card");
  pending = null; reset(ui); ui.edit(extraId); ui.form.value.api_key = "erase-on-unmount";
  pending = new Promise(done => { resolve = done; });
  const saving = ui.save(true); const countDuringSave = calls.length;
  await ui.save(true); await ui.load(); await ui.test(extraId);
  assert.equal(calls.length, countDuringSave, "single-flight prevents save/load/test races");
  apps[0].unmount(); assert.equal(ui.form.value.api_key, "");
  resolve(registry()); await saving; assert.equal(ui.notice.value, null, "late save ignored");
  pending = null;

  const html = await renderCard(api => {
    api.items.value[0].name = '<img src=x onerror="alert(1)">';
    api.items.value[0].config.model.value = "<script>evil()</script>";
  });
  assert.match(html, /&lt;img/); assert.match(html, /&lt;script&gt;/);
  assert.doesNotMatch(html, /<img|<script|never-hydrate|raw-secret/);
  assert.match(html, /已配置/); assert.doesNotMatch(html, /测试成功|连接健康<\/span>/);
  assert.match(html, /上移/); assert.match(html, /MCP/); assert.doesNotMatch(html, /预留|主要|清除/);
  assert.match(await renderCard(), /默认模型/);
  assert.match(html, /视觉/);
  assert.match(await renderCard(api => { api.items.value[1].config.supports_vision.value = false; }), /仅文本/);
  const dialogHtml = await renderCard(api => {
    api.items.value[0].config.max_tokens.value = 0;
    api.edit("primary");
    assert.equal(api.form.value.api_key, ""); assert.equal(api.form.value.max_tokens, "0");
  });
  assert.match(dialogHtml, /max_tokens/);
  assert.doesNotMatch(dialogHtml, /<input(?=[^>]*aria-label="max_tokens")(?=[^>]*disabled)[^>]*>/);
  assert.doesNotMatch(dialogHtml, /never-hydrate|环境变量锁定|主要|清除/);
  const modalHtml = dialogHtml.slice(dialogHtml.indexOf('<section role="dialog"'));
  assert.deepEqual([...modalHtml.matchAll(/<p class="hint"[^>]*>(.*?)<\/p>/g)].map(match => match[1]),
    ["保存前请测试连通性，测试不代表保存配置"], "only the requested instruction remains in the modal");
  assert.doesNotMatch(modalHtml, /数据库|默认 \/ 未配置|浏览器直连|思考模型|尚未配置密钥|环境变量/);
  const envHtml = await renderCard(api => {
    environment(api.items.value[0]); api.items.value[0].name = "自定义环境模型";
    api.edit("primary"); assert.equal(api.dialogOpen.value, false);
  });
  assert.match(envHtml, /自定义环境模型/); assert.match(envHtml, /环境变量/);
  assert.doesNotMatch(envHtml, /role="dialog"|主要|清除/);
  const envRow = envHtml.slice(envHtml.indexOf('role="listitem"'), envHtml.indexOf('role="listitem"', envHtml.indexOf('role="listitem"') + 1));
  const rowButtons = [...envRow.matchAll(/<button\b[^>]*>[\s\S]*?<\/button>/g)].map(match => match[0]);
  assert.match(rowButtons.find(button => button.includes("配置")), /\sdisabled(?:\s|>)/);
  assert.doesNotMatch(rowButtons.find(button => button.includes("测试")), /\sdisabled(?:\s|>)/);
  assert.doesNotMatch(envRow, /删除/);
  assert.doesNotMatch(rowButtons.find(button => button.includes("下移")), /\sdisabled(?:\s|>)/);
  assert.match(envRow, /role="switch"/); assert.doesNotMatch(envRow, /is-disabled[^>]*role="switch"/);
  for (const [dbCount, withEnv] of [[20, false], [20, true], [19, true]]) {
    const capacityHtml = await renderCard(api => {
      api.hydrate(capacityRegistry(dbCount, withEnv));
      if (withEnv) { api.edit("primary"); assert.equal(api.dialogOpen.value, false); }
    });
    assert.equal([...capacityHtml.matchAll(/role="listitem"/g)].length, dbCount + Number(withEnv));
    assert.match(capacityHtml, new RegExp(`模型 ${dbCount + Number(withEnv)}`));
    assert.doesNotMatch(capacityHtml, /\/ 20/);
    const addButton = [...capacityHtml.matchAll(/<button\b[^>]*>[\s\S]*?<\/button>/g)].map(match => match[0]).find(button => button.includes("添加模型"));
    assert.equal(/\sdisabled(?:\s|>)/.test(addButton), dbCount === 20, "add button depends only on DB count");
    if (withEnv) {
      const firstRow = capacityHtml.slice(capacityHtml.indexOf('role="listitem"'), capacityHtml.indexOf('role="listitem"', capacityHtml.indexOf('role="listitem"') + 1));
      const configure = [...firstRow.matchAll(/<button\b[^>]*>[\s\S]*?<\/button>/g)].map(match => match[0]).find(button => button.includes("配置"));
      assert.match(configure, /\sdisabled(?:\s|>)/);
      assert.match(firstRow, /环境变量/); assert.doesNotMatch(firstRow, /删除/);
    }
  }
  const dirtyHtml = await renderCard(api => { api.move(extraId, -1); });
  assert.match(dirtyHtml, /有未保存的列表更改。/); assert.doesNotMatch(dirtyHtml, /主要模型/);
  const invalidHtml = await renderCard(api => {
    api.items.value[0].config.max_tokens = { value: null, source: "db", editable: true, error: "<script>unsafe-config-error</script>" };
    api.items.value[0].config.base_url.error = "<img src=x>";
    api.items.value[0].config.available = false;
    api.edit("primary");
  });
  assert.match(invalidHtml, /此字段的已保存配置无效/); assert.doesNotMatch(invalidHtml, /unsafe-config-error|<img/);
  const currentButton = [...invalidHtml.matchAll(/<button\b[^>]*>[\s\S]*?<\/button>/g)]
    .map(match => match[0]).find(button => button.includes("测试当前配置"));
  assert.ok(currentButton); assert.doesNotMatch(currentButton, /\sdisabled(?:\s|>)/);
  const newHtml = await renderCard(api => { api.edit(); });
  assert.match(newHtml, /测试当前配置/); assert.match(newHtml, /保存前请测试连通性，测试不代表保存配置/);
  const beforeSsr = calls.length;
  const resultHtml = await renderCard(api => {
    api.edit(extraId);
    api.notice.value = { ok: true, message: "已保存配置测试成功" };
  });
  assert.match(resultHtml, /role="dialog"[\s\S]*已保存配置测试成功/);
  assert.equal(calls.length, beforeSsr, "SSR never reads, saves or tests models");

  const source = await readFile(new URL("../src/components/ModelSettingsCard.vue", import.meta.url), "utf8");
  assert.doesNotMatch(source, /v-html|dangerouslyUseHTMLString|localStorage|sessionStorage|console\./);
  assert.doesNotMatch(source, /['"]primary['"]\s*\)|(?:editingId|item\.id)\s*===\s*['"]primary['"]|clearPrimary|primaryClearReason|clearVisionSettings|sourceLabel|预留/);
  assert.equal((source.match(/await testModel\(/g) ?? []).length, 2);
  const settings = await readFile(new URL("../src/views/SettingsView.vue", import.meta.url), "utf8");
  assert.match(settings, /<el-col :xs="24" :lg="24">\s*<ModelSettingsCard @unauthorized="handleUnauthorized"/);
  assert.doesNotMatch(settings, /VisionSettingsCard/);

  const invalidViews = [null, [], {}, "private-response-secret"];
  for (const revision of [-1, 1.5, "8", null, true, Infinity, Number.MAX_SAFE_INTEGER + 1]) {
    invalidViews.push({ ...registry(), revision });
  }
  for (const mutate of [
    view => { view.private_key = "private-response-secret"; },
    view => { view.items = Array.from({ length: 21 }, (_, index) => ({ ...view.items[0], id: index === 0 ? "primary" : randomUUID() })); },
    view => { view.items = capacityRegistry(21, true).items; },
    view => { environment(view.items[1]); },
    view => { environment(view.items[0]); environment(view.items[1]); },
    view => { view.items[1].id = "primary"; },
    view => { delete view.items[0].source; },
    view => { delete view.items[0].config_editable; },
    view => { view.items[0].source = "auto"; },
    view => { view.items[0].config_editable = "true"; },
    view => { view.items[0].config_editable = false; },
    view => { view.items[0].source = "env"; },
    view => { environment(view.items[0]); view.items[0].config_editable = true; },
    ...["base_url", "model", "api_key", "supports_vision", "max_tokens"].flatMap(key => [
      view => { environment(view.items[0]); view.items[0].config[key].editable = true; },
      view => { view.items[0].config[key].editable = false; },
      view => { view.items[0].config[key].source = "env"; },
    ]),
    view => { view.items.push(structuredClone(view.items[1])); },
    view => { view.items[1].id = "00000000-0000-1000-8000-000000000000"; },
    view => { view.items[1].id = "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"; },
    view => { view.items[0].name = " "; },
    view => { view.items[0].name = "a".repeat(129); },
    view => { view.items[0].name = null; },
    view => { view.items[0].enabled = 1; },
    view => { view.items[0].usages = ["mcp", "mcp"]; },
    view => { view.items[0].usages = ["unknown"]; },
    view => { view.items[0].api_key = "private-response-secret"; },
    view => { view.items[0].config.api_key = "private-response-secret"; },
    view => { view.items[0].config.api_key.value = "private-response-secret"; },
    view => { view.items[0].config.api_key.raw = "private-response-secret"; },
    view => { view.items[0].config.api_key.masked = "private-response-secret"; },
    view => { view.items[0].config.api_key.masked = "****" + "x".repeat(125); },
    view => { view.items[0].config.api_key.configured = "true"; },
    view => { view.items[0].config.api_key.editable = null; },
    view => { view.items[0].config.secret = "private-response-secret"; },
    view => { delete view.items[0].config.model; },
    view => { view.items[0].config.model.value = 7; },
    view => { view.items[0].config.model.value = "a".repeat(257); },
    view => { view.items[0].config.base_url.value = "a".repeat(2049); },
    view => { view.items[0].config.base_url.source = "private-response-secret"; },
    view => { view.items[0].config.base_url.editable = "false"; },
    view => { view.items[0].config.base_url.error = {}; },
    view => { view.items[0].config.base_url.error = "a".repeat(2049); },
    view => { view.items[0].config.supports_vision.value = "false"; },
    view => { view.items[0].config.supports_vision.raw = "private-response-secret"; },
    view => { view.items[0].config.supports_vision.source = "invalid"; },
    view => { view.items[0].config.available = 1; },
    view => { view.items[0].config.max_tokens.value = -1; },
    view => { view.items[0].config.max_tokens.value = 1.5; },
    view => { view.items[0].config.max_tokens.value = Number.MAX_SAFE_INTEGER + 1; },
    view => { view.items[0].config.max_tokens.value = "0"; },
    view => { view.items[0].config.max_tokens.error = []; },
    view => { view.items[0].config.max_tokens.source = "invalid"; },
  ]) {
    const view = registry(); mutate(view); invalidViews.push(view);
  }
  for (const view of invalidViews) assert.equal(isModelRegistryView(view), false);
  for (const cap of [null, 0, 2147483648, Number.MAX_SAFE_INTEGER]) {
    const view = registry(); view.items[0].config.max_tokens.value = cap;
    view.items[0].config.supports_vision = { value: false, source: "db", editable: true };
    view.items[0].config.api_key.masked = null;
    view.items[0].config.base_url.value = null;
    view.items[0].config.base_url.error = "invalid saved URL";
    assert.equal(isModelRegistryView(view), true, "nullable repair views and legacy safe-integer caps remain readable");
  }
  const fullView = registry(); fullView.revision = Number.MAX_SAFE_INTEGER;
  while (fullView.items.length < 20) fullView.items.push({ ...fullView.items[1], id: randomUUID() });
  assert.equal(isModelRegistryView(fullView), true);
  const fullEnvView = capacityRegistry(20, true);
  assert.equal(isModelRegistryView(fullEnvView), true, "20 DB plus one legacy env row is valid");
  assert.equal(isModelRegistryView({ revision: 0, items: [] }), true, "empty registries are valid");
  const noPrimary = registry(); noPrimary.items.shift();
  assert.equal(isModelRegistryView(noPrimary), true, "no mandatory primary ID");
  const envView = registry(); environment(envView.items[0]);
  envView.items[0].config.model.source = "db"; envView.items[0].config.max_tokens.source = "unset";
  assert.equal(isModelRegistryView(envView), true, "an env row locks all fields, including fallback DB/unset values");

  const originalFetch = globalThis.fetch; const originalDocument = globalThis.document;
  const requests = []; let apiFailure = false;
  let apiResponse = registry();
  globalThis.document = { cookie: "siteflow_csrf=cookie-token" };
  globalThis.fetch = async (url, options) => {
    requests.push({ url, options });
    return apiFailure ? new Response(JSON.stringify({ detail: { code: "model_registry_conflict", message: "raw-secret" } }), { status: 409 })
      : new Response(JSON.stringify(apiResponse));
  };
  try {
    await client.getModelRegistry();
    assert.equal(requests.at(-1).url, "/api/v1/integrations/models");
    assert.equal(requests.at(-1).options.credentials, "include");
    const payload = { expected_revision: 7, items: [{ id: "primary", name: "主要", enabled: false, usages: [], config: { supports_vision: false, max_tokens: null } }] };
    await client.saveModelRegistry(payload);
    assert.equal(requests.at(-1).options.method, "PUT");
    assert.deepEqual(JSON.parse(requests.at(-1).options.body), payload);
    assert.equal(requests.at(-1).options.headers.get("X-CSRF-Token"), "cookie-token");
    client.setCsrfToken("auth-token"); await client.testModel(extraId);
    assert.equal(requests.at(-1).url, `/api/v1/integrations/models/${extraId}/test`);
    assert.equal(requests.at(-1).options.method, "POST");
    assert.equal(requests.at(-1).options.body, undefined);
    assert.equal(requests.at(-1).options.headers.get("X-CSRF-Token"), "auth-token");
    const draft = { expected_revision: 7, config: { model: "draft", supports_vision: false, max_tokens: null } };
    await client.testModel(extraId, draft);
    assert.deepEqual(JSON.parse(requests.at(-1).options.body), draft);
    assert.equal(requests.at(-1).options.headers.get("X-CSRF-Token"), "auth-token");
    await client.clearVisionSettings();
    assert.equal(requests.at(-1).url, "/api/v1/integrations/vision");
    assert.equal(requests.at(-1).options.method, "DELETE");
    assert.equal(requests.at(-1).options.body, undefined);
    assert.equal(requests.at(-1).options.headers.get("X-CSRF-Token"), "auth-token");
    await client.testModel("id/with space"); assert.match(requests.at(-1).url, /id%2Fwith%20space\/test$/);
    apiFailure = true;
    await assert.rejects(client.saveModelRegistry(payload), error => error instanceof client.ApiError && error.status === 409 &&
      error.code === "model_registry_conflict" && !error.message.includes("raw-secret"));
    apiFailure = false;
    for (const view of invalidViews) {
      apiResponse = view;
      for (const operation of [() => client.getModelRegistry(), () => client.saveModelRegistry(payload)]) {
        await assert.rejects(operation(), error => error instanceof ApiError && error.status === 502 &&
          error.code === "invalid_model_registry_response" && !error.message.includes("private-response-secret"));
      }
    }
    // Exercise the actual validating API functions through the live component harness.
    boundaryClient = client; apiResponse = fullEnvView;
    const capacityMounted = await mount(); const capacityUi = capacityMounted.ui;
    assert.equal(capacityUi.saved.value.items.length, 21, "actual API validator accepts the full env registry on initial load");
    assert.equal(capacityUi.notice.value, null);
    const deletedId = capacityUi.items.value.at(-1).id;
    const preservedMetadata = fullEnvView.items.filter(item => item.id !== deletedId).map(({ id, name, enabled, usages }) => ({ id, name, enabled, usages }));
    await capacityUi.remove(deletedId);
    apiResponse = { revision: 8, items: fullEnvView.items.filter(item => item.id !== deletedId) };
    await capacityUi.save();
    assert.deepEqual(JSON.parse(requests.at(-1).options.body).items, preservedMetadata, "API-bound delete save sends every surviving row, including env");
    assert.equal(capacityUi.saved.value.items.length, 20); assert.equal(capacityUi.dirty.value, false);
    assert.equal(capacityUi.saved.value.items[0].source, "env");
    capacityMounted.app.unmount();
    boundaryClient = client; apiResponse = registry();
    const guarded = await mount(); const guard = guarded.ui;
    for (const view of invalidViews) {
      reset(guard); guard.move(extraId, -1); guard.edit(extraId);
      guard.form.value.api_key = "keep-secret-draft"; guard.form.value.name = "keep-name-draft";
      const previousSaved = guard.saved.value;
      const previousItems = JSON.stringify(guard.items.value);
      apiResponse = view;
      const beforeBadGet = requests.length;
      await guard.load();
      assert.equal(requests.length, beforeBadGet + 1, "malformed GET makes no follow-up requests");
      assert.equal(guard.saved.value, previousSaved); assert.equal(JSON.stringify(guard.items.value), previousItems);
      assert.equal(guard.form.value.api_key, "keep-secret-draft"); assert.equal(guard.dialogOpen.value, true);
      assert.equal(guard.conflict.value, false, "malformed GET alone is not an uncertain mutation");
      assert.match(guard.notice.value.message, /响应格式无效/);
      const beforeBadPut = requests.length;
      await guard.save(true);
      assert.equal(requests.length, beforeBadPut + 1, "malformed acknowledgement triggers no retry or automatic GET");
      assert.equal(guard.saved.value, previousSaved); assert.equal(JSON.stringify(guard.items.value), previousItems);
      assert.equal(guard.form.value.api_key, "keep-secret-draft"); assert.equal(guard.form.value.name, "keep-name-draft");
      assert.equal(guard.conflict.value, true); assert.match(guard.notice.value.message, /保存可能已完成.*刷新/);
      assert.doesNotMatch(JSON.stringify(guard.notice.value), /private-response-secret/);
      await guard.save(true); assert.equal(requests.length, beforeBadPut + 1);
      await guard.load(); assert.equal(guard.conflict.value, true, "another malformed GET cannot unblock writes");
      apiResponse = registry(); apiResponse.revision = 12; await guard.load();
      assert.equal(guard.saved.value.revision, 12); assert.equal(guard.conflict.value, false);
      assert.equal(guard.form.value.api_key, "");
    }
    apiResponse = { ...registry(), private_key: "private-response-secret" };
    const invalidInitial = await mount();
    assert.equal(invalidInitial.ui.saved.value, null); assert.deepEqual(invalidInitial.ui.items.value, []);
    assert.equal(invalidInitial.ui.busy.value, false);
    const safeHtml = await renderCard(api => { api.notice.value = invalidInitial.ui.notice.value; });
    assert.match(safeHtml, /响应格式无效/); assert.doesNotMatch(safeHtml, /private-response-secret|keep-secret-draft/);
    const beforeStaleSaved = guard.saved.value;
    const beforeStaleNotice = guard.notice.value;
    let finishResponse;
    globalThis.fetch = () => new Promise(resolve => { finishResponse = resolve; });
    const staleLoad = guard.load();
    guarded.app.unmount();
    finishResponse(new Response(JSON.stringify({ ...registry(), private_key: "private-response-secret" })));
    await staleLoad;
    assert.equal(guard.saved.value, beforeStaleSaved, "stale malformed response cannot replace saved state after unmount");
    assert.equal(guard.notice.value, beforeStaleNotice, "stale validation errors cannot update unmounted state");
    boundaryClient = null;
  } finally {
    client.setCsrfToken(null); globalThis.fetch = originalFetch;
    if (originalDocument === undefined) delete globalThis.document; else globalThis.document = originalDocument;
  }
  console.log("Model settings regression passed");
} finally {
  for (const app of apps) app.unmount();
  delete globalThis.__modelSettingsMocks;
  await server.close();
}
