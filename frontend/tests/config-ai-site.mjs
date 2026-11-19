import assert from "node:assert/strict";
import { access, readFile } from "node:fs/promises";
import ts from "typescript";
import { computed, nextTick, reactive, ref } from "vue";
import { createServer } from "vite";

const source = await readFile(new URL("../src/views/SitesView.vue", import.meta.url), "utf8");
assert.doesNotMatch(source, /ConfigAIDialog|config-ai|openAI|applyAIProposal|canUseAI|aiOpen|aiApplying|aiDraftKey|aiSite|AI 生成配置/,
  "neither the list nor the site editor exposes standalone AI generation");
for (const path of ["components/ConfigAIDialog.vue", "types/config-ai.ts", "utils/config-ai.ts"]) {
  await assert.rejects(access(new URL(`../src/${path}`, import.meta.url)), { code: "ENOENT" });
}
for (const binding of ['@click="openCreate"', '@click="openEdit(row.id)"', '@click="() => save()"',
  '@click="openCopySite"', '@change="onTemplateChange"', 'v-model="form.steps"', '@update:groups="form.groups = $event"']) {
  assert.ok(source.includes(binding), `normal editor keeps ${binding}`);
}

const script = source.slice(source.indexOf(">") + 1, source.indexOf("</script>"));
const ast = ts.createSourceFile("SitesView.ts", script, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS);
const functions = ["emptyForm", "emptyCredential", "selectedTemplateId", "trustedUrls", "groupToForm",
  "resetEditor", "openCreate", "openEdit", "loadChain", "loadTemplateChain", "changeTemplate", "onTemplateChange",
  "validateCredentialInputs", "validate", "lanePolicy", "buildPayload", "save"];
const variables = ["features", "editorOpen", "editingId", "editingRevision", "editingHistoryKey", "saving", "editorError",
  "formBaseline", "credentialBaseline", "loadingForm", "interactionStarted", "form", "chain", "credential",
  "credentialUsername", "credentialPassword", "credentialError", "cookieInput", "cookieError", "credentialTab",
  "hasCredentialInputs", "formDirty", "credentialDirty", "dirty", "chainSignals", "signalOptions", "availableSignals",
  "configGeneration", "configImportOpen", "configText", "configError", "configBusy"];
const declarations = ast.statements.filter(node =>
  ts.isFunctionDeclaration(node) && functions.includes(node.name?.text) ||
  ts.isVariableStatement(node) && node.declarationList.declarations.some(item => variables.includes(item.name.getText(ast))));
assert.equal(declarations.length, functions.length + variables.length, "execute real editor functions and state");
const code = ts.transpileModule(declarations.map(node => node.getText(ast)).join("\n"), {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext },
}).outputText;
const server = await createServer({ optimizeDeps: { noDiscovery: true, include: [] }, server: { middlewareMode: true, hmr: false } });
try {
  const workflow = await server.ssrLoadModule("/src/utils/workflow.ts");
  const schema = await server.ssrLoadModule("/src/utils/schema.ts");
  const requests = [];
  const messages = [];
  let detail;
  const bindings = {
    computed, nextTick, reactive, ref, ...workflow, ...schema,
    apiRequest: async (url, options) => {
      requests.push({ url, ...options });
      if (url === "/templates/7") return { id: 7, parent_id: null, steps: [], groups: [], signals: [], resolved_signals: [], resolved: { steps: [], groups: [] } };
      assert.ok(["/sites", "/sites/42"].includes(url), "editor only calls normal site/template endpoints");
      if (!options) return detail;
      return { id: 42, config_revision: (detail?.config_revision ?? 0) + 1, history_key: "site-42", credential: {} };
    },
    load: async () => {},
    handleUnauthorized: async () => false,
    saveCredentialFor: async () => assert.fail("no credentials entered"),
    saveCookiesFor: async () => assert.fail("no cookies entered"),
    ElMessage: { success: message => messages.push(message) },
  };
  const ui = new Function(...Object.keys(bindings), `${code}\nreturn { ${[...functions, ...variables].join(", ")} };`)(...Object.values(bindings));
  ui.features.value = [{ key: "action.wait", config_schema: { type: "object", properties: {} } }];
  ui.openCreate();
  await nextTick();
  assert.equal(ui.editorOpen.value, true);
  assert.equal(ui.loadingForm.value, false);
  assert.equal(ui.dirty.value, false);
  assert.deepEqual(requests, [], "manual creation opens without a generation request");
  await ui.save();
  assert.ok(ui.editorError.value, "normal form validation is still enforced");
  assert.deepEqual(requests, []);
  ui.form.name = "  Manual site  ";
  ui.form.base_url = "  https://example.com/  ";
  ui.form.steps = [workflow.stepToForm({ step_key: "wait", feature_type: "action.wait", config: { seconds: 2 }, position: 1000 })];
  ui.interactionStarted.value = true;
  assert.equal(ui.dirty.value, true);
  await ui.save();
  assert.equal(ui.editorError.value, "");
  assert.equal(requests.length, 1);
  assert.equal(requests[0].method, "POST");
  const created = JSON.parse(requests[0].body);
  assert.equal(created.name, "Manual site");
  assert.equal(created.template_id, null);
  assert.equal(created.steps[0].config.seconds, 2);
  assert.equal(ui.editorOpen.value, false);

  detail = { ...created, id: 42, config_revision: 3, history_key: "site-42", credential: ui.emptyCredential(), template_id: 7 };
  await ui.openEdit(42);
  assert.equal(ui.editorError.value, "");
  assert.equal(ui.chain.value[0].id, 7, "existing site still loads its template chain");
  assert.equal(ui.dirty.value, false);
  ui.form.steps[0].config.seconds = 5;
  ui.form.notes = "Manual edit";
  ui.interactionStarted.value = true;
  assert.equal(ui.dirty.value, true);
  await ui.save(false);
  assert.equal(ui.editorError.value, "");
  assert.equal(requests.at(-1).method, "PUT");
  const updated = JSON.parse(requests.at(-1).body);
  assert.equal(updated.expected_revision, 3);
  assert.equal(updated.expected_history_key, "site-42");
  assert.equal(updated.template_id, 7);
  assert.equal(updated.steps[0].config.seconds, 5);
  assert.equal(updated.notes, "Manual edit");
  assert.equal(ui.editorOpen.value, true);
  assert.equal(ui.dirty.value, false);

  ui.openCreate();
  await nextTick();
  ui.form.name = "Template site";
  ui.form.base_url = "https://example.com/";
  ui.onTemplateChange({ target: { value: "7" } });
  await nextTick();
  await nextTick();
  assert.equal(ui.chain.value[0].id, 7);
  await ui.save();
  assert.equal(ui.editorError.value, "");
  assert.equal(requests.at(-1).method, "POST");
  assert.equal(JSON.parse(requests.at(-1).body).template_id, 7);
  assert.deepEqual(JSON.parse(requests.at(-1).body).steps, [], "template-only sites need no local steps");
  assert.equal(messages.length, 3);
  console.log("Site editor without standalone AI generation regression passed");
} finally {
  await server.close();
}
