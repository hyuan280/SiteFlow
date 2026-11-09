import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { createServer } from "vite";
import { createRenderer, createSSRApp, h, nextTick, ssrContextKey } from "vue";
import { renderToString } from "@vue/server-renderer";

export const date = "2026-11-07T12:00:00Z";
export const id = randomUUID(), otherId = randomUUID();
export const message = (id, role = "user", content = "hello", fields = {}) => ({ id, role, content, created_at: date, ...fields });
export const session = (fields = {}) => ({ id, title: "Session", title_locked: false, title_revision: 0, status: "idle", created_at: date, updated_at: date, ...fields });
export const target = { name: "Example", base_url: "https://example.com/", credential_mode: "none", confirmed: true, proxy: false, screenshot_consent: true };
export const base = (fields = {}) => ({ ...session(), target: { ...target }, browser: { status: "ready", lane_key: "interaction", expires_at: date }, messages: [], ...fields });
export class ApiError extends Error {
  constructor(status, message, code) { super(message); this.status = status; this.code = code; }
}
export const tick = async () => { await Promise.resolve(); await nextTick(); await Promise.resolve(); };

export async function harness(exposed) {
  const requests = [], streams = [], confirmations = [], routes = [], stored = new Map();
  let cleared = 0, storageBlocked = false, now = Date.parse(date), timerId = 0;
  const timers = new Map();
  const originals = Object.fromEntries(["EventSource", "crypto", "localStorage", "setInterval", "clearInterval"].map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]));
  const originalNow = Date.now;
  const deferred = () => {
    let resolve, reject;
    const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
    return { promise, resolve, reject };
  };
  globalThis.EventSource = class {
    constructor(url) { this.url = url; streams.push(this); }
    addEventListener(name, callback) { assert.equal(name, "update"); this.update = callback; }
    emit(value) { this.update({ data: JSON.stringify(value) }); }
    close() { this.closed = true; }
  };
  if (!globalThis.crypto) globalThis.crypto = { randomUUID };
  Object.defineProperty(globalThis, "localStorage", { configurable: true, get() {
    if (storageBlocked) throw new Error("blocked");
    return { getItem: key => stored.get(key), setItem: (key, value) => stored.set(key, value) };
  } });
  globalThis.setInterval = callback => { timers.set(++timerId, callback); return timerId; };
  globalThis.clearInterval = key => timers.delete(key);
  Date.now = () => now;
  globalThis.__assistantHarness = {
    ApiError,
    apiRequest(url, options = {}) { const pending = deferred(); requests.push({ url, ...options, ...pending }); return pending.promise; },
    useAuthStore: () => ({ clear() { cleared++; } }),
    useRouter: () => ({ replace: async value => { routes.push(value); } }),
    ElMessageBox: { confirm(...args) { const pending = deferred(); confirmations.push({ args, ...pending }); return pending.promise; } },
  };
  const server = await createServer({ optimizeDeps: { noDiscovery: true, include: [] }, server: { middlewareMode: true, hmr: false },
    plugins: [{ name: "assistant-harness", enforce: "pre", transform(code, path) {
      if (!path.endsWith("/AIAssistantView.vue")) return;
      for (const line of ['import { useRouter } from "vue-router";', 'import { ElMessageBox } from "element-plus";',
        'import { ApiError, apiRequest } from "@/api/client";', 'import { useAuthStore } from "@/stores/auth";']) {
        code = code.replace(line, line.replace(/import (.*?) from .*;/, "const $1 = globalThis.__assistantHarness;"));
      }
      return code.replace('import AssistantSettingsDialog from "@/components/AssistantSettingsDialog.vue";', 'const AssistantSettingsDialog = { render: () => null };')
        .replace("</script>", `defineExpose({ ${exposed} });\n</script>`);
    } }] });
  const { default: View } = await server.ssrLoadModule("/src/views/AIAssistantView.vue");
  const types = await server.ssrLoadModule("/src/types/assistant.ts");
  let ui, bindings;
  const renderer = createRenderer({ createComment: () => ({}), insert() {}, remove() {}, parentNode() {}, nextSibling() {} });
  const app = renderer.createApp({ setup() { bindings = View.setup({}, { expose(value) { ui = value; } }); return () => null; } });
  app.provide(ssrContextKey, { modules: new Set() });
  app.mount({});
  const last = () => requests.at(-1);
  const respond = async value => { last().resolve(value); await tick(); };
  const choose = async (value = base()) => { const pending = ui.selectSession(value.id); await respond(value); await pending; };
  const emit = value => streams.at(-1).emit(value);
  const finish = async (pending, value, { list = true } = {}) => {
    await respond(value);
    assert.equal(last().url, `/assistant/sessions/${value.id}`);
    await respond(value);
    await pending;
    if (list) { assert.equal(last().url, "/assistant/sessions"); await respond({ items: [] }); }
  };
  const html = async () => {
    const renderApp = createSSRApp({ ...View, setup: () => bindings });
    renderApp.config.warnHandler = () => {};
    renderApp.component("el-dialog", { props: ["modelValue", "title"], setup: (props, { slots }) => () => props.modelValue ? h("section", { role: "dialog", "aria-label": props.title }, [slots.default?.(), slots.footer?.()]) : null });
    renderApp.component("el-button", { props: ["disabled", "loading"], setup: (props, { slots }) => () => h("button", { disabled: props.disabled || props.loading }, slots.default?.()) });
    renderApp.component("el-table", { setup: (_, { slots }) => () => h("table", slots.default?.()) });
    renderApp.component("el-table-column", { setup: () => () => null });
    return renderToString(renderApp);
  };
  let unmounted = false;
  const unmount = () => { if (!unmounted) app.unmount(); unmounted = true; };
  return { ui, types, requests, streams, confirmations, routes, stored, timers, last, respond, choose, emit, finish, html, unmount,
    cleared: () => cleared, blockStorage: value => { storageBlocked = value; }, advance: async ms => { now += ms; for (const callback of [...timers.values()]) callback(); await tick(); },
    async close() {
      unmount();
      for (const [key, descriptor] of Object.entries(originals)) { if (descriptor) Object.defineProperty(globalThis, key, descriptor); else delete globalThis[key]; }
      Date.now = originalNow;
      delete globalThis.__assistantHarness;
      await server.close();
    } };
}
