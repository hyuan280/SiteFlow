import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import ts from "typescript";
import { createServer } from "vite";
import { nextTick, reactive, ref, watch } from "vue";

const source = await readFile(new URL("../src/views/TemplatesView.vue", import.meta.url), "utf8");
const script = source.slice(source.indexOf(">") + 1, source.indexOf("</script>"));
const ast = ts.createSourceFile("Templates.ts", script, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS);
const names = ["openSiteImport", "importFromSite", "resetEditor", "emptyForm", "groupToForm", "buildPayload", "save"];
const functions = ast.statements.filter(node => ts.isFunctionDeclaration(node) && names.includes(node.name?.text));
assert.equal(functions.length, names.length);
const stateNames = ["siteImportOpen", "siteImportLoading", "siteOptions", "preserveImportedLocal",
  "editorGeneration", "siteImportGeneration", "disposed"];
const declarations = ast.statements.filter(node => ts.isVariableStatement(node) &&
  node.declarationList.declarations.some(item => stateNames.includes(item.name.getText(ast))));
const lifecycle = ast.statements.filter(node => ts.isExpressionStatement(node) &&
  /^(watch\(editorOpen|onBeforeUnmount\()/.test(node.getText(ast)));
const code = ts.transpileModule([...declarations, ...lifecycle, ...functions].map(node => node.getText(ast)).join("\n"), {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext },
}).outputText;
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};
const tick = async () => { await Promise.resolve(); await nextTick(); };
const server = await createServer({ optimizeDeps: { noDiscovery: true, include: [] },
  server: { middlewareMode: true, hmr: false } });
try {
  const utils = await server.ssrLoadModule("/src/utils/workflow.ts");
  const requests = [], confirmations = [], notices = [];
  let unmount;
  const bindings = {
    ref, watch, onBeforeUnmount: callback => { unmount = callback; },
    form: reactive({ name: "Keep name", description: "Keep description", enabled: false,
      parent_id: undefined, steps: [], groups: [], signals: [] }),
    editingId: ref(12), editingRevision: ref(7), editingHistoryKey: ref("destination-key"),
    editorOpen: ref(true), loadingForm: ref(false), saving: ref(false), editorError: ref(""),
    formBaseline: ref(""), interactionStarted: ref(false), groupsLocked: ref(true), chain: ref([]),
    ...utils, nextTick,
    selectedParentId: () => bindings.form.parent_id ?? null,
    validate() {}, load: async () => {}, handleUnauthorized: async () => false,
    apiRequest(url, options) { const pending = deferred(); requests.push({ url, options, ...pending }); return pending.promise; },
    loadChain(id) { const pending = deferred(); requests.push({ url: `chain:${id}`, ...pending }); return pending.promise; },
    ElMessageBox: { confirm() { const pending = deferred(); confirmations.push(pending); return pending.promise; } },
    ElMessage: Object.fromEntries(["success", "error", "warning"].map(kind => [kind, text => notices.push({ kind, text })])),
  };
  const ui = new Function(...Object.keys(bindings), `${code}\nreturn { ${[...names, ...stateNames].join(", ")} };`)(...Object.values(bindings));
  const candidate = { parent_id: 3, steps: [{ step_key: "open", feature_type: "flow.open", label: null,
    group_key: null, position: null, enabled: null, config: { wait_until: null }, condition: null,
    on_error: null, timeout_seconds: null }],
    groups: [{ group_key: "local", name: "Local", position: 700, step_anchor: null }],
    signals: [{ signal_key: "done", label: "Done", description: "", include_texts: ["ok"],
      include_selectors: [], exclude_texts: [], exclude_selectors: [], enabled: true, match_mode: "any" }] };

  const opening = ui.openSiteImport();
  assert.equal(requests.at(-1).url, "/sites?view=options");
  requests.at(-1).resolve([{ id: 4, name: "Saved site", template_name: "Base" }]);
  await opening;
  assert.equal(ui.siteOptions.value[0].id, 4);
  const importing = ui.importFromSite(4);
  assert.equal(requests.length, 1, "confirmation precedes preview fetch");
  confirmations.at(-1).resolve(); await tick();
  assert.equal(requests.at(-1).url, "/templates/import-from-site/4");
  requests.at(-1).resolve(candidate); await tick();
  assert.equal(requests.at(-1).url, "chain:3");
  requests.at(-1).resolve([{ id: 3 }]); await importing;
  assert.deepEqual([bindings.form.name, bindings.form.description, bindings.form.enabled],
    ["Keep name", "Keep description", false], "metadata is never copied from the site");
  assert.equal(bindings.editingRevision.value, 7);
  assert.equal(bindings.editingHistoryKey.value, "destination-key");
  assert.equal(bindings.groupsLocked.value, true, "import does not unlock used-template restrictions");
  assert.deepEqual(bindings.form.steps[0].config, { wait_until: null });
  assert.equal(bindings.form.steps[0].enabled, "inherit");
  assert.equal(bindings.form.groups[0].step_anchor, null);
  assert.equal(bindings.interactionStarted.value, true);
  assert.ok(requests.every(item => !item.options?.method), "import never writes automatically");
  const payload = ui.buildPayload();
  assert.equal(payload.steps[0].enabled, null);
  assert.equal(payload.steps[0].position, null);
  assert.equal(payload.groups[0].step_anchor, null);
  const saving = ui.save();
  assert.equal(requests.at(-1).url, "/templates/12?preserve_local=true");
  assert.equal(requests.at(-1).options.method, "PUT");
  assert.equal(JSON.parse(requests.at(-1).options.body).expected_revision, 7);
  requests.at(-1).resolve({}); await saving;

  bindings.editorOpen.value = true;
  ui.siteImportOpen.value = true;
  const late = ui.importFromSite(4);
  confirmations.at(-1).resolve(); await tick();
  const oldRequest = requests.at(-1);
  ui.resetEditor();
  bindings.form.name = "New editor";
  oldRequest.resolve({ ...candidate, parent_id: null }); await late;
  assert.equal(bindings.form.name, "New editor");
  assert.equal(bindings.form.steps.length, 0, "old editor response cannot fill a new form");

  ui.siteImportOpen.value = true;
  const modified = ui.importFromSite(4);
  confirmations.at(-1).resolve(); await tick();
  bindings.form.description = "Typed while loading";
  requests.at(-1).resolve({ ...candidate, parent_id: null }); await modified;
  assert.equal(bindings.form.steps.length, 0, "late import cannot overwrite intervening edits");
  assert.equal(notices.at(-1).kind, "warning");

  const older = ui.importFromSite(4);
  confirmations.at(-1).resolve(); await tick();
  const olderRequest = requests.at(-1);
  const newer = ui.importFromSite(5);
  confirmations.at(-1).resolve(); await tick();
  requests.at(-1).resolve({ ...candidate, parent_id: null }); await newer;
  olderRequest.resolve({ ...candidate, parent_id: null, steps: [] }); await older;
  assert.equal(bindings.form.steps.length, 1, "latest source selection wins");

  ui.siteImportOpen.value = true;
  const closed = ui.importFromSite(4);
  confirmations.at(-1).resolve(); await tick();
  bindings.editorOpen.value = false;
  requests.at(-1).resolve({ ...candidate, parent_id: null, steps: [] }); await closed;
  assert.equal(bindings.form.steps.length, 1, "closed editor ignores pending imports");

  bindings.editorOpen.value = true;
  const unmounted = ui.openSiteImport();
  unmount(); requests.at(-1).reject(new Error("Late private error")); await unmounted;
  assert.notEqual(notices.at(-1).text, "Late private error");
  assert.match(source, /:disabled="loadingForm \|\| saving \|\| siteImportLoading"/);
  console.log("Template site import regression passed");
} finally {
  await server.close();
}
