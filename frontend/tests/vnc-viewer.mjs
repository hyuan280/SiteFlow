import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import { createServer } from "vite";
import { createRenderer, nextTick, reactive, ssrContextKey } from "vue";

const source = await readFile(new URL("../src/components/VncViewer.vue", import.meta.url), "utf8");
const originals = new Map(["fetch", "window", "document", "navigator", "__vncViewerTest"]
  .map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]));
const calls = [];
const clients = [];
const timers = new Map();
const apps = new Set();
let handler;
let clipboard;
let timerId = 0;
const json = (value = { ok: true }, status = 200) => new Response(JSON.stringify(value), { status });
const deferred = () => {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};
const settle = async () => { await new Promise(resolve => setImmediate(resolve)); await nextTick(); };
const expectedKeys = [
  [0xffe1, "ShiftLeft", false], [0xffe2, "ShiftRight", false],
  [0xffe3, "ControlLeft", false], [0xffe4, "ControlRight", false],
  [0xffe9, "AltLeft", false], [0xffea, "AltRight", false],
  [0xffeb, "MetaLeft", false], [0xffec, "MetaRight", false],
  [0xffe3, "ControlLeft", true], [0x76, "KeyV"], [0xffe3, "ControlLeft", false],
];

class FakeRFB {
  constructor(screen, url, options) {
    Object.assign(this, { screen, url, options, listeners: new Map(), keys: [], focuses: [], legacyPastes: [], disconnects: 0 });
    clients.push(this);
  }
  addEventListener(name, listener) { this.listeners.set(name, listener); }
  fire(name, event) { this.listeners.get(name)?.(event); }
  disconnect() { this.disconnects += 1; this.fire("disconnect"); }
  sendKey(...args) { this.keys.push(args); }
  focus(options) { this.focuses.push(options); }
  clipboardPasteFrom(text) { this.legacyPastes.push(text); assert.fail("RFB ClientCutText must never be used"); }
}

const globals = {
  document: { cookie: "siteflow_csrf=test-csrf", hasFocus: () => true, visibilityState: "visible" },
  window: {
    location: { protocol: "https:", host: "siteflow.test" },
    setTimeout(callback, delay) { const id = ++timerId; timers.set(id, { callback, delay }); return id; },
    clearTimeout(id) { timers.delete(id); },
  },
  navigator: { get clipboard() { return clipboard; } },
  fetch: async (url, options) => { calls.push({ url, ...options }); return handler(url, options); },
  __vncViewerTest: { RFB: FakeRFB },
};
for (const [key, value] of Object.entries(globals)) {
  Object.defineProperty(globalThis, key, { configurable: true, writable: true, value });
}

const server = await createServer({
  optimizeDeps: { noDiscovery: true, include: [] },
  server: { middlewareMode: true, hmr: false },
  plugins: [{ name: "vnc-viewer-harness", enforce: "pre", transform(code, id) {
    if (!id.endsWith("/VncViewer.vue")) return;
    assert.ok(code.includes('import RFB from "@novnc/novnc";'), "RFB import must be stubbed before SSR loading");
    return code.replace('import RFB from "@novnc/novnc";', 'const { RFB } = globalThis.__vncViewerTest;')
      .replace("</script>", `defineExpose({ screen, state, errorMessage, clipboardOpen, clipboardText,
        clipboardError, clipboardBusy, clipboardInput, connect, disconnect, close, openClipboard,
         pasteClipboard, handleKeydown, handlePaste, receiveOpen, receiveText, receiveStatus,
         receiveInput, clearReceivedClipboard, copyClipboard });\n</script>`);
  } }],
});
const renderer = createRenderer({ createComment: () => ({}), insert() {}, remove() {}, parentNode() {}, nextSibling() {} });

try {
  const { default: Viewer } = await server.ssrLoadModule("/src/components/VncViewer.vue");
  const mount = async (t, embedded = true) => {
    handler = async () => json();
    clipboard = { readText: async () => "local clipboard", writeText: async () => {} };
    document.hasFocus = () => true;
    document.visibilityState = "visible";
    calls.length = 0;
    let ui;
    let inputFocus = 0;
    const events = [];
    const props = reactive({ modelValue: true, laneKey: "lane /\u4e2d", laneLabel: "Test lane", embedded });
    const app = renderer.createApp({ setup() {
      Viewer.setup(props, { expose(value) { ui = value; }, emit(...args) { events.push(args); } });
      ui.screen.value = {};
      ui.clipboardInput.value = { focus() { inputFocus += 1; } };
      return () => null;
    } });
    app.provide(ssrContextKey, { modules: new Set() });
    const unmount = () => { if (apps.delete(app)) app.unmount(); };
    apps.add(app);
    t.after(unmount);
    app.mount({});
    if (!embedded) await ui.connect();
    await settle();
    const client = clients.at(-1);
    assert.equal(ui.state.value, "connecting");
    client.fire("connect");
    assert.equal(ui.state.value, "connected");
    return { ui, client, props, events, unmount, inputFocus: () => inputFocus };
  };
  const keyEvent = (overrides = {}) => ({
    key: "v", ctrlKey: true, metaKey: false, altKey: false, shiftKey: false, repeat: false,
    prevented: 0, stopped: 0,
    preventDefault() { this.prevented += 1; }, stopPropagation() { this.stopped += 1; }, ...overrides,
  });
  const pasteEvent = (text, types = ["text/plain"]) => ({
    ...keyEvent(), clipboardData: { types, getData(type) { assert.equal(type, "text/plain"); return text; } },
  });
  const assertIntercepted = event => {
    assert.equal(event.prevented, 1, "prevent native/noVNC stale paste");
    assert.equal(event.stopped, 1, "stop shortcut before it reaches noVNC");
  };

  await test("embedded and dialog screens wire capture handlers and manual fallback controls", () => {
    const screens = [...source.matchAll(/<div\b[^>]*class="vnc-screen"[^>]*>/g)].map(match => match[0]);
    assert.equal(screens.length, 2);
    for (const screen of screens) {
      assert.match(screen, /ref="screen"/);
      assert.match(screen, /@keydown\.capture="handleKeydown"/);
      assert.match(screen, /@paste\.capture="handlePaste"/);
    }
    const [embedded, dialog] = source.slice(source.indexOf("<template>")).split(/<el-dialog\s/, 2);
    assert.match(embedded, /v-if="embedded"[\s\S]*class="vnc-screen"/);
    assert.match(dialog, /v-else[\s\S]*@opened="connect"[\s\S]*@closed="disconnect"[\s\S]*class="vnc-screen"/);
    assert.equal((source.match(/:disabled="state !== 'connected' \|\| clipboardBusy" @click="openClipboard"/g) ?? []).length, 2);
    assert.match(source, /<textarea[\s\S]*v-model="clipboardText"[\s\S]*:disabled="clipboardBusy"/);
    assert.match(source, /:loading="clipboardBusy"[^>]*@click="pasteClipboard\(async \(\) => clipboardText\)"/);
    assert.doesNotMatch(source, /@close-auto-focus\.prevent/);
  });

  for (const embedded of [true, false]) {
    await test(`${embedded ? "embedded" : "dialog"}: cursor and awaited HTTP before exact remote key sequence`, async t => {
      const { ui, client, props } = await mount(t, embedded);
      assert.equal(client.showDotCursor, true);
      assert.equal(client.viewOnly, false);
      assert.equal(client.focusOnClick, true);
      assert.equal(client.url, `wss://siteflow.test/api/v1/lanes/${encodeURIComponent(props.laneKey)}/vnc`);
      const response = deferred();
      const body = deferred();
      handler = () => response.promise;
      ui.openClipboard();
      ui.clipboardText.value = "\u4e2d\u6587\n\ud83d\ude00\r\n  e\u0301\t";
      const text = ui.clipboardText.value;
      const pasting = ui.pasteClipboard(async () => ui.clipboardText.value);
      assert.equal(ui.clipboardBusy.value, true);
      await settle();
      assert.equal(calls.length, 1);
      assert.equal(calls[0].url, `/api/v1/lanes/${encodeURIComponent(props.laneKey)}/clipboard`);
      assert.equal(calls[0].method, "POST");
      assert.deepEqual(JSON.parse(calls[0].body), { text });
      assert.equal(calls[0].headers.get("Content-Type"), "application/json");
      assert.equal(calls[0].headers.get("X-CSRF-Token"), "test-csrf");
      assert.equal(calls[0].credentials, "include");
      assert.equal(calls[0].signal.aborted, false);
      assert.deepEqual(client.keys, [], "HTTP must finish before any key is sent");
      response.resolve({ ok: true, text: () => body.promise });
      await settle();
      assert.deepEqual(client.keys, [], "response body/ok acknowledgement must also be awaited");
      body.resolve('{"ok":true}');
      await pasting;
      assert.deepEqual(client.keys, expectedKeys);
      assert.deepEqual(client.focuses, [{ preventScroll: true }]);
      assert.equal(ui.clipboardOpen.value, false);
      assert.equal(ui.clipboardText.value, "");
      assert.equal(ui.clipboardBusy.value, false);
      assert.equal(timers.size, 0);
    });
  }

  await test("Unicode, CRLF/multiline, whitespace and empty text are relayed exactly without RFB clipboard", async t => {
    const { ui, client } = await mount(t);
    for (const text of ["\u4e2d\u6587 \ud83d\ude80 \ud834\udd1e e\u0301", "line one\r\n\nline three\n", " \t\n ", ""]) {
      const before = calls.length;
      client.keys.length = 0;
      await ui.pasteClipboard(async () => text);
      assert.equal(calls.length, before + 1);
      assert.deepEqual(JSON.parse(calls.at(-1).body), { text });
      assert.deepEqual(client.keys, expectedKeys);
    }
    assert.deepEqual(client.legacyPastes, []);
  });

  for (const modifier of ["ctrlKey", "metaKey"]) {
    await test(`${modifier} + V is synchronously intercepted before asynchronous clipboard read`, async t => {
      const { ui, client } = await mount(t);
      const read = deferred();
      let reads = 0;
      clipboard = { readText() { reads += 1; return read.promise; } };
      const event = keyEvent({ ctrlKey: false, [modifier]: true, key: "V" });
      ui.handleKeydown(event);
      assertIntercepted(event);
      assert.equal(reads, 1);
      assert.equal(calls.length, 0);
      assert.deepEqual(client.keys, []);
      read.resolve("\u672c\u673a\n\ud83d\ude80");
      await settle();
      assert.deepEqual(JSON.parse(calls[0].body), { text: "\u672c\u673a\n\ud83d\ude80" });
      assert.deepEqual(client.keys, expectedKeys);
    });
  }

  await test("unrelated keys and non-text paste are ignored; repeat shortcuts never start a paste", async t => {
    const { ui, client } = await mount(t);
    clipboard = { readText() { assert.fail("ignored events must not read clipboard"); } };
    for (const overrides of [{ key: "x" }, { ctrlKey: false }, { altKey: true }, { shiftKey: true }, { key: "Insert", shiftKey: true }]) {
      const event = keyEvent(overrides);
      ui.handleKeydown(event);
      assert.equal(event.prevented, 0);
      assert.equal(event.stopped, 0);
    }
    const repeat = keyEvent({ repeat: true });
    ui.handleKeydown(repeat);
    assertIntercepted(repeat);
    for (const event of [pasteEvent("image", ["image/png"]), { ...pasteEvent(""), clipboardData: null }]) {
      ui.handlePaste(event);
      assert.equal(event.prevented, 0);
      assert.equal(event.stopped, 0);
    }
    ui.disconnect();
    const disconnected = keyEvent();
    ui.handleKeydown(disconnected);
    ui.openClipboard();
    await ui.pasteClipboard(async () => { assert.fail("disconnected paste must not read"); });
    assert.equal(disconnected.prevented, 0);
    assert.equal(ui.clipboardOpen.value, false);
    assert.equal(calls.length, 0);
    assert.deepEqual(client.keys, []);
  });

  await test("native text/plain paste uses event text, including empty, without navigator permission", async t => {
    const { ui, client } = await mount(t);
    clipboard = undefined;
    for (const text of ["\u7c98\u8d34\n\ud83d\ude00\r\n ", ""]) {
      client.keys.length = 0;
      const event = pasteEvent(text, ["text/html", "text/plain"]);
      ui.handlePaste(event);
      assertIntercepted(event);
      await settle();
      assert.deepEqual(JSON.parse(calls.at(-1).body), { text });
      assert.deepEqual(client.keys, expectedKeys);
    }
  });

  for (const mode of ["denied", "missing", "missing-readText"]) {
    await test(`${mode} local clipboard opens focused manual fallback, which can send exact text`, async t => {
      const mounted = await mount(t);
      const { ui, client } = mounted;
      clipboard = mode === "denied" ? { readText: async () => { throw new DOMException("Denied", "NotAllowedError"); } }
        : mode === "missing" ? undefined : {};
      const event = keyEvent();
      ui.handleKeydown(event);
      assertIntercepted(event);
      await settle();
      assert.equal(calls.length, 0);
      assert.deepEqual(client.keys, []);
      assert.equal(ui.clipboardOpen.value, true);
      assert.notEqual(ui.clipboardError.value, "");
      assert.equal(ui.clipboardBusy.value, false);
      assert.equal(mounted.inputFocus(), 1);
      ui.clipboardText.value = "\u624b\u52a8\n\ud83d\ude00 ";
      await ui.pasteClipboard(async () => ui.clipboardText.value);
      assert.deepEqual(JSON.parse(calls[0].body), { text: "\u624b\u52a8\n\ud83d\ude00 " });
      assert.deepEqual(client.keys, expectedKeys);
      assert.equal(ui.clipboardError.value, "");
    });
  }

  for (const [name, response] of [
    ["HTTP failure", () => json({ detail: "failed" }, 503)],
    ["network failure", () => { throw new TypeError("offline"); }],
    ["negative acknowledgement", () => json({ ok: false })],
    ["missing acknowledgement", () => json({})],
    ["malformed acknowledgement", () => new Response("not JSON")],
  ]) {
    await test(`${name} never sends remote Ctrl+V and preserves the manual draft`, async t => {
      const { ui, client } = await mount(t);
      handler = response;
      ui.clipboardText.value = "retry me";
      await ui.pasteClipboard(async () => ui.clipboardText.value);
      assert.equal(calls.length, 1);
      assert.deepEqual(client.keys, []);
      assert.deepEqual(client.focuses, []);
      assert.equal(ui.clipboardOpen.value, true);
      assert.equal(ui.clipboardText.value, "retry me");
      assert.notEqual(ui.clipboardError.value, "");
      assert.equal(ui.clipboardBusy.value, false);
      assert.equal(timers.size, 0);
    });
  }

  await test("busy guard coalesces shortcut/native/manual duplicates during read and HTTP", async t => {
    const { ui, client } = await mount(t);
    const read = deferred();
    const response = deferred();
    let reads = 0;
    clipboard = { readText() { reads += 1; return read.promise; } };
    handler = () => response.promise;
    ui.handleKeydown(keyEvent());
    const duplicate = async () => {
      const event = keyEvent();
      ui.handleKeydown(event);
      assertIntercepted(event);
      const paste = pasteEvent("duplicate native");
      ui.handlePaste(paste);
      assertIntercepted(paste);
      await ui.pasteClipboard(async () => { assert.fail("busy manual paste must not read"); });
      ui.openClipboard();
      assert.equal(ui.clipboardOpen.value, false);
      assert.equal(reads, 1);
    };
    await duplicate();
    assert.equal(calls.length, 0);
    read.resolve("first only");
    await settle();
    await duplicate();
    assert.equal(calls.length, 1);
    assert.deepEqual(client.keys, []);
    response.resolve(json());
    await settle();
    assert.deepEqual(client.keys, expectedKeys);
    assert.equal(ui.clipboardBusy.value, false);
  });

  for (const phase of ["read", "HTTP"]) {
    for (const change of ["reconnect", "disconnect", "remote-disconnect", "lane", "close", "unmount"]) {
      await test(`late ${phase} completion after ${change} cannot send keys to old or new RFB`, async t => {
        const mounted = await mount(t);
        const { ui, client, props } = mounted;
        const pending = deferred();
        if (phase === "HTTP") handler = () => pending.promise;
        const pasting = ui.pasteClipboard(() => phase === "read" ? pending.promise : Promise.resolve("old lane text"));
        await settle();
        const signal = calls[0]?.signal;
        if (change === "reconnect") { await ui.connect(); clients.at(-1).fire("connect"); }
        if (change === "disconnect") ui.disconnect();
        if (change === "remote-disconnect") client.fire("disconnect");
        if (change === "lane") props.laneKey = "another lane";
        if (change === "close") props.modelValue = false;
        if (change === "unmount") mounted.unmount();
        await nextTick();
        if (signal && change !== "lane") assert.equal(signal.aborted, true);
        pending.resolve(phase === "read" ? "stale local text" : json());
        await pasting;
        assert.deepEqual(client.keys, []);
        assert.deepEqual(clients.at(-1).keys, []);
        assert.deepEqual(client.focuses, []);
        assert.equal(calls.length, phase === "read" ? 0 : 1);
        assert.equal(ui.clipboardBusy.value, false);
        assert.equal(timers.size, 0);
        if (change === "reconnect") {
          const replacement = clients.at(-1);
          assert.notEqual(replacement, client);
          handler = async () => json();
          await ui.pasteClipboard(async () => "new connection text");
          assert.deepEqual(client.keys, []);
          assert.deepEqual(replacement.keys, expectedKeys);
        }
      });
    }
  }

  await test("stale request finalizer cannot clear a replacement connection's busy guard", async t => {
    const { ui, client } = await mount(t);
    const oldResponse = deferred();
    handler = () => oldResponse.promise;
    const oldPaste = ui.pasteClipboard(async () => "old");
    await settle();
    await ui.connect();
    const replacement = clients.at(-1);
    replacement.fire("connect");
    const newResponse = deferred();
    handler = () => newResponse.promise;
    const newPaste = ui.pasteClipboard(async () => "new");
    await settle();
    oldResponse.resolve(json());
    await oldPaste;
    assert.equal(ui.clipboardBusy.value, true);
    assert.deepEqual(client.keys, []);
    assert.deepEqual(replacement.keys, []);
    await ui.pasteClipboard(async () => { assert.fail("new request still holds busy lock"); });
    assert.equal(calls.length, 2);
    newResponse.resolve(json());
    await newPaste;
    assert.deepEqual(replacement.keys, expectedKeys);
    assert.equal(ui.clipboardBusy.value, false);
  });

  await test("20-second timeout aborts HTTP and never sends keys even if fetch ignores abort", async t => {
    const { ui, client } = await mount(t);
    const response = deferred();
    handler = () => response.promise;
    const pasting = ui.pasteClipboard(async () => "timed out");
    await settle();
    const timer = [...timers.values()][0];
    assert.equal(timer.delay, 20000);
    timer.callback();
    assert.equal(calls[0].signal.aborted, true);
    response.resolve(json());
    await pasting;
    assert.deepEqual(client.keys, []);
    assert.equal(ui.clipboardBusy.value, false);
    assert.equal(timers.size, 0);
  });

  await test("abort-aware HTTP timeout opens a retryable fallback without remote keys", async t => {
    const { ui, client } = await mount(t);
    handler = (_url, options) => new Promise((_resolve, reject) => {
      options.signal.addEventListener("abort", () => reject(new DOMException("Timed out", "AbortError")), { once: true });
    });
    const pasting = ui.pasteClipboard(async () => "timed out");
    await settle();
    [...timers.values()][0].callback();
    await pasting;
    assert.deepEqual(client.keys, []);
    assert.equal(ui.clipboardOpen.value, true);
    assert.notEqual(ui.clipboardError.value, "");
    assert.equal(ui.clipboardBusy.value, false);
    assert.equal(timers.size, 0);
  });

  await test("lane change cannot relay new-lane clipboard text then send Ctrl+V to old-lane RFB", async t => {
    const { ui, client, props } = await mount(t);
    props.laneKey = "new-lane";
    await nextTick();
    await ui.pasteClipboard(async () => "new lane only");
    assert.ok(calls.every(call => call.url === "/api/v1/lanes/new-lane/clipboard"));
    assert.deepEqual(client.keys, [], "the old-lane RFB must never receive the new lane's paste keys");
  });

  await test("clipboard read completing after its deadline reports timeout and opens fallback", async t => {
    const { ui, client } = await mount(t);
    const read = deferred();
    const pasting = ui.pasteClipboard(() => read.promise);
    [...timers.values()][0].callback();
    read.resolve("too late");
    await pasting;
    assert.equal(calls.length, 0);
    assert.deepEqual(client.keys, []);
    assert.equal(ui.clipboardBusy.value, false);
    assert.equal(timers.size, 0);
    assert.notEqual(ui.clipboardError.value, "", "timeout must not look like a successful paste");
    assert.equal(ui.clipboardOpen.value, true);
  });

  await test("size guard counts Unicode code points, accepts the limit, rejects limit + 1 before HTTP", async t => {
    const { ui, client } = await mount(t);
    for (const text of ["a".repeat(1048577), "\ud83d\ude00".repeat(1048577)]) {
      await ui.pasteClipboard(async () => text);
      assert.equal(calls.length, 0);
      assert.deepEqual(client.keys, []);
      assert.equal(ui.clipboardOpen.value, true);
      assert.match(ui.clipboardError.value, /1048576/);
      assert.equal(ui.clipboardBusy.value, false);
    }
    for (const text of ["a".repeat(1048576), "\ud83d\ude00".repeat(1048576)]) {
      client.keys.length = 0;
      await ui.pasteClipboard(async () => text);
      assert.deepEqual(JSON.parse(calls.at(-1).body), { text });
      assert.deepEqual(client.keys, expectedKeys);
    }
    assert.equal(calls.length, 2);
    assert.equal(timers.size, 0);
  });

  const copyKeys = [...expectedKeys.slice(0, 9), [0x63, "KeyC"], expectedKeys.at(-1)];
  const fireTimer = delay => {
    const entry = [...timers.entries()].find(([, timer]) => timer.delay === delay);
    assert.ok(entry, `expected ${delay}ms timer`);
    timers.delete(entry[0]);
    entry[1].callback();
  };

  await test("both toolbars expose copy-out and the read-only fallback is separate from the paste draft", () => {
    assert.equal((source.match(/@click="copyClipboard\(\)">复制到本机/g) ?? []).length, 2);
    assert.match(source, /ref="receiveInput"[\s\S]*:value="receiveText"[\s\S]*readonly/);
    assert.match(source, /@click="copyClipboard\(false, true\)"/);
    assert.match(source, /@close="clearReceivedClipboard"/);
    assert.match(source, /仅在页面位于前台且获得焦点/);
    assert.doesNotMatch(source, /setInterval/);
  });

  for (const modifier of ["ctrlKey", "metaKey"]) {
    await test(`${modifier}+C releases modifiers, sends Linux Ctrl+C, waits 300ms, then GETs exact Unicode`, async t => {
      const { ui, client, props } = await mount(t);
      const writes = [];
      const write = deferred();
      const body = deferred();
      const text = "\u4e2d\u6587 \ud83d\ude80 \ud834\udd1e e\u0301\r\n\t ";
      handler = () => {
        assert.deepEqual(client.keys, copyKeys, "remote action must precede GET");
        return { ok: true, text: () => body.promise };
      };
      clipboard = { writeText(value) { writes.push(value); return write.promise; } };
      const event = keyEvent({ ctrlKey: false, [modifier]: true, key: "C" });
      ui.handleKeydown(event);
      assertIntercepted(event);
      assert.deepEqual(client.keys, copyKeys);
      assert.equal(calls.length, 0);
      await settle();
      assert.equal(calls.length, 0, "no GET before bounded settling time");
      fireTimer(300);
      await settle();
      assert.equal(calls.length, 1);
      assert.equal(calls[0].url, `/api/v1/lanes/${encodeURIComponent(props.laneKey)}/clipboard`);
      assert.equal(calls[0].method, "GET");
      assert.equal(calls[0].cache, "no-store");
      assert.equal(calls[0].credentials, "include");
      assert.deepEqual(writes, [], "response body must be awaited");
      body.resolve(JSON.stringify({ text }));
      await settle();
      assert.deepEqual(writes, [text]);
      assert.equal(ui.clipboardBusy.value, true);
      assert.doesNotMatch(ui.receiveStatus.value, /已复制/);
      write.resolve();
      await settle();
      assert.match(ui.receiveStatus.value, /已复制/);
      assert.equal(ui.receiveText.value, "", "successful shortcut must not retain sensitive text");
      assert.equal(ui.receiveOpen.value, false);
      assert.equal(ui.clipboardBusy.value, false);
      assert.equal(timers.size, 0);
    });
  }

  await test("RFB event is only a bounded signal for explicit copy, its lossy text is never written", async t => {
    const { ui, client } = await mount(t);
    const writes = [];
    const text = "\u8fdc\u7aef\u6587\u672c\ud83d\ude80";
    handler = () => json({ text });
    clipboard = { writeText: async value => { writes.push(value); } };
    client.fire("clipboard", { detail: { text: "unsolicited" } });
    await settle();
    assert.equal(calls.length, 0);
    const copying = ui.copyClipboard(true);
    assert.equal(calls.length, 0);
    client.fire("clipboard", { detail: { get text() { assert.fail("never read lossy RFB text"); } } });
    await copying;
    assert.deepEqual(writes, [text]);
    assert.equal(calls.length, 1);
    assert.equal(timers.size, 0);
    client.fire("clipboard", { detail: { text: "later context menu copy" } });
    await settle();
    assert.equal(calls.length, 1, "no unsolicited sync or polling after completion");
    assert.deepEqual(writes, [text]);
  });

  await test("toolbar retrieves existing remote context-menu copy without sending any keys or waiting", async t => {
    const { ui, client } = await mount(t);
    const writes = [];
    clipboard = { writeText: async text => { writes.push(text); } };
    handler = () => json({ text: "context menu \u4e2d\u6587" });
    ui.clipboardText.value = "unsent local paste draft";
    await ui.copyClipboard();
    assert.deepEqual(client.keys, []);
    assert.deepEqual(writes, ["context menu \u4e2d\u6587"]);
    assert.equal(ui.clipboardText.value, "unsent local paste draft");
    assert.equal(timers.size, 0);
    await settle();
    assert.equal(calls.length, 1);
  });

  for (const mode of ["denied", "missing", "missing-writeText", "insecure-http"]) {
    await test(`${mode} copy-out offers selected read-only text and fresh-click retry without refetch`, async t => {
      const { ui } = await mount(t);
      const text = "\u624b\u52a8\u590d\u5236 \ud83d\ude00\r\n";
      handler = () => json({ text });
      clipboard = mode === "denied" ? { writeText: async () => { throw new DOMException("Denied", "NotAllowedError"); } }
        : mode === "missing-writeText" ? {} : undefined;
      const oldProtocol = window.location.protocol;
      if (mode === "insecure-http") window.location.protocol = "http:";
      let focused = 0;
      let selected = 0;
      ui.receiveInput.value = { focus() { focused += 1; }, select() { selected += 1; } };
      ui.clipboardText.value = "preserve paste input";
      try {
        await ui.copyClipboard();
        assert.equal(ui.receiveOpen.value, true);
        assert.equal(ui.receiveText.value, text);
        assert.equal(ui.clipboardText.value, "preserve paste input");
        assert.match(ui.receiveStatus.value, /HTTP|权限/);
        assert.doesNotMatch(ui.receiveStatus.value, /已复制/);
        assert.equal(focused, 1);
        assert.equal(selected, 1);
        assert.equal(ui.clipboardBusy.value, false);
        const writes = [];
        clipboard = { writeText(value) { writes.push(value); return Promise.resolve(); } };
        const retry = ui.copyClipboard(false, true);
        assert.deepEqual(writes, [text], "fresh click invokes writeText synchronously, preserving activation");
        await retry;
        assert.equal(calls.length, 1, "fresh-click write uses already fetched text");
        assert.match(ui.receiveStatus.value, /已复制/);
        ui.clearReceivedClipboard();
        assert.equal(ui.receiveOpen.value, false);
        assert.equal(ui.receiveText.value, "");
        assert.equal(ui.receiveStatus.value, "");
        assert.equal(timers.size, 0);
      } finally {
        window.location.protocol = oldProtocol;
      }
    });
  }

  await test("empty remote clipboard never clears host clipboard; whitespace remains valid text", async t => {
    const { ui } = await mount(t);
    const writes = [];
    clipboard = { writeText: async text => { writes.push(text); } };
    handler = () => json({ text: "" });
    await ui.copyClipboard();
    assert.deepEqual(writes, []);
    assert.match(ui.receiveStatus.value, /为空/);
    assert.equal(ui.receiveOpen.value, true);
    assert.equal(ui.receiveText.value, "");
    ui.clearReceivedClipboard();
    handler = () => json({ text: " \t\n" });
    await ui.copyClipboard();
    assert.deepEqual(writes, [" \t\n"]);
    assert.equal(timers.size, 0);
  });

  for (const failure of ["HTTP", "network", "malformed", "missing-text", "non-string", "null"]) {
    await test(`copy GET ${failure} reports failure without writing stale buffered text`, async t => {
      const { ui, client } = await mount(t);
      clipboard = { writeText() { assert.fail("failed GET must not write clipboard"); } };
      handler = () => {
        if (failure === "HTTP") return json({ detail: "failed" }, 503);
        if (failure === "network") throw new TypeError("offline");
        if (failure === "malformed") return new Response("not JSON");
        return json(failure === "missing-text" ? {} : failure === "null" ? null : { text: 123 });
      };
      ui.receiveText.value = "stale buffer";
      await ui.copyClipboard();
      assert.equal(ui.receiveText.value, "");
      assert.equal(ui.receiveOpen.value, true);
      assert.match(ui.receiveStatus.value, /失败/);
      assert.equal(ui.clipboardBusy.value, false);
      assert.deepEqual(client.keys, []);
      assert.equal(timers.size, 0);
    });
  }

  await test("copy filters composing/editable events and coalesces repeat/native/paste duplicates", async t => {
    const { ui, client } = await mount(t);
    for (const overrides of [
      { isComposing: true }, { target: { closest: () => ({}) } }, { target: { isContentEditable: true } },
      { altKey: true }, { shiftKey: true }, { ctrlKey: false }, { key: "x" },
    ]) {
      const event = keyEvent({ key: "c", ...overrides });
      ui.handleKeydown(event);
      assert.equal(event.prevented, 0);
    }
    const repeat = keyEvent({ key: "c", repeat: true });
    ui.handleKeydown(repeat);
    assertIntercepted(repeat);
    assert.deepEqual(client.keys, []);
    const response = deferred();
    handler = () => response.promise;
    const copying = ui.copyClipboard(true);
    ui.handleKeydown(keyEvent({ key: "c" }));
    ui.handleKeydown(keyEvent());
    ui.handlePaste(pasteEvent("not while copying"));
    await ui.copyClipboard();
    assert.deepEqual(client.keys, copyKeys);
    assert.equal(calls.length, 0);
    fireTimer(300);
    await settle();
    await ui.copyClipboard();
    assert.equal(calls.length, 1);
    response.resolve(json({ text: "one copy only" }));
    await copying;
    assert.equal(timers.size, 0);
  });

  await test("local paste POST echo never retrieves or rewrites the host clipboard", async t => {
    const { ui, client } = await mount(t);
    clipboard = { writeText() { assert.fail("POST echo must never write to host clipboard"); } };
    handler = () => {
      client.fire("clipboard", { detail: { text: "paste echo" } });
      return json();
    };
    await ui.pasteClipboard(async () => "\u672c\u673a\u7c98\u8d34");
    client.fire("clipboard", { detail: { text: "delayed paste echo" } });
    await settle();
    assert.equal(calls.length, 1);
    assert.equal(calls[0].method, "POST");
    assert.deepEqual(client.keys, expectedKeys);
    assert.equal(timers.size, 0);
  });

  await test("composing and local textarea shortcuts preserve both local copy and local paste", async t => {
    const { ui, client } = await mount(t);
    clipboard = {
      readText() { assert.fail("local textarea/composition must not read host clipboard"); },
      writeText() { assert.fail("local textarea/composition must not write host clipboard"); },
    };
    for (const key of ["v", "c"]) {
      for (const overrides of [{ isComposing: true }, { target: { closest: () => ({ tagName: "TEXTAREA" }) } }]) {
        const event = keyEvent({ key, ...overrides });
        ui.handleKeydown(event);
        assert.equal(event.prevented, 0);
        assert.equal(event.stopped, 0);
      }
    }
    assert.equal(calls.length, 0);
    assert.deepEqual(client.keys, []);
    assert.equal(timers.size, 0);
  });

  await test("remote copy key failure releases Ctrl and never fetches or writes clipboard", async t => {
    const { ui, client } = await mount(t);
    client.sendKey = (...args) => {
      client.keys.push(args);
      if (args[1] === "KeyC") throw new Error("remote key failure");
    };
    clipboard = { writeText() { assert.fail("failed remote copy must not write"); } };
    await ui.copyClipboard(true);
    assert.deepEqual(client.keys, copyKeys);
    assert.equal(calls.length, 0);
    assert.match(ui.receiveStatus.value, /失败/);
    assert.equal(ui.receiveOpen.value, true);
    assert.equal(ui.clipboardBusy.value, false);
    assert.equal(timers.size, 0);
    client.fire("clipboard");
    await settle();
    assert.equal(calls.length, 0);
  });

  for (const focus of ["background", "hidden", "focus-lost-during-fetch"]) {
    await test(`${focus}: no host clipboard write without foreground focus`, async t => {
      const { ui } = await mount(t);
      const response = deferred();
      handler = () => response.promise;
      clipboard = { writeText() { assert.fail("background write forbidden"); } };
      if (focus === "background") document.hasFocus = () => false;
      if (focus === "hidden") document.visibilityState = "hidden";
      const copying = ui.copyClipboard();
      if (focus === "focus-lost-during-fetch") document.hasFocus = () => false;
      response.resolve(json({ text: "\u654f\u611f\u6587\u672c" }));
      await copying;
      assert.equal(ui.receiveOpen.value, true);
      assert.equal(ui.receiveText.value, "\u654f\u611f\u6587\u672c");
      assert.match(ui.receiveStatus.value, /前台/);
      assert.equal(timers.size, 0);
    });
  }

  for (const phase of ["settling", "HTTP", "write"]) {
    for (const change of ["reconnect", "disconnect", "remote-disconnect", "lane", "close", "unmount", "securityfailure", "fallback-close"]) {
      await test(`copy-out late ${phase} after ${change} cannot invoke a stale host write or success`, async t => {
        const mounted = await mount(t);
        const { ui, client, props } = mounted;
        const pending = deferred();
        const writes = [];
        handler = () => phase === "HTTP" ? pending.promise : json({ text: "old lane text" });
        clipboard = { writeText(text) { writes.push(text); return pending.promise; } };
        const copying = ui.copyClipboard(phase === "settling");
        await settle();
        const signal = calls[0]?.signal;
        if (change === "reconnect") { await ui.connect(); clients.at(-1).fire("connect"); }
        if (change === "disconnect") ui.disconnect();
        if (change === "remote-disconnect") client.fire("disconnect");
        if (change === "lane") props.laneKey = "different lane";
        if (change === "close") props.modelValue = false;
        if (change === "unmount") mounted.unmount();
        if (change === "securityfailure") client.fire("securityfailure");
        if (change === "fallback-close") ui.clearReceivedClipboard();
        await nextTick();
        await copying;
        pending.resolve(phase === "HTTP" ? json({ text: "stale Unicode \u4e2d\u6587" }) : undefined);
        client.fire("clipboard");
        await settle();
        if (signal) assert.equal(signal.aborted, true);
        assert.deepEqual(writes, phase === "write" ? ["old lane text"] : []);
        assert.equal(ui.receiveText.value, "");
        assert.equal(ui.receiveStatus.value, "");
        assert.equal(ui.receiveOpen.value, false);
        assert.equal(ui.clipboardBusy.value, false);
        assert.equal(calls.length, phase === "settling" ? 0 : 1);
        assert.equal(timers.size, 0);
      });
    }
  }

  await test("copy finalizer cannot release a replacement operation's busy guard or signal", async t => {
    const { ui, client } = await mount(t);
    const response = deferred();
    handler = () => response.promise;
    const oldCopy = ui.copyClipboard();
    await ui.connect();
    const replacement = clients.at(-1);
    replacement.fire("connect");
    const newCopy = ui.copyClipboard(true);
    await oldCopy;
    response.resolve(json({ text: "stale" }));
    await settle();
    assert.equal(ui.clipboardBusy.value, true);
    handler = () => json({ text: "new" });
    client.fire("clipboard");
    await settle();
    assert.equal(calls.length, 1, "old RFB signal must not release new wait");
    replacement.fire("clipboard");
    await newCopy;
    assert.equal(calls.length, 2);
    assert.equal(ui.clipboardBusy.value, false);
    assert.match(ui.receiveStatus.value, /已复制/);
    assert.equal(timers.size, 0);
  });

  for (const phase of ["HTTP", "write"]) {
    await test(`20-second copy ${phase} timeout frees busy guard even when API ignores cancellation`, async t => {
      const { ui } = await mount(t);
      const pending = deferred();
      const writes = [];
      handler = () => phase === "HTTP" ? pending.promise : json({ text: "readable fallback" });
      clipboard = { writeText(text) { writes.push(text); return pending.promise; } };
      const copying = ui.copyClipboard();
      await settle();
      fireTimer(20000);
      await copying;
      assert.equal(calls[0].signal.aborted, true);
      assert.equal(ui.clipboardBusy.value, false);
      assert.equal(ui.receiveOpen.value, true);
      assert.match(ui.receiveStatus.value, /超时/);
      assert.doesNotMatch(ui.receiveStatus.value, /已复制/);
      pending.resolve(phase === "HTTP" ? json({ text: "too late" }) : undefined);
      await settle();
      assert.deepEqual(writes, phase === "write" ? ["readable fallback"] : []);
      assert.doesNotMatch(ui.receiveStatus.value, /已复制/);
      assert.equal(timers.size, 0);
    });
  }

  assert.ok(clients.every(client => client.legacyPastes.length === 0));
} finally {
  for (const app of apps) app.unmount();
  await server.close();
  for (const [key, descriptor] of originals) {
    if (descriptor) Object.defineProperty(globalThis, key, descriptor);
    else delete globalThis[key];
  }
}
