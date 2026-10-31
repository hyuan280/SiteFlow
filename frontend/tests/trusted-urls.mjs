import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import ts from "typescript";
import { createServer } from "vite";

const source = await readFile(new URL("../src/views/SitesView.vue", import.meta.url), "utf8");
const script = source.slice(source.indexOf(">") + 1, source.indexOf("</script>"));
const ast = ts.createSourceFile("SitesView.ts", script, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS);
const names = [
  "emptyForm", "trustedUrls", "importBitwardenMode", "selectedTemplateId", "lanePolicy",
  "buildPayload", "buildSiteExport", "importSitesFromFile", "openEdit", "copyFromSite",
];
// Run the real SFC functions, not a duplicate implementation of the mappings.
const functions = ast.statements.filter(node => ts.isFunctionDeclaration(node) && names.includes(node.name?.text));
assert.equal(functions.length, names.length);
const code = ts.transpileModule(functions.map(node => node.getText(ast)).join("\n"), {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext },
}).outputText;
const server = await createServer({
  optimizeDeps: { noDiscovery: true, include: [] },
  server: { middlewareMode: true, hmr: false },
});
try {
  const { parseExportFile, importText, importArray } = await server.ssrLoadModule("/src/utils/exportFile.ts");
  const form = {};
  const ref = value => ({ value });
  const requests = [];
  let detail;
  const bindings = {
    form, parseExportFile, importText, importArray,
    loadingForm: ref(false), interactionStarted: ref(false), editorOpen: ref(false),
    editingId: ref(null), editorError: ref(""), credential: ref({}), cookieInput: ref(""),
    cookieError: ref(""), chain: ref([]), formBaseline: ref(""), credentialBaseline: ref(""),
    copying: ref(false), copyOpen: ref(true),
    resetEditor: () => Object.assign(form, ui.emptyForm()),
    nextTick: async () => {}, load: async () => {}, loadChain: async () => [],
    handleUnauthorized: async () => false,
    stepToForm: value => value, signalToForm: value => value, groupToForm: value => value,
    exportStep: value => value, exportSignal: value => value,
    ElMessage: { info() {}, success() {}, error(message) { throw new Error(message); } },
    ElMessageBox: { confirm: async () => {} },
    apiRequest: async (url, options) => {
      requests.push({ url, ...options });
      if (options?.method === "POST") return { id: 42 };
      if (url === "/sites" || url === "/templates") return [];
      return detail;
    },
  };
  const ui = new Function(...Object.keys(bindings), `${code}\nreturn { ${names.join(", ")} };`)(...Object.values(bindings));
  Object.assign(form, ui.emptyForm());
  assert.deepEqual(form.trusted_urls, []);
  assert.notEqual(ui.emptyForm().trusted_urls, ui.emptyForm().trusted_urls);

  const example = String.raw`^https://openid\.13a\.com/login(?:[/?#].*)?$`;
  const patterns = [example, String.raw`(?i)^https://auth\.example\.com/(?P<path>login|totp)$`];
  assert.deepEqual(ui.trustedUrls(undefined), []);
  assert.deepEqual(ui.trustedUrls(patterns), patterns, "Python-only syntax must not be JS-validated");
  assert.notEqual(ui.trustedUrls(patterns), patterns);
  assert.deepEqual(ui.trustedUrls([" padded "]), [" padded "], "do not normalize regex text");
  for (const invalid of [null, "", example, {}, 1, false, [1], [null], [example, {}], [""], ["   "], ["a".repeat(1025)], Array(21).fill(example)]) {
    assert.throws(() => ui.trustedUrls(invalid), /trusted_urls|可信 URL/);
  }
  assert.equal(ui.trustedUrls(Array(20).fill(example)).length, 20);
  assert.equal(ui.trustedUrls(["a".repeat(1024)])[0].length, 1024);
  assert.equal([...ui.trustedUrls(["\u{1f600}".repeat(1024)])[0]].length, 1024, "count Python Unicode characters, not UTF-16 units");
  assert.deepEqual(ui.trustedUrls(["["]), ["["], "backend owns regex syntax errors");

  detail = {
    ...ui.emptyForm(), id: 7, name: "Source", base_url: "https://site.example.com",
    template_id: null, template_name: null, lane_policy: "general", tags: [], credential: {},
    trusted_urls: patterns,
  };
  await ui.openEdit(7);
  assert.deepEqual(form.trusted_urls, patterns);
  form.trusted_urls.push("extra");
  assert.deepEqual(detail.trusted_urls, patterns, "editing must not mutate fetched arrays");
  form.name = "Destination";
  form.base_url = "https://destination.example.com";
  await ui.copyFromSite(7);
  assert.equal(form.name, "Destination");
  assert.equal(form.base_url, "https://destination.example.com");
  assert.deepEqual(form.trusted_urls, patterns);
  assert.notEqual(form.trusted_urls, detail.trusted_urls);
  assert.deepEqual(ui.buildPayload().trusted_urls, patterns);

  const exported = await ui.buildSiteExport([{ id: 7 }]);
  assert.deepEqual(exported.sites[0].trusted_urls, patterns);
  requests.length = 0;
  await ui.importSitesFromFile({ text: async () => JSON.stringify(exported) });
  const imported = JSON.parse(requests.find(request => request.method === "POST").body);
  assert.deepEqual(imported.trusted_urls, patterns, "export/import preserves escapes and Python syntax");
  detail = { ...detail, ...imported };
  await ui.openEdit(42);
  assert.deepEqual(ui.buildPayload().trusted_urls, patterns, "import/edit/save roundtrip");

  delete detail.trusted_urls;
  await ui.openEdit(7);
  assert.deepEqual(form.trusted_urls, []);
  form.trusted_urls = [...patterns];
  await ui.copyFromSite(7);
  assert.deepEqual(form.trusted_urls, [], "copying older details clears stale trust");
  const legacy = await ui.buildSiteExport([{ id: 7 }]);
  assert.deepEqual(legacy.sites[0].trusted_urls, []);
  delete legacy.sites[0].trusted_urls;
  requests.length = 0;
  await ui.importSitesFromFile({ text: async () => JSON.stringify(legacy) });
  assert.deepEqual(JSON.parse(requests.find(request => request.method === "POST").body).trusted_urls, []);

  for (const invalid of [null, example, {}, [false], [example, 123], [""], ["a".repeat(1025)], Array(21).fill(example)]) {
    requests.length = 0;
    const file = { ...exported, sites: [exported.sites[0], { ...exported.sites[0], name: "Invalid", trusted_urls: invalid }] };
    await assert.rejects(ui.importSitesFromFile({ text: async () => JSON.stringify(file) }), /trusted_urls|可信 URL/);
    assert.ok(requests.every(request => !request.method), "validate every row before creating any sites");
  }
  form.trusted_urls = [123];
  assert.throws(() => ui.buildPayload(), /trusted_urls/);
  detail.trusted_urls = "not an array";
  await assert.rejects(ui.buildSiteExport([{ id: 7 }]), /trusted_urls/);

  const field = source.slice(source.indexOf("<StringListField"), source.indexOf('<FloatingField label="备注"'));
  assert.match(field, /v-model="form\.trusted_urls"/);
  assert.ok(field.includes(`<code>${example}</code>`), "static Vue text must show single regex backslashes");
  for (const text of ["TOTP", "同源", "全匹配", "HTTP(S)", "后端校验", "不安全", "20", "1024"]) {
    assert.ok(field.includes(text), text);
  }
  assert.match(source, /trustedUrls\(form\.trusted_urls\);/, "validate before save");
  console.log("Trusted URLs regression passed");
} finally {
  await server.close();
}
