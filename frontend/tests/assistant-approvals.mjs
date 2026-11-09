import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { readFile } from "node:fs/promises";
import { createServer } from "vite";
import { createRenderer, createSSRApp, h, nextTick, ssrContextKey } from "vue";
import { renderToString } from "@vue/server-renderer";
import { parse } from "@vue/compiler-sfc";

const source = await readFile(new URL("../src/views/AIAssistantView.vue", import.meta.url), "utf8");
const exposed = "detail, selectedId, action, error, content, pendingApproval, approvalKey, approvalBusy, approvalExpired, approvalSeconds, canUseBrowser, canScreenshot, selectSession, refreshDetail, decideApproval, send, cancel";
const server = await createServer({
  optimizeDeps: { noDiscovery: true, include: [] },
  server: { middlewareMode: true, hmr: false },
  plugins: [{ name: "assistant-approvals-test", enforce: "pre", transform(code, id) {
    if (!id.endsWith("/AIAssistantView.vue")) return;
    return code
      .replace('import { useRouter } from "vue-router";', 'const { useRouter } = globalThis.__approvalMocks;')
      .replace('import { ElMessageBox } from "element-plus";', 'const { ElMessageBox } = globalThis.__approvalMocks;')
      .replace('import { ApiError, apiRequest } from "@/api/client";', 'const { ApiError, apiRequest } = globalThis.__approvalMocks;')
      .replace('import { useAuthStore } from "@/stores/auth";', 'const { useAuthStore } = globalThis.__approvalMocks;')
      .replace('import AssistantSettingsDialog from "@/components/AssistantSettingsDialog.vue";', 'const AssistantSettingsDialog = { render: () => null };')
      .replace("</script>", `defineExpose({ ${exposed} });\n</script>`);
  } }],
});
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};
const tick = async () => { await Promise.resolve(); await nextTick(); await Promise.resolve(); };
const requests = [], streams = [], routes = [], timers = new Map();
let cleared = 0, timerId = 0, timerStarts = 0, now = Date.parse("2026-11-07T12:00:00Z");
class ApiError extends Error {
  constructor(status, message, code) { super(message); this.status = status; this.code = code; }
}
class EventSourceMock {
  constructor(url) { this.url = url; streams.push(this); }
  addEventListener(name, callback) { assert.equal(name, "update"); this.update = callback; }
  emit(value) { this.update({ data: JSON.stringify(value) }); }
  close() { this.closed = true; }
}
const originals = { EventSource: globalThis.EventSource, crypto: globalThis.crypto, setInterval: globalThis.setInterval,
  clearInterval: globalThis.clearInterval, now: Date.now, fetch: globalThis.fetch };
globalThis.EventSource = EventSourceMock;
if (!globalThis.crypto) globalThis.crypto = { randomUUID };
globalThis.setInterval = callback => { timerStarts++; timers.set(++timerId, callback); return timerId; };
globalThis.clearInterval = id => { timers.delete(id); };
Date.now = () => now;
globalThis.__approvalMocks = {
  ApiError,
  apiRequest(url, options = {}) { const pending = deferred(); requests.push({ url, ...options, ...pending }); return pending.promise; },
  useAuthStore: () => ({ clear() { cleared++; } }),
  useRouter: () => ({ replace: async route => { routes.push(route); } }),
  ElMessageBox: { confirm() { throw new Error("Running approvals must not use legacy confirmations"); } },
};
const id = randomUUID(), otherId = randomUUID(), operationId = randomUUID();
const date = new Date(now).toISOString();
const approval = (fields = {}) => ({ id: randomUUID(), operation_id: operationId, feature_type: "auth.form_login", status: "pending",
  created_at: date, expires_at: new Date(now + 180000).toISOString(), title: "重试登录 <script>literal</script>", ...fields });
const tool = { id: 2, role: "tool", tool_name: "retry_step", status: "running", title: "重试登录", content: "", created_at: date };
const base = (fields = {}) => ({ id, title: "会话", title_locked: false, title_revision: 0, status: "running",
  target: { name: "Example", base_url: "https://example.com/", credential_mode: "none", authorized: true, proxy: false, screenshot_consent: true },
  browser: { status: "ready", lane_key: "interaction", expires_at: date },
  messages: [{ id: 1, role: "user", content: "继续", created_at: date }, { ...tool }], ...fields });
const renderer = createRenderer({ createComment: () => ({}), insert() {}, remove() {}, parentNode() {}, nextSibling() {} });
let app;
try {
  const { default: View } = await server.ssrLoadModule("/src/views/AIAssistantView.vue");
  const { isAssistantDetail, isAssistantPendingApproval } = await server.ssrLoadModule("/src/types/assistant.ts");
  let ui, bindings;
  app = renderer.createApp({ setup() {
    bindings = View.setup({}, { expose(value) { ui = value; } });
    return () => null;
  } });
  app.provide(ssrContextKey, { modules: new Set() });
  app.mount({});
  const last = () => requests.at(-1);
  const respond = async value => { last().resolve(value); await tick(); };
  const choose = async value => { const pending = ui.selectSession(value.id); await respond(value); await pending; };
  const emit = value => streams.at(-1).emit(value);
  // Render the production SSR template against the mounted view's reactive setup state.
  const dialogProps = [];
  const html = async () => {
    dialogProps.length = 0;
    const renderApp = createSSRApp({ ...View, setup: () => bindings });
    renderApp.config.warnHandler = () => {};
    renderApp.component("el-dialog", { props: ["modelValue", "title", "closeOnClickModal", "closeOnPressEscape", "showClose"],
      setup: (props, { slots }) => () => {
        if (!props.modelValue) return null;
        dialogProps.push({ ...props });
        return h("section", { role: "dialog", "aria-label": props.title }, [slots.default?.(), slots.footer?.()]);
      } });
    renderApp.component("el-button", { props: ["disabled", "loading"], setup: (props, { slots }) => () =>
      h("button", { disabled: props.disabled || props.loading }, slots.default?.()) });
    return renderToString(renderApp);
  };
  await respond({ items: [] });

  // Optional legacy/null field; exact public approval metadata at both HTTP and SSE boundaries.
  const first = approval();
  for (const pending_approval of [undefined, null, first]) assert.equal(isAssistantDetail(base({ pending_approval })), true);
  const invalid = [false, [], {}, "pending", ...Object.keys(first).map(key => ({ ...first, [key]: undefined })),
    ...["id", "operation_id"].flatMap(key => [null, "bad", 1].map(value => ({ ...first, [key]: value }))),
    ...["approved", "denied", "expired", null, true].map(status => ({ ...first, status })),
    ...["created_at", "expires_at"].flatMap(key => [null, "2026-11-07", "bad", 1].map(value => ({ ...first, [key]: value }))),
    ...["", null, 1, "a".repeat(129)].map(feature_type => ({ ...first, feature_type })),
    ...["", "   ", null, 1].map(title => ({ ...first, title })),
    ...["step", "credentials", "token", "_private", "raw_step"].map(key => ({ ...first, [key]: "PRIVATE" }))];
  for (const value of invalid) {
    assert.equal(isAssistantPendingApproval(value), false);
    assert.equal(isAssistantDetail(base({ pending_approval: value })), false);
  }
  await choose(base({ pending_approval: null }));
   ui.content.value = "keep draft";
  const beforePending = requests.length;
  emit(base({ pending_approval: first }));
  assert.equal(ui.pendingApproval.value.id, first.id, "running tool opens approval from SSE");
  assert.equal(timers.size, 1);
  assert.equal(ui.canUseBrowser.value, false);
  assert.equal(ui.canScreenshot.value, false, "approval waiting never permits screenshots or refresh navigation");
  const startCount = timerStarts;
  emit(base({ pending_approval: first })); emit(base({ pending_approval: first }));
  assert.equal(timerStarts, startCount, "replayed snapshots do not reopen dialog or restart countdown");
  assert.equal(requests.length, beforePending, "receiving approval has no mutations or reads");
  let rendered = await html();
   assert.equal((rendered.match(/role="dialog"/g) ?? []).length, 0, "approvals are inline, never blocking dialogs");
  assert.match(rendered, /重试登录 &lt;script&gt;literal&lt;\/script&gt;/, "tool title is escaped, not HTML");
  assert.match(rendered, new RegExp(operationId));
  assert.match(rendered, /auth.form_login/);
  assert.match(rendered, /重复登录.*签到.*费用.*副作用/);
   assert.match(rendered, /剩余授权时间 180 秒/);
   assert.equal(dialogProps.length, 0);
  assert.match(rendered, /拒绝并终止此操作/);
  assert.match(rendered, /批准并继续/);
   assert.match(rendered, /<button[^>]*>停止<\/button>/);
   assert.ok(rendered.indexOf('aria-label="重复操作需要授权"') < rendered.indexOf('class="composer"'), "prompt is directly above composer");

  const stable = ui.detail.value;
  emit(base({ pending_approval: { ...first, credentials: "PRIVATE" } }));
  assert.equal(ui.detail.value, stable, "invalid SSE cannot publish any part of the snapshot");
  const approving = ui.decideApproval("approve"), approvalPost = last();
  assert.equal(approvalPost.url, `/assistant/sessions/${id}/retry-approvals/${first.id}/decision`);
  assert.equal(approvalPost.method, "POST");
  assert.deepEqual(JSON.parse(approvalPost.body), { decision: "approve" });
  assert.equal(ui.approvalBusy.value, true);
  assert.equal(ui.action.value, "", "approval writes do not hold main stop latch");
  await ui.decideApproval("approve"); await ui.decideApproval("deny");
  assert.equal(last(), approvalPost, "double decision is synchronously blocked");
  const completed = base({ pending_approval: null, status: "idle", messages: [base().messages[0], { ...tool, status: "success", content: "done" }] });
  emit(completed);
  assert.equal(ui.pendingApproval.value, null, "SSE resolution closes dialog before POST returns");
  assert.equal(timers.size, 0);
  approvalPost.resolve(base({ pending_approval: first })); await tick();
  assert.equal(ui.detail.value.status, "idle", "stale decision response cannot reopen a finished call");
  assert.equal(ui.detail.value.messages.at(-1).content, "done");
  await respond(completed); await approving;
  emit(base({ pending_approval: first }));
  assert.equal(ui.pendingApproval.value, null, "terminal progress and seen approval prevent replay");
  assert.equal(ui.content.value, "keep draft");

  // Denial is only a decision; the backend ends the waiting operation, not another UI execution call.
  const denied = approval();
  await choose(base({ pending_approval: denied }));
  const denying = ui.decideApproval("deny");
  assert.deepEqual(JSON.parse(last().body), { decision: "deny" });
  const deniedState = base({ pending_approval: null });
  await respond(deniedState);
  assert.equal(ui.pendingApproval.value, null);
  await respond(deniedState); await denying;
  emit(base({ pending_approval: denied }));
  assert.equal(ui.pendingApproval.value, null, "resolved approval replay cannot prompt again within selection");

  // A replaced pending ID wins over an earlier HTTP decision, including same-message-ID snapshots.
  const older = approval(), newer = approval();
  await choose(base({ pending_approval: older }));
  const oldDecision = ui.decideApproval("approve"), oldPost = last();
  emit(base({ pending_approval: newer }));
  assert.equal(ui.pendingApproval.value.id, newer.id);
  oldPost.resolve(base({ pending_approval: null })); await tick();
  assert.equal(ui.pendingApproval.value.id, newer.id);
  const readAfterDecision = last();
  emit(base({ pending_approval: newer }));
  readAfterDecision.resolve(base({ pending_approval: older })); await oldDecision;
  assert.equal(ui.pendingApproval.value.id, newer.id, "read raced by SSE cannot replace new approval");
  emit(base({ pending_approval: older }));
  assert.equal(ui.pendingApproval.value.id, newer.id, "old approval ID replay cannot replace the new prompt");

  // Selection closes only local UI; reselecting the still-pending approval must show it again.
  await choose(base({ pending_approval: newer }));
  const abandoned = ui.decideApproval("approve"), abandonedPost = last();
  const previousStream = streams.at(-1);
  await choose(base({ id: otherId, pending_approval: null }));
  assert.equal(timers.size, 0);
  previousStream.emit(base({ pending_approval: newer }));
  const beforeAbandoned = requests.length;
  abandonedPost.reject(new ApiError(401, "PRIVATE abandoned")); await abandoned;
  assert.equal(cleared, 0);
  assert.equal(requests.length, beforeAbandoned);
  assert.equal(ui.pendingApproval.value, null);
  assert.equal(ui.approvalBusy.value, false);
  await choose(base({ pending_approval: newer }));
  assert.equal(ui.pendingApproval.value.id, newer.id);
  const abandonedSuccess = ui.decideApproval("deny"), abandonedSuccessPost = last();
  await choose(base({ id: otherId, pending_approval: null }));
  await choose(base({ pending_approval: newer }));
  const beforeAbandonedSuccess = requests.length;
  abandonedSuccessPost.resolve(base({ pending_approval: null })); await abandonedSuccess;
  assert.equal(requests.length, beforeAbandonedSuccess, "a stale success cannot refresh even after returning to the same session ID");
  assert.equal(ui.pendingApproval.value.id, newer.id, "selection epoch, not session ID alone, guards decision responses");

  // Deadline ticks disable both decisions, make one GET, and never synthesize a deny/approve.
  const expiring = approval({ expires_at: new Date(now + 2000).toISOString() });
  await choose(base({ pending_approval: expiring }));
  now += 1000;
  for (const callback of [...timers.values()]) callback();
  assert.equal(ui.approvalSeconds.value, 1);
  const beforeExpiry = requests.length;
  now += 1000;
  for (const callback of [...timers.values()]) callback();
  assert.equal(ui.approvalExpired.value, true);
  assert.equal(timers.size, 0);
  assert.equal(requests.length, beforeExpiry + 1);
  assert.equal(last().url, `/assistant/sessions/${id}`);
  assert.equal(last().method, undefined);
  await ui.decideApproval("approve"); await ui.decideApproval("deny");
  assert.equal(requests.length, beforeExpiry + 1);
  rendered = await html();
  assert.match(rendered, /授权等待已超时/);
  assert.match(rendered, /<button[^>]*disabled[^>]*>拒绝并终止此操作<\/button>/);
  assert.match(rendered, /<button[^>]*disabled[^>]*>批准并继续<\/button>/);
  await respond(base({ pending_approval: expiring }));
  emit(base({ pending_approval: expiring }));
  await ui.decideApproval("approve");
  assert.equal(requests.length, beforeExpiry + 1, "expired frame replay never repeats deadline read");
  emit(base({ pending_approval: null }));
  assert.equal(ui.pendingApproval.value, null, "server timeout feedback owns closure");
  await choose(base({ pending_approval: approval({ expires_at: date }) }));
  assert.equal(ui.approvalExpired.value, true, "already-expired HTTP snapshot does not start a timer");
  assert.equal(timers.size, 0);
  assert.equal(last().method, undefined);
  await respond(base({ pending_approval: null }));

  // Clock drift since last tick is checked again at click time.
  const drift = approval({ expires_at: new Date(now + 500).toISOString() });
  await choose(base({ pending_approval: drift }));
  now += 501;
  const beforeDrift = requests.length;
  await ui.decideApproval("approve");
  assert.equal(requests.length, beforeDrift + 1);
  assert.equal(last().method, undefined);
  await respond(base({ pending_approval: null }));

  // A 202 send releases the main latch before its following GET, so Stop works while approval waits.
  await choose(base({ status: "idle", messages: [], pending_approval: null }));
   ui.content.value = "继续";
  const sending = ui.send(), sendPost = last();
  const whileSending = approval();
  emit(base({ pending_approval: whileSending }));
  sendPost.resolve({ accepted: true }); await tick();
  assert.equal(ui.action.value, "");
  assert.equal(ui.pendingApproval.value.id, whileSending.id);
  const sendGet = last();
  const stopping = ui.cancel(), stopPost = last();
  assert.equal(stopPost.url, `/assistant/sessions/${id}/cancel`);
  const stopped = base({ status: "idle", pending_approval: null });
  stopPost.resolve(stopped); await tick();
  const stopGet = last();
  sendGet.resolve(base({ pending_approval: whileSending })); await sending;
  const sendList = last();
  stopGet.resolve(stopped); await stopping;
  sendList.resolve({ items: [] });
  if (last().url === "/assistant/sessions") await respond({ items: [] });
  assert.equal(ui.pendingApproval.value, null);
  assert.equal(timers.size, 0);

  // Stop also remains available during the approval HTTP write; its SSE state cannot be undone.
  const stopApproval = approval();
  await choose(base({ pending_approval: stopApproval }));
  const writeBeforeStop = ui.decideApproval("approve"), writePost = last();
  const concurrentStop = ui.cancel(), cancelPost = last();
  assert.equal(cancelPost.url, `/assistant/sessions/${id}/cancel`);
  emit(stopped);
  cancelPost.resolve(stopped); await tick();
  await respond(stopped); await concurrentStop;
  if (last().url === "/assistant/sessions") await respond({ items: [] });
  writePost.resolve(base({ pending_approval: stopApproval })); await tick();
  assert.equal(ui.detail.value.status, "idle");
  await respond(stopped); await writeBeforeStop;

  // Failed writes are never replayed automatically and do not expose backend/provider secrets.
  await choose(base({ pending_approval: approval() }));
  const failing = ui.decideApproval("deny"), beforeFailure = requests.length;
  last().reject(new TypeError("PRIVATE credentials")); await failing;
  assert.equal(requests.length, beforeFailure);
  assert.equal(ui.approvalBusy.value, false);
  assert.ok(ui.pendingApproval.value);
  assert.doesNotMatch(ui.error.value, /PRIVATE|credentials/);
  const invalidWrite = ui.decideApproval("approve"), beforeInvalidWrite = requests.length;
  last().resolve(base({ pending_approval: { ...approval(), step: { password: "PRIVATE" } } })); await invalidWrite;
  assert.equal(requests.length, beforeInvalidWrite, "invalid write snapshot never publishes or triggers a follow-up read");
  assert.ok(ui.pendingApproval.value);
  assert.doesNotMatch(ui.error.value, /PRIVATE|password/);
  const unauthorized = ui.decideApproval("deny");
  last().reject(new ApiError(401, "PRIVATE auth")); await unauthorized;
  assert.equal(cleared, 1);
  assert.deepEqual(routes.at(-1), { name: "login" });
  assert.equal(ui.detail.value, null);
  assert.equal(timers.size, 0);

  // Verify auth/CSRF on the real API client, without issuing an execution or message call.
  const client = await server.ssrLoadModule("/src/api/client.ts");
  client.setCsrfToken("approval-csrf");
  const wire = [];
  globalThis.fetch = async (url, options) => { wire.push({ url, options }); return new Response("{}", { status: 200 }); };
  await client.apiRequest(`/assistant/sessions/${id}/retry-approvals/${first.id}/decision`, { method: "POST", body: '{"decision":"approve"}' });
  assert.equal(wire[0].url, `/api/v1/assistant/sessions/${id}/retry-approvals/${first.id}/decision`);
  assert.equal(wire[0].options.credentials, "include");
  assert.equal(wire[0].options.headers.get("X-CSRF-Token"), "approval-csrf");
  assert.equal(wire[0].options.headers.get("Content-Type"), "application/json");
  client.setCsrfToken(null);

  await choose(base({ pending_approval: approval() }));
  const unmountedWrite = ui.decideApproval("approve"), unmountedPost = last(), unmountedStream = streams.at(-1);
  app.unmount(); app = null;
  assert.equal(timers.size, 0);
  const beforeUnmounted = requests.length;
  unmountedPost.reject(new ApiError(401, "PRIVATE unmounted")); await unmountedWrite;
  unmountedStream.emit(base({ pending_approval: first }));
  assert.equal(requests.length, beforeUnmounted);
  assert.equal(ui.pendingApproval.value, null);
  assert.equal(ui.approvalBusy.value, false);
  assert.equal(cleared, 1);
  assert.equal(unmountedStream.closed, true);
  assert.equal(requests.filter(request => request.url.endsWith("/messages")).length, 1, "only the explicit user send posts a message");
  assert.ok(requests.every(request => /^\/assistant\/sessions(?:\/[0-9a-f-]+(?:\/(?:messages|cancel|retry-approvals\/[0-9a-f-]+\/decision))?)?$/.test(request.url)),
    "no frontend retry, execution, or legacy authorization call is made");
  const ast = parse(source).descriptor.template.ast;
  const dialogs = [];
  const walk = node => { if (node.tag === "el-dialog") dialogs.push(node); for (const child of node.children ?? []) walk(child); };
  walk(ast);
   assert.equal(dialogs.filter(node => node.props.some(prop => prop.name === "bind" && prop.arg?.content === "key" && prop.exp?.content === "approvalKey")).length, 0);
  console.log("Assistant approvals regression passed");
} finally {
  app?.unmount();
  globalThis.EventSource = originals.EventSource;
  globalThis.fetch = originals.fetch;
  globalThis.setInterval = originals.setInterval;
  globalThis.clearInterval = originals.clearInterval;
  Date.now = originals.now;
  if (!originals.crypto) delete globalThis.crypto;
  delete globalThis.__approvalMocks;
  await server.close();
}
