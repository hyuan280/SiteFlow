import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import ts from "typescript";
import { createServer } from "vite";
import { createRenderer, reactive, nextTick, ref, ssrContextKey } from "vue";

// Vite SSR loads the real SFC; a minimal renderer keeps its watchers/lifecycle live without a DOM.
const source = await readFile(new URL("../src/components/WorkflowHistoryDialog.vue", import.meta.url), "utf8");
const exposed = "loadVersions, changePage, viewSnapshot, restore, deleteVersion, canDelete, deletingRevision, versions, snapshot, selectedRevision, page, loading, snapshotLoading, restoring, error, snapshotError, restoreError, acceptCurrentDependencies, canRestore";
const server = await createServer({
  optimizeDeps: { noDiscovery: true, include: [] },
  server: { middlewareMode: true, hmr: false },
  plugins: [{
    name: "workflow-history-test", enforce: "pre",
    transform(code, id) {
      if (!id.endsWith("/WorkflowHistoryDialog.vue")) return;
      return code
        .replace('import { ElMessage, ElMessageBox } from "element-plus";', 'const { ElMessage, ElMessageBox } = globalThis.__historyMocks;')
        .replace('import { useRouter } from "vue-router";', 'const { useRouter } = globalThis.__historyMocks;')
        .replace('import { ApiError, apiRequest } from "@/api/client";', 'const { ApiError, apiRequest } = globalThis.__historyMocks;')
        .replace('import { useAuthStore } from "@/stores/auth";', 'const { useAuthStore } = globalThis.__historyMocks;')
        .replace("</script>", `defineExpose({ ${exposed} });\n</script>`);
    },
  }],
});
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};
const tick = async () => { await Promise.resolve(); await nextTick(); };
const requests = [];
const confirmations = [];
const events = [];
let cleared = 0;
const routes = [];
class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}
globalThis.__historyMocks = {
  ApiError,
  apiRequest(url, options) {
    const pending = deferred();
    requests.push({ url, ...options, ...pending });
    return pending.promise;
  },
  ElMessage: { success() {} },
  ElMessageBox: { confirm(message, title, options) {
    const pending = deferred();
    confirmations.push({ message, title, options, ...pending });
    return pending.promise;
  } },
  useAuthStore: () => ({ clear() { cleared++; } }),
  useRouter: () => ({ replace: async route => { routes.push(route); } }),
};
const summary = (revision, operation = "update") => ({
  revision, operation, created_at: "2026-10-31T01:00:00Z", created_by: "admin",
  restored_from_revision: operation === "restore" ? 1 : null, content_hash: "abc", schema_version: 1,
});
const list = (current = 5, history_key = "site-one") => ({
  history_key, current_revision: current, total: 45, items: [summary(3), summary(2)],
});
const detail = (revision, history_key = "site-one", owner_type = "site") => ({
  ...summary(revision), history_key, owner_type,
  snapshot: { local: { name: "<script>alert(1)</script>" }, dependencies: [{ id: 2, version: 1 }], features: {}, resolved: null, resolution_error: "历史解析失败" },
});
const renderer = createRenderer({
  createComment: () => ({}), insert() {}, remove() {}, parentNode() {}, nextSibling() {},
});
let app;
try {
  const { default: History } = await server.ssrLoadModule("/src/components/WorkflowHistoryDialog.vue");
  const props = reactive({ modelValue: false, target: { ownerType: "site", id: 1, name: "One", historyKey: "site-one" } });
  let ui;
  app = renderer.createApp({ setup() {
    History.setup(props, { expose(value) { ui = value; }, emit(event, value) { events.push({ event, value }); } });
    return () => null;
  } });
  app.provide(ssrContextKey, { modules: new Set() });
  app.mount({});
  assert.equal(requests.length, 0, "closed dialog does not request history");
  props.modelValue = true;
  assert.equal(requests.at(-1).url, "/sites/1/versions?limit=20&offset=0");
  requests.at(-1).resolve(list());
  await tick();
  assert.equal(ui.versions.value.current_revision, 5);
  assert.equal(ui.canRestore.value, false, "must load a snapshot before restoring");

  const first = ui.viewSnapshot(2);
  const oldDetail = requests.at(-1);
  const second = ui.viewSnapshot(3);
  assert.equal(requests.at(-1).url, "/sites/1/versions/3");
  requests.at(-1).resolve(detail(3));
  await second;
  oldDetail.resolve(detail(2));
  await first;
  assert.equal(ui.snapshot.value.revision, 3, "late snapshot cannot replace newer selection");
  assert.equal(ui.canRestore.value, true, "historical resolution error does not bypass current backend validation");

  const beforeRestore = requests.length;
  const restore = ui.restore();
  assert.match(confirmations.at(-1).message, /One.*v3.*v5/);
  assert.match(confirmations.at(-1).message, /不含登录凭据或 Cookie/);
  assert.equal(requests.length, beforeRestore, "confirmation required before POST");
  await ui.restore();
  assert.equal(confirmations.length, 1, "duplicate restore is blocked while confirming");
  confirmations.at(-1).resolve();
  await tick();
  const post = requests.at(-1);
  assert.equal(post.url, "/sites/1/versions/3/restore");
  assert.equal(post.method, "POST");
  assert.deepEqual(JSON.parse(post.body), { expected_revision: 5, expected_history_key: "site-one", accept_current_dependencies: false });
  post.reject(new ApiError(409, "历史依赖已变化"));
  await restore;
  assert.equal(requests.length, beforeRestore + 1, "409 never auto-retries or refreshes expected revision");
  assert.equal(ui.acceptCurrentDependencies.value, false);
  assert.equal(ui.snapshot.value.revision, 3, "conflict preserves selected snapshot");
  assert.match(ui.restoreError.value, /历史依赖已变化.*未自动重试/);

  ui.acceptCurrentDependencies.value = true;
  const accepted = ui.restore();
  assert.match(confirmations.at(-1).message, /已选择接受当前依赖/);
  ui.acceptCurrentDependencies.value = false;
  ui.versions.value.history_key = "replacement-during-confirmation";
  confirmations.at(-1).resolve();
  await tick();
  assert.deepEqual(JSON.parse(requests.at(-1).body), { expected_revision: 5, expected_history_key: "site-one", accept_current_dependencies: true }, "capture identity and explicit choice before confirmation");
  requests.at(-1).resolve({ id: 1, config_revision: 6, history_key: "site-one" });
  await tick();
  assert.equal(events.at(-1).event, "restored");
  assert.equal(requests.at(-1).url, "/sites/1/versions?limit=20&offset=0");
  requests.at(-1).resolve(list(6));
  await accepted;
  assert.equal(ui.versions.value.current_revision, 6);
  assert.equal(ui.snapshot.value, null);
  assert.equal(ui.acceptCurrentDependencies.value, false);

  const refresh = ui.loadVersions();
  requests.at(-1).reject(new Error("history unavailable"));
  await refresh;
  assert.equal(ui.error.value, "history unavailable");
  assert.equal(ui.versions.value, null);
  assert.equal(ui.canRestore.value, false, "failed refresh must not retain a usable expected revision");
  const retry = ui.loadVersions();
  requests.at(-1).resolve(list(6));
  await retry;
  assert.equal(ui.error.value, "");
  const failedSnapshot = ui.viewSnapshot(2);
  requests.at(-1).reject(new Error("snapshot unavailable"));
  await failedSnapshot;
  assert.equal(ui.snapshot.value, null);
  assert.equal(ui.canRestore.value, false);
  assert.equal(ui.snapshotError.value, "snapshot unavailable");

  ui.changePage(2);
  const oldPage = requests.at(-1);
  assert.equal(oldPage.url, "/sites/1/versions?limit=20&offset=20");
  ui.changePage(3);
  requests.at(-1).resolve(list(8));
  await tick();
  oldPage.reject(new Error("stale page failure"));
  await tick();
  assert.equal(ui.versions.value.current_revision, 8);
  assert.equal(ui.error.value, "", "stale page error cannot replace current state");
  assert.equal(ui.loading.value, false);

  const oldSnapshotPromise = ui.viewSnapshot(2);
  const oldSnapshot = requests.at(-1);
  props.target = { ownerType: "template", id: 9, name: "Template", historyKey: "template-nine" };
  assert.equal(ui.page.value, 1, "switching owner resets pagination");
  assert.equal(requests.at(-1).url, "/templates/9/versions?limit=20&offset=0");
  oldSnapshot.reject(new Error("stale snapshot"));
  await oldSnapshotPromise;
  assert.equal(ui.snapshotError.value, "");
  requests.at(-1).resolve(list(4, "template-nine"));
  await tick();
  const showTemplate = ui.viewSnapshot(1);
  requests.at(-1).resolve(detail(1, "template-nine", "template"));
  await showTemplate;
  const obsoleteConfirmation = ui.restore();
  const confirm = confirmations.at(-1);
  props.target = { ownerType: "site", id: 1, name: "One", historyKey: "site-one" };
  const countAfterSwitch = requests.length;
  confirm.resolve();
  await obsoleteConfirmation;
  assert.equal(requests.length, countAfterSwitch, "old confirmation must not restore either old or new target");
  requests.at(-1).resolve(list());
  await tick();

  const pendingView = ui.viewSnapshot(2);
  requests.at(-1).resolve(detail(2));
  await pendingView;
  const cancel = ui.restore();
  confirmations.at(-1).reject("cancel");
  await cancel;
  assert.equal(ui.restoring.value, false);
  const closeConfirmation = ui.restore();
  props.modelValue = false;
  const countAfterClose = requests.length;
  confirmations.at(-1).resolve();
  await closeConfirmation;
  assert.equal(requests.length, countAfterClose, "closing invalidates pending confirmation");
  assert.equal(ui.snapshot.value, null);

  props.modelValue = true;
  const lateList = requests.at(-1);
  props.modelValue = false;
  props.modelValue = true;
  lateList.resolve(list(99));
  await tick();
  assert.equal(ui.versions.value, null);
  assert.equal(ui.loading.value, true, "old finally must not clear current loading flag");
  requests.at(-1).resolve(list());
  await tick();
  const staleRestore = ui.viewSnapshot(3);
  requests.at(-1).resolve(detail(3, "wrong-key"));
  await staleRestore;
  assert.equal(ui.canRestore.value, false, "history key must match");
  assert.equal(ui.snapshot.value, null, "do not display a replacement object's snapshot");
  assert.match(ui.snapshotError.value, /对象已替换/);
  const deleted = ui.viewSnapshot(3);
  requests.at(-1).resolve({ ...detail(3), operation: "delete" });
  await deleted;
  assert.equal(ui.canRestore.value, false, "delete tombstones cannot be restored");

  const active = ui.viewSnapshot(3);
  requests.at(-1).resolve(detail(3));
  await active;
  const lateRestore = ui.restore();
  confirmations.at(-1).resolve();
  await tick();
  const inFlightPost = requests.at(-1);
  props.target = { ownerType: "template", id: 7, name: "New", historyKey: "template-seven" };
  const newList = requests.at(-1);
  const previousEvents = events.length;
  inFlightPost.resolve({ id: 1, history_key: "site-one" });
  await lateRestore;
  assert.equal(events.length, previousEvents + 1, "successful mutation notifies parent even after switching");
  assert.equal(requests.at(-1), newList, "old mutation cannot refresh new owner's history");
  assert.equal(ui.versions.value, null);
  newList.reject(new ApiError(401, "登录过期"));
  await tick();
  assert.equal(cleared, 1);
  assert.deepEqual(routes, [{ name: "login" }]);
  assert.equal(ui.error.value, "");
  const unauthorizedDetail = ui.loadVersions();
  requests.at(-1).resolve(list(1, "template-seven"));
  await unauthorizedDetail;
  const failingDetail = ui.viewSnapshot(1);
  requests.at(-1).reject(new ApiError(401, "登录过期"));
  await failingDetail;
  assert.equal(cleared, 2, "snapshot 401 uses same auth handler");
  const show = ui.viewSnapshot(1);
  requests.at(-1).resolve(detail(1, "template-seven", "template"));
  await show;
  const unauthorizedRestore = ui.restore();
  confirmations.at(-1).resolve();
  await tick();
  assert.equal(requests.at(-1).url, "/templates/7/versions/1/restore");
  assert.equal(JSON.parse(requests.at(-1).body).expected_history_key, "template-seven");
  requests.at(-1).reject(new ApiError(401, "登录过期"));
  await unauthorizedRestore;
  assert.equal(cleared, 3, "restore 401 uses same auth handler");

  const replacedList = ui.loadVersions();
  requests.at(-1).resolve(list(1, "replacement-seven"));
  await replacedList;
  assert.match(ui.error.value, /对象已替换/);
  assert.equal(ui.versions.value, null, "do not show replacement object's history from stale list entry");
  const blockedCount = requests.length;
  await ui.viewSnapshot(1);
  await ui.restore();
  assert.equal(requests.length, blockedCount, "replaced owner cannot load snapshots or restore");

  const originalList = ui.loadVersions();
  requests.at(-1).resolve(list(1, "template-seven"));
  await originalList;
  const originalSnapshot = ui.viewSnapshot(1);
  requests.at(-1).resolve(detail(1, "template-seven", "template"));
  await originalSnapshot;
  const sameIdConfirmation = ui.restore();
  const oldIdentityConfirmation = confirmations.at(-1);
  props.target = { ...props.target, historyKey: "replacement-seven" };
  assert.equal(ui.versions.value, null, "same ID with new history key starts a new session");
  const replacementList = requests.at(-1);
  const identitySwitchCount = requests.length;
  oldIdentityConfirmation.resolve();
  await sameIdConfirmation;
  assert.equal(requests.length, identitySwitchCount, "same-ID identity change invalidates old confirmation");
  replacementList.resolve(list(1, "replacement-seven"));
  await tick();
  assert.equal(ui.versions.value.history_key, "replacement-seven");
  const deletionList = ui.loadVersions();
  requests.at(-1).resolve(list(5, "replacement-seven"));
  await deletionList;
  assert.equal(ui.canDelete(5), false, "current version is protected");
  assert.equal(ui.canDelete(99), false, "unlisted revisions cannot be deleted");
  assert.equal(ui.canDelete(2), true);
  const deleteCount = requests.length;
  await ui.deleteVersion(5);
  assert.equal(requests.length, deleteCount);
  const cancelDelete = ui.deleteVersion(2);
  assert.match(confirmations.at(-1).message, /Template|New/);
  assert.match(confirmations.at(-1).message, /v2.*无法查看或恢复.*不影响当前配置/);
  assert.equal(requests.length, deleteCount, "deletion requires confirmation");
  const confirmationCount = confirmations.length;
  await ui.deleteVersion(3);
  assert.equal(confirmations.length, confirmationCount, "duplicate deletion is blocked");
  ui.changePage(2);
  await ui.viewSnapshot(2);
  await ui.restore();
  assert.equal(requests.length, deleteCount, "other actions are blocked while deleting");
  confirmations.at(-1).reject("cancel");
  await cancelDelete;
  assert.equal(ui.deletingRevision.value, null);

  const beforeDeleteView = ui.viewSnapshot(2);
  requests.at(-1).resolve(detail(2, "replacement-seven", "template"));
  await beforeDeleteView;
  const conflictDelete = ui.deleteVersion(2);
  assert.equal(ui.canRestore.value, false, "restore is disabled during deletion");
  confirmations.at(-1).resolve();
  await tick();
  assert.equal(requests.at(-1).url, "/workflow-history/replacement-seven/versions/2");
  assert.equal(requests.at(-1).method, "DELETE");
  const conflictCount = requests.length;
  requests.at(-1).reject(new ApiError(409, "当前版本不可删除"));
  await conflictDelete;
  assert.equal(requests.length, conflictCount, "conflicts are never retried automatically");
  assert.match(ui.error.value, /当前版本不可删除.*未自动重试/);
  assert.equal(ui.snapshot.value.revision, 2, "failed deletion preserves snapshot");

  const successfulDelete = ui.deleteVersion(2);
  confirmations.at(-1).resolve();
  await tick();
  requests.at(-1).resolve();
  await tick();
  assert.equal(ui.snapshot.value, null, "deleted snapshot cannot remain restorable");
  assert.equal(requests.at(-1).url, "/templates/7/versions?limit=20&offset=0");
  requests.at(-1).resolve({ ...list(5, "replacement-seven"), total: 1, items: [summary(5)] });
  await successfulDelete;
  assert.equal(ui.deletingRevision.value, null);
  assert.equal(ui.versions.value.total, 1);

  // Removing the last item on a page must select the last remaining page.
  ui.changePage(2);
  requests.at(-1).resolve({ ...list(22, "replacement-seven"), total: 21, items: [summary(1)] });
  await tick();
  const lastPageDelete = ui.deleteVersion(1);
  confirmations.at(-1).resolve();
  await tick();
  requests.at(-1).resolve();
  await tick();
  assert.equal(requests.at(-1).url, "/templates/7/versions?limit=20&offset=20");
  requests.at(-1).resolve({ ...list(22, "replacement-seven"), total: 20, items: [] });
  await tick();
  assert.equal(ui.page.value, 1);
  assert.equal(requests.at(-1).url, "/templates/7/versions?limit=20&offset=0");
  requests.at(-1).resolve({ ...list(22, "replacement-seven"), total: 20 });
  await lastPageDelete;

  const unauthorizedDelete = ui.deleteVersion(2);
  confirmations.at(-1).resolve();
  await tick();
  requests.at(-1).reject(new ApiError(401, "登录过期"));
  await unauthorizedDelete;
  assert.equal(cleared, 4, "deletion uses the shared auth handler");

  const staleDeleteConfirmation = ui.deleteVersion(2);
  const oldDeleteConfirm = confirmations.at(-1);
  props.target = { ownerType: "site", id: 1, name: "One", historyKey: "site-one" };
  const switchedDeleteCount = requests.length;
  oldDeleteConfirm.resolve();
  await staleDeleteConfirmation;
  assert.equal(requests.length, switchedDeleteCount, "target change invalidates delete confirmation");
  requests.at(-1).resolve(list());
  await tick();
  const lateDelete = ui.deleteVersion(2);
  confirmations.at(-1).resolve();
  await tick();
  const deleteInFlight = requests.at(-1);
  assert.equal(deleteInFlight.url, "/workflow-history/site-one/versions/2");
  props.modelValue = false;
  props.modelValue = true;
  const reopenedList = requests.at(-1);
  deleteInFlight.resolve();
  await lateDelete;
  assert.equal(requests.at(-1), reopenedList, "late delete cannot refresh a new dialog session");
  assert.equal(ui.deletingRevision.value, null);
  reopenedList.resolve(list());
  await tick();
  const closedDelete = ui.deleteVersion(2);
  props.modelValue = false;
  const closedDeleteCount = requests.length;
  confirmations.at(-1).resolve();
  await closedDelete;
  assert.equal(requests.length, closedDeleteCount, "closing invalidates delete confirmation");
  props.modelValue = true;
  requests.at(-1).resolve(list());
  await tick();
  const beforeMissingIdentity = requests.length;
  props.target = { ...props.target, historyKey: "" };
  await tick();
  assert.equal(requests.length, beforeMissingIdentity, "missing history identity must not fall back to ID-only requests");
  assert.match(ui.error.value, /缺少对象身份/);
  assert.equal(ui.canRestore.value, false);
  props.target = { ...props.target, historyKey: "replacement-seven" };
  requests.at(-1).resolve(list(1, "replacement-seven"));
  await tick();
  const unmounted = ui.loadVersions();
  app.unmount();
  app = null;
  requests.at(-1).resolve(list(999));
  await unmounted;
  assert.notEqual(ui.versions.value?.current_revision, 999, "unmount invalidates requests");

  assert.doesNotMatch(source, /v-html/);
  for (const field of ["local", "dependencies", "resolved"]) {
    assert.ok(source.includes(`{{ JSON.stringify(snapshot.snapshot.${field}, null, 2) }}`));
  }
  assert.match(source, /完整普通配置/);
  assert.doesNotMatch(source, /已脱敏/);
  assert.match(source, /96vw/);
  assert.match(source, /white-space: normal/);
  assert.match(source, /label="操作类型"/);
  assert.equal((source.match(/label="操作"/g) ?? []).length, 1);
  assert.match(source, /查看快照<\/el-button>\s*<el-button[^>]*type="danger"[^>]*>删除版本<\/el-button>/);

  // Follow the repo's AST-based tests to exercise real view functions without router/DOM setup.
  const importUtils = await server.ssrLoadModule("/src/utils/exportFile.ts");
  for (const [view, kind, revisionField] of [["Sites", "sites", "config_revision"], ["Templates", "templates", "version"]]) {
    const text = await readFile(new URL(`../src/views/${view}View.vue`, import.meta.url), "utf8");
    const script = text.slice(text.indexOf(">") + 1, text.indexOf("</script>"));
    const ast = ts.createSourceFile(`${view}.ts`, script, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS);
    const copy = view === "Sites" ? "copyFromSite" : "copyFromTemplate";
    const importFile = view === "Sites" ? "importSitesFromFile" : "importTemplatesFromFile";
    const names = ["emptyForm", "resetEditor", "openCreate", "openEdit", "buildPayload", "save", "remove", "openHistory", copy, importFile];
    if (view === "Sites") names.push("importBitwardenMode");
    const functions = ast.statements.filter(node => ts.isFunctionDeclaration(node) && names.includes(node.name?.text));
    assert.equal(functions.length, names.length);
    const code = ts.transpileModule(functions.map(node => node.getText(ast)).join("\n"), {
      compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext },
    }).outputText;
    const sent = [];
    let conflict = false;
    let serverRevision = 7;
    let serverHistoryKey = `${kind}-original`;
    let deleteConfirmation;
    const form = {};
    const bindings = {
      ...importUtils,
      form, ApiError, nextTick: async callback => { callback?.(); },
      editingId: ref(null), editingRevision: ref(null), editingHistoryKey: ref(null), editorOpen: ref(false),
      editorError: ref(""), loadingForm: ref(false), saving: ref(false), interactionStarted: ref(false),
      formBaseline: ref(""), credentialBaseline: ref(""), chain: ref([]), groupsLocked: ref(false),
      credential: ref({}), credentialUsername: ref(""), credentialPassword: ref(""),
      credentialError: ref(""), cookieInput: ref(""), cookieError: ref(""), credentialTab: ref("account"),
      hasCredentialInputs: ref(false), copying: ref(false), copyOpen: ref(false),
      historyOpen: ref(false), historyTarget: ref(null),
      emptyCredential: () => ({}), validate() {}, selectedTemplateId: () => null,
      selectedParentId: () => null, lanePolicy: () => "general", trustedUrls: value => [...value],
      stepToForm: value => value, signalToForm: value => value, groupToForm: value => value,
      loadChain: async () => [], load: async () => {}, handleUnauthorized: async () => false,
      ElMessage: { success() {}, error() {} }, ElMessageBox: { confirm: async () => { await deleteConfirmation?.promise; } },
      apiRequest: async (url, options) => {
        sent.push({ url, ...options });
        if (!options?.method && ["/sites", "/templates"].includes(url)) return [];
        if (conflict && options?.method === "PUT") throw new ApiError(409, "配置版本冲突");
        return { ...uiView.emptyForm(), name: "Saved", id: 11, [revisionField]: serverRevision, history_key: serverHistoryKey,
          lane_policy: "general", tags: [], credential: {}, trusted_urls: [] };
      },
    };
    const uiView = new Function(...Object.keys(bindings), `${code}\nreturn { ${names.join(", ")} };`)(...Object.values(bindings));
    await uiView.openEdit(11);
    assert.equal(bindings.editingRevision.value, 7);
    assert.equal(bindings.editingHistoryKey.value, `${kind}-original`);
    form.name = "Unsaved draft";
    conflict = true;
    serverHistoryKey = `${kind}-replacement`;
    const requestCount = sent.length;
    await uiView.save();
    assert.equal(sent.length, requestCount + 1, "save does not fetch latest revision or retry");
    assert.equal(sent.at(-1).method, "PUT");
    assert.equal(JSON.parse(sent.at(-1).body).expected_revision, 7);
    assert.equal(JSON.parse(sent.at(-1).body).expected_history_key, `${kind}-original`, "save keeps old identity even when server reuses ID and revision");
    assert.equal(form.name, "Unsaved draft");
    assert.equal(bindings.editorOpen.value, true);
    assert.equal(bindings.editingRevision.value, 7);
    assert.equal(bindings.editingHistoryKey.value, `${kind}-original`);
    assert.match(bindings.editorError.value, /冲突/);
    await uiView.save();
    assert.equal(JSON.parse(sent.at(-1).body).expected_revision, 7, "manual save still uses draft's original version");
    assert.equal(JSON.parse(sent.at(-1).body).expected_history_key, `${kind}-original`);
    serverHistoryKey = `${kind}-original`;
    if (view === "Sites") {
      conflict = false;
      serverRevision = 8;
      await uiView.save(false);
      assert.equal(bindings.editingRevision.value, 8, "successful save advances draft revision from response");
      assert.equal(bindings.editingHistoryKey.value, `${kind}-original`);
      assert.equal(bindings.editorOpen.value, true);
      await uiView.save(false);
      assert.equal(JSON.parse(sent.at(-1).body).expected_revision, 8);
    }

    uiView.openHistory({ id: 11, name: "No draft history", history_key: `${kind}-original` });
    assert.equal(bindings.historyOpen.value, false, "history only opens from list");
    uiView.openCreate();
    await uiView[copy](11);
    assert.equal(bindings.editingRevision.value, null, "copy must not adopt source revision");
    assert.equal(bindings.editingHistoryKey.value, null, "copy must not adopt source identity");
    conflict = false;
    await uiView.save();
    const create = sent.findLast(request => request.method === "POST");
    assert.equal(create.url, `/${kind}`);
    assert.equal("expected_revision" in JSON.parse(create.body), false);
    assert.equal("expected_history_key" in JSON.parse(create.body), false);
    const deleteRow = { id: 11, name: "Delete", [revisionField]: 9, history_key: "old/key? &=" };
    deleteConfirmation = deferred();
    const deletion = uiView.remove(deleteRow);
    deleteRow.history_key = "replacement";
    deleteRow[revisionField] = 10;
    deleteConfirmation.resolve();
    await deletion;
    deleteConfirmation = null;
    assert.equal(sent.findLast(request => request.method === "DELETE").url, `/${kind}/11?expected_revision=9&expected_history_key=${encodeURIComponent("old/key? &=")}`, "delete captures and encodes original identity before confirmation");
    const beforeMissingDeleteIdentity = sent.length;
    await uiView.remove({ id: 11, name: "Missing identity", [revisionField]: 9, history_key: "" });
    assert.equal(sent.length, beforeMissingDeleteIdentity, "delete without identity fails closed");
    uiView.openHistory({ id: 11, name: "History", history_key: `${kind}-original` });
    assert.equal(bindings.historyOpen.value, true);
    assert.equal(bindings.historyTarget.value.ownerType, kind === "sites" ? "site" : "template");
    assert.equal(bindings.historyTarget.value.historyKey, `${kind}-original`);
    sent.length = 0;
    await uiView[importFile]({ text: async () => JSON.stringify({
      kind, [kind]: [{ name: "Imported", base_url: "https://example.com", trusted_urls: [], expected_revision: 999, version: 999, config_revision: 999, history_key: "source-key", expected_history_key: "source-key" }],
    }) });
    assert.equal("expected_revision" in JSON.parse(sent.find(request => request.method === "POST").body), false, "imports never forward source revisions");
    assert.equal("expected_history_key" in JSON.parse(sent.find(request => request.method === "POST").body), false, "imports never forward source identity");
    bindings.editingId.value = 11;
    bindings.editingHistoryKey.value = null;
    const missingIdentityCount = sent.length;
    await uiView.save();
    assert.equal(sent.length, missingIdentityCount, "editing without identity fails closed");
    assert.match(bindings.editorError.value, /对象身份/);
    assert.match(text, /<WorkflowHistoryDialog[^>]+@restored="load"/);
    assert.ok(text.indexOf('@click="openHistory(row)"') < text.indexOf('class="editor-page"'), "entry belongs to list, not editor");
  }
  console.log("Workflow history regression passed");
} finally {
  app?.unmount();
  delete globalThis.__historyMocks;
  await server.close();
}
