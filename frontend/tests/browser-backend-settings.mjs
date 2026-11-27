import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { createServer } from "vite";
import { createRenderer, createSSRApp, ssrContextKey } from "vue";
import { renderToString } from "vue/server-renderer";
import ElementPlus, { ID_INJECTION_KEY, ZINDEX_INJECTION_KEY } from "element-plus";

const source = await readFile(new URL("../src/components/BrowserBackendSettingsCard.vue", import.meta.url), "utf8");
const page = await readFile(new URL("../src/views/BrowserView.vue", import.meta.url), "utf8");
const calls = [];
const confirmations = [];
let confirm = async () => {};
const apps = [];
const originalFetch = globalThis.fetch;
const originalDocument = globalThis.document;
const originalSetTimeout = globalThis.setTimeout;
let seed;
let handler;
let timerCallback;
let timerDelay;
const view = () => ({
  backend: "cloak-manager", connected: true,
  license: { configured: true, source: "manager", editable: true },
  license_tier: "free", binary_version: "156.0.8078.47.1", error: null,
});
const envView = () => ({ ...view(), license: { configured: true, source: "env", editable: false } });
const clearedView = (tier = "none", version = view().binary_version) => ({
  ...view(), license: { configured: false, source: "unset", editable: true },
  license_tier: tier, binary_version: version,
});
const lockedView = (source = "manager", error = "private-unknown-write-error") => ({
  ...view(), license: { configured: source === "manager", source, editable: false }, error,
});
const unsupported = () => ({
  backend: "unsupported", connected: false,
  license: { configured: false, source: "unknown", editable: false },
  license_tier: "unknown", binary_version: null, error: null,
});
const json = (value, status = 200) => new Response(JSON.stringify(value), { status });
const settle = () => new Promise(resolve => setImmediate(resolve));
globalThis.document = { cookie: "" };
globalThis.fetch = async (url, options) => {
  calls.push({ url, method: options.method, body: options.body, signal: options.signal });
  return handler ? handler(url, options) : json(view());
};
globalThis.__browserSettingsTest = {
  seed: api => seed?.(api),
  ElMessageBox: { confirm(...args) { confirmations.push(args); return confirm(); } },
};
const server = await createServer({
  optimizeDeps: { noDiscovery: true, include: [] }, server: { middlewareMode: true, hmr: false },
  plugins: [{ name: "browser-settings-harness", enforce: "pre", transform(code, id) {
    if (!id.endsWith("/BrowserBackendSettingsCard.vue")) return;
    return code.replace('import { ElMessageBox } from "element-plus";',
      'const { ElMessageBox } = globalThis.__browserSettingsTest;')
      .replace("</script>", `const exposed = { settings, draft, busy, confirming, refreshRequired, notice, inputDisabled, canSave, canClear, licenseWarning, versionHint, tierLabel, run };
      defineExpose(exposed); globalThis.__browserSettingsTest.seed(exposed);\n</script>`);
  } }],
});
const renderer = createRenderer({ createComment: () => ({}), insert() {}, remove() {}, parentNode() {}, nextSibling() {} });
try {
  const { isBrowserSettings, compareBrowserVersion, KEYLESS_VERSION_REFERENCE } = await server.ssrLoadModule("/src/types/browser-settings.ts");
  const { default: Card } = await server.ssrLoadModule("/src/components/BrowserBackendSettingsCard.vue");
  const mount = async () => {
    let ui;
    const events = [];
    const app = renderer.createApp({ setup() {
      Card.setup({}, { expose(value) { ui = value; }, emit(...args) { events.push(args); } });
      return () => null;
    } });
    app.provide(ssrContextKey, { modules: new Set() }); app.mount({}); apps.push(app);
    await settle();
    return { ui, events, app };
  };
  const render = async (value, customize) => {
    seed = api => {
      api.settings.value = value;
      api.refreshRequired.value = !value;
      customize?.(api);
    };
    try {
      return await renderToString(createSSRApp(Card).use(ElementPlus)
        .provide(ID_INJECTION_KEY, { prefix: 0, current: 0 }).provide(ZINDEX_INJECTION_KEY, { current: 0 }));
    } finally { seed = undefined; }
  };

  assert.match(page, /<\/PageHeader>\s*<BrowserBackendSettingsCard @unauthorized="handleUnauthorized"\s*\/>\s*<el-card[^>]*v-loading="lanesLoading"/);
  assert.doesNotMatch(source, /setInterval|localStorage|sessionStorage|console\.|v-html|show-password|clearable/);
  assert.equal((source.match(/apiRequest<unknown>\(/g) ?? []).length, 1);
  assert.doesNotMatch(source, /\/start|\/test|\/jobs|\/lane-settings/);
  assert.equal(KEYLESS_VERSION_REFERENCE, "146.0.7680.177.5");
  for (const [version, expected] of [
    ["146.0.7680.177.5", 0], ["146.0.7680.177", -1], ["146.0.7680.177.10", 1],
    ["146.0.7680.177.4", -1], ["146.0.7680.178", 1], ["146.0.7680.99.99", -1],
    ["145.999999.9999.9999.9999", -1], ["147.0.0.0", 1],
    ["0146.00.07680.0177.05", 0],
    ["146.0.7680.177." + "9".repeat(40), 1],
    ["146.0.7680.177." + "9".repeat(64 - "146.0.7680.177.".length), 1],
    ["9999999999999999999999999999999999999999999999999999999999.0.0.0", 1],
    ["146.0.7680.177." + "9".repeat(60), null],
    [null, null], [undefined, null], [146, null], ["", null], ["146.0.7680", null],
    ["146.0.7680.177.5.0", null], ["146.0.7680.177.5-beta", null],
    ["146.0.7680.177.5\n", null], [" 146.0.7680.177.5", null],
    ["146.0.7680.-177.5", null], ["146.0.7680.177.x", null],
  ]) assert.equal(compareBrowserVersion(version), expected, String(version));

  for (const valid of [view(), envView(), unsupported(),
    lockedView(), lockedView("unset"), lockedView("manager", ""),
    { ...envView(), error: "private-env-error" },
    { ...view(), license: { configured: false, source: "unset", editable: true }, license_tier: "none", binary_version: null },
    { ...view(), connected: false, license: { configured: false, source: "unknown", editable: false }, license_tier: "unknown", binary_version: null },
    { ...envView(), connected: false },
    { ...view(), license_tier: "pro" }, { ...view(), license_tier: "keyless" },
  ]) assert.equal(isBrowserSettings(valid), true);
  const invalid = [null, [], {}, "secret-response"];
  for (const mutate of [
    v => { v.backend = "secret-response"; },
    v => { v.connected = "true"; },
    v => { v.license.configured = "true"; },
    v => { v.license.editable = 1; },
    v => { v.license.source = "db"; },
    v => { v.license.source = "env"; },
    v => { v.license.source = "unset"; },
    v => { v.license.source = "unknown"; },
    v => { v.license.configured = false; },
    v => { v.license.editable = false; },
    v => { v.connected = false; },
    v => { v.license.key = "secret-response"; },
    v => { v.license.masked = "****secret-response"; },
    v => { v.license_key = "secret-response"; },
    v => { v.license_tier = "enterprise"; },
    v => { v.error = {}; },
    v => { v.error = "private-error-with-editable-license"; },
    v => { v.error = ""; },
    v => { v.license.configured = false; v.license.source = "unset"; v.error = "pending-write"; },
    v => { v.license.source = "env"; v.error = "pending-write"; },
    v => { v.error = "x".repeat(2049); },
    v => { delete v.error; },
    v => { v.binary_version = "156.0.1\nsecret-response"; },
    v => { v.binary_version = "156.0.1\n"; },
    v => { v.binary_version = "156.0.1\r\n"; },
    v => { v.binary_version = "<img src=x>"; },
    v => { v.binary_version = "156.0.1 secret-response"; },
    v => { v.binary_version = "v156.0.1"; },
    v => { v.binary_version = ""; },
    v => { v.binary_version = 156; },
    v => { delete v.binary_version; },
    v => { v.binary_version = "1." + "1".repeat(100); },
  ]) { const value = view(); mutate(value); invalid.push(value); }
  for (const value of invalid) assert.equal(isBrowserSettings(value), false, JSON.stringify(value));

  const { ui, events } = await mount();
  assert.deepEqual(calls.map(({ url, method, body }) => ({ url, method, body })), [
    { url: "/api/v1/lanes/browser-settings", method: "GET", body: undefined },
  ], "mount reads only the fixed endpoint, with no mutations");
  assert.equal(ui.draft.value, "");
  assert.equal(ui.canSave.value, false);
  assert.equal(ui.canClear.value, true);
  assert.equal(ui.inputDisabled.value, false);
  const beforeEmpty = calls.length;
  const beforeBlankConfirm = confirmations.length;
  for (const blank of ["", "   ", "\t\n"]) { ui.draft.value = blank; await ui.run("save"); }
  assert.equal(calls.length, beforeEmpty, "empty keys never clear configuration");
  assert.equal(confirmations.length, beforeBlankConfirm, "blank save never opens clear confirmation");
  ui.draft.value = " unsaved-secret ";
  await ui.run("read");
  assert.equal(ui.draft.value, " unsaved-secret ", "read preserves a draft and never hydrates a key");
  await ui.run("save");
  assert.deepEqual(JSON.parse(calls.at(-1).body), { license_key: "unsaved-secret" });
  assert.equal(calls.at(-1).method, "PUT");
  assert.equal(ui.draft.value, "");
  assert.equal(ui.notice.value.type, "success");
  assert.doesNotMatch(ui.notice.value.message, /unsaved-secret/);

  handler = () => json({ ...view(), license_tier: "none" });
  ui.draft.value = "bad-license";
  await ui.run("save");
  assert.equal(ui.draft.value, "", "completed saves always clear the secret even if license is not yet effective");
  assert.equal(ui.notice.value.type, "warning");
  assert.match(ui.licenseWarning.value, /尚未确认有效许可/);
  handler = () => json(envView());
  ui.draft.value = "should-clear-on-lock";
  await ui.run("read");
  assert.equal(ui.draft.value, "");
  ui.draft.value = "programmatic-secret";
  const beforeLocked = calls.length;
  await ui.run("save");
  assert.equal(calls.length, beforeLocked, "runtime guard enforces environment lock");

  const lockedHtml = await render(envView());
  assert.match(lockedHtml, /CloakBrowser Manager/);
  assert.match(lockedHtml, /环境变量.*只读/);
  assert.match(lockedHtml, /CLOAKBROWSER_LICENSE_KEY/);
  assert.match(lockedHtml, /<input(?=[^>]*type="password")(?=[^>]*disabled)[^>]*>/);
  const saveButton = [...lockedHtml.matchAll(/<button\b[^>]*>[\s\S]*?<\/button>/g)].map(m => m[0]).find(v => v.includes("保存许可证"));
  assert.match(saveButton, /\sdisabled(?:\s|=|>)/);
  assert.match(lockedHtml, /免费版仅允许 1 个同时运行的 profile/);
  assert.match(lockedHtml, /多个标签页共享 profile/);
  assert.match(lockedHtml, /队列等待释放/);
  assert.doesNotMatch(lockedHtml, /SITEFLOW_BROWSER_MAX_ACTIVE_PROFILES|--force-recreate worker/);
  assert.doesNotMatch(lockedHtml, /不提供清除操作|可能超出免费许可容量/);
  const clearButton = html => [...html.matchAll(/<button\b[^>]*>[\s\S]*?<\/button>/g)]
    .map(m => m[0]).find(button => button.includes("清除许可证"));
  assert.match(clearButton(lockedHtml), /\sdisabled(?:\s|=|>)/);
  const unsupportedHtml = await render(unsupported());
  assert.doesNotMatch(unsupportedHtml, /CloakBrowser Manager|<form|<input|保存许可证/);
  assert.match(unsupportedHtml, /不支持/);
  const loadingHtml = await render(null, api => { api.busy.value = "read"; });
  assert.match(loadingHtml, /正在加载/);
  assert.doesNotMatch(loadingHtml, /<form/);
  const failureHtml = await render(null, api => { api.notice.value = { type: "error", message: "加载失败，请刷新设置" }; });
  assert.match(failureHtml, /加载失败/);
  assert.doesNotMatch(failureHtml, /<form/);
  const privateError = await render({ ...view(), error: "secret-response <script>" });
  assert.doesNotMatch(privateError, /secret-response|<script>/);
  assert.match(privateError, /Manager 报告异常/);
  assert.match(privateError, /<input(?=[^>]*type="password")(?=[^>]*disabled)[^>]*>/);
  for (const license of [view().license, { configured: false, source: "unset", editable: true }]) {
    const legacy = { ...view(), license, license_tier: "keyless", binary_version: KEYLESS_VERSION_REFERENCE };
    const legacyHtml = await render(legacy, api => {
      assert.equal(api.licenseWarning.value, "", "legacy keyless mode is not an invalid license");
    });
    assert.match(legacyHtml, /许可等级：无密钥/);
    assert.doesNotMatch(legacyHtml, /当前无有效许可|尚未确认有效许可/);
    handler = () => json(legacy); await ui.run("read");
    assert.equal(ui.licenseWarning.value, "");
  }
  for (const license_tier of ["free", "pro", "none"]) {
    const html = await render({ ...view(), license_tier, binary_version: "146.0.7680.177" }, api => {
      assert.match(api.versionHint.value, /许可等级优先/);
      assert.doesNotMatch(api.versionHint.value, /可能允许不使用密钥|可免费运行|无密钥模式的 Manager/);
    });
    assert.match(html, /不能仅凭旧版本推断可无密钥运行/);
  }
  for (const binary_version of [null, "146.0.7680", "146.0.7680.177.5.0"]) {
    await render({ ...view(), license_tier: "keyless", binary_version }, api => {
      assert.match(api.versionHint.value, /报告旧版无密钥模式.*版本未确认/);
      assert.match(api.versionHint.value, /不能据此保证可无密钥启动/);
      assert.doesNotMatch(api.tierLabel.value, /无密钥/);
    });
    for (const license_tier of ["free", "pro", "none", "unknown"]) {
      await render({ ...view(), license_tier, binary_version }, api => {
        if (license_tier === "free" || license_tier === "pro") {
          assert.match(api.versionHint.value, /版本未确认.*报告(免费版|专业版).*此许可模式需要有效 LICENSE_KEY/);
        } else if (license_tier === "none") {
          assert.match(api.versionHint.value, /版本未确认.*当前报告无有效许可.*需要配置有效 LICENSE_KEY/);
        } else assert.match(api.versionHint.value, /版本 \/ 许可模式未知/);
        assert.doesNotMatch(api.versionHint.value, /可能允许不使用密钥/);
      });
    }
  }
  await render({ ...view(), license_tier: "keyless" }, api => {
    assert.match(api.versionHint.value, /需要有效 LICENSE_KEY.*冲突/);
    assert.doesNotMatch(api.tierLabel.value, /无密钥/);
  });
  const disconnectedHtml = await render({ ...view(), connected: false, license: { ...view().license, editable: false } });
  assert.match(disconnectedHtml, /Manager 未连接/);
  assert.match(disconnectedHtml, /el-alert--info[\s\S]*此版本高于参考版本/);
  for (const locked of [lockedView(), lockedView("unset"), lockedView("manager", "")]) {
    const lockedManagerHtml = await render(locked);
    assert.match(lockedManagerHtml, /Manager 报告异常/);
    assert.doesNotMatch(lockedManagerHtml, /private-unknown-write-error/);
    assert.match(lockedManagerHtml, /<input(?=[^>]*type="password")(?=[^>]*disabled)[^>]*>/);
    const lockedSave = [...lockedManagerHtml.matchAll(/<button\b[^>]*>[\s\S]*?<\/button>/g)]
      .map(m => m[0]).find(button => button.includes("保存许可证"));
    assert.match(lockedSave, /\sdisabled(?:\s|=|>)/);
    assert.match(clearButton(lockedManagerHtml), /\sdisabled(?:\s|=|>)/);
  }

  // Clear is explicit, confirmed, and never inferred from an empty save.
  handler = () => json(view()); await ui.run("read");
  ui.draft.value = "unsaved-clear-secret";
  confirm = async () => { throw new Error("cancel"); };
  const beforeCancel = calls.length;
  await ui.run("clear");
  assert.equal(calls.length, beforeCancel);
  assert.equal(ui.draft.value, "unsaved-clear-secret");
  assert.equal(ui.confirming.value, false);
  assert.match(confirmations.at(-1)[0], /新版本需要有效 LICENSE_KEY.*旧版本.*Manager 支持旧版无密钥模式/);
  assert.match(confirmations.at(-1)[0], /不会停止正在运行的浏览器.*不保证.*重新授权/);
  assert.match(confirmations.at(-1)[0], /仅清除 Manager 保存的密钥.*不会修改环境变量或 wrapper 的 license\.key 文件.*不保证所有来源的密钥都已移除/);
  assert.equal(confirmations.at(-1)[2].cancelButtonText, "取消");
  confirm = async () => {};
  for (const result of [clearedView(), clearedView("keyless", KEYLESS_VERSION_REFERENCE),
    clearedView("keyless"), clearedView("keyless", null), clearedView("none", KEYLESS_VERSION_REFERENCE)]) {
    handler = () => json(view()); await ui.run("read");
    ui.draft.value = "discard-after-clear";
    handler = () => json(result);
    await ui.run("clear");
    assert.equal(calls.at(-1).method, "PUT");
    assert.equal(calls.at(-1).body, '{"license_key":""}', "clear sends exactly the explicit empty key payload");
    assert.equal(ui.draft.value, "");
    assert.equal(ui.notice.value.type, "success", "healthy Manager source=unset confirms clear even with tier none");
    assert.match(ui.notice.value.message, /许可证已清除/);
    if (result.license_tier === "none") assert.match(ui.licenseWarning.value, /当前无有效许可/);
    if (result.binary_version === view().binary_version) assert.match(ui.versionHint.value, /清除后将无法新启动/);
    if (result.license_tier === "keyless" && result.binary_version === KEYLESS_VERSION_REFERENCE) {
      assert.match(ui.notice.value.message, /Manager 当前报告旧版无密钥模式/);
    } else {
      assert.match(ui.notice.value.message, /新版本仍需要有效 LICENSE_KEY/);
      assert.doesNotMatch(ui.notice.value.message, /当前报告旧版无密钥模式/);
    }
    const before = calls.length, beforeConfirm = confirmations.length;
    await ui.run("clear");
    assert.equal(calls.length, before);
    assert.equal(confirmations.length, beforeConfirm, "unconfigured cannot clear again");
    assert.match(clearButton(await render(result)), /\sdisabled(?:\s|=|>)/);
  }
  handler = () => json(view()); await ui.run("read");
  await ui.run("clear");
  assert.equal(ui.notice.value.type, "warning", "still configured is not a confirmed clear");

  // A completed PUT can return unknown status when the provider's post-clear read fails.
  for (const result of [unsupported(), {
    ...clearedView("unknown", null), connected: false,
    license: { configured: false, source: "unknown", editable: false }, error: "private-provider-status-error",
  }, {
    ...clearedView("unknown", null),
    license: { configured: false, source: "unknown", editable: false },
  }, {
    ...clearedView(), connected: false, license: { configured: false, source: "unset", editable: false },
  }, lockedView("unset")]) {
    assert.equal(isBrowserSettings(result), true);
    handler = () => json(view()); await ui.run("read");
    handler = () => json(result);
    ui.draft.value = "discard-after-uncertain-clear";
    const before = calls.length;
    await ui.run("clear");
    assert.equal(calls.length, before + 1, "unknown post-clear status never triggers an automatic retry");
    assert.equal(calls.at(-1).body, '{"license_key":""}');
    assert.equal(ui.draft.value, "", "completed write clears the secret even when provider status is unknown");
    assert.equal(ui.notice.value.type, "warning", "configured=false alone does not confirm clear");
    assert.match(ui.notice.value.message, /尚未确认.*当前状态可能未知.*刷新确认.*不会自动重试/);
    assert.doesNotMatch(ui.notice.value.message, /private-provider-status-error/);
    assert.equal(ui.canClear.value, false);
  }

  for (const blocked of [envView(), lockedView(), lockedView("unset"), unsupported(), clearedView(),
    { ...view(), connected: false, license: { ...view().license, editable: false } }]) {
    handler = () => json(blocked); await ui.run("read");
    const before = calls.length, beforeConfirm = confirmations.length;
    await ui.run("clear");
    assert.equal(calls.length, before);
    assert.equal(confirmations.length, beforeConfirm, "read-only, errors and disconnected cannot even confirm");
  }

  // Confirmation is not a write lock: refresh is allowed and invalidates its generation.
  let agree;
  for (const change of ["refresh", "env", "error", "disconnected", "unconfigured", "unmount"]) {
    handler = () => json(view());
    const pending = await mount();
    confirm = () => new Promise(resolve => { agree = resolve; });
    const before = calls.length, beforeConfirm = confirmations.length;
    const clearing = pending.ui.run("clear");
    assert.equal(pending.ui.confirming.value, true);
    await pending.ui.run("clear"); await pending.ui.run("save");
    assert.equal(confirmations.length, beforeConfirm + 1);
    assert.equal(calls.length, before);
    if (change === "refresh") await pending.ui.run("read");
    if (change === "env") pending.ui.settings.value = envView();
    if (change === "error") pending.ui.settings.value = lockedView();
    if (change === "disconnected") pending.ui.settings.value.connected = false;
    if (change === "unconfigured") pending.ui.settings.value = clearedView();
    if (change === "unmount") pending.app.unmount();
    agree(); await clearing;
    assert.ok(calls.slice(before).every(call => call.method === "GET"), `${change} invalidates pending confirmation`);
  }
  confirm = async () => {};

  handler = () => json(view());
  const inFlightClear = await mount();
  let finishClear;
  handler = () => new Promise(resolve => { finishClear = resolve; });
  inFlightClear.ui.draft.value = "clear-in-flight-secret";
  const writingClear = inFlightClear.ui.run("clear");
  await settle();
  const beforeBusyClear = calls.length, beforeBusyConfirm = confirmations.length;
  assert.equal(inFlightClear.ui.busy.value, "clear");
  assert.equal(inFlightClear.ui.inputDisabled.value, true);
  assert.equal(inFlightClear.ui.canSave.value, false);
  assert.equal(inFlightClear.ui.canClear.value, false);
  await inFlightClear.ui.run("clear"); await inFlightClear.ui.run("save"); await inFlightClear.ui.run("read");
  assert.equal(calls.length, beforeBusyClear);
  assert.equal(confirmations.length, beforeBusyConfirm);
  const clearSignal = calls.at(-1).signal;
  inFlightClear.app.unmount();
  assert.equal(clearSignal.aborted, true);
  finishClear(json(clearedView())); await writingClear;
  assert.equal(inFlightClear.ui.settings.value.license.configured, true, "late clear cannot hydrate unmounted state");
  assert.equal(inFlightClear.ui.notice.value, null);
  assert.equal(inFlightClear.ui.draft.value, "");

  handler = () => json(view()); await ui.run("read");
  handler = (_url, options) => options.method === "PUT" ? json({}, 409) : json(envView());
  const beforeClearConflict = calls.length;
  await ui.run("clear");
  assert.deepEqual(calls.slice(beforeClearConflict).map(call => call.method), ["PUT", "GET"]);
  assert.equal(ui.canClear.value, false);
  assert.match(ui.notice.value.message, /不会自动重试保存或清除/);

  for (const response of [() => json({}, 500), () => { throw new TypeError("private-clear-error"); },
    () => json({ ...clearedView(), license_key: "private-clear-error" })]) {
    handler = () => json(view()); await ui.run("read");
    handler = response;
    const before = calls.length;
    await ui.run("clear"); await ui.run("clear"); await ui.run("save");
    assert.equal(calls.length, before + 1);
    assert.equal(ui.refreshRequired.value, true);
    assert.match(ui.notice.value.message, /结果可能未知.*刷新设置/);
    assert.doesNotMatch(ui.notice.value.message, /private-clear-error/);
    handler = () => json(lockedView()); await ui.run("read");
    assert.equal(ui.canClear.value, false, "sticky Manager error remains locked after refresh");
  }

  for (const blocked of [unsupported(), {
    ...view(), connected: false, license: { configured: false, source: "unknown", editable: false },
  }]) {
    handler = () => json(blocked); await ui.run("read");
    ui.draft.value = "blocked-secret";
    const before = calls.length;
    await ui.run("save");
    assert.equal(calls.length, before, "unsupported and disconnected backends never receive mutations");
  }

  handler = () => json(view()); await ui.run("read");
  // A conflict performs exactly one status refresh, never repeats the PUT.
  handler = (_url, options) => options.method === "PUT"
    ? json({ detail: "private-submitted-key" }, 409) : json(envView());
  ui.draft.value = "private-submitted-key";
  const beforeConflict = calls.length;
  await ui.run("save");
  assert.deepEqual(calls.slice(beforeConflict).map(c => c.method), ["PUT", "GET"]);
  assert.equal(ui.settings.value.license.source, "env");
  assert.equal(ui.draft.value, "");
  assert.equal(ui.canSave.value, false);
  assert.match(ui.notice.value.message, /不会自动重试保存/);

  handler = () => json(view()); await ui.run("read");
  handler = () => json({ detail: "private-submitted-key" }, 409);
  ui.draft.value = "private-submitted-key";
  await ui.run("save");
  assert.equal(ui.refreshRequired.value, true, "failed conflict refresh keeps writes blocked");
  assert.equal(ui.draft.value, "private-submitted-key");
  assert.doesNotMatch(ui.notice.value.message, /private-submitted-key/);

  for (const status of [400, 403, 408, 500, 504]) {
    handler = () => json(view()); await ui.run("read");
    handler = () => json({ detail: "private-submitted-key" }, status);
    ui.draft.value = "private-submitted-key";
    const before = calls.length;
    await ui.run("save"); await ui.run("save");
    assert.equal(calls.length, before + 1, "failed saves do not retry and require refresh");
    assert.equal(ui.refreshRequired.value, true);
    assert.match(ui.notice.value.message, /未知.*刷新设置/);
    assert.doesNotMatch(ui.notice.value.message, /private-submitted-key/);
  }
  handler = () => json(view()); await ui.run("read");
  handler = () => { throw new TypeError("private-submitted-key"); };
  ui.draft.value = "private-submitted-key";
  await ui.run("save");
  assert.match(ui.notice.value.message, /结果可能未知/);
  assert.equal(ui.draft.value, "private-submitted-key");

  for (const locked of [lockedView(), lockedView("unset"), lockedView("manager", "")]) {
    handler = () => json(view()); await ui.run("read");
    ui.draft.value = "private-submitted-key";
    handler = () => { throw new TypeError("private-submitted-key"); };
    await ui.run("save");
    assert.equal(ui.refreshRequired.value, true);
    handler = () => json(locked); await ui.run("read");
    assert.deepEqual(ui.settings.value, locked, "refresh accepts the sticky unknown-write lock");
    assert.equal(ui.refreshRequired.value, false, "the locked response is valid, not a parse failure");
    assert.equal(ui.inputDisabled.value, true);
    assert.equal(ui.canSave.value, false, "refresh must not enable another ambiguous write");
    assert.equal(ui.draft.value, "private-submitted-key", "read preserves the unsent draft");
    assert.equal(ui.notice.value, null);
    assert.match(ui.licenseWarning.value, /Manager 报告异常/);
    assert.doesNotMatch(ui.licenseWarning.value, /private-unknown-write-error|private-submitted-key/);
    const before = calls.length;
    await ui.run("save");
    assert.equal(calls.length, before, "sticky lock blocks programmatic saves too");
  }

  for (const invalidView of invalid) {
    handler = () => json(invalidView);
    await ui.run("read");
    assert.equal(ui.refreshRequired.value, true);
    assert.equal(ui.draft.value, "private-submitted-key");
    assert.doesNotMatch(ui.notice.value.message, /secret-response/);
  }
  handler = () => json(view()); await ui.run("read");
  handler = () => json({ ...view(), license_key: "unexpected-secret" });
  ui.draft.value = "private-submitted-key";
  await ui.run("save");
  assert.equal(ui.draft.value, "", "even a malformed successful response clears the submitted secret");
  assert.equal(ui.refreshRequired.value, true);
  assert.match(ui.notice.value.message, /结果可能未知/);

  // Read/write races are serialized, including keyboard/programmatic submits.
  handler = () => json(view()); await ui.run("read");
  let resolve;
  handler = () => new Promise(done => { resolve = done; });
  ui.draft.value = "pending-secret";
  const beforePending = calls.length;
  const pendingSave = ui.run("save");
  await ui.run("save"); await ui.run("read"); await ui.run("clear");
  assert.equal(calls.length, beforePending + 1);
  assert.equal(ui.inputDisabled.value, true);
  resolve(json(view())); await pendingSave;
  assert.equal(ui.draft.value, "");
  const pendingRead = ui.run("read");
  ui.draft.value = "typed-during-read";
  await ui.run("save");
  resolve(json(view())); await pendingRead;
  assert.equal(ui.draft.value, "typed-during-read");

  // Exercise the real AbortSignal path without waiting for the download deadline.
  globalThis.setTimeout = (callback, delay, ...args) => {
    timerCallback = callback; timerDelay = delay;
    return originalSetTimeout(callback, delay, ...args);
  };
  handler = (_url, options) => new Promise((_resolve, reject) => {
    options.signal.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")), { once: true });
  });
  const timedSave = ui.run("save");
  assert.equal(timerDelay, 370000);
  timerCallback(); await timedSave;
  assert.equal(ui.busy.value, "");
  assert.equal(ui.refreshRequired.value, true);
  assert.match(ui.notice.value.message, /超时.*刷新设置/);
  const timedRead = ui.run("read");
  assert.equal(timerDelay, 35000);
  timerCallback(); await timedRead;
  assert.equal(ui.refreshRequired.value, true);
  globalThis.setTimeout = originalSetTimeout;

  handler = () => json(view()); await ui.run("read");
  globalThis.setTimeout = (callback, delay, ...args) => {
    timerCallback = callback; timerDelay = delay;
    return originalSetTimeout(callback, delay, ...args);
  };
  handler = (_url, options) => new Promise((_resolve, reject) => {
    options.signal.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")), { once: true });
  });
  const beforeTimedClear = calls.length;
  const timedClear = ui.run("clear");
  await settle();
  assert.equal(ui.busy.value, "clear");
  assert.equal(timerDelay, 370000, "clear shares the save download budget");
  timerCallback(); await timedClear;
  assert.equal(ui.busy.value, "");
  assert.equal(ui.refreshRequired.value, true);
  assert.match(ui.notice.value.message, /超时.*刷新设置/);
  await ui.run("clear");
  assert.equal(calls.length, beforeTimedClear + 1, "uncertain clear cannot retry without refresh");
  globalThis.setTimeout = originalSetTimeout;

  handler = () => json({ detail: "secret-auth-error" }, 401);
  await ui.run("read");
  assert.equal(events.at(-1)[0], "unauthorized");
  assert.equal(events.at(-1)[1].status, 401);
  assert.equal(ui.draft.value, "");
  handler = () => json(view()); await ui.run("read");
  ui.draft.value = "auth-save-secret";
  handler = () => json({}, 401); await ui.run("save");
  assert.equal(events.at(-1)[0], "unauthorized");
  assert.equal(ui.draft.value, "");

  // Late completions after unmount must neither hydrate state nor emit 401.
  handler = () => new Promise(done => { resolve = done; });
  const stale = await mount();
  const staleSignal = calls.at(-1).signal;
  stale.ui.draft.value = "unmount-secret";
  stale.app.unmount();
  assert.equal(staleSignal.aborted, true);
  assert.equal(stale.ui.draft.value, "");
  resolve(json({}, 401)); await settle();
  assert.equal(stale.ui.settings.value, null);
  assert.deepEqual(stale.events, []);
  const late = await mount();
  late.app.unmount(); resolve(json(envView())); await settle();
  assert.equal(late.ui.settings.value, null);

  handler = () => json(view());
  const lateSave = await mount();
  handler = () => new Promise(done => { resolve = done; });
  lateSave.ui.draft.value = "late-save-secret";
  const saving = lateSave.ui.run("save");
  const saveSignal = calls.at(-1).signal;
  lateSave.app.unmount();
  assert.equal(saveSignal.aborted, true);
  resolve(json(envView())); await saving;
  assert.equal(lateSave.ui.settings.value.license.source, "manager", "late writes cannot hydrate an unmounted card");
  assert.equal(lateSave.ui.notice.value, null);
  assert.equal(lateSave.ui.draft.value, "");

  assert.ok(calls.every(call => call.url === "/api/v1/lanes/browser-settings"));
  assert.ok(calls.every(call => call.method === "GET" && call.body === undefined ||
    call.method === "PUT" && Object.keys(JSON.parse(call.body)).join() === "license_key"));
  console.log("Browser backend settings regression passed");
} finally {
  for (const app of apps) app.unmount();
  globalThis.fetch = originalFetch;
  globalThis.setTimeout = originalSetTimeout;
  if (originalDocument === undefined) delete globalThis.document;
  else globalThis.document = originalDocument;
  delete globalThis.__browserSettingsTest;
  await server.close();
}
