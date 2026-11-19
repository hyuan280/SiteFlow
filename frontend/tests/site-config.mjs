import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import ts from "typescript";
import { createServer } from "vite";
import { computed, nextTick, reactive, ref } from "vue";

const source = await readFile(new URL("../src/views/SitesView.vue", import.meta.url), "utf8");
const script = source.slice(source.indexOf(">") + 1, source.indexOf("</script>"));
const ast = ts.createSourceFile("SitesView.ts", script, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS);
const functions = ["emptyForm", "emptyCredential", "groupToForm", "selectedTemplateId", "trustedUrls",
  "resetEditor", "loadChain", "loadTemplateChain", "validateCredentialInputs", "validate", "lanePolicy", "buildPayload",
  "openConfigImport", "readConfigFile", "exportFullConfig", "importConfig", "save"];
const variables = ["features", "templates", "editorOpen", "editingId", "editingRevision", "editingHistoryKey",
  "saving", "editorError", "formBaseline", "credentialBaseline", "loadingForm", "interactionStarted",
  "form", "chain", "credential", "credentialUsername", "credentialPassword", "credentialError",
  "cookieInput", "cookieError", "credentialTab", "hasCredentialInputs", "chainSignals", "signalOptions",
  "availableSignals", "configImportOpen", "configText", "configError", "configBusy", "configGeneration"];
const declarations = ast.statements.filter(node =>
  ts.isFunctionDeclaration(node) && functions.includes(node.name?.text) ||
  ts.isVariableStatement(node) && node.declarationList.declarations.some(item => variables.includes(item.name.getText(ast))));
assert.equal(declarations.length, functions.length + variables.length, "extract real functions and state, not test copies");
const code = ts.transpileModule(declarations.map(node => node.getText(ast)).join("\n"), {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext },
}).outputText;
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};
const tick = async () => { await Promise.resolve(); await nextTick(); };
const clone = value => JSON.parse(JSON.stringify(value));
const server = await createServer({ optimizeDeps: { noDiscovery: true, include: [] },
  server: { middlewareMode: true, hmr: false } });
try {
  const workflow = await server.ssrLoadModule("/src/utils/workflow.ts");
  const schema = await server.ssrLoadModule("/src/utils/schema.ts");
  const exportFile = await server.ssrLoadModule("/src/utils/exportFile.ts");
  const { ApiError } = await server.ssrLoadModule("/src/api/client.ts");
  const config = () => ({
    name: "Imported site", base_url: "https://import.example/", template_id: null, enabled: false,
    lane_policy: "auto", support_resign: false, resign_max_days: 8, bitwarden_enabled: true,
    credential_provider_id: "builtin.bitwarden", bitwarden_match_mode: "custom",
    bitwarden_custom_match: "vault.example", checkin_after: "06:45", timezone: "UTC",
    tags: ["one", "two"], trusted_urls: ["^https://trusted\\.example/"], notes: "Validated notes",
    steps: [
      { step_key: "inherit", feature_type: "action.wait", label: null, group_key: null, position: null,
        enabled: null, config: { seconds: null }, condition: null, on_error: null, timeout_seconds: null },
      { step_key: "disabled", feature_type: "action.wait", label: "Disabled", group_key: "anchored", position: 2000,
        enabled: false, config: { seconds: 2 }, condition: "always", on_error: "continue", timeout_seconds: 9 },
    ],
    groups: [{ group_key: "floating", name: "Floating", position: 1000, step_anchor: null },
      { group_key: "anchored", name: "Anchored", position: 2000, step_anchor: 1500 }],
    signals: [{ signal_key: "done", label: "Done", description: "Keep description", include_texts: ["ok"],
      include_selectors: [".done"], exclude_texts: ["failed"], exclude_selectors: [".error"], enabled: false, match_mode: "all" }],
  });
  function harness() {
    const requests = [], confirmations = [], notices = [], downloads = [], blobs = [], revoked = [];
    let loads = 0;
    const bindings = {
      computed, nextTick, reactive, ref, ...workflow, ...schema, ...exportFile, Blob,
      apiRequest(url, options) {
        const pending = deferred();
        requests.push({ url, ...options, ...pending });
        return pending.promise;
      },
      handleUnauthorized: async () => false,
      load: async () => { loads += 1; },
      saveCredentialFor: async () => assert.fail("config operations must not save credentials"),
      saveCookiesFor: async () => assert.fail("config operations must not save cookies"),
      ElMessageBox: { confirm(...args) {
        const pending = deferred(); confirmations.push({ ...pending, args }); return pending.promise;
      } },
      ElMessage: Object.fromEntries(["success", "error", "warning"].map(kind => [kind, text => notices.push({ kind, text })])),
      URL: { createObjectURL(blob) { blobs.push(blob); return `blob:config-${blobs.length}`; },
        revokeObjectURL(url) { revoked.push(url); } },
      document: { createElement(tag) {
        assert.equal(tag, "a");
        return { click() { downloads.push({ href: this.href, download: this.download }); } };
      } },
    };
    const ui = new Function(...Object.keys(bindings), `${code}\nreturn { ${[...functions, ...variables.filter(name => name !== "configGeneration")].join(", ")} };`)(...Object.values(bindings));
    ui.editorOpen.value = true;
    ui.editingId.value = 42;
    ui.editingRevision.value = 7;
    ui.editingHistoryKey.value = "destination-key";
    ui.features.value = [{ key: "action.wait", config_schema: { type: "object", properties: {} } }];
    Object.assign(ui.form, { name: "Destination draft", base_url: "https://draft.example/", template_id: 3,
      steps: [workflow.stepToForm({ ...config().steps[1], step_key: "old-step" })],
      groups: [ui.groupToForm({ group_key: "old-group", name: "Old group", position: 500, step_anchor: 100 })],
      signals: [workflow.signalToForm({ ...config().signals[0], signal_key: "old-signal" })] });
    ui.chain.value = [{ id: 3, name: "Old template", steps: [], resolved_signals: [] }];
    ui.formBaseline.value = JSON.stringify(ui.form);
    return { ui, requests, confirmations, notices, downloads, blobs, revoked, loads: () => loads };
  }
  function startImport(h, raw = config()) {
    h.ui.openConfigImport();
    h.ui.configText.value = typeof raw === "string" ? raw : JSON.stringify(raw);
    return h.ui.importConfig();
  }
  async function acceptImport(h, local = config(), raw = local) {
    const importing = startImport(h, raw);
    h.requests.at(-1).resolve({ local });
    await tick();
    assert.equal(h.confirmations.length, 1);
    h.confirmations.at(-1).resolve();
    await importing;
  }
  const saved = h => ({ id: h.ui.editingId.value ?? 99, config_revision: 8,
    history_key: "saved-key", credential: h.ui.emptyCredential() });

  await test("full export posts the current draft and downloads only the standalone response, without saving", async () => {
    for (const id of [42, null]) {
      const h = harness(), { ui } = h;
      ui.editingId.value = id;
      ui.form.name = "  Unsaved draft  ";
      ui.form.notes = "Latest unsaved change";
      ui.form.steps[0].config.seconds = 123;
      ui.credentialUsername.value = "private user";
      ui.credentialPassword.value = "private password";
      ui.cookieInput.value = "private cookie";
      const before = JSON.stringify(ui.form);
      const exporting = ui.exportFullConfig();
      const request = h.requests[0];
      assert.equal(request.url, id === null ? "/sites/config/export" : "/sites/42/config/export");
      assert.equal(request.method, "POST");
      assert.deepEqual(JSON.parse(request.body), ui.buildPayload());
      assert.equal(JSON.parse(request.body).name, "Unsaved draft");
      assert.equal(JSON.parse(request.body).steps[0].config.seconds, 123);
      await ui.exportFullConfig(); await ui.save(); await ui.importConfig();
      assert.equal(h.requests.length, 1, "busy operations cannot start a second request");
      const standalone = { ...config(), name: "Flat/site:name", template_id: null };
      request.resolve(standalone); await exporting;
      assert.equal(h.downloads.length, 1);
      assert.deepEqual(h.downloads[0], { href: "blob:config-1", download: "siteflow-site-Flat_site_name-full.json" });
      assert.equal(h.blobs[0].type, "application/json;charset=utf-8");
      assert.deepEqual(JSON.parse(await h.blobs[0].text()), standalone, "download is the resolved config, not a site-list wrapper");
      assert.deepEqual(h.revoked, ["blob:config-1"]);
      assert.equal(JSON.stringify(ui.form), before);
      assert.equal(ui.credentialPassword.value, "private password");
      assert.equal(ui.cookieInput.value, "private cookie");
      assert.equal(h.loads(), 0, "export never persists or reloads the editor");
      assert.equal(ui.configBusy.value, false);
    }
  });

  await test("plain SiteWrite import uses destination CAS and validated local state, replacing all collections", async () => {
    const h = harness(), { ui } = h;
    ui.credential.value = { ...ui.emptyCredential(), configured: true, cookies_configured: true, cookie_count: 2 };
    ui.credentialUsername.value = "Keep user"; ui.credentialPassword.value = "Keep password";
    ui.cookieInput.value = "Keep cookie"; ui.credentialTab.value = "cookie";
    const secrets = JSON.stringify([ui.credential.value, ui.credentialUsername.value, ui.credentialPassword.value,
      ui.cookieInput.value, ui.credentialTab.value]);
    const baseline = ui.formBaseline.value;
    const raw = { ...config(), name: "Raw name", expected_revision: 999, expected_history_key: "source-key" };
    const importing = startImport(h, raw);
    assert.equal(h.requests[0].url, "/sites/42/validate?replace_local=true");
    assert.equal(h.requests[0].method, "POST");
    assert.deepEqual(JSON.parse(h.requests[0].body), { ...raw, expected_revision: 7, expected_history_key: "destination-key" });
    assert.equal(ui.form.name, "Destination draft");
    h.requests[0].resolve({ local: config() }); await tick();
    assert.match(h.confirmations[0].args[0], /完整替换/);
    assert.match(h.confirmations[0].args[0], /Cookie/);
    assert.equal(ui.form.name, "Destination draft", "validation alone does not apply the candidate");
    h.confirmations[0].resolve(); await importing;
    assert.equal(ui.form.name, "Imported site", "use validated.local rather than the unvalidated file");
    assert.equal(ui.form.template_id, undefined);
    assert.deepEqual(ui.chain.value, [], "explicit template_id null clears the old template chain");
    assert.deepEqual(ui.form.steps.map(step => step.step_key), ["inherit", "disabled"]);
    assert.deepEqual(ui.form.groups.map(group => group.group_key), ["floating", "anchored"]);
    assert.deepEqual(ui.form.signals.map(signal => signal.signal_key), ["done"]);
    assert.deepEqual(ui.form.steps.map(step => step.enabled), ["inherit", "off"]);
    assert.deepEqual(ui.form.groups.map(group => group.step_anchor), [null, 1500]);
    assert.equal(ui.form.importedConfig, true);
    assert.equal(ui.interactionStarted.value, true);
    assert.equal(ui.configImportOpen.value, false);
    assert.equal(ui.formBaseline.value, baseline, "import leaves the editor dirty until explicit save");
    assert.deepEqual(ui.buildPayload(), { ...config(), expected_revision: 7, expected_history_key: "destination-key" },
      "all settings, auto lane, null overrides, disabled entries and anchors survive the real utils roundtrip");
    assert.equal(JSON.stringify([ui.credential.value, ui.credentialUsername.value, ui.credentialPassword.value,
      ui.cookieInput.value, ui.credentialTab.value]), secrets);
    assert.equal(h.requests.length, 1, "only validation, never an automatic config/credential/cookie write");
    assert.equal(h.loads(), 0);
    assert.deepEqual([ui.editingRevision.value, ui.editingHistoryKey.value], [7, "destination-key"]);
  });

  await test("plain template_name config and legacy single-site wrapper resolve template names and load the real chain", async () => {
    for (const wrapped of [false, true]) {
      const h = harness(), { ui } = h;
      ui.templates.value = [{ id: 5, name: "Named template" }];
      const raw = { ...config(), template_name: "Named template" };
      delete raw.template_id;
      const importing = startImport(h, wrapped ? { kind: "sites", version: 1, sites: [raw] } : raw);
      const body = JSON.parse(h.requests[0].body);
      assert.equal(body.template_id, 5);
      assert.ok(!("template_name" in body));
      h.requests[0].resolve({ local: { ...config(), template_id: 5 } }); await tick();
      assert.equal(h.requests.at(-1).url, "/templates/5");
      h.requests.at(-1).resolve({ id: 5, parent_id: 2 }); await tick();
      assert.equal(h.requests.at(-1).url, "/templates/2");
      h.requests.at(-1).resolve({ id: 2, parent_id: null }); await tick();
      h.confirmations[0].resolve(); await importing;
      assert.deepEqual(ui.chain.value.map(item => item.id), [2, 5]);
      assert.equal(ui.buildPayload().template_id, 5);
      assert.deepEqual(h.requests.map(item => item.method), ["POST", undefined, undefined]);
    }
    const h = harness();
    await acceptImport(h, config(), { ...config(), template_name: null });
    assert.equal(h.ui.buildPayload().template_id, null);
  });

  await test("cancel preserves the draft; empty groups/signals replace rather than merge", async () => {
    const h = harness(), before = JSON.stringify(h.ui.form);
    const importing = startImport(h);
    h.requests[0].resolve({ local: config() }); await tick();
    h.confirmations[0].reject("cancel"); await importing;
    assert.equal(JSON.stringify(h.ui.form), before);
    assert.equal(h.ui.configImportOpen.value, true);
    assert.equal(h.ui.configError.value, "");
    assert.equal(h.notices.length, 0);
    const empty = harness();
    await acceptImport(empty, { ...config(), steps: [], groups: [], signals: [] });
    for (const key of ["steps", "groups", "signals"]) assert.deepEqual(empty.ui.form[key], []);
    const exporting = empty.ui.exportFullConfig();
    assert.equal(empty.requests.at(-1).url, "/sites/42/config/export?replace_local=true");
    empty.requests.at(-1).resolve({ ...config(), steps: [], groups: [], signals: [] });
    await exporting;
    const saving = empty.ui.save(false);
    assert.equal(empty.requests.at(-1).url, "/sites/42/config/import", "empty replacement can be saved to clear all local collections");
    empty.requests.at(-1).resolve(saved(empty));
    await saving;
  });

  await test("empty-string overrides survive import and stale template requests cannot replace the imported chain", async () => {
    const h = harness(), { ui } = h;
    const oldLoad = ui.loadTemplateChain();
    const oldRequest = h.requests.at(-1);
    const local = config();
    local.steps[0].condition = "";
    local.steps[0].label = "";
    await acceptImport(h, local);
    oldRequest.resolve({ id: 3, parent_id: null, resolved_signals: [{ signal_key: "wrong" }] });
    await oldLoad;
    assert.deepEqual(ui.chain.value, []);
    assert.equal(ui.buildPayload().steps[0].condition, "");
    assert.equal(ui.buildPayload().steps[0].label, "");
    assert.equal(ui.buildPayload().steps[1].condition, "always");
  });

  await test("explicit save selects imported POST, ordinary PUT and new-site POST with destination CAS", async () => {
    for (const mode of ["imported", "ordinary", "new-imported", "new-ordinary"]) {
      const h = harness(), { ui } = h;
      const isNew = mode.startsWith("new-");
      if (isNew) { ui.editingId.value = null; ui.editingRevision.value = null; ui.editingHistoryKey.value = null; }
      if (mode.endsWith("imported")) {
        await acceptImport(h, config(), { ...config(), expected_revision: 999, expected_history_key: "source-key" });
        const validation = JSON.parse(h.requests[0].body);
        if (isNew) {
          assert.equal(h.requests[0].url, "/sites/validate?replace_local=true");
          assert.ok(!("expected_revision" in validation));
          assert.ok(!("expected_history_key" in validation));
        }
      }
      const expectedBody = ui.buildPayload();
      const saving = ui.save(false);
      const request = h.requests.at(-1);
      assert.equal(ui.editorError.value, "", `${mode} passes real form validation`);
      assert.equal(request.url, isNew ? "/sites" : mode === "imported" ? "/sites/42/config/import" : "/sites/42");
      assert.equal(request.method, isNew || mode === "imported" ? "POST" : "PUT");
      assert.deepEqual(JSON.parse(request.body), expectedBody);
      if (!isNew) assert.deepEqual([expectedBody.expected_revision, expectedBody.expected_history_key], [7, "destination-key"]);
      request.resolve(saved(h)); await saving;
      assert.equal(ui.editorError.value, "");
      assert.deepEqual([ui.editingRevision.value, ui.editingHistoryKey.value], [8, "saved-key"]);
      assert.equal(ui.editorOpen.value, true);
      assert.equal(ui.formBaseline.value, JSON.stringify(ui.form));
      assert.equal(h.loads(), 1);
    }
  });

  await test("invalid inputs and validation 422/409 leave the entire existing draft intact", async () => {
    const invalid = ["{", "null", "[]", ...["steps", "groups", "signals"].map(key => ({ ...config(), [key]: undefined })),
      { ...config(), groups: {} }, { ...config(), template_name: "Missing template" },
      { ...config(), template_name: 5 }, { kind: "sites", sites: [config(), config()] }];
    for (const raw of invalid) {
      const h = harness(), before = JSON.stringify(h.ui.form);
      await startImport(h, raw);
      assert.equal(h.requests.length, 0);
      assert.equal(h.confirmations.length, 0);
      assert.equal(JSON.stringify(h.ui.form), before);
      assert.ok(h.ui.configError.value);
      assert.equal(h.ui.configBusy.value, false);
    }
    for (const status of [422, 409]) {
      const h = harness(), before = JSON.stringify(h.ui.form);
      const importing = startImport(h);
      h.requests[0].reject(new ApiError(status, `Validation ${status}`)); await importing;
      assert.equal(JSON.stringify(h.ui.form), before);
      assert.equal(h.ui.configError.value, `Validation ${status}`);
      assert.equal(h.confirmations.length, 0);
      assert.equal(h.ui.configImportOpen.value, true);
    }
    const h = harness();
    await acceptImport(h);
    const before = JSON.stringify(h.ui.form), baseline = h.ui.formBaseline.value;
    const saving = h.ui.save();
    h.requests.at(-1).reject(new ApiError(409, "Destination changed")); await saving;
    assert.equal(JSON.stringify(h.ui.form), before);
    assert.equal(h.ui.formBaseline.value, baseline);
    assert.equal(h.ui.editorError.value, "Destination changed");
    assert.equal(h.ui.form.importedConfig, true);
    assert.equal(h.ui.editorOpen.value, true);
    assert.deepEqual([h.ui.editingRevision.value, h.ui.editingHistoryKey.value], [7, "destination-key"]);
    assert.equal(h.loads(), 0);
  });

  await test("late validation, template loading and confirmation cannot overwrite edits, closed or reset editors", async () => {
    for (const phase of ["validation", "chain", "confirmation"]) {
      for (const action of ["edit", "close-editor", "close-import", "reset", "reopen"]) {
        const h = harness(), { ui } = h;
        const importing = startImport(h);
        let pending = h.requests[0];
        if (phase !== "validation") {
          pending.resolve({ local: { ...config(), template_id: phase === "chain" ? 5 : null } }); await tick();
          pending = phase === "chain" ? h.requests.at(-1) : h.confirmations[0];
        }
        if (action === "edit") ui.form.notes = "Typed while waiting";
        if (action === "close-editor") ui.editorOpen.value = false;
        if (action === "close-import") ui.configImportOpen.value = false;
        if (action === "reset") { ui.resetEditor(); ui.form.name = "Different editor"; }
        if (action === "reopen") { ui.openConfigImport(); ui.configText.value = "New file"; }
        const before = JSON.stringify(ui.form);
        pending.resolve(phase === "validation" ? { local: config() } : phase === "chain" ? { id: 5, parent_id: null } : undefined);
        await importing;
        assert.equal(JSON.stringify(ui.form), before, `${phase}/${action} preserves the latest draft`);
        assert.equal(h.notices.length, 0, `${phase}/${action} must not report an applied import`);
        assert.equal(ui.configBusy.value, false);
        if (action === "edit") assert.match(ui.configError.value, /修改/);
        else assert.equal(ui.configError.value, "");
      }
    }
    for (const reject of [false, true]) {
      const h = harness(), importing = startImport(h);
      h.ui.resetEditor(); h.ui.openConfigImport();
      if (reject) h.requests[0].reject(new ApiError(409, "Stale conflict"));
      else h.requests[0].resolve({ local: config() });
      await importing;
      assert.equal(h.ui.configError.value, "", "old errors do not leak into a reopened import");
      assert.equal(h.confirmations.length, 0);
    }
  });

  await test("file reads clear the input and ignore stale completion after typing, closing, reset or reopening", async () => {
    for (const action of ["read", "type", "close", "reset", "reopen", "error", "stale-error"]) {
      const h = harness(), { ui } = h, file = deferred();
      ui.configText.value = "old"; ui.configError.value = "old error";
      ui.openConfigImport();
      assert.equal(ui.configText.value, ""); assert.equal(ui.configError.value, "");
      assert.equal(ui.configImportOpen.value, true);
      const input = { value: "chosen.json", files: [{ text: () => file.promise }] };
      const reading = ui.readConfigFile({ target: input });
      assert.equal(input.value, "");
      if (action === "type") ui.configText.value = "User typed JSON";
      if (action === "close") ui.configImportOpen.value = false;
      if (action === "reset" || action === "stale-error") ui.resetEditor();
      if (action === "reopen") { ui.openConfigImport(); ui.configText.value = "New text"; }
      const before = ui.configText.value;
      if (action.endsWith("error")) file.reject(new Error("Read failed"));
      else file.resolve(JSON.stringify(config()));
      await reading;
      assert.equal(ui.configText.value, action === "read" ? JSON.stringify(config()) : before);
      assert.equal(ui.configError.value, action === "error" ? "Read failed" : "");
      assert.equal(h.requests.length, 0);
    }
    const h = harness(), input = { value: "cancelled", files: [] };
    await h.ui.readConfigFile({ target: input });
    assert.equal(input.value, "");
  });

  await test("late export success/error after close or reset cannot download or overwrite the new editor error", async () => {
    for (const action of ["close", "reset"]) for (const fails of [false, true]) {
      const h = harness(), exporting = h.ui.exportFullConfig();
      if (action === "close") h.ui.editorOpen.value = false;
      else h.ui.resetEditor();
      h.ui.editorError.value = "New editor error";
      if (fails) h.requests[0].reject(new Error("Old export failed"));
      else h.requests[0].resolve(config());
      await exporting;
      assert.equal(h.downloads.length, 0);
      assert.equal(h.notices.length, 0);
      assert.equal(h.ui.editorError.value, "New editor error");
      assert.equal(h.ui.configBusy.value, false);
    }
  });
} finally {
  await server.close();
}
