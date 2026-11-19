import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { createServer } from "vite";
import { createSSRApp, reactive } from "vue";
import { renderToString } from "vue/server-renderer";
import ElementPlus, { ID_INJECTION_KEY, ZINDEX_INJECTION_KEY } from "element-plus";

// Use the same API feature view as purchase-clicks, not a hand-written schema.
const venvPython = fileURLToPath(new URL("../../.venv/bin/python", import.meta.url));
const [feature, legacy] = JSON.parse(execFileSync(process.env.PYTHON ?? (existsSync(venvPython) ? venvPython : "python3"), [
  "-c",
  "import json; from sqlalchemy import create_engine; from sqlalchemy.orm import Session; from app.features.catalog import FEATURES; from app.models.setting import AppSetting; from app.services.feature_settings import FeatureSettingsService; engine = create_engine('sqlite://'); AppSetting.__table__.create(engine); service = FeatureSettingsService(Session(engine)); print(json.dumps([service.view(FEATURES[key]).model_dump(mode='json') for key in ('post.resign_loop', 'post.resign')]))",
], { cwd: fileURLToPath(new URL("../../backend", import.meta.url)), encoding: "utf8" }));
const schema = feature.config_schema;
const defaults = {
  attendance_url: "{base_url}/calendar",
  calendar_title_selector: [".cal-board > .cal-water"],
  calendar_prev_selector: ['.cal-arr[aria-label="上一月"]'],
  calendar_next_selector: ['.cal-arr[aria-label="下一月"]'],
  calendar_day_selector: ['.cal-grid > button.cal-cell:not(.mute):has(.cal-num:text-is("{day}"))'],
  calendar_selected_selector: ['.cal-grid > button.cal-cell.on:not(.mute):has(.cal-num:text-is("{day}"))'],
  done_selector: ['.cal-grid > button.cal-cell:not(.mute):has(.cal-num:text-is("{day}")) .cal-badge.att'],
  button_selector: [".cal-att button.cal-att-pill.act"],
  wait_seconds: 10,
  max_month_jumps: 12,
  max_retries: 3,
};
const legacyDefaults = {
  attendance_url: "{base_url}/attendance.php",
  card_count_regex: String.raw`[补補][签簽]卡[:：]\s*(\d+)`,
  calendar_title_selector: ".fc-toolbar-title",
  calendar_prev_selector: ".fc-prev-button",
  calendar_next_selector: ".fc-next-button",
  calendar_day_selector: ".fc-daygrid-day",
  calendar_day_number_selector: ".fc-daygrid-day-number",
  calendar_event_selector: ".fc-daygrid-event-harness a",
  calendar_block_selector: ".fc-daygrid-block-event",
  calendar_dot_selector: ".fc-daygrid-dot-event",
  ajax_path: "ajax.php",
  ajax_action: "attendanceRetroactive",
  ajax_date_param: "params[date]",
};
const selectors = Object.keys(defaults).filter(key => key.endsWith("_selector"));
const dateSelectors = ["calendar_day_selector", "calendar_selected_selector", "done_selector"];
assert.equal(feature.key, "post.resign_loop");
assert.equal(feature.name, "页面循环补签");
assert.equal(schema.title, "PostResignLoopConfig");
assert.equal(schema.additionalProperties, false);
assert.equal(Object.keys(defaults).length, 11);
assert.deepEqual(feature.defaults, defaults);
assert.deepEqual(Object.keys(schema.properties).sort(), Object.keys(defaults).sort());
for (const [key, value] of Object.entries(defaults)) {
  const property = schema.properties[key];
  assert.deepEqual(property.default, value, key);
  assert.match(property.title, /[\u4e00-\u9fff]/, key);
  assert.equal(property["ui:widget"], undefined, `${key} must use generic controls`);
  assert.notEqual(property["ui:hidden"], true, key);
}
for (const key of selectors) {
  const property = schema.properties[key];
  assert.equal(property.type, "array", key);
  assert.deepEqual(property.items, { type: "string", minLength: 1 });
  assert.equal(property.minItems, 1);
  assert.equal(property.maxItems, 20);
}
for (const key of dateSelectors) {
  for (const token of ["{date}", "{day}", "{month}", "{year}"]) {
    assert.ok(schema.properties[key].description.includes(token), `${key}: ${token}`);
  }
}
for (const [key, minimum, maximum] of [["wait_seconds", 1, 60], ["max_month_jumps", 0, 120], ["max_retries", 0, 10]]) {
  assert.equal(schema.properties[key].type, "integer");
  assert.equal(schema.properties[key].minimum, minimum);
  assert.equal(schema.properties[key].maximum, maximum);
}
assert.equal(legacy.config_schema.title, "PostResignConfig");
assert.deepEqual(legacy.defaults, legacyDefaults);
assert.deepEqual(Object.keys(legacy.config_schema.properties).sort(), Object.keys(legacyDefaults).sort());
for (const [key, value] of Object.entries(legacyDefaults)) {
  assert.equal(legacy.config_schema.properties[key].type, "string", key);
  assert.equal(legacy.config_schema.properties[key].default, value, key);
}

const server = await createServer({
  optimizeDeps: { noDiscovery: true, include: [] },
  server: { middlewareMode: true, hmr: false },
  plugins: [{
    name: "resign-loop-test-expose", enforce: "pre",
    transform(source, id) {
      if (id.endsWith("/SchemaConfigForm.vue")) {
        source = source.replace("</script>", "defineExpose({ valueOf, setField, clearField, openPicker, applyPicked, addArrayItem, updateArrayItem, removeArrayItem });\n</script>");
      }
      if (id.endsWith("/FeatureSettingsEditor.vue")) {
        source = source.replace("</script>", "defineExpose({ text, validationError, save });\n</script>");
      }
      // Stub only the closed browser-only noVNC dialog, retaining real picker handlers.
      if (id.endsWith("/SchemaConfigForm.vue") || id.endsWith("/StringListField.vue")) {
        source = source.replace('import ElementPickerDialog from "@/components/ElementPickerDialog.vue";',
          'const ElementPickerDialog = { render: () => null };');
      }
      return source;
    },
  }],
});

function freeze(value) {
  if (value && typeof value === "object") {
    Object.values(value).forEach(freeze);
    Object.freeze(value);
  }
  return value;
}

async function setup(Component, props, emit = () => {}) {
  let exposed;
  await renderToString(createSSRApp({ setup() {
    Component.setup(props, { expose(value) { exposed = value; }, emit });
    return () => null;
  } }));
  return exposed;
}

const render = (Component, props) => renderToString(createSSRApp(Component, props)
  .use(ElementPlus).provide(ID_INJECTION_KEY, { prefix: 0, current: 0 })
  .provide(ZINDEX_INJECTION_KEY, { current: 0 }));
const escapeHtml = value => String(value).replace(/[&<>"']/g, char => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
})[char]);
const noPurchaseWidget = html => assert.doesNotMatch(html, /purchase-clicks|点击 1|点击范围|点击 1 范围|添加点击|候选选择器仅在定位时/);

try {
  const { default: Form } = await server.ssrLoadModule("/src/components/SchemaConfigForm.vue");
  const { default: Step } = await server.ssrLoadModule("/src/components/StepFields.vue");
  const { default: Editor } = await server.ssrLoadModule("/src/components/FeatureSettingsEditor.vue");
  const { schemaDefaults } = await server.ssrLoadModule("/src/utils/schema.ts");
  freeze(schema);
  freeze(feature.defaults);
  const cloned = schemaDefaults(schema);
  assert.deepEqual(cloned, defaults);
  for (const key of selectors) {
    assert.notEqual(cloned[key], schema.properties[key].default);
    cloned[key].push("#independent-{day}");
  }
  assert.deepEqual(schemaDefaults(schema), defaults);

  const renderStep = (readonly, step, inherited, current = feature) => render(Step, {
    step, feature: current, features: [feature, legacy], inherited, readonly,
    signals: [], cfboxEnabled: false, pickerBaseUrl: "https://example.com",
  });
  const step = reactive({ feature_type: feature.key, enabled: "inherit", config: {}, condition: "", on_error: "" });
  for (const readonly of [false, true]) {
    const html = await renderStep(readonly, step);
    assert.ok(html.includes(feature.name));
    for (const property of Object.values(schema.properties)) assert.ok(html.includes(property.title), property.title);
    for (const key of selectors) assert.ok(html.includes(escapeHtml(defaults[key][0])), key);
    assert.doesNotMatch(html, /\[object Object\]/);
    noPurchaseWidget(html);
    if (readonly) assert.doesNotMatch(html, /<input|<select|<button/);
    else {
      assert.match(html, /class="inline-list"/);
      assert.match(html, /class="row-pick"/);
      assert.match(html, /拾取并添加/);
      assert.match(html, /type="number" value="10" min="1" max="60"/);
      assert.match(html, /type="number" value="12" min="0" max="120"/);
      assert.match(html, /type="number" value="3" min="0" max="10"/);
    }
    assert.deepEqual(step.config, {}, "rendering defaults must not create overrides");
  }

  const inherited = freeze({
    calendar_day_selector: ['button.day:not(.adjacent):has-text("{day}")', '[data-date="{date}"]'],
    wait_seconds: 25,
  });
  let emissions = 0;
  const props = reactive({ schema, modelValue: {}, inherited, pickerBaseUrl: "https://example.com" });
  const form = await setup(Form, props, (event, value) => {
    assert.equal(event, "update:modelValue");
    props.modelValue = value;
    emissions++;
  });
  for (const readonly of [false, true]) {
    const html = await renderStep(readonly, step, inherited);
    for (const selector of inherited.calendar_day_selector) assert.ok(html.includes(escapeHtml(selector)));
    assert.ok(html.includes("25"));
    noPurchaseWidget(html);
    assert.deepEqual(step.config, {});
  }
  await render(Form, props);
  assert.equal(emissions, 0, "rendering inherited values must not emit overrides");
  assert.deepEqual(props.modelValue, {});
  assert.equal(form.valueOf("calendar_day_selector", schema.properties.calendar_day_selector), inherited.calendar_day_selector);

  // Every selector uses the generic replace/append picker and array editing path.
  for (const key of selectors) {
    const property = schema.properties[key];
    const original = [...form.valueOf(key, property)];
    const picked = 'button:not(.mute):has(.num:text-is("{day}"))';
    const appended = '[data-date="{date}"][data-month="{month}"][data-year="{year}"]';
    form.openPicker(key, property, 0);
    form.applyPicked(picked);
    assert.deepEqual(props.modelValue[key], [picked, ...original.slice(1)]);
    form.openPicker(key, property);
    form.applyPicked(appended);
    form.addArrayItem(key, property);
    form.updateArrayItem(key, property, original.length + 1, "#manual-{day}");
    form.removeArrayItem(key, property, 0);
    assert.deepEqual(props.modelValue[key], [...original.slice(1), appended, "#manual-{day}"]);
    assert.ok((await render(Form, props)).includes(escapeHtml(appended)));
    form.clearField(key);
    assert.deepEqual(props.modelValue, {});
    assert.deepEqual(form.valueOf(key, property), original);
  }
  form.setField("wait_seconds", 40);
  assert.match(await render(Form, props), /恢复继承/);
  form.clearField("wait_seconds");
  assert.equal(form.valueOf("wait_seconds", schema.properties.wait_seconds), 25);
  form.setField("max_retries", 0);
  assert.equal(form.valueOf("max_retries", schema.properties.max_retries), 0);
  form.clearField("max_retries");
  assert.equal(form.valueOf("max_retries", schema.properties.max_retries), 3);
  props.inherited = null;
  for (const key of selectors) assert.equal(form.valueOf(key, schema.properties[key]), schema.properties[key].default);
  assert.deepEqual(feature.defaults, defaults);
  assert.deepEqual(inherited.calendar_day_selector, ['button.day:not(.adjacent):has-text("{day}")', '[data-date="{date}"]']);

  const editor = await setup(Editor, { feature });
  assert.equal(editor.validationError.value, "");
  for (const key of selectors) {
    for (const invalid of ["#day-{day}", [""], [1], Array(21).fill("#day-{day}"), []]) {
      editor.text.value = JSON.stringify({ [key]: invalid });
      assert.match(editor.validationError.value, new RegExp(key));
    }
  }
  for (const [key, values] of [
    ["wait_seconds", [0, 61, 1.5, "10", true, null]],
    ["max_retries", [-1, 11, 1.5, "3", true, null]],
    ["max_month_jumps", [-1, 121, 1.5, "12", true, null]],
  ]) {
    for (const invalid of values) {
      editor.text.value = JSON.stringify({ [key]: invalid });
      assert.match(editor.validationError.value, new RegExp(key));
    }
  }
  for (const max_retries of [0, 3, 10]) {
    editor.text.value = JSON.stringify({ max_retries });
    assert.equal(editor.validationError.value, "");
  }
  const dayTemplate = 'button.day:not(.adjacent):has(.num:text-is("{day}"))';
  form.openPicker("calendar_day_selector", schema.properties.calendar_day_selector, 0);
  form.applyPicked(dayTemplate);
  assert.deepEqual(props.modelValue, { calendar_day_selector: [dayTemplate] });
  const saved = { ...defaults, ...props.modelValue };
  editor.text.value = JSON.stringify(saved);
  assert.equal(editor.validationError.value, "");
  const originalFetch = globalThis.fetch;
  const originalDocument = globalThis.document;
  const requests = [];
  const updates = [];
  const savingEditor = await setup(Editor, { feature }, (event, value) => updates.push({ event, value }));
  savingEditor.text.value = editor.text.value;
  globalThis.document = { cookie: "" };
  globalThis.fetch = async (url, options) => {
    requests.push({ url, method: options.method, ...JSON.parse(options.body) });
    return new Response(JSON.stringify({ ...feature, defaults: saved }));
  };
  try {
    await savingEditor.save();
    assert.deepEqual(requests, [{
      url: "/api/v1/features/post.resign_loop/settings", method: "PUT",
      defaults: saved, required_fields: feature.required_fields, execution_settings: feature.execution_settings,
    }]);
    assert.deepEqual(updates, [{ event: "updated", value: { ...feature, defaults: saved } }]);
  } finally {
    globalThis.fetch = originalFetch;
    if (originalDocument === undefined) delete globalThis.document;
    else globalThis.document = originalDocument;
  }

  const legacyStep = { ...step, feature_type: legacy.key, config: {} };
  for (const readonly of [false, true]) {
    const html = await renderStep(readonly, legacyStep, null, legacy);
    for (const value of Object.values(legacyDefaults)) assert.ok(html.includes(escapeHtml(value)), value);
    assert.doesNotMatch(html, /class="inline-list"|class="row-pick"|拾取并添加/);
    noPurchaseWidget(html);
    assert.deepEqual(legacyStep.config, {});
  }
  assert.deepEqual(legacy.defaults, legacyDefaults);
  console.log("Resign loop regression passed (11-field API schema, editable/readonly, generic selector picking, inheritance, retry bounds, template save, legacy FullCalendar unchanged)");
} finally {
  await server.close();
}
