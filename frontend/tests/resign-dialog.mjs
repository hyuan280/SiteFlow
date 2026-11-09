import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { createServer } from "vite";
import { createSSRApp, reactive } from "vue";
import { renderToString } from "vue/server-renderer";
import ElementPlus, { ID_INJECTION_KEY, ZINDEX_INJECTION_KEY } from "element-plus";

const venvPython = fileURLToPath(new URL("../../.venv/bin/python", import.meta.url));
const feature = JSON.parse(execFileSync(process.env.PYTHON ?? (existsSync(venvPython) ? venvPython : "python3"), [
  "-c",
  "from sqlalchemy import create_engine; from sqlalchemy.orm import Session; from app.features.catalog import FEATURES; from app.models.setting import AppSetting; from app.services.feature_settings import FeatureSettingsService; engine = create_engine('sqlite://'); AppSetting.__table__.create(engine); print(FeatureSettingsService(Session(engine)).view(FEATURES['post.resign_dialog']).model_dump_json())",
], { cwd: fileURLToPath(new URL("../../backend", import.meta.url)), encoding: "utf8" }));
const schema = feature.config_schema;
const defaults = {
  entry_selector: ['.attendance-card header button[title*="补签卡"]'],
  dialog_selector: ['[role="dialog"][aria-label^="补签"]'],
  dialog_ready_selector: [],
  button_selector: ["button"],
  date_regex: String.raw`(?<!\d)(?P<month>\d{2})-(?P<day>\d{2})(?!\d)`,
  missing_button_as_signed: false,
  dialog_wait_seconds: 10,
  success_wait_seconds: 10,
  success_selector: [],
  success_regex: "补签成功",
  done_selector: ['.attendance-calendar__day.is-done[title^="{date}"]'],
  card_count_selector: ['.attendance-card header button[title*="补签卡"]'],
  card_count_regex: String.raw`补签\s*[×x]\s*(\d+)`,
};
const selectors = Object.keys(defaults).filter(key => key.endsWith("_selector"));
assert.equal(feature.name, "弹窗按钮补签");
assert.equal(feature.category, "post");
assert.equal(feature.cardinality, "multiple");
assert.equal(schema.title, "PostResignDialogConfig");
assert.equal(schema.additionalProperties, false);
assert.deepEqual(feature.defaults, defaults);
assert.deepEqual(Object.keys(schema.properties).sort(), Object.keys(defaults).sort());
for (const [key, value] of Object.entries(defaults)) {
  assert.deepEqual(schema.properties[key].default, value);
  assert.match(schema.properties[key].title, /[\u4e00-\u9fff]/);
  assert.equal(schema.properties[key]["ui:widget"], undefined);
}
for (const key of selectors) {
  assert.equal(schema.properties[key].type, "array");
  assert.deepEqual(schema.properties[key].items, { type: "string", minLength: 1 });
}
assert.equal(schema.properties.dialog_ready_selector.title, "弹窗内容就绪选择器");
assert.equal(schema.properties.dialog_ready_selector.maxItems, 20);
assert.equal(schema.properties.dialog_ready_selector.minItems ?? 0, 0);

const server = await createServer({
  optimizeDeps: { noDiscovery: true, include: [] },
  server: { middlewareMode: true, hmr: false },
  plugins: [{
    name: "resign-dialog-test-expose", enforce: "pre",
    transform(source, id) {
      if (id.endsWith("/SchemaConfigForm.vue")) {
        source = source.replace("</script>", "defineExpose({ valueOf, setField, clearField, openPicker, applyPicked, addArrayItem, updateArrayItem, removeArrayItem });\n</script>");
      }
      if (id.endsWith("/FeatureSettingsEditor.vue")) {
        source = source.replace("</script>", "defineExpose({ text, validationError, save });\n</script>");
      }
      if (id.endsWith("/SchemaConfigForm.vue") || id.endsWith("/StringListField.vue")) {
        source = source.replace('import ElementPickerDialog from "@/components/ElementPickerDialog.vue";',
          'const ElementPickerDialog = { render: () => null };');
      }
      return source;
    },
  }],
});

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

try {
  const { default: Form } = await server.ssrLoadModule("/src/components/SchemaConfigForm.vue");
  const { default: Step } = await server.ssrLoadModule("/src/components/StepFields.vue");
  const { default: Editor } = await server.ssrLoadModule("/src/components/FeatureSettingsEditor.vue");
  const { schemaDefaults } = await server.ssrLoadModule("/src/utils/schema.ts");
  const cloned = schemaDefaults(schema);
  for (const key of selectors) cloned[key].push("#independent-{date}");
  assert.deepEqual(schemaDefaults(schema), defaults);

  const step = reactive({ feature_type: feature.key, enabled: "inherit", config: {}, condition: "", on_error: "" });
  const inherited = {
    button_selector: ["button.date", "button.resign"], success_wait_seconds: 25,
    dialog_ready_selector: [".dates-loaded", ".empty-dates"],
  };
  for (const readonly of [false, true]) {
    const html = await render(Step, {
      step, feature, features: [feature], readonly, signals: [], cfboxEnabled: false,
      pickerBaseUrl: "https://example.com",
    });
    assert.ok(html.includes(feature.name));
    for (const property of Object.values(schema.properties)) assert.ok(html.includes(property.title), property.title);
    for (const key of selectors) {
      if (defaults[key].length) assert.ok(html.includes(escapeHtml(defaults[key][0])), key);
    }
    assert.ok(html.includes(escapeHtml(defaults.date_regex)));
    assert.ok(html.includes(escapeHtml(defaults.card_count_regex)));
    assert.doesNotMatch(html, /\[object Object\]|purchase-clicks|添加点击/);
    if (readonly) assert.doesNotMatch(html, /<input|<select|<button/);
    else {
      assert.match(html, /拾取并添加/);
      assert.match(html, /type="number" value="10" min="1" max="60"/);
      assert.match(html, /type="checkbox"/);
    }
    assert.deepEqual(step.config, {}, "displaying defaults must not create overrides");
    const inheritedHtml = await render(Step, {
      step, feature, features: [feature], readonly, inherited, signals: [], cfboxEnabled: false,
      pickerBaseUrl: "https://example.com",
    });
    for (const selector of inherited.button_selector) assert.ok(inheritedHtml.includes(escapeHtml(selector)));
    for (const selector of inherited.dialog_ready_selector) assert.ok(inheritedHtml.includes(escapeHtml(selector)));
    assert.ok(inheritedHtml.includes("25"));
    assert.deepEqual(step.config, {}, "displaying inheritance must not create overrides");
  }

  let emissions = 0;
  const props = reactive({ schema, modelValue: {}, inherited, pickerBaseUrl: "https://example.com" });
  const form = await setup(Form, props, (event, value) => {
    assert.equal(event, "update:modelValue");
    props.modelValue = value;
    emissions++;
  });
  await render(Form, props);
  assert.equal(emissions, 0);
  assert.deepEqual(form.valueOf("button_selector", schema.properties.button_selector), inherited.button_selector);
  for (const key of selectors) {
    const property = schema.properties[key];
    const original = [...form.valueOf(key, property)];
    const picked = key === "done_selector" ? '[data-date="{date}"].done' : 'button:text-matches("\\d{2}-\\d{2}")';
    form.openPicker(key, property, original.length ? 0 : undefined);
    form.applyPicked(picked);
    assert.deepEqual(props.modelValue[key], [picked, ...original.slice(1)]);
    form.openPicker(key, property);
    form.applyPicked("#second-{day}");
    form.addArrayItem(key, property);
    const length = props.modelValue[key].length;
    form.updateArrayItem(key, property, length - 1, "#manual-{date}");
    form.removeArrayItem(key, property, 0);
    assert.deepEqual(props.modelValue[key], [...original.slice(1), "#second-{day}", "#manual-{date}"]);
    form.clearField(key);
    assert.deepEqual(props.modelValue, {});
    assert.deepEqual(form.valueOf(key, property), original);
  }
  for (const key of ["done_selector", "success_selector", "dialog_ready_selector"]) {
    form.setField(key, []);
    assert.deepEqual(form.valueOf(key, schema.properties[key]), []);
    form.clearField(key);
  }
  form.setField("missing_button_as_signed", true);
  assert.equal(form.valueOf("missing_button_as_signed", schema.properties.missing_button_as_signed), true);
  form.clearField("missing_button_as_signed");
  assert.equal(form.valueOf("missing_button_as_signed", schema.properties.missing_button_as_signed), false);
  form.setField("success_wait_seconds", 40);
  assert.match(await render(Form, props), /恢复继承/);
  form.clearField("success_wait_seconds");
  assert.equal(form.valueOf("success_wait_seconds", schema.properties.success_wait_seconds), 25);
  assert.deepEqual(inherited.button_selector, ["button.date", "button.resign"]);
  assert.deepEqual(inherited.dialog_ready_selector, [".dates-loaded", ".empty-dates"]);
  assert.deepEqual(feature.defaults, defaults);

  const updates = [];
  const editor = await setup(Editor, { feature }, (event, value) => updates.push({ event, value }));
  assert.equal(editor.validationError.value, "");
  for (const key of selectors) {
    for (const invalid of ["button", [""], [1], Array(21).fill("button"),
      ...(["done_selector", "success_selector", "dialog_ready_selector"].includes(key) ? [] : [[]])]) {
      editor.text.value = JSON.stringify({ [key]: invalid });
      assert.match(editor.validationError.value, new RegExp(key));
    }
  }
  for (const key of ["dialog_wait_seconds", "success_wait_seconds"]) {
    for (const invalid of [0, 61, 1.5, "10"]) {
      editor.text.value = JSON.stringify({ [key]: invalid });
      assert.match(editor.validationError.value, new RegExp(key));
    }
  }
  editor.text.value = JSON.stringify({ missing_button_as_signed: "false" });
  assert.match(editor.validationError.value, /missing_button_as_signed/);
  editor.text.value = JSON.stringify({ dialog_ready_selector: [] });
  assert.equal(editor.validationError.value, "");
  const saved = {
    ...defaults, success_selector: ['[role="dialog"][aria-label^="补签"] p'],
    dialog_ready_selector: [".dates-loaded", ".empty-dates"],
  };
  editor.text.value = JSON.stringify(saved);
  assert.equal(editor.validationError.value, "");
  const originalFetch = globalThis.fetch;
  const originalDocument = globalThis.document;
  const requests = [];
  globalThis.document = { cookie: "" };
  globalThis.fetch = async (url, options) => {
    requests.push({ url, method: options.method, ...JSON.parse(options.body) });
    return new Response(JSON.stringify({ ...feature, defaults: saved }));
  };
  try {
    await editor.save();
    assert.deepEqual(requests, [{
      url: "/api/v1/features/post.resign_dialog/settings", method: "PUT",
      defaults: saved, required_fields: feature.required_fields, execution_settings: feature.execution_settings,
    }]);
    assert.deepEqual(updates, [{ event: "updated", value: { ...feature, defaults: saved } }]);
  } finally {
    globalThis.fetch = originalFetch;
    if (originalDocument === undefined) delete globalThis.document;
    else globalThis.document = originalDocument;
  }
  console.log("Resign dialog config passed (real API schema, editable/readonly, selector picking, inheritance, boolean/waits, settings save)");
} finally {
  await server.close();
}
