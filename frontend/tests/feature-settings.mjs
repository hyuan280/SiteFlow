import assert from "node:assert/strict";
import { createServer } from "vite";
import { createSSRApp, h, reactive } from "vue";
import { renderToString } from "vue/server-renderer";
import { readFile } from "node:fs/promises";
import ElementPlus, { ID_INJECTION_KEY, ZINDEX_INJECTION_KEY } from "element-plus";

const server = await createServer({
  optimizeDeps: { noDiscovery: true, include: [] },
   server: { middlewareMode: true, hmr: false },
  plugins: [{
    name: "feature-settings-test-expose", enforce: "pre",
    transform(source, id) {
      if (id.endsWith("/FeatureSettingsEditor.vue")) {
        return source.replace("</script>", "defineExpose({ text, required, validationError, executionSettings, captchaMethod, methodOptions, save });\n</script>");
      }
      if (id.endsWith("/StepFields.vue")) {
        return source.replace("</script>", "defineExpose({ schema, llmEnabled, llmPrompt, resetResultField });\n</script>");
      }
      if (id.endsWith("/SchemaConfigForm.vue")) {
        // The closed picker imports browser-only noVNC, unrelated to these form regressions.
        return source.replace('import ElementPickerDialog from "@/components/ElementPickerDialog.vue";',
          'const ElementPickerDialog = { render: () => null };');
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
    const resultFeature = {
      key: "flow.result", requires: [], defaults: { output_value: true, message: "" },
      required_fields: [], base_required_fields: [],
       config_schema: { properties: {
         output_value: { type: "boolean", default: true },
         message: { type: "string", default: "", maxLength: 2000 },
         llm_enabled: { type: "boolean", default: false, title: "Generic LLM switch" },
         llm_prompt: { type: "string", default: "", maxLength: 4000, title: "Generic LLM prompt" },
      } },
      execution_settings: {
        llm_enabled: false, use_vision: true, judgment_scope: "success_only", model: "",
        llm_prompt: "请判断当前页面是否已登录且已签到，仅输出 True 或 False。",
      },
    };
    let resultEditor;
    await renderToString(createSSRApp({ setup() {
      Editor.setup({ feature: resultFeature }, {
        expose(value) { resultEditor = value; }, emit() {},
      });
      return () => null;
    } }));
    assert.deepEqual(resultEditor.executionSettings.value, resultFeature.execution_settings);
     assert.notEqual(resultEditor.executionSettings.value, resultFeature.execution_settings);
     assert.equal(resultEditor.validationError.value, "");
     resultEditor.text.value = JSON.stringify({ llm_enabled: true, llm_prompt: "" });
     assert.equal(resultEditor.validationError.value, "", "step prompt defaults may be blank for global fallback");
     resultEditor.text.value = JSON.stringify({ llm_prompt: "a".repeat(4000) });
     assert.equal(resultEditor.validationError.value, "");
     for (const invalid of [{ llm_prompt: "a".repeat(4001) }, { llm_enabled: "true" }]) {
       resultEditor.text.value = JSON.stringify(invalid);
       assert.ok(resultEditor.validationError.value);
     }
     resultEditor.text.value = JSON.stringify(resultFeature.defaults);
    const renderEditor = (value) => renderToString(createSSRApp(Editor, { feature: value })
      .use(ElementPlus).provide(ID_INJECTION_KEY, { prefix: 0, current: 0 })
      .provide(ZINDEX_INJECTION_KEY, { current: 0 }));
    const disabledHtml = await renderEditor(resultFeature);
    for (const label of ["大模型结果判断总开关", "使用视觉判断", "判断范围", "模型 ID 覆盖", "结果判断提示词"]) {
      assert.ok(disabledHtml.includes(label), label);
    }
     for (const notice of ["不发送页面文本", "supports_vision", "页面文本模式", "系统默认模型", "API 密钥", "必须明确要求", "外部模型提供商", "不会静默回退"]) {
       assert.ok(disabledHtml.includes(notice), notice);
     }
     assert.match(disabledHtml, /旧步骤未配置时默认不调用模型/);
     assert.match(disabledHtml, /步骤自定义提示词留空时使用这里的全局提示词/);
    assert.match(disabledHtml, /role="switch"[^>]*aria-checked="false"/);
    assert.match(disabledHtml, /<input(?=[^>]* disabled)(?=[^>]*aria-label="使用视觉判断")[^>]*>/);
    assert.match(disabledHtml, /<input[^>]*value="success_only"[^>]* disabled checked/);
    assert.match(disabledHtml, /<input[^>]*value="all"[^>]* disabled/);
    assert.match(disabledHtml, /<input[^>]*disabled[^>]*aria-label="结果判断模型 ID"/);
    assert.match(disabledHtml, /<textarea[^>]*disabled[^>]*aria-label="结果判断提示词"/);
    resultEditor.executionSettings.value.llm_enabled = true;
    resultEditor.executionSettings.value.use_vision = false;
    resultEditor.executionSettings.value.judgment_scope = "all";
    resultEditor.executionSettings.value.model = "provider/model-id";
    resultEditor.executionSettings.value.llm_prompt = "检查登录和签到状态，结论仅输出 True/False。";
    assert.equal(resultFeature.execution_settings.llm_enabled, false, "editing must not mutate the feature");
    const enabledHtml = await renderEditor({ ...resultFeature, execution_settings: resultEditor.executionSettings.value });
    assert.match(enabledHtml, /role="switch"[^>]*aria-checked="true"/);
    assert.doesNotMatch(enabledHtml, /<(?:input|textarea)[^>]* disabled/);
    assert.match(enabledHtml, /<input[^>]*value="all"[^>]* checked/);
    assert.doesNotMatch(enabledHtml, /视觉模型提示词|图片答题提示词/);
    const quizHtml = await renderEditor({ ...feature, key: "sign.image_quiz" });
    assert.match(quizHtml, /图片答题提示词/);
     assert.match(quizHtml, /需要人工处理/);
     assert.doesNotMatch(quizHtml, /大模型结果判断总开关/);
     const gridFeature = {
       ...feature, key: "challenge.grid_captcha", requires: ["vision"],
       defaults: { max_refresh: 2, success_signals: ["验证成功"], success_when_missing: [] },
       required_fields: [], base_required_fields: [],
       config_schema: { properties: {
         max_refresh: { type: "integer", minimum: 0, maximum: 10 },
         success_signals: { type: "array", items: { type: "string", minLength: 1 } },
         success_when_missing: { type: "array", items: { type: "string", minLength: 1 } },
       } },
       execution_settings: { llm_prompt: "Match the target to all cells, indexed 1-9; do not guess." },
     };
     const gridHtml = await renderEditor(gridFeature);
     assert.match(gridHtml, /九宫格验证码提示词/);
     assert.match(gridHtml, /需要系统设置中的大模型开启「支持视觉」/);
     assert.match(gridHtml, /需要人工处理/);
     assert.doesNotMatch(gridHtml, /大模型结果判断总开关|默认识别方式|图片答题提示词/);
     let gridEditor;
     await renderToString(createSSRApp({ setup() {
       Editor.setup({ feature: gridFeature }, {
         expose(value) { gridEditor = value; }, emit() {},
       });
       return () => null;
     } }));
     assert.equal(gridEditor.validationError.value, "");
     assert.deepEqual(gridEditor.methodOptions.value, []);
     for (const invalid of ["", "   ", "a".repeat(4001)]) {
       gridEditor.executionSettings.value.llm_prompt = invalid;
       assert.match(gridEditor.validationError.value, /九宫格验证码提示词/);
     }
     gridEditor.executionSettings.value.llm_prompt = gridFeature.execution_settings.llm_prompt;
     for (const invalid of [{ max_refresh: 11 }, { success_signals: [""] }, { captcha_method: "vision" }]) {
       gridEditor.text.value = JSON.stringify(invalid);
       assert.ok(gridEditor.validationError.value);
     }
     gridEditor.text.value = JSON.stringify(gridFeature.defaults);
     globalThis.fetch = async (url, options) => {
       request = { url, ...JSON.parse(options.body) };
       return new Response(JSON.stringify(gridFeature));
     };
     try {
       await gridEditor.save();
       assert.deepEqual(request, {
         url: "/api/v1/features/challenge.grid_captcha/settings",
         defaults: gridFeature.defaults, required_fields: [],
         execution_settings: gridFeature.execution_settings,
       });
     } finally { globalThis.fetch = originalFetch; }
     const captchaHtml = await renderEditor(feature);
    assert.match(captchaHtml, /视觉模型提示词/);
    assert.doesNotMatch(captchaHtml, /大模型结果判断总开关/);
    const settings = resultEditor.executionSettings.value;
    const prompt = settings.llm_prompt;
    for (const invalid of ["", "   ", "a".repeat(4001)]) {
      settings.llm_prompt = invalid;
      assert.match(resultEditor.validationError.value, /结果判断提示词/);
    }
    settings.llm_prompt = prompt;
    settings.model = "a".repeat(257);
    assert.match(resultEditor.validationError.value, /256/);
    settings.model = "a".repeat(256);
    assert.equal(resultEditor.validationError.value, "");
    settings.model = "";
    globalThis.fetch = async (url, options) => {
      request = { url, ...JSON.parse(options.body) };
      return new Response(JSON.stringify(resultFeature));
    };
    try {
      await resultEditor.save();
      assert.deepEqual(request, {
        url: "/api/v1/features/flow.result/settings",
        defaults: resultFeature.defaults, required_fields: [], execution_settings: { ...settings },
      });
      settings.llm_enabled = false;
      await resultEditor.save();
      assert.equal(request.execution_settings.llm_enabled, false);
      assert.equal(request.execution_settings.use_vision, false, "disabled settings retain edits");
      assert.equal(request.execution_settings.llm_prompt, prompt);
    } finally { globalThis.fetch = originalFetch; }
    const { featureSupportsLlm, stepUsesLlm } = await server.ssrLoadModule("/src/utils/workflow.ts");
    assert.equal(featureSupportsLlm(resultFeature), true);
    assert.equal(stepUsesLlm(resultFeature, {}), false);
    assert.equal(stepUsesLlm(resultFeature, { output_value: true }), false);
     const activeResult = { ...resultFeature, execution_settings: { ...resultFeature.execution_settings, llm_enabled: true } };
     for (const [config, inherited, expected] of [
       [{}, undefined, false],
       [{ output_value: true }, { output_value: true }, false],
       [{ llm_prompt: "Legacy prompt" }, undefined, false],
       [{ llm_enabled: "true" }, undefined, false],
       [{ llm_enabled: true }, undefined, true],
       [{}, { llm_enabled: true }, true],
       [{ llm_enabled: false }, { llm_enabled: true }, false],
       [{ llm_enabled: true }, { llm_enabled: false }, true],
       [{ llm_enabled: null }, { llm_enabled: true }, false],
       [{ llm_enabled: true, output_value: true }, { output_value: false }, true],
       [{ llm_enabled: true, output_value: false }, { output_value: true }, false],
       [{}, { llm_enabled: true, output_value: true }, true],
       [{}, { llm_enabled: true, output_value: false }, false],
       [{ llm_enabled: true, output_value: null }, { output_value: false }, true],
     ]) {
       assert.equal(stepUsesLlm(activeResult, config, inherited), expected, JSON.stringify({ config, inherited }));
     }
     assert.equal(stepUsesLlm({ ...activeResult, defaults: { llm_enabled: true } }, {}), false, "global defaults must not opt legacy steps in");
     assert.equal(stepUsesLlm({ ...activeResult, config_schema: { properties: { llm_enabled: { default: true } } } }, {}), false);
     const defaultFalse = { ...activeResult, defaults: { output_value: false } };
     assert.equal(stepUsesLlm(defaultFalse, { llm_enabled: true }), false);
     assert.equal(stepUsesLlm(defaultFalse, { llm_enabled: true, output_value: true }), true);
     assert.equal(stepUsesLlm({ ...defaultFalse, defaults: {}, config_schema: { properties: { output_value: { default: false } } } }, { llm_enabled: true }), false);
     const allResult = { ...activeResult, execution_settings: { ...activeResult.execution_settings, judgment_scope: "all", use_vision: false } };
     assert.equal(stepUsesLlm(allResult, { output_value: false }), false);
     assert.equal(stepUsesLlm(allResult, { llm_enabled: true, output_value: false }), true);
     assert.equal(stepUsesLlm(allResult, {}, { llm_enabled: true, output_value: false }), true);
     assert.equal(stepUsesLlm(allResult, { llm_enabled: false }, { llm_enabled: true }), false);
     assert.equal(stepUsesLlm(resultFeature, { llm_enabled: true }), false);
     assert.equal(stepUsesLlm(resultFeature, {}, { llm_enabled: true }), false);
     assert.equal(stepUsesLlm({ ...allResult, execution_settings: null }, { llm_enabled: true }), false);
     assert.equal(stepUsesLlm({ ...allResult, execution_settings: { ...allResult.execution_settings, llm_enabled: false } }, { llm_enabled: true }), false);

     const { default: StepFields } = await server.ssrLoadModule("/src/components/StepFields.vue");
     const stepProps = (config = {}, inherited = null, value = activeResult) => reactive({
       step: {
         uid: "result", step_key: "result", feature_type: "flow.result", label: "",
         group_key: "", enabled: "inherit", config, condition: "", on_error: "",
       },
       feature: value, features: [value], inherited, signals: [], cfboxEnabled: false,
       lockFeature: true, readonly: false,
     });
     const renderStep = (props) => renderToString(createSSRApp({ render: () => h("div", [
       h(StepFields, props),
       stepUsesLlm(props.feature, props.step.config, props.inherited) ? h("span", { class: "llm-badge" }, "大模型") : null,
     ]) }).use(ElementPlus).provide(ID_INJECTION_KEY, { prefix: 1, current: 0 })
       .provide(ZINDEX_INJECTION_KEY, { current: 0 }));
     const assertNoGenericControls = (html) => assert.doesNotMatch(html, /Generic LLM switch|Generic LLM prompt/);
     const legacy = stepProps();
     const legacyHtml = await renderStep(legacy);
     assert.match(legacyHtml, /role="switch"[^>]*aria-checked="false"[^>]*aria-label="开启模型判断"/);
     assert.doesNotMatch(legacyHtml, /步骤模型判断提示词|llm-badge/);
     assert.deepEqual(legacy.step.config, {}, "rendering must not opt in legacy steps");
     assertNoGenericControls(legacyHtml);

     const inherited = { llm_enabled: true, llm_prompt: "Template prompt: True/False", output_value: true };
     const props = stepProps({}, inherited);
     let fields;
     await renderToString(createSSRApp({ setup() {
       StepFields.setup(props, { expose(value) { fields = value; }, emit() {} });
       return () => null;
     } }));
     assert.equal(fields.llmEnabled.value, true);
     assert.equal(fields.llmPrompt.value, inherited.llm_prompt);
     assert.equal(fields.schema.value.properties.llm_enabled, undefined);
     assert.equal(fields.schema.value.properties.llm_prompt, undefined);
     assert.ok(props.feature.config_schema.properties.llm_enabled, "schema filtering must not mutate feature metadata");
     const inheritedHtml = await renderStep(props);
     assert.match(inheritedHtml, /role="switch"[^>]*aria-checked="true"[^>]*aria-label="开启模型判断"/);
     assert.match(inheritedHtml, /继承模板设置/);
     assert.match(inheritedHtml, /继承模板提示词/);
     // Element Plus SSR omits textarea contents, but renders the bound character count.
     assert.ok(inheritedHtml.includes(`${inherited.llm_prompt.length} / 4000`));
     assert.match(inheritedHtml, /llm-badge/);
     assertNoGenericControls(inheritedHtml);
     assert.deepEqual(props.step.config, {}, "inherited values must not be materialized as overrides");

     fields.llmEnabled.value = false;
     assert.deepEqual(props.step.config, { llm_enabled: false });
     assert.equal(fields.llmEnabled.value, false);
     const siteFalseHtml = await renderStep(props);
     assert.match(siteFalseHtml, /role="switch"[^>]*aria-checked="false"/);
     assert.match(siteFalseHtml, /恢复继承/);
     assert.doesNotMatch(siteFalseHtml, /步骤模型判断提示词|llm-badge/);
     fields.resetResultField("llm_enabled");
     assert.equal(fields.llmEnabled.value, true);
     assert.deepEqual(props.step.config, {});
     assert.equal(inherited.llm_enabled, true, "site overrides must not mutate the template");

     fields.llmPrompt.value = "Custom prompt: output True or False";
     assert.equal(props.step.config.llm_prompt, "Custom prompt: output True or False");
     assert.ok((await renderStep(props)).includes(`${props.step.config.llm_prompt.length} / 4000`));
     fields.llmPrompt.value = "";
     assert.equal(fields.llmPrompt.value, "", "explicit blank must override the inherited prompt");
     assert.equal(props.step.config.llm_prompt, "");
     const blankHtml = await renderStep(props);
     assert.match(blankHtml, /留空使用全局结果判断提示词/);
     assert.match(blankHtml, /maxlength="4000"/);
     assert.match(blankHtml, /必须明确要求以 True 或 False 作结论/);
     assert.doesNotMatch(blankHtml, /Template prompt: True\/False/);
     props.readonly = true;
     const blankReadonly = await renderStep(props);
     assert.match(blankReadonly, /使用全局提示词/);
     assert.doesNotMatch(blankReadonly, /<textarea|role="switch"/);
     assertNoGenericControls(blankReadonly);
     fields.resetResultField("llm_prompt");
     assert.equal(fields.llmPrompt.value, inherited.llm_prompt);
     const inheritedReadonly = await renderStep(props);
     assert.match(inheritedReadonly, /开启模型判断：是（继承）/);
     assert.match(inheritedReadonly, /Template prompt: True\/False（继承）/);
     props.readonly = false;

     fields.llmEnabled.value = true;
     fields.llmPrompt.value = "Keep prompt: True/False";
     const savedConfig = structuredClone({ ...props.step.config });
     props.feature = { ...activeResult, execution_settings: { ...activeResult.execution_settings, llm_enabled: false } };
     assert.equal(fields.llmEnabled.value, true, "global off must retain the effective step setting");
     const globalOffHtml = await renderStep(props);
     assert.match(globalOffHtml, /<input(?=[^>]* disabled)(?=[^>]*aria-label="开启模型判断")[^>]*>/);
     assert.match(globalOffHtml, /role="switch"[^>]*aria-checked="true"/);
     assert.match(globalOffHtml, /<textarea(?=[^>]* disabled)(?=[^>]*aria-label="步骤模型判断提示词")[^>]*>/);
     assert.match(globalOffHtml, /当前步骤配置不生效/);
     assert.match(globalOffHtml, /<button(?=[^>]* disabled)[^>]*>[\s\S]*?恢复继承/);
     assert.equal(fields.llmPrompt.value, savedConfig.llm_prompt);
     assert.doesNotMatch(globalOffHtml, /llm-badge/);
     assert.deepEqual(props.step.config, savedConfig, "global off must preserve step config");
     assert.deepEqual(inherited, { llm_enabled: true, llm_prompt: "Template prompt: True/False", output_value: true });
     props.readonly = true;
     const globalOffReadonly = await renderStep(props);
     assert.match(globalOffReadonly, /全局关闭，当前不生效/);
     assert.match(globalOffReadonly, /Keep prompt: True\/False/);
     assert.doesNotMatch(globalOffReadonly, /<textarea|role="switch"|llm-badge/);
     props.readonly = false;
     props.feature = activeResult;
     const reenabledHtml = await renderStep(props);
     assert.match(reenabledHtml, /llm-badge/);
     assert.doesNotMatch(reenabledHtml, /<textarea[^>]* disabled/);
     assert.deepEqual(props.step.config, savedConfig);
     const inactiveReadonly = stepProps({ llm_enabled: false }, inherited);
     inactiveReadonly.readonly = true;
     const inactiveReadonlyHtml = await renderStep(inactiveReadonly);
     assert.match(inactiveReadonlyHtml, /开启模型判断：否/);
     assert.doesNotMatch(inactiveReadonlyHtml, /Template prompt: True\/False|llm-badge/);
    const captchaFeature = { ...feature, requires: [] };
    assert.equal(featureSupportsLlm(captchaFeature), true);
    assert.equal(stepUsesLlm(captchaFeature, {}), false);
    assert.equal(stepUsesLlm(captchaFeature, {}, { captcha_method: "vision" }), true);
    assert.equal(stepUsesLlm(captchaFeature, { captcha_method: "ddddocr" }, { captcha_method: "vision" }), false);
    const quizFeature = { ...captchaFeature, key: "sign.image_quiz", requires: ["vision"] };
    assert.equal(featureSupportsLlm(quizFeature), true);
     assert.equal(stepUsesLlm(quizFeature, {}), true);
     assert.equal(featureSupportsLlm(gridFeature), true);
     assert.equal(stepUsesLlm(gridFeature, {}), true);
    assert.equal(featureSupportsLlm(null), false);
    assert.equal(stepUsesLlm(undefined, {}), false);
    assert.equal(featureSupportsLlm({ ...captchaFeature, config_schema: {} }), false);
    const { schemaDefaults } = await server.ssrLoadModule("/src/utils/schema.ts");
   assert.deepEqual(schemaDefaults(feature.config_schema), { captcha_method: "ddddocr" });
    for (const file of ["SchemaConfigForm", "WorkflowStepEditor"]) {
     const source = await readFile(new URL(`../src/components/${file}.vue`, import.meta.url), "utf8");
     assert.doesNotMatch(source, /prefer_llm|llm_prompt|execution_settings|FeatureSettingsEditor/);
   }
  console.log("Feature settings regression passed");
} finally {
  await server.close();
}
