import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { readFile } from "node:fs/promises";
import { createServer } from "vite";
import { createRenderer, createSSRApp, h, nextTick, ssrContextKey } from "vue";
import { renderToString } from "vue/server-renderer";
import ElementPlus, { ID_INJECTION_KEY, ZINDEX_INJECTION_KEY } from "element-plus";
import * as Elements from "element-plus";

const originals = new Map(["fetch", "document", "localStorage", "ResizeObserver", "getComputedStyle", "setInterval", "clearInterval"]
  .map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]));
const calls = [];
const navigation = [];
const messages = [];
const apps = new Set();
const timers = new Map();
const listeners = new Map();
const observers = [];
const stored = new Map();
const writes = [];
const storageKey = "siteflow.dashboard.layout.v1";
let storageFailure = false;
let clearCount = 0;
let timerId = 0;
let seed;
let handler;
const json = (value, status = 200) => new Response(JSON.stringify(value), { status });
const settle = async () => {
  await new Promise(resolve => setImmediate(resolve));
  await nextTick();
};
const resourceView = {
  cpu_percent: 12.5, memory_percent: 25, memory_used_bytes: 1024 ** 3,
  memory_total_bytes: 4 * 1024 ** 3, scope: "API host", sampled_at: "2026-11-25T00:00:00Z", error: null,
  docker_available: false, siteflow_cpu_percent: null, siteflow_memory_percent: null,
  siteflow_memory_used_bytes: null, siteflow_container_count: 0, docker_error: null,
};
const matrixCell = (status, counts = { [status]: 1 }) => ({
  status, attempts: Object.values(counts).reduce((sum, count) => sum + count, 0), counts,
  last_run_id: 812, last_job_id: 22, last_message: "",
});
const dailyMatrixView = {
  start_date: "2026-11-25", end_date: "2026-11-25", dates: ["2026-11-25"], sites: [
    { site_id: 101, name: "site-101", enabled: true, cells: { "2026-11-25": matrixCell("success") } },
    { site_id: 102, name: "site-102", enabled: false, cells: { "2026-11-25": matrixCell("deferred") } },
  ],
};
const lanesView = { worker_connected: true, lanes: [
  { key: "browser-1", status: "running" }, { key: "browser-2", status: "idle" },
  { key: "http-1", status: "running" }, { key: "http-2", status: "queued" },
] };
const job = (id, site_id, status, site_name = `site-${site_id}`) => ({ id, site_id, site_name, status });
const runView = {
  id: 812, created_at: "2026-11-25T00:00:00Z", status: "success", site_count: 2,
  jobs: [job(22, 101, "success"), job(23, 102, "deferred")],
};
const value = { value: null, source: "unset", editable: true, error: null };
const token = { configured: false, source: "unset", editable: true, masked: null };
const integrationsView = {
  proxy: { enabled: false, source: "unset", editable: true, protocol: null, url_masked: null, error: null },
  twofauth: { enabled: false, url: value, token, error: null },
  cfbox: { enabled: false, url: value, api_key: token, error: null },
  bitwarden: { enabled: false, enabled_setting: false, enabled_source: "unset", url: value, token,
    default_url: "http://vault.invalid", source: "unset", error: null },
};
const schedulerView = { enabled: true, enabled_source: "default", mode: "cron", cron: "0 10 * * *",
  interval_seconds: 3600, ranges: [], source: "default", editable: true, timezone: "UTC", next_run_at: null };
const defaultResponse = path => {
  switch (path) {
    case "/sites?view=options&include_valid=true": return [{ id: 101, enabled: true }, { id: 102, enabled: false }];
    case "/sites?view=options": return [];
    case "/templates": return [{ id: 301 }];
    case "/features": return [{ key: "flow.open" }, { key: "flow.result" }];
    case "/lanes": return lanesView;
    case "/analytics/matrix?days=1&include_disabled=false": return dailyMatrixView;
    case "/analytics/trends?days=14": return { total: 5, success: 3, fail: 1, deferred: 1, success_rate: 60, start_date: "2026-11-25", points: [] };
    case "/analytics/matrix?days=14&include_disabled=false": return { sites: [], dates: [] };
    case "/runs?limit=1": return [{ id: runView.id }];
    case `/runs/${runView.id}`: return runView;
    case "/system-settings": return { show_system_resources: false };
    case "/system-resources": return resourceView;
    case "/integrations": return integrationsView;
    case "/scheduler-settings": return schedulerView;
    case "/artifacts/stats": return { total_count: 0, total_bytes: 0, retention_days: 30, cleanup_interval_seconds: 3600 };
    default:
      if (path.startsWith("/analytics/failures?") && new URLSearchParams(path.split("?")[1]).get("days") === "14")
        return { items: [], total: 0 };
      throw new Error(`Unexpected API request: ${path}`);
  }
};

globalThis.document = {
  cookie: "", hidden: false,
  addEventListener(name, callback) {
    if (!listeners.has(name)) listeners.set(name, new Set());
    listeners.get(name).add(callback);
  },
  removeEventListener(name, callback) { listeners.get(name)?.delete(callback); },
};
globalThis.localStorage = {
  getItem(key) { if (storageFailure) throw new Error("storage blocked"); return stored.get(key) ?? null; },
  setItem(key, content) {
    if (storageFailure) throw new Error("storage blocked");
    writes.push({ key, content }); stored.set(key, content);
  },
};
globalThis.setInterval = (callback, delay) => { const id = ++timerId; timers.set(id, { callback, delay }); return id; };
globalThis.clearInterval = id => timers.delete(id);
globalThis.ResizeObserver = class {
  constructor(callback) { this.callback = callback; this.elements = []; observers.push(this); }
  observe(element) { this.elements.push(element); }
  disconnect() { this.disconnected = true; }
};
globalThis.getComputedStyle = element => {
  assert.ok(element.computedStyle, "layout mocks must declare their computed styles");
  return element.computedStyle;
};
globalThis.fetch = async (url, options = {}) => {
  assert.ok(url.startsWith("/api/v1/"), url);
  const path = url.slice("/api/v1".length);
  const call = { path, method: options.method ?? "GET", body: options.body };
  calls.push(call);
  return handler ? handler(call) : json(defaultResponse(path));
};
globalThis.__dashboardLayoutTest = {
  seed: (ui, name) => seed?.(ui, name),
  useRouter: () => ({
    async push(location) { navigation.push({ action: "push", ...location }); },
    async replace(location) { navigation.push({ action: "replace", ...location }); },
  }),
  useAuthStore: () => ({ clear() { clearCount++; } }),
  ElMessage: Object.fromEntries(["success", "error", "warning"].map(type => [type, message => messages.push({ type, message })])),
  ElMessageBox: { async confirm() { throw new Error("Unexpected confirmation"); } },
};

const server = await createServer({
  root: fileURLToPath(new URL("..", import.meta.url)),
  optimizeDeps: { noDiscovery: true, include: [] },
  server: { middlewareMode: true, hmr: false },
  plugins: [{ name: "dashboard-layout-harness", enforce: "pre", transform(source, id) {
    const name = id.split("/").at(-1);
    const exposed = {
      "DashboardView.vue": "loading, refreshing, errors, sites, templates, features, lanes, runningLanes, dailyMatrix, dailySites, dailySuccessRate, latestRun, resources, showResources, layoutDialog, layout, draftLayout, displayTimezone, visibleCards, enabledSites, finalJobs, slices, pieStyle, resourceMetrics, load, editLayout, moveCard, saveLayout, formatDate, openRun, openAnalytics",
      "AnalyticsView.vue": "days, overviewLoading, failureLoading, error, failureError",
      "SettingsView.vue": "activeTab, settingsTabs, systemSettings, showSystemResources, systemSettingsLoading, systemSettingsSaving, systemSettingsError, loadSystemSettings, saveSystemSettings",
      "HorizontalTabs.vue": "scroller, panel, sizePanel, dragging, focusIndex, reveal, onWheel, onPointerDown, onPointerMove, stopDrag, onClick, onKeydown",
      "ResourceRing.vue": "host, siteflow, segments, accessibleLabel",
    }[name];
    if (!exposed) return;
    // Keep the real API client (including ApiError); replace only external UI/store dependencies.
    return source
      .replace('import { useRouter } from "vue-router";', 'const { useRouter } = globalThis.__dashboardLayoutTest;')
      .replace('import { useAuthStore } from "@/stores/auth";', 'const { useAuthStore } = globalThis.__dashboardLayoutTest;')
      .replace(/import \{ ElMessage(?:, ElMessageBox)? \} from "element-plus";/,
        'const { ElMessage, ElMessageBox } = globalThis.__dashboardLayoutTest;')
      // These unrelated editors import browser-only pickers or start their own requests.
      .replace('import StringListField from "@/components/StringListField.vue";', 'const StringListField = { render: () => null };')
      .replace('import ModelSettingsCard from "@/components/ModelSettingsCard.vue";', 'const ModelSettingsCard = { render: () => null };')
      .replace("</script>", `const testExposed = { ${exposed} }; defineExpose(testExposed); globalThis.__dashboardLayoutTest.seed(testExposed, ${JSON.stringify(name)});\n</script>`);
  } }],
});
const renderer = createRenderer({ createComment: () => ({}), insert() {}, remove() {}, parentNode() {}, nextSibling() {} });
const unmount = app => { if (apps.delete(app)) app.unmount(); };
// Mount setup inside a real instance so lifecycle hooks, watchers and defineModel run normally.
const mount = async (Component, props = {}, configure) => {
  let ui;
  const app = renderer.createApp({ props: Component.props, emits: Component.emits, setup(actualProps, context) {
    Component.setup(actualProps, { ...context, expose(exposed) { ui = exposed; } });
    return configure?.(ui, actualProps) ?? (() => null);
  } }, props);
  app.provide(ssrContextKey, { modules: new Set() });
  app.mount({}); apps.add(app);
  await settle();
  return { ui, app };
};
const render = async (Component, configure, props = {}, configureApp) => {
  seed = (ui, name) => { if (name === `${Component.__name}.vue`) configure?.(ui); };
  try {
    const app = createSSRApp(Component, props);
    if (Component.__name === "SettingsView") {
      // The scheduler's deprecated radio API is outside this suite. Keep the actual
      // switch, inputs, alerts and buttons, but render its radio group as a slot.
      for (const [name, component] of Object.entries(Elements)) {
        if (/^El/.test(name) && component && (typeof component === "function" || component.setup || component.render || component.props))
          app.component(name, name === "ElRadioGroup" ? { setup: (_, { slots }) => () => h("div", slots.default?.()) } : component);
      }
      app.use(Elements.ElLoading);
    } else app.use(ElementPlus);
    app
      .provide(ID_INJECTION_KEY, { prefix: 0, current: 0 }).provide(ZINDEX_INJECTION_KEY, { current: 0 });
    configureApp?.(app);
    return await renderToString(app);
  } finally { seed = undefined; }
};

let passed = 0;
const test = async (name, run) => {
  calls.length = 0; navigation.length = 0; messages.length = 0;
  stored.clear(); writes.length = 0; storageFailure = false; clearCount = 0; handler = undefined;
  try {
    await run(); passed++; console.log(`ok ${passed} - ${name}`);
  } finally {
    for (const app of [...apps]) unmount(app);
    assert.equal(timers.size, 0, "mounted views must clean up polling timers");
    assert.equal(listeners.get("visibilitychange")?.size ?? 0, 0, "dashboard must remove its visibility listener");
  }
};

try {
  const { dashboardCards, defaultDashboardLayout, parseDashboardLayout, dashboardStatus, dashboardStatuses } =
    await server.ssrLoadModule("/src/utils/dashboard.ts");
  const { default: Dashboard } = await server.ssrLoadModule("/src/views/DashboardView.vue");
  const { default: Settings } = await server.ssrLoadModule("/src/views/SettingsView.vue");
  const { default: Analytics } = await server.ssrLoadModule("/src/views/AnalyticsView.vue");
  const { default: Tabs } = await server.ssrLoadModule("/src/components/HorizontalTabs.vue");
  const { default: Header } = await server.ssrLoadModule("/src/components/PageHeader.vue");
  const { default: ResourceRing } = await server.ssrLoadModule("/src/components/ResourceRing.vue");
  const defaults = defaultDashboardLayout();
  const keys = Object.keys(dashboardCards);
  const copyDashboard = ui => target => {
    for (const key of ["loading", "refreshing", "errors", "sites", "templates", "features", "lanes", "dailyMatrix", "latestRun", "resources", "showResources", "layout", "displayTimezone"])
      target[key].value = ui[key].value;
  };
  const cardTitles = html => [...html.matchAll(/<h2[^>]*>(.*?)<\/h2>/g)].map(match => match[1]);
  const expectedTitles = layout => layout.map(card => dashboardCards[card.key]);

  await test("resource rings clamp finite percentages and partition one SVG without adding SiteFlow twice", async () => {
    const { ui, app } = await mount(ResourceRing, { label: "CPU", hostPercent: 0, siteflowPercent: null, detail: "Host capacity" });
    for (const [hostPercent, siteflowPercent, host, siteflow] of [
      [75, null, 75, null], [-20, null, 0, null], [140, null, 100, null],
      [0, null, 0, null], [100, null, 100, null], [37.5, 12.5, 37.5, 12.5],
      [75, 20, 75, 20], [75, 200, 75, 75], [75, -10, 75, 0],
      [0, 10, 0, 0], [140, 120, 100, 100], [75, 0, 75, 0],
      [75, NaN, 75, null], [75, Infinity, 75, null], [75, -Infinity, 75, null],
    ]) {
      const props = { label: "CPU", hostPercent, siteflowPercent, detail: "Host capacity" };
      Object.assign(app._instance.props, props); await nextTick();
      assert.equal(ui.host.value, host); assert.equal(ui.siteflow.value, siteflow);
      const expected = siteflow === null
        ? [["\u5bbf\u4e3b\u673a\u5df2\u7528", host, 0], ["\u7a7a\u95f2", 100 - host, host]]
        : [["SiteFlow", siteflow, 0], ["\u5176\u4ed6\u5df2\u7528", host - siteflow, siteflow], ["\u7a7a\u95f2", 100 - host, host]];
      assert.deepEqual(ui.segments.value.map(({ label, value, offset }) => [label, value, offset]), expected);
      assert.equal(ui.segments.value.reduce((sum, segment) => sum + segment.value, 0), 100);
      const accessible = `CPU\uff0c\u5bbf\u4e3b\u673a\u5df2\u7528 ${host.toFixed(1)}%\uff0c${expected.map(([label, value]) => `${label} ${value.toFixed(1)}%`).join("\uff0c")}`;
      assert.equal(ui.accessibleLabel.value, accessible);
      const html = await render(ResourceRing, undefined, props);
      const charts = [...html.matchAll(/<svg\b[^>]*>([\s\S]*?)<\/svg>/g)];
      assert.equal(charts.length, 1, "all segments share one chart");
      assert.match(charts[0][0], /role="img"/);
      assert.ok(charts[0][0].includes(`aria-label="${accessible}"`));
      const circles = [...charts[0][1].matchAll(/<circle\b[^>]*>/g)].map(match => match[0]);
      const segments = circles.filter(tag => tag.includes("stroke-dasharray="));
      assert.equal(segments.length, expected.length);
      for (const [index, tag] of segments.entries()) {
        const [, value, offset] = expected[index];
        assert.match(tag, /pathLength="100"/);
        assert.ok(tag.includes(`stroke-dasharray="${value} ${100 - value}"`));
        assert.ok(tag.includes(`stroke-dashoffset="${-offset}"`));
      }
      assert.equal(new Set(circles.map(tag => tag.match(/cx="[^"]+" cy="[^"]+" r="[^"]+"/)?.[0])).size, 1,
        "used and idle segments must not become concentric rings");
      assert.match(html, new RegExp(`class="resource-ring__value"[^>]*>${host.toFixed(1).replace(".", "\\.")}%<`));
      assert.match(html, /<h3[^>]*>CPU<\/h3>/); assert.match(html, /Host capacity/);
      const legends = [...html.matchAll(/class="resource-ring__legend"[^>]*>([\s\S]*?)<\/div>/g)].map(match => match[1]);
      assert.equal(legends.length, expected.length);
      for (const [index, [label, value]] of expected.entries()) {
        assert.ok(legends[index].includes(label));
        assert.ok(legends[index].includes(`${value.toFixed(1)}%`), "zero is rendered as 0.0%, not unavailable");
      }
      assert.doesNotMatch(html, /NaN|Infinity|>--</);
    }
  });

  await test("invalid host samples show unavailable state and no segments even with valid SiteFlow data", async () => {
    const { ui, app } = await mount(ResourceRing, { label: "CPU", hostPercent: null, siteflowPercent: 20, detail: "Unavailable" });
    for (const hostPercent of [null, NaN, Infinity, -Infinity]) {
      app._instance.props.hostPercent = hostPercent; await nextTick();
      assert.equal(ui.host.value, null); assert.equal(ui.siteflow.value, null);
      assert.deepEqual(ui.segments.value, []);
      assert.equal(ui.accessibleLabel.value, "CPU\u8d44\u6e90\u6682\u4e0d\u53ef\u7528");
      const html = await render(ResourceRing, undefined, { label: "CPU", hostPercent, siteflowPercent: 20, detail: "Unavailable" });
      assert.match(html, /class="resource-ring__value"[^>]*>--</);
      assert.match(html, /role="img" aria-label="CPU\u8d44\u6e90\u6682\u4e0d\u53ef\u7528"/);
      assert.match(html, /\u91c7\u6837\u6682\u4e0d\u53ef\u7528/);
      assert.doesNotMatch(html, /stroke-dasharray|resource-ring__legend|NaN|Infinity/);
    }
    app._instance.props.hostPercent = 0; await nextTick();
    assert.equal(ui.host.value, 0); assert.equal(ui.segments.value.length, 3, "a later valid zero sample recovers");
  });

  await test("layout defaults cover the registry, preserve widths and return independent data", () => {
    assert.deepEqual(new Set(defaults.map(card => card.key)), new Set(keys));
    assert.equal(defaults.length, keys.length);
    for (const key of ["overview", "analytics", "latest-run", "resources"]) assert.ok(keys.includes(key));
    assert.equal(defaults.find(card => card.key === "analytics").width, "half");
    assert.equal(defaults.find(card => card.key === "latest-run").width, "half");
    assert.equal(defaults.find(card => card.key === "resources").width, "full");
    assert.equal(defaults.find(card => card.key === "overview").width, "full");
    const changed = defaultDashboardLayout(); changed[0].width = "invalid"; changed.reverse();
    assert.deepEqual(defaultDashboardLayout(), defaults);
  });

  await test("layout parsing tolerates malformed JSON, invalid shapes, duplicates and unknown keys", () => {
    for (const raw of [null, "", "{", "null", "true", "42", '"layout"', "{}", "[]"])
      assert.deepEqual(parseDashboardLayout(raw), defaults, String(raw));
    const parsed = parseDashboardLayout(JSON.stringify([
      null, 1, [], {}, { key: "analytics" }, { width: "full" },
      { key: "not-a-card", width: "full" }, { key: "__proto__", width: "full" },
      { key: "resources", width: "half" }, { key: "resources", width: "full" },
      { key: "latest-run", width: "full" }, { key: "analytics", width: "invalid", extra: true },
    ]));
    assert.deepEqual(parsed, [
      { key: "resources", width: "half" }, { key: "latest-run", width: "full" }, { key: "analytics", width: "half" },
      ...defaults.filter(card => !["resources", "latest-run", "analytics"].includes(card.key)),
    ]);
    for (const width of [null, false, 1, {}, "FULL", "", "half", "full"]) {
      assert.equal(parseDashboardLayout(JSON.stringify([{ key: "analytics", width }]))[0].width, width === "full" ? "full" : "half");
    }
    const reversed = defaults.toReversed().map(card => ({ ...card, width: card.width === "half" ? "full" : "half" }));
    assert.deepEqual(parseDashboardLayout(JSON.stringify(reversed)), reversed);
  });

  await test("dashboard mounts, reorders and saves layout locally, then restores it on remount", async () => {
    stored.set(storageKey, "broken json");
    const { ui, app } = await mount(Dashboard);
    assert.deepEqual(ui.layout.value, defaults);
    const before = calls.length;
    ui.editLayout();
    assert.equal(ui.layoutDialog.value, true);
    assert.notEqual(ui.draftLayout.value, ui.layout.value);
    for (const [index, card] of ui.draftLayout.value.entries()) assert.notEqual(card, ui.layout.value[index]);
    ui.moveCard(0, -1); ui.moveCard(defaults.length - 1, 1);
    assert.deepEqual(ui.draftLayout.value, defaults, "out-of-bounds moves do nothing");
    const overviewIndex = defaults.findIndex(card => card.key === "overview");
    const direction = overviewIndex === defaults.length - 1 ? -1 : 1;
    ui.moveCard(overviewIndex, direction);
    const expected = defaults.map(card => ({ ...card }));
    [expected[overviewIndex], expected[overviewIndex + direction]] = [expected[overviewIndex + direction], expected[overviewIndex]];
    expected[0].width = "full";
    ui.draftLayout.value[0].width = "full";
    assert.deepEqual(ui.draftLayout.value, expected);
    assert.deepEqual(ui.layout.value, defaults, "reorder and width edits affect only the draft");
    assert.deepEqual(cardTitles(await render(Dashboard, copyDashboard(ui))), expectedTitles(defaults.filter(card => card.key !== "resources")));
    assert.equal(writes.length, 0, "editing does not persist before save");
    ui.saveLayout();
    assert.deepEqual(ui.layout.value, expected);
    assert.notEqual(ui.layout.value, ui.draftLayout.value);
    for (const [index, card] of ui.layout.value.entries()) assert.notEqual(card, ui.draftLayout.value[index]);
    assert.equal(ui.layoutDialog.value, false);
    assert.deepEqual(writes, [{ key: storageKey, content: JSON.stringify(expected) }]);
    assert.equal(messages.at(-1).type, "success");
    assert.equal(calls.length, before, "layout operations never call the backend");
    unmount(app);
    const restored = await mount(Dashboard);
    assert.deepEqual(restored.ui.layout.value, expected);
    const html = await render(Dashboard, copyDashboard(restored.ui));
    assert.deepEqual(cardTitles(html), expectedTitles(expected.filter(card => card.key !== "resources")));
    const fullCards = [...html.matchAll(/<article\b[^>]*class="[^"]*is-full[^"]*"/g)];
    assert.equal(fullCards.length, expected.filter(card => card.key !== "resources" && card.width === "full").length);
    restored.ui.editLayout(); restored.ui.draftLayout.value = defaultDashboardLayout(); restored.ui.saveLayout();
    assert.deepEqual(restored.ui.layout.value, defaults);
    assert.deepEqual(JSON.parse(stored.get(storageKey)), defaults);
  });

  await test("canceling draft reorder, width changes and reset leaves the effective and persisted layout unchanged", async () => {
    const saved = defaults.toReversed().map(card => ({ ...card }));
    stored.set(storageKey, JSON.stringify(saved));
    const { ui } = await mount(Dashboard);
    const before = calls.length;
    ui.editLayout(); ui.moveCard(0, 1);
    ui.draftLayout.value[0].width = ui.draftLayout.value[0].width === "full" ? "half" : "full";
    assert.notDeepEqual(ui.draftLayout.value, saved);
    ui.layoutDialog.value = false;
    assert.deepEqual(ui.layout.value, saved);
    assert.deepEqual(ui.visibleCards.value, saved.filter(card => card.key !== "resources"));
    assert.deepEqual(cardTitles(await render(Dashboard, copyDashboard(ui))), expectedTitles(ui.visibleCards.value));
    assert.equal(stored.get(storageKey), JSON.stringify(saved)); assert.equal(writes.length, 0);
    ui.editLayout();
    assert.deepEqual(ui.draftLayout.value, saved, "reopening discards canceled edits");
    ui.draftLayout.value = defaultDashboardLayout();
    ui.layoutDialog.value = false;
    assert.deepEqual(ui.layout.value, saved, "canceling restore-default does not apply it");
    ui.editLayout(); assert.deepEqual(ui.draftLayout.value, saved);
    assert.equal(calls.length, before); assert.equal(writes.length, 0);
  });

  await test("blocked browser storage falls back and failed save keeps the layout dialog open", async () => {
    storageFailure = true;
    const { ui } = await mount(Dashboard);
    assert.deepEqual(ui.layout.value, defaults);
    assert.ok(ui.dailyMatrix.value, "storage errors do not block dashboard requests");
    ui.editLayout(); ui.moveCard(0, 1); ui.draftLayout.value[0].width = "full"; ui.saveLayout();
    assert.deepEqual(ui.layout.value, defaults, "failed storage cannot apply the edited draft");
    assert.equal(ui.layoutDialog.value, true);
    assert.equal(writes.length, 0);
    assert.equal(messages.at(-1).type, "error");
  });

  await test("failed layout save preserves a previously saved layout and retries the unapplied draft", async () => {
    const saved = defaults.toReversed().map(card => ({ ...card }));
    stored.set(storageKey, JSON.stringify(saved));
    const { ui } = await mount(Dashboard);
    ui.editLayout(); ui.moveCard(0, 1);
    ui.draftLayout.value[0].width = ui.draftLayout.value[0].width === "full" ? "half" : "full";
    const edited = ui.draftLayout.value.map(card => ({ ...card }));
    storageFailure = true; ui.saveLayout();
    assert.deepEqual(ui.layout.value, saved); assert.deepEqual(ui.draftLayout.value, edited);
    assert.equal(ui.layoutDialog.value, true); assert.equal(messages.at(-1).type, "error");
    assert.equal(stored.get(storageKey), JSON.stringify(saved)); assert.equal(writes.length, 0);
    assert.deepEqual(cardTitles(await render(Dashboard, copyDashboard(ui))), expectedTitles(saved.filter(card => card.key !== "resources")));
    storageFailure = false; ui.saveLayout();
    assert.deepEqual(ui.layout.value, edited); assert.equal(ui.layoutDialog.value, false);
    assert.deepEqual(JSON.parse(stored.get(storageKey)), edited); assert.equal(writes.length, 1);
    ui.draftLayout.value[0].width = "invalid";
    assert.deepEqual(ui.layout.value, edited, "applied cards do not retain draft object references");
  });

  await test("resources off skips sampling; enabling requests metrics; disabling clears and hides them", async () => {
    const { ui } = await mount(Dashboard);
    assert.equal(ui.loading.value, false); assert.equal(ui.refreshing.value, false);
    assert.deepEqual(ui.errors.value, []);
    assert.equal(ui.enabledSites.value, 1);
    assert.equal(ui.templates.value.length, 1); assert.equal(ui.features.value.length, 2);
    assert.equal(calls.filter(call => call.path === "/system-settings").length, 1);
    assert.equal(calls.some(call => call.path === "/system-resources"), false);
    assert.equal(ui.showResources.value, false); assert.equal(ui.resources.value, null);
    assert.deepEqual(ui.visibleCards.value, defaults.filter(card => card.key !== "resources"));
    assert.deepEqual(cardTitles(await render(Dashboard, copyDashboard(ui))), expectedTitles(ui.visibleCards.value));
    calls.length = 0;
    handler = ({ path }) => json(path === "/system-settings" ? { show_system_resources: true } : defaultResponse(path));
    await ui.load();
    assert.equal(calls.filter(call => call.path === "/system-resources").length, 1);
    assert.ok(calls.findIndex(call => call.path === "/system-settings") < calls.findIndex(call => call.path === "/system-resources"));
    assert.deepEqual(ui.resources.value, resourceView); assert.deepEqual(ui.visibleCards.value, defaults);
    assert.equal(ui.resourceMetrics.value[0].value, 12.5);
    assert.equal(ui.resourceMetrics.value[1].detail, "1.0 GB / 4.0 GB");
    const html = await render(Dashboard, copyDashboard(ui));
    assert.deepEqual(cardTitles(html), expectedTitles(defaults));
    assert.match(html, /<button type="button" class="resource-scope\b[^"]*"[^>]*>\u7edf\u8ba1\u53e3\u5f84<\/button>/);
    assert.doesNotMatch(html, /API host/, "scope belongs to the tooltip, not the card body");
    calls.length = 0; handler = undefined; await ui.load();
    assert.equal(ui.resources.value, null); assert.equal(ui.showResources.value, false);
    assert.equal(calls.some(call => call.path === "/system-resources"), false);
    assert.equal(ui.visibleCards.value.some(card => card.key === "resources"), false);
  });

  await test("dashboard merges Docker samples into host metrics, forwards ring props and explains the grouping", async () => {
    const sample = { ...resourceView, docker_available: true, siteflow_cpu_percent: 3.5,
      siteflow_memory_percent: 10, siteflow_memory_used_bytes: 0.4 * 1024 ** 3, siteflow_container_count: 4 };
    handler = ({ path }) => json(path === "/system-settings" ? { show_system_resources: true }
      : path === "/system-resources" ? sample : defaultResponse(path));
    const { ui } = await mount(Dashboard);
    assert.deepEqual(ui.resources.value, sample);
    assert.deepEqual(ui.resourceMetrics.value, [
      { label: "CPU", value: 12.5, siteflow: 3.5, detail: "\u5360\u5bbf\u4e3b\u673a\u5168\u90e8 CPU \u7b97\u529b" },
      { label: "\u5185\u5b58", value: 25, siteflow: 10, detail: "1.0 GB / 4.0 GB" },
    ]);
    const html = await render(Dashboard, copyDashboard(ui));
    const rings = [...html.matchAll(/<section\b[^>]*class="resource-ring"[^>]*>([\s\S]*?)<\/section>/g)].map(match => match[1]);
    assert.equal(rings.length, 2);
    for (const [index, [host, siteflow, other, idle, detail]] of [
      [12.5, 3.5, 9, 87.5, "\u5360\u5bbf\u4e3b\u673a\u5168\u90e8 CPU \u7b97\u529b"],
      [25, 10, 15, 75, "1.0 GB / 4.0 GB"],
    ].entries()) {
      assert.match(rings[index], new RegExp(`class="resource-ring__value"[^>]*>${host.toFixed(1).replace(".", "\\.")}%<`));
      assert.ok(rings[index].includes(detail));
      assert.deepEqual([...rings[index].matchAll(/stroke-dasharray="([^"]+)"/g)].map(match => Number(match[1].split(" ")[0])),
        [siteflow, other, idle], "SiteFlow is a subset of host usage, not an additional total");
    }
    assert.match(html, /\u5df2\u6c47\u603b 4 \u4e2a SiteFlow \u670d\u52a1\u5bb9\u5668/);
    assert.match(html, /\u5185\u5b58 0\.4 GB/);
    assert.match(html, /SiteFlow \u5df2\u5305\u542b\u5728\u5bbf\u4e3b\u673a\u5df2\u7528\u8d44\u6e90\u4e2d/);
    assert.match(html, /\u6240\u6709\u767e\u5206\u6bd4\u5747\u4ee5\u5bbf\u4e3b\u673a\u603b\u8d44\u6e90\u4e3a\u5206\u6bcd/);
    assert.doesNotMatch(html, /\u5f53\u524d\u4ec5\u663e\u793a\u5bbf\u4e3b\u673a\u8d44\u6e90/);
  });

  await test("Docker unavailable or failed drops stale splits but preserves host-only charts and guidance", async () => {
    let sample = { ...resourceView, docker_available: true, siteflow_cpu_percent: 3.5,
      siteflow_memory_percent: 10, siteflow_memory_used_bytes: 0.4 * 1024 ** 3, siteflow_container_count: 4 };
    handler = ({ path }) => json(path === "/system-settings" ? { show_system_resources: true }
      : path === "/system-resources" ? sample : defaultResponse(path));
    const { ui } = await mount(Dashboard);
    assert.equal(ui.resourceMetrics.value[0].siteflow, 3.5);
    for (const docker_error of [null, "Docker sampling failed"]) {
      sample = { ...sample, docker_available: false, docker_error };
      await ui.load();
      assert.deepEqual(ui.errors.value, [], "Docker failure is not a failure of the host resource request");
      assert.deepEqual(ui.resourceMetrics.value.map(({ value, siteflow, detail }) => ({ value, siteflow, detail })), [
        { value: 12.5, siteflow: null, detail: "\u5360\u5bbf\u4e3b\u673a\u5168\u90e8 CPU \u7b97\u529b" },
        { value: 25, siteflow: null, detail: "1.0 GB / 4.0 GB" },
      ]);
      const html = await render(Dashboard, copyDashboard(ui));
      assert.deepEqual([...html.matchAll(/stroke-dasharray="([^"]+)"/g)].map(match => Number(match[1].split(" ")[0])),
        [12.5, 87.5, 25, 75]);
      assert.match(html, /\u5f53\u524d\u4ec5\u663e\u793a\u5bbf\u4e3b\u673a\u8d44\u6e90/);
      assert.match(html, /\u6302\u8f7d Docker Socket \u540e\u53ef\u663e\u793a SiteFlow \u670d\u52a1\u5360\u7528/);
      assert.doesNotMatch(html, /\u5176\u4ed6\u5df2\u7528|\u5df2\u6c47\u603b 4|\u5185\u5b58 0\.4 GB/);
      if (docker_error) assert.match(html, /Docker sampling failed\uff0c\u65e0\u6cd5\u62c6\u5206\u7684\u6307\u6807\u4ec5\u663e\u793a\u5bbf\u4e3b\u673a\u5360\u7528/);
      else assert.doesNotMatch(html, /class="resource-error"/);
    }
  });

  await test("nullable Docker CPU and memory degrade independently and zero remains a valid split", async () => {
    let sample;
    handler = ({ path }) => json(path === "/system-settings" ? { show_system_resources: true }
      : path === "/system-resources" ? sample : defaultResponse(path));
    sample = { ...resourceView, docker_available: true, siteflow_container_count: 0 };
    const { ui } = await mount(Dashboard);
    for (const [cpu, memory, bytes] of [[null, 10, 0.4 * 1024 ** 3], [3.5, null, null], [null, null, null], [0, 0, 0]]) {
      sample = { ...sample, siteflow_cpu_percent: cpu, siteflow_memory_percent: memory, siteflow_memory_used_bytes: bytes };
      await ui.load();
      assert.deepEqual(ui.resourceMetrics.value.map(metric => [metric.value, metric.siteflow]), [[12.5, cpu], [25, memory]]);
      assert.equal(ui.resourceMetrics.value[1].detail, "1.0 GB / 4.0 GB");
      const html = await render(Dashboard, copyDashboard(ui));
      const charts = [...html.matchAll(/<svg\b[^>]*class="resource-ring__chart"[^>]*>([\s\S]*?)<\/svg>/g)].map(match => match[0]);
      assert.equal(charts.length, 2);
      for (const [index, [host, siteflow]] of [[12.5, cpu], [25, memory]].entries()) {
        assert.deepEqual([...charts[index].matchAll(/stroke-dasharray="([^"]+)"/g)].map(match => Number(match[1].split(" ")[0])),
          siteflow === null ? [host, 100 - host] : [siteflow, host - siteflow, 100 - host]);
        if (siteflow === 0) assert.match(charts[index], /SiteFlow 0\.0%/);
      }
      assert.match(html, /\u5df2\u6c47\u603b 0 \u4e2a SiteFlow \u670d\u52a1\u5bb9\u5668/);
      if (bytes === null) assert.doesNotMatch(html, /\uff0c\u5185\u5b58/);
      if (bytes === 0) assert.match(html, /\uff0c\u5185\u5b58 0\.0 GB/);
    }
  });

  await test("resource scope is passed to the accessible tooltip and resource errors escape secret HTML", async () => {
    const secret = '<img src="secret" onerror="secret()">';
    const sample = { ...resourceView, scope: `API host ${secret}`, error: `Host failed ${secret}`, docker_error: `Docker failed ${secret}` };
    handler = ({ path }) => json(path === "/system-settings" ? { show_system_resources: true }
      : path === "/system-resources" ? sample : defaultResponse(path));
    const { ui } = await mount(Dashboard);
    const tooltips = [];
    const html = await render(Dashboard, copyDashboard(ui), {}, app => app.mixin({ created() {
      if (this.$options.name === "ElTooltip") tooltips.push(this.$props);
    } }));
    assert.ok(tooltips.some(props => props.content === sample.scope && props.placement === "top" && props.visible === false),
      "the real tooltip receives scope as text content");
    assert.match(html, /<button type="button" class="resource-scope\b[^"]*"[^>]*>\u7edf\u8ba1\u53e3\u5f84<\/button>/);
    assert.doesNotMatch(html, /API host/, "scope is not visible body text");
    assert.match(html, /Host failed &lt;img src=&quot;secret&quot; onerror=&quot;secret\(\)&quot;&gt;/);
    assert.match(html, /Docker failed &lt;img src=&quot;secret&quot; onerror=&quot;secret\(\)&quot;&gt;/);
    assert.ok(!html.includes(secret)); assert.doesNotMatch(html, /<img\b[^>]*src="secret"|<[^>]+\sonerror=/);
  });

  await test("each failed dashboard block leaves unrelated cards usable and a refresh can recover", async () => {
    for (const failingPath of ["/templates", "/lanes", "/scheduler-settings", "/analytics/matrix?days=1&include_disabled=false", "/runs?limit=1", `/runs/${runView.id}`, "/system-settings", "/system-resources"]) {
      calls.length = 0;
      handler = ({ path }) => path === failingPath ? json({ detail: "isolated failure" }, 503)
        : json(path === "/system-settings" ? { show_system_resources: true } : defaultResponse(path));
      const { ui, app } = await mount(Dashboard);
      assert.equal(ui.loading.value, false); assert.equal(ui.refreshing.value, false);
      assert.equal(ui.errors.value.length, 1, failingPath); assert.match(ui.errors.value[0], /isolated failure/);
      if (failingPath !== "/analytics/matrix?days=1&include_disabled=false") assert.deepEqual(ui.dailyMatrix.value, dailyMatrixView);
      if (!failingPath.startsWith("/runs")) assert.equal(ui.latestRun.value.id, runView.id);
      if (failingPath !== "/templates") assert.equal(ui.sites.value.length, 2);
      if (failingPath !== "/lanes") assert.deepEqual(ui.lanes.value, lanesView);
      else assert.equal(ui.lanes.value, null);
      assert.equal(ui.displayTimezone.value, failingPath === "/scheduler-settings" ? "Asia/Shanghai" : schedulerView.timezone);
      if (!failingPath.startsWith("/system-")) assert.deepEqual(ui.resources.value, resourceView);
      if (failingPath === "/system-settings") assert.equal(calls.some(call => call.path === "/system-resources"), false);
      const html = await render(Dashboard, copyDashboard(ui));
      assert.match(html, /isolated failure/);
      assert.deepEqual(cardTitles(html), expectedTitles(ui.visibleCards.value));
      handler = undefined; await ui.load();
      assert.deepEqual(ui.errors.value, []); assert.deepEqual(ui.dailyMatrix.value, dailyMatrixView);
      assert.equal(ui.latestRun.value.id, runView.id);
      unmount(app);
    }
  });

  await test("dashboard 401 clears authentication and replaces the route with login", async () => {
    for (const failingPath of ["/sites?view=options&include_valid=true", "/lanes", "/scheduler-settings", "/analytics/matrix?days=1&include_disabled=false", `/runs/${runView.id}`, "/system-settings", "/system-resources"]) {
      clearCount = 0; navigation.length = 0;
      handler = ({ path }) => path === failingPath ? json({ detail: "expired" }, 401)
        : json(path === "/system-settings" ? { show_system_resources: true } : defaultResponse(path));
      const { ui, app } = await mount(Dashboard);
      assert.equal(clearCount, 1, failingPath);
      assert.deepEqual(navigation, [{ action: "replace", name: "login" }]);
      assert.deepEqual(ui.errors.value, [], "401 is an auth transition, not a card error");
      assert.equal(ui.refreshing.value, false); unmount(app);
    }
  });

  await test("dashboard treats timezone-less server timestamps as UTC and renders the scheduler timezone", async () => {
    const previousTZ = process.env.TZ;
    process.env.TZ = "America/Los_Angeles";
    try {
      const timestamp = "2026-11-25T20:05:06";
      let timezone = "Asia/Shanghai";
      handler = ({ path }) => json(path === "/scheduler-settings" ? { ...schedulerView, timezone }
        : path === `/runs/${runView.id}` ? { ...runView, created_at: timestamp }
        : path === "/system-settings" ? { show_system_resources: true }
        : path === "/system-resources" ? { ...resourceView, sampled_at: timestamp }
        : defaultResponse(path));
      const { ui } = await mount(Dashboard);
      assert.equal(calls.filter(call => call.path === "/scheduler-settings").length, 1);
      for (const [zone, expected] of [
        ["Asia/Shanghai", "2026/11/26 04:05:06"],
        ["America/New_York", "2026/11/25 15:05:06"],
        ["UTC", "2026/11/25 20:05:06"],
      ]) {
        timezone = zone; await ui.load();
        assert.equal(ui.displayTimezone.value, zone);
        assert.equal(ui.formatDate(timestamp), expected);
        assert.equal(ui.formatDate(`${timestamp}Z`), expected);
        assert.equal(ui.formatDate("2026-11-26T04:05:06+08:00"), expected);
        const html = await render(Dashboard, copyDashboard(ui));
        assert.ok([...html.matchAll(/<small\b[^>]*>(.*?)<\/small>/g)].some(match => match[1] === expected),
          "run creation time uses the loaded timezone");
        assert.equal(html.split(expected).length - 1, 2, "resource sample time uses the same timezone");
      }
      timezone = ""; await ui.load();
      assert.equal(ui.displayTimezone.value, "Asia/Shanghai");
      assert.equal(ui.formatDate(timestamp), "2026/11/26 04:05:06");
    } finally {
      if (previousTZ === undefined) delete process.env.TZ;
      else process.env.TZ = previousTZ;
    }
  });

  await test("overview's fourth card renders reactive running/total lanes and Worker availability", async () => {
    const { ui } = await mount(Dashboard);
    assert.deepEqual(calls.filter(call => call.path === "/lanes"), [{ path: "/lanes", method: "GET", body: undefined }]);
    assert.deepEqual(ui.lanes.value, lanesView); assert.equal(ui.runningLanes.value, 2);
    for (const [data, count, total, status] of [
      [lanesView, 2, 4, "Worker \u5728\u7ebf"],
      [{ worker_connected: false, lanes: [{ key: "only", status: "idle" }] }, 0, 1, "Worker \u4e0d\u53ef\u8fbe"],
      [{ worker_connected: true, lanes: [] }, 0, 0, "Worker \u5728\u7ebf"],
      [null, 0, null, "\u72b6\u6001\u6682\u4e0d\u53ef\u7528"],
    ]) {
      ui.lanes.value = data;
      assert.equal(ui.runningLanes.value, count);
      const html = await render(Dashboard, copyDashboard(ui));
      const cards = [...html.matchAll(/<button\b[^>]*class="stat-card"[^>]*>([\s\S]*?)<\/button>/g)].map(match => match[1]);
      assert.equal(cards.length, 4);
      assert.match(cards[3], /<span[^>]*>\u8f66\u9053\u72b6\u6001<\/span>/);
      assert.match(cards[3], new RegExp(`<strong[^>]*>${total === null ? "--" : `${count} / ${total}`}<\\/strong>`));
      assert.ok(cards[3].includes(status));
      assert.match(cards[0], /<strong[^>]*>2<\/strong>/); assert.match(cards[1], /<strong[^>]*>1<\/strong>/);
      assert.match(cards[2], /<strong[^>]*>2<\/strong>/);
    }
  });

  await test("lane HTTP and network failures clear stale lane data without breaking overview or other blocks", async () => {
    const { ui } = await mount(Dashboard);
    for (const failure of [() => json({ detail: "lanes offline" }, 503), () => { throw new TypeError("lanes offline"); }]) {
      handler = ({ path }) => path === "/lanes" ? failure() : json(defaultResponse(path));
      await ui.load();
      assert.equal(ui.lanes.value, null); assert.equal(ui.runningLanes.value, 0);
      assert.deepEqual(ui.errors.value, ["\u8f66\u9053\u72b6\u6001\uff1alanes offline"]);
      assert.equal(ui.sites.value.length, 2); assert.equal(ui.enabledSites.value, 1);
      assert.equal(ui.templates.value.length, 1); assert.equal(ui.features.value.length, 2);
      assert.deepEqual(ui.dailyMatrix.value, dailyMatrixView); assert.equal(ui.latestRun.value.id, runView.id);
      assert.equal(ui.refreshing.value, false); assert.equal(ui.loading.value, false);
      handler = undefined; await ui.load();
      assert.deepEqual(ui.errors.value, []); assert.deepEqual(ui.lanes.value, lanesView);
      assert.equal(ui.runningLanes.value, 2);
    }
    handler = ({ path }) => path === "/templates" ? json({ detail: "templates offline" }, 503) : json(defaultResponse(path));
    const fresh = await mount(Dashboard);
    assert.equal(fresh.ui.sites.value.length, 0, "failed overview batch does not hydrate partially");
    assert.deepEqual(fresh.ui.lanes.value, lanesView, "lanes load independently of the overview batch");
  });

  await test("today analytics navigation opens the default analytics route without a query", async () => {
    const { ui } = await mount(Dashboard);
    const before = calls.length;
    ui.openAnalytics();
    assert.deepEqual(navigation, [{ action: "push", name: "analytics" }]);
    assert.equal(calls.length, before, "navigation does not reload dashboard data");
  });

  await test("today analytics counts each enabled site once, excludes disabled results and includes not-run sites in success rate", async () => {
    const matrix = { ...dailyMatrixView, sites: [
      { site_id: 101, name: "success-after-retries", enabled: true,
        cells: { "2026-11-25": matrixCell("success", { success: 1, fail: 99 }) } },
      { site_id: 102, name: "failed-retries", enabled: true,
        cells: { "2026-11-25": matrixCell("fail", { fail: 100 }) } },
      { site_id: 103, name: "not-started", enabled: true, cells: {} },
      { site_id: 104, name: "disabled-result", enabled: false,
        cells: { "2026-11-25": matrixCell("error", { error: 100 }) } },
    ] };
    handler = ({ path }) => json(path === "/analytics/matrix?days=1&include_disabled=false" ? matrix
      : path === "/sites?view=options&include_valid=true" ? matrix.sites.map(site => ({ id: site.site_id, enabled: site.enabled }))
      : defaultResponse(path));
    const { ui } = await mount(Dashboard);
    assert.deepEqual(ui.errors.value, []);
    assert.equal(ui.enabledSites.value, 3);
    assert.deepEqual(ui.dailySites.value.map(site => site.site_id), [101, 102, 103]);
    assert.deepEqual(ui.slices.value.filter(slice => slice.count).map(({ key, count }) => [key, count]),
      [["success", 1], ["fail", 1], ["not_run", 1]]);
    assert.equal(ui.slices.value.reduce((sum, slice) => sum + slice.count, 0), 3,
      "attempts and counts never multiply a site's contribution or override the server's success-priority status");
    assert.equal(ui.dailySuccessRate.value, 1 / 3 * 100, "unrun enabled sites remain in the denominator");
    const notRun = ui.slices.value.find(slice => slice.key === "not_run");
    assert.equal(notRun.label, "\u672a\u8fd0\u884c"); assert.equal(notRun.color, "#dcdfe6");
    const grayStop = ui.pieStyle.value.background.match(/#dcdfe6 ([\d.]+)% ([\d.]+)%/);
    assert.ok(grayStop, "not-run sites have a gray segment");
    assert.ok(Math.abs(Number(grayStop[1]) - 2 / 3 * 100) < 1e-10);
    assert.ok(Math.abs(Number(grayStop[2]) - 100) < 1e-10);
    const html = await render(Dashboard, copyDashboard(ui));
    assert.match(html, /class="pie-center"[^>]*><strong[^>]*>3<\/strong><small[^>]*>\u542f\u7528\u7ad9\u70b9<\/small>/);
    assert.match(html, /class="success-rate"[^>]*><strong[^>]*>33\.3<small[^>]*>%<\/small>/);
    assert.match(html, /background:#dcdfe6[^>]*><\/i><span[^>]*>\u672a\u8fd0\u884c<\/span><strong[^>]*>1<\/strong>/);
  });

  await test("today analytics selects the server end_date cell, not the browser date or the first matrix date", async () => {
    const browserDate = new Date().toISOString().slice(0, 10);
    const serverDate = browserDate === "2001-01-02" ? "2001-01-03" : "2001-01-02";
    const matrix = { start_date: "2001-01-01", end_date: serverDate, dates: ["2001-01-01", serverDate], sites: [
      { site_id: 101, name: "server-success", enabled: true, cells: {
        [browserDate]: matrixCell("error"), "2001-01-01": matrixCell("fail"), [serverDate]: matrixCell("success"),
      } },
      { site_id: 102, name: "only-other-days", enabled: true, cells: {
        [browserDate]: matrixCell("success"), "2001-01-01": matrixCell("success"),
      } },
    ] };
    handler = ({ path }) => json(path === "/analytics/matrix?days=1&include_disabled=false" ? matrix : defaultResponse(path));
    const { ui } = await mount(Dashboard);
    for (const timezone of ["Pacific/Kiritimati", "America/Los_Angeles", "UTC"]) {
      ui.displayTimezone.value = timezone;
      assert.deepEqual(ui.slices.value.filter(slice => slice.count).map(({ key, count }) => [key, count]),
        [["success", 1], ["not_run", 1]]);
      assert.equal(ui.dailySuccessRate.value, 50);
      const html = await render(Dashboard, copyDashboard(ui));
      assert.ok(html.includes(`${serverDate} \u00b7`), "the analytics note displays the server date as well");
      assert.match(html, /class="success-rate"[^>]*><strong[^>]*>50\.0<small[^>]*>%<\/small>/);
    }
  });

  await test("today analytics handles unavailable data, zero enabled sites and enabled sites with no results without invalid rings", async () => {
    handler = ({ path }) => path === "/analytics/matrix?days=1&include_disabled=false"
      ? json({ detail: "matrix unavailable" }, 503) : json(defaultResponse(path));
    const { ui } = await mount(Dashboard);
    assert.equal(ui.dailyMatrix.value, null);
    assert.equal(ui.dailySites.value.length, 0); assert.equal(ui.dailySuccessRate.value, 0);
    assert.equal(ui.pieStyle.value.background, "#edf1f6");
    const unavailable = await render(Dashboard, copyDashboard(ui));
    assert.match(unavailable, /\u6682\u65e0\u4eca\u65e5\u8fd0\u884c\u5206\u6790/);
    assert.doesNotMatch(unavailable, /class="pie-chart"|NaN|Infinity/);
    for (const sites of [[], dailyMatrixView.sites.filter(site => !site.enabled), [
      { site_id: 101, name: "not-started", enabled: true, cells: {} },
      { site_id: 102, name: "also-not-started", enabled: true, cells: {} },
    ]]) {
      handler = ({ path }) => json(path === "/analytics/matrix?days=1&include_disabled=false"
        ? { ...dailyMatrixView, sites } : defaultResponse(path));
      await ui.load();
      assert.deepEqual(ui.errors.value, []);
      const enabled = sites.filter(site => site.enabled).length;
      assert.equal(ui.dailySites.value.length, enabled); assert.equal(ui.dailySuccessRate.value, 0);
      assert.equal(ui.slices.value.find(slice => slice.key === "not_run").count, enabled);
      assert.equal(ui.slices.value.reduce((sum, slice) => sum + slice.count, 0), enabled);
      assert.equal(ui.pieStyle.value.background, enabled ? "conic-gradient(#dcdfe6 0% 100%)" : "#edf1f6");
      const html = await render(Dashboard, copyDashboard(ui));
      assert.match(html, new RegExp(`class="pie-center"[^>]*><strong[^>]*>${enabled}<\\/strong>`));
      assert.match(html, /class="success-rate"[^>]*><strong[^>]*>0\.0<small[^>]*>%<\/small>/);
      assert.doesNotMatch(html, /NaN|Infinity|\u6682\u65e0\u4eca\u65e5\u8fd0\u884c\u5206\u6790/);
    }
  });

  await test("today analytics reload reflects disabling and re-enabling a site with existing results", async () => {
    const { ui } = await mount(Dashboard);
    for (const enabled of [true, false, true]) {
      const sites = dailyMatrixView.sites.map(site => site.site_id === 102 ? { ...site, enabled } : site);
      handler = ({ path }) => json(path === "/analytics/matrix?days=1&include_disabled=false" ? { ...dailyMatrixView, sites }
        : path === "/sites?view=options&include_valid=true" ? sites.map(site => ({ id: site.site_id, enabled: site.enabled }))
        : defaultResponse(path));
      calls.length = 0; await ui.load();
      assert.deepEqual(ui.errors.value, []);
      assert.equal(calls.filter(call => call.path === "/analytics/matrix?days=1&include_disabled=false").length, 1);
      assert.equal(ui.enabledSites.value, enabled ? 2 : 1);
      assert.equal(ui.dailySites.value.length, enabled ? 2 : 1);
      assert.equal(ui.slices.value.find(slice => slice.key === "success").count, 1);
      assert.equal(ui.slices.value.find(slice => slice.key === "deferred").count, enabled ? 1 : 0);
      assert.equal(ui.slices.value.reduce((sum, slice) => sum + slice.count, 0), enabled ? 2 : 1);
      assert.equal(ui.dailySuccessRate.value, enabled ? 50 : 100);
      assert.equal(ui.pieStyle.value.background, enabled
        ? "conic-gradient(#67c23a 0% 50%, #b88230 50% 100%)" : "conic-gradient(#67c23a 0% 100%)");
      const html = await render(Dashboard, copyDashboard(ui));
      assert.match(html, new RegExp(`class="pie-center"[^>]*><strong[^>]*>${enabled ? 2 : 1}<\\/strong>`));
      assert.match(html, new RegExp(`class="success-rate"[^>]*><strong[^>]*>${enabled ? "50" : "100"}\\.0<small[^>]*>%<\\/small>`));
    }
  });

  await test("dashboard initial load, refresh and polling GET only the one-day enabled-site matrix, never today trends", async () => {
    const { ui } = await mount(Dashboard);
    await ui.load();
    const timer = [...timers.values()].find(item => item.delay === 15000);
    timer.callback(); await settle();
    assert.deepEqual(ui.errors.value, []);
    assert.deepEqual(calls.filter(call => call.path.startsWith("/analytics/")), Array.from({ length: 3 }, () => ({
      path: "/analytics/matrix?days=1&include_disabled=false", method: "GET", body: undefined,
    })));
    assert.equal(calls.some(call => call.path.startsWith("/analytics/trends?")), false);
  });

  await test("analytics defaults all requests to 14 days and offers no today range or query override", async () => {
    const { ui } = await mount(Analytics);
    assert.equal(ui.days.value, 14);
    assert.equal(ui.overviewLoading.value, false); assert.equal(ui.failureLoading.value, false);
    assert.equal(ui.error.value, ""); assert.equal(ui.failureError.value, "");
    const analyticsCalls = calls.filter(call => call.path.startsWith("/analytics/"));
    assert.equal(analyticsCalls.length, 3);
    for (const call of analyticsCalls) assert.equal(new URLSearchParams(call.path.split("?")[1]).get("days"), "14");
    const source = await readFile(new URL("../src/views/AnalyticsView.vue", import.meta.url), "utf8");
    assert.deepEqual([...source.matchAll(/<el-radio-button\s+:value="(\d+)"/g)].map(match => Number(match[1])), [7, 14, 30, 90]);
    assert.doesNotMatch(source, /useRoute\b|route\.query|<el-radio-button[^>]*>\s*\u4eca\u65e5/);
  });

  await test("latest jobs dedupe by site and greatest id before filtering waiting states, retaining deferred", async () => {
    const jobs = [
      job(14, 1, "deferred", "renamed"), job(2, 1, "fail", "old name"),
      job(25, 2, "running"), job(3, 2, "success"),
      job(26, 3, "queued"), job(4, 3, "success"),
      job(27, 4, "waiting"), job(5, 4, "fail"),
      job(28, 5, "waiting_resource"), job(6, 5, "error"),
      job(29, 6, "waiting_browser"), job(7, 6, "success"),
      job(30, 7, "pending"), job(8, 7, "success"),
      job(31, 8, "future_status"), job(9, 8, "success"),
      job(32, null, "deferred", "deleted-A"), job(10, null, "fail", "deleted-A"),
      job(33, null, "success", "deleted-B"), job(34, 0, "error", "zero-id"),
      ...["success", "fail", "error", "unknown", "waiting_human", "canceled", "interrupted", "skipped"]
        .map((status, index) => job(40 + index, 20 + index, status)),
    ];
    handler = ({ path }) => json(path === `/runs/${runView.id}` ? { ...runView, jobs } : defaultResponse(path));
    const { ui } = await mount(Dashboard);
    const expected = [14, 32, 33, 34, 40, 41, 42, 43, 44, 45, 46, 47];
    assert.deepEqual(ui.finalJobs.value.map(item => item.id), expected);
    assert.equal(ui.finalJobs.value.filter(item => item.status === "deferred").length, 2);
    assert.equal(ui.finalJobs.value.find(item => item.site_id === 1).site_name, "renamed");
    const html = await render(Dashboard, copyDashboard(ui));
    assert.equal([...html.matchAll(/class="job-row"/g)].length, expected.length);
    assert.match(html, /deleted-A/); assert.match(html, /deleted-B/); assert.doesNotMatch(html, /old name|future_status/);
    assert.ok(ui.slices.value.some(item => item.key === "deferred"));
    assert.equal(ui.slices.value.some(item => item.key === "skipped"), false);
    for (const status of dashboardStatuses) {
      assert.equal(dashboardStatus(status.key).label, status.label);
      assert.equal(dashboardStatus(status.key).color, status.color);
    }
    assert.equal(dashboardStatus("future_status").label, "future_status");
    assert.match(ui.pieStyle.value.background, /^conic-gradient\(/);
    ui.dailyMatrix.value = { ...dailyMatrixView, sites: [] }; assert.equal(ui.pieStyle.value.background, "#edf1f6");
  });

  await test("run deep links use stable run, job and site ids rather than names or list indexes", async () => {
    const { ui } = await mount(Dashboard);
    ui.openRun(); ui.openRun(job(701, 902, "success", "same name"));
    ui.openRun(job(702, null, "deferred", "same name")); ui.openRun(job(703, 0, "error"));
    assert.deepEqual(navigation, [
      { action: "push", name: "runs", query: { run: 812 } },
      { action: "push", name: "runs", query: { run: 812, job: 701, site: 902 } },
      { action: "push", name: "runs", query: { run: 812, job: 702 } },
      { action: "push", name: "runs", query: { run: 812, job: 703, site: 0 } },
    ]);
    ui.latestRun.value = null; ui.openRun(); assert.equal(navigation.length, 4);
    handler = ({ path }) => json(path === "/runs?limit=1" ? [] : defaultResponse(path));
    calls.length = 0; await ui.load();
    assert.equal(ui.latestRun.value, null); assert.deepEqual(ui.finalJobs.value, []);
    assert.equal(calls.some(call => /^\/runs\/\d/.test(call.path)), false);
  });

  await test("polling skips hidden documents, serializes refresh and cleans up on unmount", async () => {
    const { ui, app } = await mount(Dashboard);
    const timer = [...timers.values()].find(item => item.delay === 15000);
    assert.ok(timer); assert.equal(listeners.get("visibilitychange").size, 1);
    calls.length = 0; document.hidden = true; timer.callback(); await settle(); assert.equal(calls.length, 0);
    document.hidden = false;
    let release;
    handler = ({ path }) => path === "/analytics/matrix?days=1&include_disabled=false" ? new Promise(resolve => { release = resolve; }) : json(defaultResponse(path));
    timer.callback(); await settle(); assert.equal(ui.refreshing.value, true);
    const count = calls.length;
    await ui.load(); timer.callback(); await settle(); assert.equal(calls.length, count);
    unmount(app);
    release(json({ ...dailyMatrixView, end_date: "2099-12-31", sites: [] })); await settle();
    assert.deepEqual(ui.dailyMatrix.value, dailyMatrixView, "late responses cannot hydrate an unmounted dashboard");
  });

  const copySettings = ui => target => {
    target.activeTab.value = "system";
    for (const key of ["systemSettings", "showSystemResources", "systemSettingsLoading", "systemSettingsSaving", "systemSettingsError"])
      target[key].value = ui[key].value;
  };
  // Element Plus assigns input ids on mount, so SSR is located by its labelled wrapper.
  const switchTag = html => html.match(/aria-labelledby="system-resources-label"[^>]*><input\b[^>]*>/)?.[0].match(/<input\b[^>]*>/)?.[0];

  await test("settings mounts system preferences, saves both switch values and honors the server response", async () => {
    const { ui } = await mount(Settings);
    assert.equal(calls.filter(call => call.path === "/system-settings").length, 1);
    assert.equal(calls.some(call => call.method !== "GET"), false);
    assert.equal(ui.systemSettingsLoading.value, false); assert.equal(ui.showSystemResources.value, false);
    assert.deepEqual(ui.systemSettings.value, { show_system_resources: false });
    assert.deepEqual(ui.settingsTabs.map(item => item.value), ["integrations", "models", "logs", "system"]);
    calls.length = 0;
    handler = ({ path, method, body }) => json(path === "/system-settings" && method === "PUT" ? JSON.parse(body) : defaultResponse(path));
    for (const enabled of [true, false]) {
      ui.showSystemResources.value = enabled; await ui.saveSystemSettings();
      assert.deepEqual(calls.at(-1), { path: "/system-settings", method: "PUT", body: JSON.stringify({ show_system_resources: enabled }) });
      assert.equal(ui.systemSettings.value.show_system_resources, enabled);
      assert.equal(ui.systemSettingsSaving.value, false); assert.equal(ui.systemSettingsError.value, "");
      assert.equal(messages.at(-1).type, "success");
    }
    handler = () => json({ show_system_resources: false });
    ui.showSystemResources.value = true; await ui.saveSystemSettings();
    assert.equal(ui.showSystemResources.value, false, "response, not optimistic input, is authoritative");
    const html = await render(Settings, copySettings(ui));
    assert.ok(switchTag(html));
    assert.doesNotMatch(switchTag(html), /\sdisabled(?:\s|=|>)/);
    assert.match(html, /aria-labelledby="system-resources-label"/);
    assert.match(html, /aria-describedby="system-resources-description"/);
    assert.equal(calls.some(call => call.path === "/system-resources"), false, "settings never samples resource metrics");
  });

  await test("settings save failures roll back either direction; busy saves cannot send duplicate PUTs", async () => {
    const { ui } = await mount(Settings);
    for (const previous of [false, true]) {
      handler = () => json({ show_system_resources: previous }); await ui.loadSystemSettings();
      for (const failure of [() => json({ detail: "save failed" }, 500), () => { throw new TypeError("offline"); }]) {
        ui.showSystemResources.value = !previous;
        handler = failure; await ui.saveSystemSettings();
        assert.equal(ui.showSystemResources.value, previous);
        assert.equal(ui.systemSettings.value.show_system_resources, previous);
        assert.ok(ui.systemSettingsError.value); assert.equal(ui.systemSettingsSaving.value, false);
        assert.equal(messages.at(-1).type, "error");
      }
    }
    let release;
    handler = () => new Promise(resolve => { release = resolve; });
    ui.showSystemResources.value = false;
    const pending = ui.saveSystemSettings();
    assert.equal(ui.systemSettingsSaving.value, true);
    const count = calls.length; await ui.saveSystemSettings(); assert.equal(calls.length, count);
    const html = await render(Settings, copySettings(ui));
    assert.match(switchTag(html), /\sdisabled(?:\s|=|>)/); assert.match(html, /aria-busy="true"/);
    release(json({ show_system_resources: false })); await pending;
    assert.equal(ui.systemSettingsSaving.value, false); assert.equal(ui.systemSettingsError.value, "");
  });

  await test("failed initial settings load disables save and offers retry, which restores the switch", async () => {
    handler = ({ path }) => path === "/system-settings" ? json({ detail: "read failed" }, 503) : json(defaultResponse(path));
    const { ui } = await mount(Settings);
    assert.equal(ui.systemSettings.value, null); assert.equal(ui.systemSettingsLoading.value, false);
    assert.equal(ui.systemSettingsError.value, "read failed");
    const before = calls.length; ui.showSystemResources.value = true; await ui.saveSystemSettings();
    assert.equal(calls.length, before, "no PUT before the first successful read");
    const failedHtml = await render(Settings, copySettings(ui));
    assert.match(switchTag(failedHtml), /\sdisabled(?:\s|=|>)/);
    assert.match(failedHtml, /read failed/); assert.match(failedHtml, /\u91cd\u65b0\u52a0\u8f7d/);
    let release;
    handler = () => new Promise(resolve => { release = resolve; });
    const retry = ui.loadSystemSettings();
    assert.equal(ui.systemSettingsLoading.value, true); assert.equal(ui.systemSettingsError.value, "");
    await ui.saveSystemSettings(); assert.equal(calls.length, before + 1);
    release(json({ show_system_resources: true })); await retry;
    assert.equal(ui.systemSettingsLoading.value, false); assert.equal(ui.showSystemResources.value, true);
    assert.deepEqual(ui.systemSettings.value, { show_system_resources: true });
    const html = await render(Settings, copySettings(ui));
    assert.doesNotMatch(switchTag(html), /\sdisabled(?:\s|=|>)/);
    assert.doesNotMatch(html, /read failed|\u91cd\u65b0\u52a0\u8f7d/);
  });

  await test("settings read and write 401 clear auth and navigate to login, with failed writes rolled back", async () => {
    const { ui } = await mount(Settings);
    handler = () => json({ detail: "expired" }, 401);
    await ui.loadSystemSettings();
    assert.equal(clearCount, 1); assert.equal(ui.systemSettingsLoading.value, false);
    ui.showSystemResources.value = true; await ui.saveSystemSettings();
    assert.equal(clearCount, 2); assert.equal(ui.showSystemResources.value, false);
    assert.equal(ui.systemSettingsSaving.value, false);
    assert.deepEqual(navigation, [{ action: "replace", name: "login" }, { action: "replace", name: "login" }]);
    assert.equal(ui.systemSettingsError.value, "");
  });

  const tabItems = [{ value: "first", label: "First", badge: 0 }, { value: "second", label: "Second" }, { value: "third", label: "Third", badge: 7 }];
  const tabScroller = () => {
    const captured = new Set();
    return { scrollLeft: 0, scrollWidth: 300, clientWidth: 100, offsetHeight: 40,
      closest() { return null; },
      children: tabItems.map((_, index) => ({ offsetLeft: index * 100, offsetWidth: 100,
        focus(options) { this.focused = options; } })),
      setPointerCapture(id) { captured.add(id); }, hasPointerCapture(id) { return captured.has(id); },
      releasePointerCapture(id) { captured.delete(id); },
    };
  };
  const event = fields => ({ prevented: false, stopped: false,
    preventDefault() { this.prevented = true; }, stopImmediatePropagation() { this.stopped = true; }, ...fields });
  const tabLayout = () => {
    const scroller = tabScroller();
    const state = { height: 1000, tabsTop: 180, scrollTop: 0, patches: [], restores: [] };
    const panel = { style: { minHeight: "" }, getBoundingClientRect: () => ({
      top: 100 + state.tabsTop + scroller.offsetHeight + 16 - content.scrollTop,
    }) };
    const content = {
      clientHeight: 600, computedStyle: { paddingBottom: "48px" },
      getBoundingClientRect: () => ({ top: 100 }),
      get scrollTop() { return Math.min(state.scrollTop, this.maxScroll); },
      set scrollTop(value) { state.restores.push(value); state.scrollTop = Math.max(0, Math.min(value, this.maxScroll)); },
      get maxScroll() {
        return Math.max(0, state.tabsTop + scroller.offsetHeight + 16 +
          Math.max(state.height, parseFloat(panel.style.minHeight) || 0) + 48 - this.clientHeight);
      },
    };
    Object.assign(scroller, {
      computedStyle: { marginBottom: "16px" },
      closest(selector) { assert.equal(selector, ".app-content"); return content; },
      getBoundingClientRect: () => ({ top: 100 + Math.max(0, state.tabsTop - content.scrollTop) }),
    });
    return { scroller, panel, content, state, configure(api, props) {
      api.scroller.value = scroller; api.panel.value = panel;
      return () => {
        const selected = props.modelValue;
        if (selected === "second") state.height = 20;
        state.patches.push({ selected, minHeight: panel.style.minHeight });
        // Browser layout clamps scrollTop when a shorter selected panel replaces a tall one.
        state.scrollTop = content.scrollTop;
        return null;
      };
    } };
  };

  await test("horizontal tabs expose accessible SSR state, selection events and reactive model reveal", async () => {
    const html = await render(Tabs, undefined, { modelValue: "second", items: tabItems });
    assert.match(html, /role="tablist"/); assert.match(html, /aria-orientation="horizontal"/);
    const tags = [...html.matchAll(/<button\b[^>]*role="tab"[^>]*>/g)].map(match => match[0]);
    assert.equal(tags.length, tabItems.length);
    for (const [index, tag] of tags.entries()) {
      assert.match(tag, new RegExp(`aria-selected="${index === 1}"`));
      assert.match(tag, new RegExp(`tabindex="${index === 1 ? 0 : -1}"`));
    }
    assert.equal(new Set(tags.map(tag => tag.match(/id="([^"]+)"/)[1])).size, tags.length);
    assert.match(html, /horizontal-tabs__badge[^>]*>0</); assert.match(html, /horizontal-tabs__badge[^>]*>7</);
    const slotted = await renderToString(createSSRApp({ render: () => h(Tabs, { modelValue: "first", items: tabItems }, {
      default: () => h("section", { "data-panel": "yes" }, "Selected content"),
    }) }));
    assert.match(slotted, /class="horizontal-tabs__panel"[^>]*>\s*<!--[\s\S]*data-panel="yes"[\s\S]*Selected content/);
    assert.ok(slotted.indexOf('class="horizontal-tabs__panel"') > slotted.lastIndexOf("</button>"));
    const changes = [];
    const scroller = tabScroller();
    const props = { modelValue: "first", items: tabItems, "onUpdate:modelValue": value => changes.push(value) };
    const { ui, app } = await mount(Tabs, props, api => { api.scroller.value = scroller; });
    assert.equal(ui.focusIndex.value, 0);
    const key = event({ key: "ArrowRight" }); ui.onKeydown(key, 0);
    assert.equal(key.prevented, true); assert.deepEqual(changes, ["second"]);
    assert.deepEqual(scroller.children[1].focused, { preventScroll: true }); assert.equal(scroller.scrollLeft, 100);
    // Root props are snapshotted by createApp; update its reactive instance props like a parent patch.
    app._instance.props.modelValue = "third"; await nextTick();
    assert.equal(ui.focusIndex.value, 2); assert.equal(scroller.scrollLeft, 200);
    app._instance.props.modelValue = "missing"; await nextTick();
    assert.equal(ui.focusIndex.value, 0); assert.equal(scroller.scrollLeft, 0);
    unmount(app);
  });

  await test("horizontal tabs preserve manual scroll on equivalent items, badge refresh and resize but reveal selection moves", async () => {
    const layout = tabLayout();
    const { scroller, content, state } = layout;
    const observerCount = observers.length;
    const { ui, app } = await mount(Tabs, { modelValue: "first", items: tabItems.map(item => ({ ...item })) },
      layout.configure);
    assert.equal(ui.focusIndex.value, 0); assert.equal(scroller.scrollLeft, 0);
    const props = app._instance.props;
    content.scrollTop = 250; state.restores.length = 0;
    scroller.scrollLeft = 160;
    props.items = tabItems.map(item => ({ ...item })); await nextTick();
    assert.equal(scroller.scrollLeft, 160, "new items identity with the same content does not reveal the selected tab");
    props.items = tabItems.map(item => ({ ...item, badge: 99 })); await nextTick();
    assert.equal(scroller.scrollLeft, 160, "polling badge updates preserve manual scroll");
    props.items[0].badge = 100; await nextTick();
    assert.equal(scroller.scrollLeft, 160, "in-place badge updates preserve manual scroll");
    scroller.clientWidth = 80;
    for (const observer of observers.slice(observerCount)) observer.callback();
    await nextTick();
    assert.equal(scroller.scrollLeft, 160, "resize does not reveal the current tab");
    assert.deepEqual(state.restores, [], "polling and resize do not run selection scroll restoration");
    assert.equal(content.scrollTop, 250);
    assert.equal(observers.length, observerCount + 1, "tabs install a sizing observer, not a selection-reveal observer");
    assert.deepEqual(observers.at(-1).elements, [scroller, content]);
    props.items = [props.items[1], props.items[2], props.items[0]]; await nextTick();
    assert.equal(ui.focusIndex.value, 2); assert.equal(scroller.scrollLeft, 220, "moving the selected item reveals its new position");
    props.modelValue = "second"; await nextTick();
    assert.equal(ui.focusIndex.value, 0); assert.equal(scroller.scrollLeft, 0, "changing selection still reveals the selected tab");
  });

  await test("tabs size the panel on mount and resize, clamp minHeight at zero and disconnect the observer", async () => {
    const layout = tabLayout();
    const { scroller, panel, content } = layout;
    const { app } = await mount(Tabs, { modelValue: "first", items: tabItems }, layout.configure);
    const observer = observers.at(-1);
    assert.deepEqual(observer.elements, [scroller, content]);
    assert.equal(panel.style.minHeight, "496px");
    scroller.scrollLeft = 160;
    content.clientHeight = 750; scroller.offsetHeight = 48;
    observer.callback();
    assert.equal(panel.style.minHeight, "638px", "viewport, tabs, margin and bottom padding determine minHeight");
    assert.equal(scroller.scrollLeft, 160, "panel sizing does not reset horizontal scroll");
    content.clientHeight = 80; observer.callback();
    assert.equal(panel.style.minHeight, "0px");
    unmount(app); assert.equal(observer.disconnected, true);
  });

  await test("sticky tab switches reserve short-page scroll range before patch and restore the pre-patch scrollTop", async () => {
    const layout = tabLayout();
    const { scroller, panel, content, state } = layout;
    const { app } = await mount(Tabs, { modelValue: "first", items: tabItems }, layout.configure);
    content.scrollTop = 320; state.restores.length = 0;
    // Invalidate the old reservation to prove the pre-watch sizes before the panel patch.
    panel.style.minHeight = "";
    app._instance.props.modelValue = "second";
    await settle();
    assert.equal(state.patches.at(-1).minHeight, "496px");
    assert.equal(content.maxScroll, 180, "a short page still has enough range to hide the heading");
    assert.deepEqual(state.restores, [320], "restoration uses the value captured before native layout clamping");
    assert.equal(content.scrollTop, 180, "the browser clamps restoration to the short page's scroll range");
    assert.equal(scroller.getBoundingClientRect().top, content.getBoundingClientRect().top);
    app._instance.props.modelValue = "third"; await settle();
    assert.equal(content.scrollTop, 180, "switching again keeps tabs pinned");
  });

  await test("sticky status is captured before patch and restoration advances to a moved tab anchor", async () => {
    const layout = tabLayout();
    const { content, state, scroller } = layout;
    const { app } = await mount(Tabs, { modelValue: "first", items: tabItems }, (api, props) => {
      const patch = layout.configure(api, props);
      return () => {
        patch();
        if (props.modelValue === "second") state.tabsTop = 240;
        return null;
      };
    });
    content.scrollTop = 180; state.restores.length = 0;
    app._instance.props.modelValue = "second"; await settle();
    assert.deepEqual(state.restores, [240], "previously sticky tabs remain pinned even if the anchor moves");
    assert.equal(content.scrollTop, 240);
    assert.equal(scroller.getBoundingClientRect().top, content.getBoundingClientRect().top);
  });

  await test("un-stuck tab switches preserve scrollTop instead of scrolling the heading out of view", async () => {
    const layout = tabLayout();
    const { content, state, scroller } = layout;
    const { app } = await mount(Tabs, { modelValue: "first", items: tabItems }, (api, props) => {
      const patch = layout.configure(api, props);
      return () => {
        patch();
        if (props.modelValue === "second") state.scrollTop = 100;
        return null;
      };
    });
    content.scrollTop = 60; state.restores.length = 0;
    app._instance.props.modelValue = "second"; await settle();
    assert.deepEqual(state.restores, [60]); assert.equal(content.scrollTop, 60);
    assert.ok(scroller.getBoundingClientRect().top > content.getBoundingClientRect().top + 1);
  });

  await test("rapid tab switches cancel pending scroll restoration and unmount cancels the final continuation", async () => {
    const layout = tabLayout();
    const { content, state } = layout;
    let switched = false;
    const { app } = await mount(Tabs, { modelValue: "first", items: tabItems }, (api, props) => {
      const patch = layout.configure(api, props);
      return () => {
        patch();
        if (props.modelValue === "second" && !switched) {
          switched = true;
          state.scrollTop = 90;
          // Queue the next pre-watch while the previous watch is still awaiting nextTick.
          app._instance.props.modelValue = "third";
        }
        return null;
      };
    });
    content.scrollTop = 180; state.restores.length = 0;
    app._instance.props.modelValue = "second"; await settle();
    assert.ok(state.patches.some(patch => patch.selected === "second"));
    assert.equal(state.patches.at(-1).selected, "third");
    assert.deepEqual(state.restores, [90], "the canceled second-tab watcher cannot overwrite the latest scroll position");
    assert.equal(content.scrollTop, 90);
    state.restores.length = 0;
    app._instance.props.modelValue = "first";
    await nextTick();
    unmount(app); await settle();
    assert.deepEqual(state.restores, [], "unmount invalidates restoration awaiting nextTick");
  });

  await test("horizontal tabs keyboard wraps, Home/End focus, and ignored keys do not change selection", async () => {
    const changes = []; const scroller = tabScroller();
    const { ui } = await mount(Tabs, { modelValue: "first", items: tabItems, "onUpdate:modelValue": value => changes.push(value) },
      api => { api.scroller.value = scroller; });
    for (const [key, index, expected] of [["ArrowLeft", 0, 2], ["ArrowRight", 2, 0], ["End", 0, 2], ["Home", 2, 0]]) {
      const input = event({ key }); ui.onKeydown(input, index);
      assert.equal(input.prevented, true); assert.equal(changes.at(-1), tabItems[expected].value);
      assert.deepEqual(scroller.children[expected].focused, { preventScroll: true });
    }
    const count = changes.length; const ignored = event({ key: "ArrowDown" }); ui.onKeydown(ignored, 0);
    assert.equal(ignored.prevented, false); assert.equal(changes.length, count);
  });

  await test("horizontal tabs wheel handles overflow, delta modes, bounds and native-scroll exceptions", async () => {
    const scroller = tabScroller();
    const { ui } = await mount(Tabs, { modelValue: "first", items: tabItems }, api => { api.scroller.value = scroller; });
    for (const [fields, start, expected, prevented] of [
      [{ deltaY: 30 }, 0, 30, true], [{ deltaY: 2, deltaMode: 1 }, 0, 32, true],
      [{ deltaY: 1, deltaMode: 2 }, 0, 100, true], [{ deltaY: 999 }, 0, 200, true],
      [{ deltaY: -999 }, 100, 0, true], [{ deltaY: 20 }, 200, 200, false],
      [{ deltaY: 30, ctrlKey: true }, 0, 0, false], [{ deltaY: 10, deltaX: 20 }, 0, 0, false],
      [{ deltaY: 10, deltaX: 10 }, 0, 0, false], [{ deltaY: 0 }, 0, 0, false],
    ]) {
      scroller.scrollLeft = start;
      const input = event({ deltaX: 0, deltaY: 0, deltaMode: 0, ctrlKey: false, ...fields });
      ui.onWheel(input); assert.equal(scroller.scrollLeft, expected); assert.equal(input.prevented, prevented);
    }
    scroller.scrollWidth = scroller.clientWidth;
    const input = event({ deltaX: 0, deltaY: 20 }); ui.onWheel(input); assert.equal(input.prevented, false);
  });

  await test("horizontal tabs mouse drag threshold, pointer cleanup and post-drag click suppression", async () => {
    const scroller = tabScroller();
    const { ui, app } = await mount(Tabs, { modelValue: "first", items: tabItems }, api => { api.scroller.value = scroller; });
    const down = { pointerType: "mouse", button: 0, isPrimary: true, pointerId: 3, clientX: 100 };
    for (const fields of [{ pointerType: "touch" }, { button: 2 }, { isPrimary: false }]) {
      ui.onPointerDown(event({ ...down, ...fields })); ui.onPointerMove(event({ pointerId: 3, clientX: 80, buttons: 1 }));
      assert.equal(ui.dragging.value, false);
    }
    ui.onPointerDown(event(down));
    const small = event({ pointerId: 3, clientX: 96, buttons: 1 }); ui.onPointerMove(small);
    assert.equal(ui.dragging.value, false); assert.equal(small.prevented, false); assert.equal(scroller.hasPointerCapture(3), false);
    ui.onPointerMove(event({ pointerId: 4, clientX: 80, buttons: 1 })); assert.equal(ui.dragging.value, false);
    const drag = event({ pointerId: 3, clientX: 80, buttons: 1 }); ui.onPointerMove(drag);
    assert.equal(ui.dragging.value, true); assert.equal(drag.prevented, true);
    assert.equal(scroller.scrollLeft, 20); assert.equal(scroller.hasPointerCapture(3), true);
    ui.stopDrag(); assert.equal(ui.dragging.value, false); assert.equal(scroller.hasPointerCapture(3), false);
    const keyboardClick = event({ detail: 0 }); ui.onClick(keyboardClick); assert.equal(keyboardClick.prevented, false);
    const click = event({ detail: 1 }); ui.onClick(click); assert.equal(click.prevented, true); assert.equal(click.stopped, true);
    const ordinary = event({ detail: 1 }); ui.onClick(ordinary); assert.equal(ordinary.prevented, false);
    ui.onPointerDown(event(down)); ui.onPointerMove(event({ pointerId: 3, clientX: 80, buttons: 1 }));
    ui.onPointerMove(event({ pointerId: 3, clientX: 70, buttons: 0 })); assert.equal(ui.dragging.value, false);
    ui.onPointerDown(event(down)); ui.onPointerMove(event({ pointerId: 3, clientX: 80, buttons: 1 }));
    unmount(app); assert.equal(scroller.hasPointerCapture(3), false);
  });

  await test("page header renders optional slots and description, escapes text and preserves actions", async () => {
    const props = { eyebrow: "DASHBOARD", title: "Title <unsafe>", description: "Description & context" };
    const html = await renderToString(createSSRApp({ render: () => h(Header, props, {
      leading: () => h("button", { "data-leading": "yes" }, "Back"),
      actions: () => h("button", { "data-action": "yes" }, "Refresh"),
    }) }));
    assert.match(html, /<h1[^>]*>Title &lt;unsafe&gt;<\/h1>/);
    assert.match(html, /Description &amp; context/); assert.match(html, /DASHBOARD/);
    assert.match(html, /page-header__leading[\s\S]*data-leading="yes"/);
    assert.match(html, /page-header__actions[\s\S]*data-action="yes"/);
    assert.doesNotMatch(html, /<unsafe>/);
    const minimal = await renderToString(createSSRApp(Header, { eyebrow: "SETTINGS", title: "Settings" }));
    assert.doesNotMatch(minimal, /page-header__(?:leading|actions|description)/);
  });

  await test("plugin summary and authorization explanation stay above tabs while plugin content uses the panel slot", async () => {
    const source = await readFile(new URL("../src/views/PluginsView.vue", import.meta.url), "utf8");
    const template = source.slice(source.indexOf("<template>"), source.lastIndexOf("</template>"));
    const tabsStart = template.indexOf("<HorizontalTabs");
    const tabsEnd = template.indexOf("</HorizontalTabs>");
    assert.ok(tabsStart > 0 && tabsEnd > tabsStart);
    for (const marker of ['class="summary-grid"', 'title="\u52a9\u624b\u6301\u7eed\u6388\u6743\u8bf4\u660e"', 'class="approval-note"']) {
      assert.ok(template.indexOf(marker) >= 0 && template.indexOf(marker) < tabsStart, marker);
    }
    for (const marker of ['class="plugin-list"', 'class="plugin-card"']) {
      assert.ok(template.indexOf(marker) > tabsStart && template.indexOf(marker) < tabsEnd, marker);
    }
    assert.doesNotMatch(template.slice(tabsStart, template.indexOf(">", tabsStart)), /\/\s*$/);
    assert.ok(template.indexOf('<el-dialog v-model="auditOpen"') > tabsEnd, "audit dialog is outside the scrolling plugin panel");
  });

  await test("CSS contracts retain responsive sticky offsets, flat tabs, panel scroll anchoring and native touch scrolling", async () => {
    const [dashboard, tabs, header, layout] = await Promise.all([
      readFile(new URL("../src/views/DashboardView.vue", import.meta.url), "utf8"),
      readFile(new URL("../src/components/HorizontalTabs.vue", import.meta.url), "utf8"),
      readFile(new URL("../src/components/PageHeader.vue", import.meta.url), "utf8"),
      readFile(new URL("../src/components/layout/MainLayout.vue", import.meta.url), "utf8"),
    ]);
    assert.match(dashboard, /\.dashboard-card\.is-full\s*\{\s*grid-column:\s*1\s*\/\s*-1/);
    assert.match(dashboard, /@media[^{}]*max-width[^{}]*\{\s*\.dashboard-grid\s*\{\s*grid-template-columns:\s*1fr/);
    assert.match(tabs, /overflow-x:\s*auto/); assert.doesNotMatch(tabs, /touch-action:\s*none|@(?:touchmove|wheel)\.prevent/);
    assert.match(tabs, /\.horizontal-tabs\s*\{[^}]*position:\s*sticky;[^}]*top:\s*-26px;[^}]*gap:\s*0;[^}]*padding:\s*0\s+30px;/);
    assert.match(tabs, /@media\s*\(max-width:\s*800px\)\s*\{\s*\.horizontal-tabs\s*\{[^}]*top:\s*-18px;[^}]*padding-inline:\s*16px;/);
    assert.match(tabs, /\.horizontal-tabs__tab\s*\{[^}]*border-radius:\s*0;/);
    assert.match(tabs, /\.horizontal-tabs__panel\s*\{[^}]*display:\s*flow-root;[^}]*min-width:\s*0;[^}]*overflow-anchor:\s*none;/);
    assert.match(layout, /\.app-content\s*\{[^}]*padding:\s*26px\s+30px\s+48px;/);
    assert.match(layout, /@media\s*\(max-width:\s*800px\)[\s\S]*?\.app-content\s*\{[^}]*padding:\s*18px\s+16px\s+36px;/);
    assert.match(header, /\.page-header__row\s*\{[^}]*min-width:\s*0/);
    assert.match(header, /\.page-header__actions\s*\{[^}]*overflow-x:\s*auto/);
  });

  console.log(`Dashboard layout regression passed (${passed} tests)`);
} finally {
  for (const app of [...apps]) unmount(app);
  await server.close();
  delete globalThis.__dashboardLayoutTest;
  for (const [key, descriptor] of originals) {
    if (descriptor) Object.defineProperty(globalThis, key, descriptor);
    else delete globalThis[key];
  }
}
