import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { createServer } from "vite";
import { createSSRApp, reactive } from "vue";
import { renderToString } from "vue/server-renderer";
import ElementPlus, { ID_INJECTION_KEY, ZINDEX_INJECTION_KEY } from "element-plus";

// Use the API feature view from an empty, in-memory settings database.
const venvPython = fileURLToPath(new URL("../../.venv/bin/python", import.meta.url));
const feature = JSON.parse(execFileSync(process.env.PYTHON ?? (existsSync(venvPython) ? venvPython : "python3"), [
  "-c",
  "from sqlalchemy import create_engine; from sqlalchemy.orm import Session; from app.features.catalog import FEATURES; from app.models.setting import AppSetting; from app.services.feature_settings import FeatureSettingsService; engine = create_engine('sqlite://'); AppSetting.__table__.create(engine); print(FeatureSettingsService(Session(engine)).view(FEATURES['auth.form_login']).model_dump_json())",
], { cwd: fileURLToPath(new URL("../../backend", import.meta.url)), encoding: "utf8" }));
const schema = feature.config_schema;
const wait = schema.properties.success_wait_seconds;
assert.equal(feature.defaults.success_wait_seconds, 10);
assert.equal(wait.type, "integer");
assert.equal(wait.default, 10);
assert.equal(wait.minimum, 1);
assert.equal(wait.maximum, 120);

const server = await createServer({
  optimizeDeps: { noDiscovery: true, include: [] },
  server: { middlewareMode: true, hmr: false },
  plugins: [{
    name: "login-readiness-test", enforce: "pre",
    transform(source, id) {
      // Only the closed, browser-only noVNC dialog is replaced.
      if (id.endsWith("/SchemaConfigForm.vue") || id.endsWith("/StringListField.vue")) {
        return source.replace('import ElementPickerDialog from "@/components/ElementPickerDialog.vue";',
          "const ElementPickerDialog = { render: () => null };");
      }
    },
  }],
});
const render = (Component, props) => renderToString(createSSRApp(Component, props)
  .use(ElementPlus).provide(ID_INJECTION_KEY, { prefix: 0, current: 0 })
  .provide(ZINDEX_INJECTION_KEY, { current: 0 }));

try {
  const { default: Step } = await server.ssrLoadModule("/src/components/StepFields.vue");
  const { default: Signals } = await server.ssrLoadModule("/src/components/SignalFields.vue");
  const step = reactive({ feature_type: feature.key, enabled: "inherit", config: {}, condition: "", on_error: "" });
  const props = { step, feature, features: [feature], signals: [], cfboxEnabled: false };
  const defaults = structuredClone(feature.defaults);
  const schemaBefore = structuredClone(schema);
  const html = await render(Step, props);
  assert.ok(html.includes(wait.title), "the real schema exposes the readiness field automatically");
  assert.match(html, /type="number" value="10" min="1" max="120"/);
  assert.deepEqual(step.config, {}, "displaying defaults must not save overrides");

  const inherited = { success_wait_seconds: 25 };
  assert.match(await render(Step, { ...props, inherited }), /type="number" value="25" min="1" max="120"/);
  step.config.success_wait_seconds = 12;
  assert.match(await render(Step, { ...props, inherited }), /type="number" value="12" min="1" max="120"/);
  assert.deepEqual(step.config, { success_wait_seconds: 12 });
  assert.deepEqual(inherited, { success_wait_seconds: 25 });
  assert.deepEqual(feature.defaults, defaults);
  assert.deepEqual(schema, schemaBefore);

  for (const match_mode of ["any", "all"]) {
    const signal = reactive({
      signal_key: "is_logged_in", label: "Logged in", description: "Account evidence", enabled: true,
      match_mode, include_texts: ["Welcome"], exclude_texts: ["Sign in"],
      include_selectors: [".account", ".logout:visible"], exclude_selectors: [".login"],
    });
    const before = JSON.parse(JSON.stringify(signal));
    const rendered = await render(Signals, { modelValue: signal, used: true });
    assert.match(rendered, /class="signal-info"[^>]*>[^<]*Playwright[^<]*:visible/);
    assert.match(rendered, /value="\.account"/);
    assert.match(rendered, /value="\.logout:visible"/);
    assert.match(rendered, /value="\.login"/);
    assert.match(rendered, new RegExp(`<option[^>]*value="${match_mode}"[^>]* selected`));
    assert.match(rendered, /class="signal-used"/);
    assert.deepEqual(JSON.parse(JSON.stringify(signal)), before, "guidance must not rewrite signal configuration");
  }
  console.log("Login readiness regression passed (real API defaults, schema-driven form, inheritance, signal guidance without mutation)");
} finally {
  await server.close();
}
