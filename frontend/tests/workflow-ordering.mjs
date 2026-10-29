import assert from "node:assert/strict";
import { createServer } from "vite";
import { createSSRApp, reactive } from "vue";
import { renderToString } from "vue/server-renderer";

// Exercise the actual SFC setup with Vite's existing Vue/TypeScript pipeline.
const server = await createServer({ optimizeDeps: { noDiscovery: true, include: [] }, server: { middlewareMode: true }, plugins: [{
  name: "ordering-test-expose", enforce: "pre",
  transform(source, id) {
    if (id.endsWith("/WorkflowStepEditor.vue")) {
      return source.replace('import StepFields from "@/components/StepFields.vue";', 'const StepFields = {};')
        .replace("defineExpose({ addStep,", "defineExpose({ moveStep, groupSections, addStep,");
    }
  },
}] });
try {
  const { default: Editor } = await server.ssrLoadModule("/src/components/WorkflowStepEditor.vue");
  globalThis.document = { querySelector: () => null };
  globalThis.HTMLElement = class {};
  for (const keyNamespace of ["site", "temp"]) {
    const step = (key, position) => ({ uid: key, step_key: key, group_key: "", position });
    const props = reactive({
      modelValue: [step("z", 3000), step("a", 3000), step("m", 3000)],
      features: [{ key: "flow.open" }], chain: [], keyNamespace,
      signals: [], cfboxEnabled: false, groups: [], inheritedGroups: [], groupsLocked: false,
    });
    let editor;
    await renderToString(createSSRApp({ setup() { Editor.setup(props, {
      expose(value) { editor = value; }, emit(event, value) {
        if (event === "update:modelValue") props.modelValue = value;
      },
    }); return () => null; } }));
    const order = () => [...props.modelValue].sort((a, b) => a.position - b.position).map(s => s.step_key);
    editor.moveStep(props.modelValue[1], -1);
    assert.deepEqual(order(), ["a", "z", "m"]);
    assert.equal(new Set(props.modelValue.map(s => s.position)).size, 3);
    editor.moveStep(props.modelValue[1], 1);
    assert.deepEqual(order(), ["z", "a", "m"]);
    props.inheritedGroups = [
      { group_key: "first", name: "First", position: 1000000, step_anchor: 1000 },
      { group_key: "second", name: "Second", position: 2000000, step_anchor: 4000 },
    ];
    props.modelValue = [step("z", 2000), step("a", 2500), step("m", 3000)];
    const blocks = () => editor.groupSections.value.flatMap(section =>
      section.group.key ? [section.group.key] : section.items.map(item => item.step.step_key));
    editor.moveStep(props.modelValue[0], 1);
    assert.deepEqual(blocks(), ["first", "a", "z", "m", "second"]);
    editor.moveStep(props.modelValue[0], 1);
    editor.moveStep(props.modelValue[0], 1);
    assert.deepEqual(blocks(), ["first", "a", "m", "second", "z"]);
    editor.moveStep(props.modelValue[0], -1);
    assert.deepEqual(blocks(), ["first", "a", "m", "z", "second"]);
    editor.addStep();
    assert.equal(blocks().at(-2), "second");
    assert.ok(props.modelValue.at(-1).position > 4000);
    props.modelValue = [step("z", 3000), step("a", 3000), step("m", 3000)];
    editor.addStep();
    assert.equal(new Set(props.modelValue.map(s => s.position)).size, 4);
    props.inheritedGroups = [];
    props.chain = [{ id: 1, name: "Parent", steps: [
      { step_key: "base", feature_type: "action.wait", position: 1000, config: {} },
    ], resolved: { steps: [
      { step_key: "base", feature_type: "action.wait", position: 1000 },
    ] } }];
    props.modelValue = [step("z", 1500), step("a", 2000), step("m", 2500)];
    editor.moveStep(props.modelValue[0], -1);
    assert.deepEqual(editor.groupSections.value.flatMap(s => s.items.map(i =>
      i.kind === "group" ? i.group.key : i.step.step_key)), ["z", "base", "a", "m"]);
  }
} finally {
  await server.close();
}
