import assert from "node:assert/strict";
import { createServer } from "vite";
import { createSSRApp } from "vue";
import { renderToString } from "vue/server-renderer";
import { readFile } from "node:fs/promises";

const server = await createServer({
  optimizeDeps: { noDiscovery: true, include: [] },
  server: { middlewareMode: true },
  plugins: [{
    name: "feature-settings-test-expose", enforce: "pre",
    transform(source, id) {
      if (id.endsWith("/FeatureSettingsEditor.vue")) {
        return source.replace("</script>", "defineExpose({ text, required, validationError, executionSettings, captchaMethod, methodOptions, save });\n</script>");
      }
    },
  }],
});
try {
  const { default: Editor } = await server.ssrLoadModule("/src/components/FeatureSettingsEditor.vue");
  let editor;
  const feature = {
     key: "captcha.image", defaults: { count: 1 },
     execution_settings: { llm_prompt: "Read image" },
    required_fields: ["selector"], base_required_fields: ["selector"],
    config_schema: { properties: {
      selector: { type: "string", minLength: 1 },
      count: { type: "integer", minimum: 1 },
      captcha_method: {
        type: "string", default: "ddddocr",
        enum: ["ddddocr", "vision", "nexusphp_gd5"],
        enum_titles: { ddddocr: "ddddocr", vision: "视觉模型识别", nexusphp_gd5: "nexusphp_gd5" },
        enum_descriptions: { ddddocr: "通用 OCR" },
      },
    } },
  };
  await renderToString(createSSRApp({ setup() {
    Editor.setup({ feature }, { expose(value) { editor = value; }, emit() {} });
    return () => null;
  } }));
  assert.equal(editor.validationError.value, "");
   assert.equal(editor.captchaMethod.value, "ddddocr");
   editor.captchaMethod.value = "vision";
   assert.deepEqual(JSON.parse(editor.text.value), { count: 1, captcha_method: "vision" });
   assert.deepEqual(editor.methodOptions.value.map((item) => item.value), ["ddddocr", "vision", "nexusphp_gd5"]);
   for (const prompt of ["", "   ", "a".repeat(4001)]) {
     editor.executionSettings.value.llm_prompt = prompt;
     assert.ok(editor.validationError.value);
   }
   editor.executionSettings.value.llm_prompt = "Read image";
  for (const invalid of ['[]', '{', '{"unknown": 1}', '{"count": 1.5}', '{"count": 0}', '{"prefer_llm": "true"}', '{"llm_prompt": ""}']) {
    editor.text.value = invalid;
    assert.ok(editor.validationError.value, invalid);
  }
  editor.text.value = '{}';
  editor.required.value = [];
  assert.match(editor.validationError.value, /selector/);
   editor.text.value = '{"selector":"#captcha"}';
   assert.equal(editor.validationError.value, "");
   let request;
   globalThis.document = { cookie: "" };
   const originalFetch = globalThis.fetch;
   globalThis.fetch = async (url, options) => {
     request = { url, ...JSON.parse(options.body) };
     return new Response(JSON.stringify(feature));
   };
   try {
     await editor.save();
     assert.deepEqual(request, {
       url: "/api/v1/features/captcha.image/settings",
       defaults: { selector: "#captcha" }, required_fields: [],
       execution_settings: { llm_prompt: "Read image" },
     });
     const { saveFeatureSettings } = await server.ssrLoadModule("/src/api/client.ts");
     await saveFeatureSettings("flow.open", {}, [], null);
     assert.equal(request.execution_settings, null);
   } finally { globalThis.fetch = originalFetch; }
   let plainEditor;
   await renderToString(createSSRApp({ setup() {
     Editor.setup({ feature: { ...feature, execution_settings: null } }, {
       expose(value) { plainEditor = value; }, emit() {},
     });
     return () => null;
   } }));
   assert.equal(plainEditor.executionSettings.value, null);
   assert.equal(plainEditor.validationError.value, "");
   const { schemaDefaults } = await server.ssrLoadModule("/src/utils/schema.ts");
   assert.deepEqual(schemaDefaults(feature.config_schema), { captcha_method: "ddddocr" });
   for (const file of ["StepFields", "SchemaConfigForm", "WorkflowStepEditor"]) {
     const source = await readFile(new URL(`../src/components/${file}.vue`, import.meta.url), "utf8");
     assert.doesNotMatch(source, /prefer_llm|llm_prompt|execution_settings|FeatureSettingsEditor/);
   }
  console.log("Feature settings regression passed");
} finally {
  await server.close();
}
