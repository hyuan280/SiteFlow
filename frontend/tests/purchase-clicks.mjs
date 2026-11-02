import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { existsSync } from "node:fs";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { createServer } from "vite";
import { createSSRApp, reactive } from "vue";
import { renderToString } from "vue/server-renderer";
import ElementPlus, { ID_INJECTION_KEY, ZINDEX_INJECTION_KEY } from "element-plus";

// Consume the API's real feature view, including factory defaults and items.$ref.
const venvPython = fileURLToPath(new URL("../../.venv/bin/python", import.meta.url));
const feature = JSON.parse(execFileSync(process.env.PYTHON ?? (existsSync(venvPython) ? venvPython : "python3"), [
  "-c",
  "from sqlalchemy import create_engine; from sqlalchemy.orm import Session; from app.features.catalog import FEATURES; from app.models.setting import AppSetting; from app.services.feature_settings import FeatureSettingsService; engine = create_engine('sqlite://'); AppSetting.__table__.create(engine); print(FeatureSettingsService(Session(engine)).view(FEATURES['post.buy_card']).model_dump_json())",
], { cwd: fileURLToPath(new URL("../../backend", import.meta.url)), encoding: "utf8" }));
const schema = feature.config_schema;
const property = schema.properties.purchase_clicks;
assert.equal(property["ui:widget"], "purchase-clicks");
assert.match(property.items.$ref, /^#\/\$defs\//);
assert.equal(property.minItems, 1);
assert.equal(property.maxItems, 10);
assert.deepEqual(property.default, [{
  scope: "product", selectors: ["input[type='submit']", "button[type='submit']"],
}]);
const quantityProperty = schema.properties.quantity_selectors;
assert.equal(quantityProperty.title, "购买数量输入框候选");
assert.equal(quantityProperty.type, "array");
assert.deepEqual(quantityProperty.items, { type: "string", minLength: 1 });
assert.equal(quantityProperty.maxItems, 20);
assert.deepEqual(quantityProperty.default, []);
assert.deepEqual(feature.defaults.quantity_selectors, []);
assert.equal(quantityProperty["ui:widget"], undefined, "quantity uses the generic selector array");

const server = await createServer({
  optimizeDeps: { noDiscovery: true, include: [] },
  server: { middlewareMode: true, hmr: false },
  plugins: [{
    name: "purchase-clicks-test-expose", enforce: "pre",
    transform(source, id) {
      const exposed = {
        "PurchaseClicksField.vue": "change",
        "StringListField.vue": "update, add, remove, openPicker, applyPicked",
        "SchemaConfigForm.vue": "entries, valueOf, setField, clearField, openPicker, applyPicked, addArrayItem, updateArrayItem, removeArrayItem",
        "FeatureSettingsEditor.vue": "text, validationError, save",
      };
      for (const [name, bindings] of Object.entries(exposed)) {
        if (id.endsWith(`/${name}`)) {
          source = source.replace("</script>", `defineExpose({ ${bindings} });\n</script>`);
        }
      }
      // The closed picker imports browser-only noVNC; keep its real field integration.
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

try {
  const { default: Clicks } = await server.ssrLoadModule("/src/components/PurchaseClicksField.vue");
  const { default: Strings } = await server.ssrLoadModule("/src/components/StringListField.vue");
  const { default: Form } = await server.ssrLoadModule("/src/components/SchemaConfigForm.vue");
  const { default: Step } = await server.ssrLoadModule("/src/components/StepFields.vue");
  const { default: Editor } = await server.ssrLoadModule("/src/components/FeatureSettingsEditor.vue");

  freeze(quantityProperty.default);
  const quantityInherited = freeze({ quantity_selectors: ["#quantity", ".quantity-fallback"] });
  const quantityProps = reactive({
    schema: { ...schema, properties: { quantity_selectors: quantityProperty } },
    modelValue: {}, inherited: quantityInherited, pickerBaseUrl: "https://example.com",
  });
  const quantityForm = await setup(Form, quantityProps, (event, value) => {
    assert.equal(event, "update:modelValue");
    quantityProps.modelValue = value;
  });
  const quantityHtml = await render(Form, quantityProps);
  assert.match(quantityHtml, /购买数量输入框候选/);
  assert.match(quantityHtml, /class="inline-list"/);
  assert.match(quantityHtml, /value="#quantity"/);
  assert.match(quantityHtml, /value="\.quantity-fallback"/);
  assert.match(quantityHtml, /class="row-pick"/);
  assert.match(quantityHtml, /拾取并添加/);
  assert.doesNotMatch(quantityHtml, /点击 1|点击范围|<select|workflow/i);
  assert.deepEqual(quantityProps.modelValue, {}, "quantity rendering must not create overrides");
  quantityForm.openPicker("quantity_selectors", quantityProperty, 0);
  quantityForm.applyPicked("#picked-quantity");
  quantityForm.openPicker("quantity_selectors", quantityProperty);
  quantityForm.applyPicked("#quantity-last");
  quantityForm.addArrayItem("quantity_selectors", quantityProperty);
  quantityForm.updateArrayItem("quantity_selectors", quantityProperty, 3, "#manual-quantity");
  quantityForm.removeArrayItem("quantity_selectors", quantityProperty, 1);
  assert.deepEqual(quantityProps.modelValue, {
    quantity_selectors: ["#picked-quantity", "#quantity-last", "#manual-quantity"],
  });
  assert.deepEqual(quantityInherited.quantity_selectors, ["#quantity", ".quantity-fallback"]);
  quantityForm.setField("quantity_selectors", []);
  assert.deepEqual(quantityForm.valueOf("quantity_selectors", quantityProperty), []);
  quantityForm.clearField("quantity_selectors");
  assert.equal(quantityForm.valueOf("quantity_selectors", quantityProperty), quantityInherited.quantity_selectors);
  quantityProps.inherited = null;
  assert.equal(quantityForm.valueOf("quantity_selectors", quantityProperty), quantityProperty.default);
  const emptyQuantityHtml = await render(Form, quantityProps);
  assert.match(emptyQuantityHtml, /购买数量输入框候选/);
  assert.match(emptyQuantityHtml, /拾取并添加/);
  assert.doesNotMatch(emptyQuantityHtml, /<input|<select|workflow/i);
  assert.deepEqual(quantityProps.modelValue, {});

  freeze(property.default);
  const inheritedClicks = freeze([
    { scope: "product", selectors: ["#buy", ".fallback"] },
    { scope: "page", selectors: ["#confirm"] },
  ]);
  const inherited = freeze({ purchase_clicks: inheritedClicks });
  const props = reactive({ schema, modelValue: {}, inherited, pickerBaseUrl: "https://example.com" });
  let emissions = 0;
  const form = await setup(Form, props, (event, value) => {
    assert.equal(event, "update:modelValue");
    props.modelValue = value;
    emissions++;
  });
  assert.equal(form.valueOf("purchase_clicks", property), inheritedClicks);
  const inheritedHtml = await render(Form, props);
  assert.match(inheritedHtml, /点击 1/);
  assert.match(inheritedHtml, /点击 2/);
  assert.match(inheritedHtml, /value="#confirm"/);
  assert.match(inheritedHtml, /<select value="page" aria-label="点击 2 范围"/);
  assert.match(inheritedHtml, /候选选择器仅在定位时依次尝试，点击后不再尝试其他候选/);
  assert.match(inheritedHtml, /拾取并添加/);
  assert.deepEqual(props.modelValue, {});
  assert.equal(emissions, 0, "rendering must not materialize inherited values");

  const clickProps = reactive({ modelValue: inheritedClicks, minItems: property.minItems, maxItems: property.maxItems });
  const clicks = await setup(Clicks, clickProps, (event, value) => {
    assert.equal(event, "update:modelValue");
    clickProps.modelValue = value;
    form.setField("purchase_clicks", value);
  });
  const append = () => clicks.change(clickProps.modelValue.length, {});
  append();
  assert.deepEqual(clickProps.modelValue.map(item => item.selectors), [["#buy", ".fallback"], ["#confirm"], [""]]);
  assert.notEqual(clickProps.modelValue[0], inheritedClicks[0]);
  assert.notEqual(clickProps.modelValue[0].selectors, inheritedClicks[0].selectors);
  clicks.change(2, { scope: "page", selectors: ["#finish"] });
  clicks.change(1, null);
  assert.deepEqual(clickProps.modelValue, [
    { scope: "product", selectors: ["#buy", ".fallback"] },
    { scope: "page", selectors: ["#finish"] },
  ], "deleting the middle click preserves the remaining order");

  const stringsProps = reactive({ label: "候选选择器", modelValue: clickProps.modelValue[0].selectors });
  const strings = await setup(Strings, stringsProps, (event, value) => {
    assert.equal(event, "update:modelValue");
    stringsProps.modelValue = value;
    clicks.change(0, { selectors: value });
  });
  strings.add();
  strings.update(2, "#third");
  strings.remove(1);
  assert.deepEqual(clickProps.modelValue[0].selectors, ["#buy", "#third"]);
  strings.openPicker(0);
  strings.applyPicked("#picked");
  strings.openPicker();
  strings.applyPicked("#picked-last");
  assert.deepEqual(clickProps.modelValue[0].selectors, ["#picked", "#third", "#picked-last"]);
  clicks.change(0, { scope: "page" });
  assert.equal(clickProps.modelValue[0].scope, "page");
  assert.deepEqual(inherited.purchase_clicks, inheritedClicks);
  assert.match(await render(Form, props), /恢复继承/);
  form.clearField("purchase_clicks");
  assert.deepEqual(props.modelValue, {});
  assert.equal(form.valueOf("purchase_clicks", property), inheritedClicks);

  props.inherited = null;
  assert.equal(form.valueOf("purchase_clicks", property), property.default);
  clickProps.modelValue = property.default;
  append();
  assert.notEqual(clickProps.modelValue[0].selectors, property.default[0].selectors);
  clicks.change(0, null);
  clicks.change(0, null);
  assert.equal(clickProps.modelValue.length, 1, "cannot remove the last click");
  while (clickProps.modelValue.length < 10) append();
  append();
  assert.equal(clickProps.modelValue.length, 10, "cannot exceed maxItems");
  assert.match(await render(Clicks, clickProps), /<button[^>]*disabled[^>]*>\+ 添加点击/);
  assert.match(await render(Clicks, { modelValue: property.default }), /<button[^>]*disabled[^>]*aria-label="删除点击 1"/);
  assert.deepEqual(property.default, [{ scope: "product", selectors: ["input[type='submit']", "button[type='submit']"] }]);

  const defaultProps = { schema, modelValue: {} };
  const defaultHtml = await render(Form, defaultProps);
  assert.match(defaultHtml, /点击 1/);
  assert.doesNotMatch(defaultHtml, /\[object Object\]/);
  assert.match(defaultHtml, /0 表示.*自动补齐.*正数表示固定购买/);
  for (const title of Object.values(schema.properties.layout.enum_titles)) assert.ok(defaultHtml.includes(title));
  for (const key of ["product_selectors", "price_selectors", "points_selectors", "card_count_selectors", "quantity_selectors"]) {
    assert.ok(defaultHtml.includes(schema.properties[key].title), key);
  }
  assert.deepEqual(defaultProps.modelValue, {}, "rendering defaults must not create overrides");
  const readonlyHtml = await render(Step, {
    step: { feature_type: feature.key, enabled: "inherit", config: {}, condition: "", on_error: "" },
    feature, features: [feature], inherited, readonly: true, signals: [], cfboxEnabled: false,
  });
  assert.doesNotMatch(readonlyHtml, /\[object Object\]|<input|<select|<button/);
  assert.match(readonlyHtml, /&quot;scope&quot;: &quot;product&quot;/);
  assert.match(readonlyHtml, /&quot;selectors&quot;:/);
  assert.ok(readonlyHtml.indexOf("#buy") < readonlyHtml.indexOf("#confirm"));
  for (const key of ["option_selector", "exchange_action", "submit_value"]) {
    assert.equal(schema.properties[key]["ui:hidden"], true);
    assert.ok(!form.entries.value.some(entry => entry.key === key));
    assert.ok(!defaultHtml.includes(schema.properties[key].title));
    assert.ok(!readonlyHtml.includes(schema.properties[key].title));
  }

  const editor = await setup(Editor, { feature });
  assert.equal(editor.validationError.value, "");
  for (const valid of [[], ["#quantity", ".fallback"], Array(20).fill("#quantity")]) {
    editor.text.value = JSON.stringify({ quantity_selectors: valid });
    assert.equal(editor.validationError.value, "");
  }
  for (const invalid of ["#quantity", [""], [1], Array(21).fill("#quantity")]) {
    editor.text.value = JSON.stringify({ quantity_selectors: invalid });
    assert.match(editor.validationError.value, /quantity_selectors/, JSON.stringify(invalid));
  }
  for (const valid of [
    [{ selectors: ["#buy"] }],
    [{ scope: "product", selectors: ["#buy", "#fallback"] }, { scope: "page", selectors: ["#confirm"] }],
  ]) {
    editor.text.value = JSON.stringify({ purchase_clicks: valid });
    assert.equal(editor.validationError.value, "");
  }
  const validClick = { scope: "product", selectors: ["#buy"] };
  for (const invalid of [
    [], Array(11).fill(validClick), [null], ["#buy"], [{}],
    [{ scope: "invalid", selectors: ["#buy"] }],
    [{ selectors: [] }], [{ selectors: [""] }], [{ selectors: [1] }],
    [{ selectors: "#buy" }], [{ selectors: Array(21).fill("#buy") }],
    [{ ...validClick, unknown: true }],
  ]) {
    editor.text.value = JSON.stringify({ purchase_clicks: invalid });
    assert.match(editor.validationError.value, /purchase_clicks/, JSON.stringify(invalid));
  }
  for (const quantity of [-1, 1.5, 366, "1"]) {
    editor.text.value = JSON.stringify({ purchase_quantity: quantity });
    assert.match(editor.validationError.value, /purchase_quantity/);
  }
  const unresolved = await setup(Editor, { feature: {
    ...feature, defaults: { purchase_clicks: [validClick] }, config_schema: { ...schema, $defs: {} },
  } });
  assert.match(unresolved.validationError.value, /无法解析 schema 引用/);

  const source = await readFile(new URL("../src/components/PurchaseClicksField.vue", import.meta.url), "utf8");
  assert.doesNotMatch(source, /draggable|dragstart|move(?:Up|Down|Item)|上移|下移|el-card|workflow/i);
  assert.match(source, /@update:model-value="change\(index, \{ selectors: \$event \}\)"/);
  assert.match(source, /@change="change\(index, \{ scope:/);
  console.log("Purchase clicks regression passed (real API schema, generic quantity selectors, picking, inheritance, readonly, validation)");
} finally {
  await server.close();
}
