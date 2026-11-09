import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { createServer } from "vite";
import { createRenderer, reactive, nextTick, ssrContextKey } from "vue";

const source = await readFile(new URL("../src/components/AssistantSettingsDialog.vue", import.meta.url), "utf8");
const exposed = "view, draft, inheritTokens, loading, busy, error, notice, needsRefresh, values, canSave, refresh, save, reset, close";
const server = await createServer({
  optimizeDeps: { noDiscovery: true, include: [] },
  server: { middlewareMode: true, hmr: false },
  plugins: [{
    name: "assistant-settings-test", enforce: "pre",
    transform(code, id) {
      if (!id.endsWith("/AssistantSettingsDialog.vue")) return;
      return code
        .replace('import { useRouter } from "vue-router";', 'const { useRouter } = globalThis.__settingsMocks;')
        .replace('import { ElMessageBox } from "element-plus";', 'const { ElMessageBox } = globalThis.__settingsMocks;')
        .replace('import { ApiError, apiRequest } from "@/api/client";', 'const { ApiError, apiRequest } = globalThis.__settingsMocks;')
        .replace('import { useAuthStore } from "@/stores/auth";', 'const { useAuthStore } = globalThis.__settingsMocks;')
        .replace("</script>", `defineExpose({ ${exposed} });\n</script>`);
    },
  }],
});
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};
const tick = async () => { await Promise.resolve(); await nextTick(); await Promise.resolve(); };
const requests = [], confirmations = [], events = [], routes = [];
let cleared = 0;
class ApiError extends Error {
  constructor(status, message, code) { super(message); this.status = status; this.code = code; }
}
globalThis.__settingsMocks = {
  ApiError,
  apiRequest(url, options = {}) {
    const pending = deferred();
    requests.push({ url, ...options, ...pending });
    return pending.promise;
  },
  useAuthStore: () => ({ clear() { cleared++; } }),
  useRouter: () => ({ replace: async route => { routes.push(route); } }),
  ElMessageBox: { confirm(...args) {
    const pending = deferred();
    confirmations.push({ args, ...pending });
    return pending.promise;
  } },
};
const defaults = { max_tool_calls: 20, max_model_rounds: 12, max_parallel_tool_calls: 4,
  turn_timeout_seconds: 180, max_tokens: null, context_max_tokens: 64000, auto_compact: true, auto_title: true,
  auto_authorize_execution: false, auto_authorize_screenshot: false, auto_authorize_workflow: false };
const settings = (revision = 0, values = defaults, source = "default") => ({ revision, source, values: { ...values } });
const renderer = createRenderer({ createComment: () => ({}), insert() {}, remove() {}, parentNode() {}, nextSibling() {} });
let app;
try {
  const { default: Dialog } = await server.ssrLoadModule("/src/components/AssistantSettingsDialog.vue");
  const { isAssistantSettingsView, parseSettingsInteger, assistantSettingBounds } = await server.ssrLoadModule("/src/types/assistant-settings.ts");
  const props = reactive({ modelValue: false });
  let ui;
  app = renderer.createApp({ setup() {
    Dialog.setup(props, { expose(value) { ui = value; }, emit(event, value) {
      events.push({ event, value });
      if (event === "update:modelValue") props.modelValue = value;
    } });
    return () => null;
  } });
  app.provide(ssrContextKey, { modules: new Set() });
  app.mount({});
  const last = () => requests.at(-1);
  const resolve = async value => { last().resolve(value); await tick(); };
  const reopen = async (value = settings()) => {
    props.modelValue = false; props.modelValue = true;
    assert.equal(last().url, "/assistant/settings");
    assert.ok(last().signal instanceof AbortSignal);
    await resolve(value);
  };
  const save = async (tokens, expectedTokens) => {
    ui.inheritTokens.value = false;
    ui.draft.max_tokens = tokens;
    const revision = ui.view.value.revision;
    const pending = ui.save();
    const request = last();
    assert.equal(request.method, "PUT");
    assert.equal(request.signal, undefined, "mutations must not pretend abort rolls back a save");
    const payload = JSON.parse(request.body);
    assert.equal(payload.expected_revision, revision);
    assert.equal(payload.values.max_tokens, expectedTokens);
    assert.equal(Object.keys(payload.values).length, 11, "PUT sends every setting");
    await ui.save(); await ui.reset(); await ui.refresh();
    assert.equal(last(), request, "all duplicate operations are blocked synchronously");
    ui.close();
    assert.equal(props.modelValue, true, "close is blocked until mutation outcome arrives");
    request.resolve(settings(revision + 1, payload.values, "db"));
    await pending;
    assert.equal(ui.view.value.revision, revision + 1);
    assert.equal(ui.view.value.source, "db");
    assert.equal(ui.busy.value, "");
    assert.match(ui.notice.value, /已保存/);
    return payload.values;
  };

  assert.equal(requests.length, 0, "closed modal does not fetch");
  props.modelValue = true;
  assert.equal(ui.loading.value, true);
  assert.equal(ui.canSave.value, false);
  await resolve(settings());
  assert.deepEqual(ui.values.value, defaults);
   assert.equal(ui.draft.context_max_tokens, "64000", "token budget, independent of output cap");
  assert.equal(ui.inheritTokens.value, true);
  assert.equal(ui.canSave.value, true);
  assert.deepEqual(ui.view.value, settings());
   assert.deepEqual(assistantSettingBounds.context_max_tokens, [16000, 1000000]);
   ui.draft.context_max_tokens = "1000000";
   assert.equal(ui.values.value.context_max_tokens, 1000000);
   ui.draft.context_max_tokens = "1000001";
  assert.equal(ui.canSave.value, false);
   ui.draft.context_max_tokens = "64000";
  ui.draft.max_tool_calls = "21";
  assert.equal(ui.view.value.values.max_tool_calls, 20, "draft never mutates loaded snapshot");
   ui.draft.auto_compact = false; ui.draft.auto_title = false;
   ui.draft.auto_authorize_execution = true;
   ui.draft.auto_authorize_screenshot = true;
   ui.draft.auto_authorize_workflow = true;
  const changed = await save("123456", 123456);
  assert.equal(changed.max_tool_calls, 21);
  assert.equal(changed.auto_compact, false);
   assert.equal(changed.auto_title, false);
   assert.equal(changed.auto_authorize_execution, true);
   assert.equal(changed.auto_authorize_screenshot, true);
   assert.equal(changed.auto_authorize_workflow, true);
  await save("0", 0);
  await save("", 0);
  await save("  ", 0);
  await save(String(Number.MAX_SAFE_INTEGER), Number.MAX_SAFE_INTEGER);
  ui.inheritTokens.value = true;
  ui.draft.max_tokens = "invalid ignored while inheriting";
  const inherited = ui.save();
  assert.equal(JSON.parse(last().body).values.max_tokens, null);
  await resolve(settings(10)); await inherited;

  const invalidNumbers = ["1.5", "1.0", "1e2", "Infinity", "NaN", "true", "false", "-1", "+1", "0x20", "1_000", "9007199254740992", true, false, 1, null];
  ui.inheritTokens.value = false;
  for (const value of invalidNumbers) {
    ui.draft.max_tokens = value;
    const count = requests.length;
    assert.equal(ui.canSave.value, false, `bad token limit: ${value}`);
    await ui.save();
    assert.equal(requests.length, count);
    assert.equal(parseSettingsInteger(value, 0, Number.MAX_SAFE_INTEGER, true), null);
  }
  ui.inheritTokens.value = true;
  for (const [key, [min, max]] of Object.entries(assistantSettingBounds)) {
    const previous = ui.draft[key];
    for (const bad of ["", "0", String(min - 1), String(max + 1), ...invalidNumbers]) {
      ui.draft[key] = bad;
      assert.equal(ui.canSave.value, false, `${key} rejects ${bad}`);
    }
    for (const good of [min, max]) {
      ui.draft[key] = String(good);
      assert.equal(ui.canSave.value, true, `${key} accepts inclusive boundary`);
    }
    ui.draft[key] = previous;
  }
   for (const key of ["auto_compact", "auto_title", "auto_authorize_execution", "auto_authorize_screenshot", "auto_authorize_workflow"]) {
    for (const bad of ["true", "false", 1, 0, null]) {
      ui.draft[key] = bad;
      assert.equal(ui.canSave.value, false, "toggles require strict booleans");
    }
    ui.draft[key] = true;
  }
  ui.inheritTokens.value = "true";
  assert.equal(ui.canSave.value, false);
  ui.inheritTokens.value = true;

  const invalidViews = [null, [], {}, { ...settings(), values: null }, { ...settings(), source: "env" },
    ...[undefined, null, "1", true, -1, 1.5, Infinity, Number.MAX_SAFE_INTEGER + 1].map(revision => ({ ...settings(), revision }))];
  for (const [key, [min, max]] of Object.entries(assistantSettingBounds)) {
    for (const bad of [undefined, null, true, String(min), min - 1, max + 1, 1.5, Infinity]) {
      invalidViews.push(settings(1, { ...defaults, [key]: bad }));
    }
  }
   for (const key of ["max_tokens", "auto_compact", "auto_title", "auto_authorize_execution", "auto_authorize_screenshot", "auto_authorize_workflow"]) {
    for (const bad of key === "max_tokens" ? [undefined, true, "0", -1, 1.5, Infinity, Number.MAX_SAFE_INTEGER + 1] : [undefined, null, "false", 0]) {
      invalidViews.push(settings(1, { ...defaults, [key]: bad }));
    }
  }
  for (const bad of invalidViews) {
    assert.equal(isAssistantSettingsView(bad), false);
    await reopen(bad);
    assert.equal(ui.view.value, null, "malformed response cannot publish partial settings or editable defaults");
    assert.equal(ui.canSave.value, false);
    assert.match(ui.error.value, /格式无效/);
  }
  assert.equal(isAssistantSettingsView(settings(Number.MAX_SAFE_INTEGER, { ...defaults, max_tokens: Number.MAX_SAFE_INTEGER }, "db")), true);

  await reopen(settings(15));
  ui.draft.max_tool_calls = "77";
  const failed = ui.save();
  last().reject(new ApiError(409, "PRIVATE diagnostics", "assistant_settings_conflict"));
  await failed;
  assert.equal(ui.draft.max_tool_calls, "77");
  assert.equal(ui.view.value.revision, 15);
  assert.equal(ui.canSave.value, false);
  assert.match(ui.error.value, /assistant_settings_conflict.*草稿已保留/);
  assert.doesNotMatch(ui.error.value, /PRIVATE/);
  const count = requests.length;
  await ui.save(); await ui.reset();
  assert.equal(requests.length, count, "conflicts never auto-retry or reuse stale revision");
  const cancelledRefresh = ui.refresh();
  assert.match(confirmations.at(-1).args[0], /丢弃当前草稿/);
  confirmations.at(-1).reject("cancel"); await cancelledRefresh;
  assert.equal(ui.draft.max_tool_calls, "77");
  const refreshing = ui.refresh();
  confirmations.at(-1).resolve(); await tick();
  await resolve(settings(16)); await refreshing;
  assert.equal(ui.draft.max_tool_calls, "20");
  assert.equal(ui.canSave.value, true);

  ui.draft.max_tool_calls = "44";
  const cancelledReset = ui.reset();
  assert.equal(requests.length, count + 1, "reset requires confirmation before POST");
  await ui.reset();
  ui.close(); assert.equal(props.modelValue, true, "confirmation also blocks close");
  confirmations.at(-1).reject("cancel"); await cancelledReset;
  assert.equal(ui.draft.max_tool_calls, "44");
  assert.equal(ui.busy.value, "");
  const resetting = ui.reset();
  // Revision must be captured before the async confirmation, not read after it.
  ui.view.value.revision = 99;
  confirmations.at(-1).resolve(); await tick();
  assert.equal(last().url, "/assistant/settings/reset");
  assert.equal(last().method, "POST");
  assert.deepEqual(JSON.parse(last().body), { expected_revision: 16 });
  assert.equal(last().signal, undefined);
  last().reject(new ApiError(409, "PRIVATE", "assistant_settings_conflict")); await resetting;
  assert.equal(ui.draft.max_tool_calls, "44", "reset conflict preserves draft");
  assert.match(ui.error.value, /assistant_settings_conflict/);
  await reopen(settings(17));
  const resetOK = ui.reset(); confirmations.at(-1).resolve(); await tick();
  await resolve(settings(18)); await resetOK;
  assert.equal(ui.view.value.source, "default");
  assert.equal(ui.view.value.revision, 18);
  assert.deepEqual(ui.values.value, defaults);

  const corruptRefresh = ui.refresh(); confirmations.at(-1).resolve(); await tick();
  last().reject(new ApiError(503, "PRIVATE corrupt data", "assistant_settings_invalid")); await corruptRefresh;
  assert.equal(ui.view.value.revision, 18, "failed refresh preserves the last valid snapshot");
  assert.equal(ui.canSave.value, false, "corrupt settings fail closed even with a previous snapshot");
  assert.match(ui.error.value, /assistant_settings_invalid/);
  await reopen(settings(19));
  const failedReset = ui.reset(); confirmations.at(-1).resolve(); await tick();
  last().reject(new Error("PRIVATE network reset")); await failedReset;
  assert.match(ui.error.value, /可能已保存/);
  assert.equal(ui.canSave.value, false);

  for (const cause of [new ApiError(503, "PRIVATE", "assistant_settings_invalid"), new Error("PRIVATE network")]) {
    props.modelValue = false; props.modelValue = true;
    last().reject(cause); await tick();
    assert.equal(ui.view.value, null);
    assert.equal(ui.canSave.value, false);
    assert.doesNotMatch(ui.error.value, /PRIVATE/);
    assert.match(ui.error.value, cause instanceof ApiError ? /assistant_settings_invalid/ : /无法加载/);
  }
  await reopen(settings(19));
  ui.draft.max_tool_calls = "42";
  const uncertainSave = ui.save(); await resolve({ revision: 20 }); await uncertainSave;
  assert.equal(ui.draft.max_tool_calls, "42");
  assert.match(ui.error.value, /可能已保存/);
  assert.equal(ui.canSave.value, false);

  props.modelValue = false; props.modelValue = true;
  const staleGET = last();
  ui.close();
  assert.equal(staleGET.signal.aborted, true);
  props.modelValue = true;
  const freshGET = last();
  staleGET.reject(new ApiError(401, "PRIVATE stale auth")); await tick();
  assert.equal(cleared, 0);
  assert.equal(ui.loading.value, true, "old finally cannot clear new loading");
  freshGET.resolve(settings(20)); await tick();
  assert.equal(ui.view.value.revision, 20);
  props.modelValue = false; props.modelValue = true;
  const closedGET = last();
  ui.close();
  closedGET.resolve(settings(999)); await tick();
  assert.equal(ui.view.value, null, "closed GET success cannot publish a late snapshot");
  await reopen(settings(20));
  const staleSave = ui.save(); const oldPUT = last();
  props.modelValue = false; props.modelValue = true;
  await resolve(settings(22));
  oldPUT.resolve(settings(21)); await staleSave;
  assert.equal(ui.view.value.revision, 22, "forced close/reopen ignores late mutations");
  assert.equal(ui.notice.value, "");
  const staleReset = ui.reset(); const oldConfirm = confirmations.at(-1);
  props.modelValue = false; props.modelValue = true; await resolve(settings(23));
  const beforeConfirm = requests.length;
  oldConfirm.resolve(); await staleReset;
  assert.equal(requests.length, beforeConfirm, "stale confirmation cannot reset a new dialog session");

  const unauthorized = ui.save();
  last().reject(new ApiError(401, "PRIVATE auth")); await unauthorized;
  assert.equal(cleared, 1);
  assert.deepEqual(routes, [{ name: "login" }]);
  assert.equal(props.modelValue, false);
  assert.equal(ui.view.value, null);
  props.modelValue = true;
  last().reject(new ApiError(401, "PRIVATE read auth")); await tick();
  assert.equal(cleared, 2, "active GET also clears authentication");
  assert.equal(props.modelValue, false);
  await reopen();
  const unmounted = ui.save(); const pendingPUT = last();
  app.unmount(); app = null;
  pendingPUT.reject(new ApiError(401, "PRIVATE late auth")); await unmounted;
  assert.equal(cleared, 2, "unmounted response cannot trigger auth side effects");
  assert.equal(ui.view.value, null);
  assert.ok(requests.every(request => request.url === "/assistant/settings" || request.url === "/assistant/settings/reset"),
    "dialog never calls integrations, models, sessions, or websites");
  assert.ok(events.every(event => event.event === "update:modelValue"));
  assert.match(source, /执行预算/);
  assert.match(source, /模型与上下文/);
  assert.match(source, /仅对之后发送的新消息生效.*正在运行的任务不变.*不会改变网站目标确认要求/);
  assert.match(source, /v-model="draft.auto_authorize_workflow"[^>]*>默认授权导入保存配置并触发真实完整工作流运行/);
  assert.match(source, /实际保存配置和触发目标站点的完整任务运行.*真实登录、签到、点击.*费用或其他业务副作用/);
  assert.match(source, /即使关闭“默认授权执行步骤”仍然有效.*仅控制助手的单步验证.*不限制已授权的完整工作流运行/);
  assert.doesNotMatch(source, /仅工作流保存能力/);
   assert.match(source, /上下文预算（token，16000\.\.1000000）/);
   assert.match(source, /目标确认、信任与代理授权始终需要显式确认/);
  assert.match(source, /:close-on-click-modal="!busy" :close-on-press-escape="!busy" :show-close="!busy"/);
  assert.match(source, /@media \(max-width: 540px\)/);
  assert.doesNotMatch(source, /v-html|localStorage|sessionStorage|console\./);
  console.log("Assistant settings regression passed");
} finally {
  app?.unmount();
  delete globalThis.__settingsMocks;
  await server.close();
}
