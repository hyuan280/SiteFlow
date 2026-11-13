import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { harness, base, session, message, ApiError, tick } from "./helpers/assistant-harness.mjs";

const h = await harness("sessions, selectedId, detail, content, listLoading, moreLoading, nextOffset, listError, loadSessions, selectSession, createSession, openTitle, titleInput, saveTitle, deleteSession");
const { ui, types, requests, last, respond } = h;
const rows = Array.from({ length: 215 }, (_, index) => session({ id: randomUUID(), title: `History ${index}` }));
const page = (offset, items = rows.slice(offset, offset + 100), next = offset + items.length < rows.length ? offset + 100 : null) =>
  ({ items, offset, limit: 100, next_offset: next, truncated: next !== null });
const ids = () => ui.sessions.value.map(item => item.id);
const noDuplicates = () => assert.equal(new Set(ids()).size, ids().length);
try {
  assert.equal(last().url, "/assistant/sessions", "initial URL stays compatible");
  await respond(page(0));
  assert.equal(last().url, `/assistant/sessions/${rows[0].id}`);
  await respond(base(rows[0]));
  assert.match(await h.html(), /加载更多会话/);
  const streams = h.streams.length;
  const count = requests.length;
  const loading = ui.loadSessions(true), pending = last();
  assert.equal(pending.url, "/assistant/sessions?offset=100&limit=100");
  await ui.loadSessions(true);
  assert.equal(requests.length, count + 1, "double clicks send only one request");
  assert.match(await h.html(), /disabled[^>]*aria-busy="true"/);
  ui.content.value = "unsent draft";
  const selecting = ui.selectSession(rows[1].id);
  await respond(base(rows[1])); await selecting;
  ui.content.value = "keep this draft";
  // A concurrent update shifts a live page and repeats one ID at the boundary.
  pending.resolve(page(100, [{ ...rows[99], title: "Updated overlap", title_revision: 1 }, ...rows.slice(100, 199)])); await loading;
  assert.equal(ui.selectedId.value, rows[1].id);
  assert.equal(ui.content.value, "keep this draft");
  assert.equal(h.streams.length, streams + 1, "pagination does not open an SSE stream");
  assert.equal(ids().length, 199); noDuplicates();
  assert.equal(ui.sessions.value[99].title, "Updated overlap", "overlaps update in place without duplicating or moving the row");
  assert.equal(h.routes.length, 0);
  assert.ok(requests.every(request => !request.method), "pagination performs reads only, no model or mutation calls");

  const final = ui.loadSessions(true);
  assert.equal(last().url, "/assistant/sessions?offset=200&limit=100");
  await respond(page(200)); await final;
  assert.equal(ui.nextOffset.value, null);
  assert.doesNotMatch(await h.html(), /加载更多会话/);
  const exhaustedCount = requests.length;
  await ui.loadSessions(true); assert.equal(requests.length, exhaustedCount);
  await h.choose(base(rows[210]));
  const oldIds = ids();
  const refresh = ui.loadSessions(); await respond(page(0)); await refresh;
  assert.deepEqual(ids(), oldIds, "head refresh preserves loaded older pages");
  assert.equal(ui.selectedId.value, rows[210].id);
  h.emit(base({ ...rows[210], messages: [message(1, "tool", "{}", { status: "success" })] }));
  assert.deepEqual(ids(), oldIds, "SSE tool updates never reset history");

  ui.openTitle(); ui.titleInput.value = "Renamed old session";
  const rename = ui.saveTitle();
  const renamed = base({ ...rows[210], title: "Renamed old session", title_locked: true, title_revision: 1 });
  await respond(renamed); await respond(renamed); await rename;
  await respond(page(0));
  assert.deepEqual(ids(), oldIds);
  assert.equal(ui.sessions.value.find(item => item.id === rows[210].id).title, renamed.title);

  const created = base({ id: randomUUID(), title: "New" });
  const create = ui.createSession(); await respond(created); await create;
  await respond(page(0, [session(created), ...rows.slice(0, 99)], 100));
  assert.ok(ids().includes(rows[210].id), "new sessions retain old history");
  const deleting = ui.deleteSession(); h.confirmations.at(-1).resolve(); await tick();
  await respond(undefined); await deleting;
  await respond(page(0)); await respond(base(rows[0]));
  assert.ok(ids().includes(rows[210].id), "deletion retains unrelated loaded pages");
  assert.ok(!ids().includes(created.id)); noDuplicates();

  // Validate legacy responses and reject all partial, malformed or inconsistent metadata.
  assert.equal(types.isAssistantSessionList({ items: [] }), true);
  assert.equal(types.isAssistantSessionList(page(200)), true);
  for (const fields of [
    { offset: -1 }, { offset: "0" }, { offset: 0.5 }, { limit: 0 }, { limit: 101 },
    { limit: true }, { next_offset: -1 }, { next_offset: 0 }, { next_offset: "100" },
    { next_offset: 100.5 }, { next_offset: Infinity }, { next_offset: Number.MAX_SAFE_INTEGER + 1 },
    { next_offset: null }, { truncated: false }, { truncated: "true" }, { truncated: undefined },
  ]) assert.equal(types.isAssistantSessionList({ ...page(0), ...fields }), false, JSON.stringify(fields));
  for (const metadata of [{ offset: 0 }, { next_offset: null }, { limit: 100 }, { truncated: false }]) {
    assert.equal(types.isAssistantSessionList({ items: [], ...metadata }), false);
  }
  assert.equal(types.isAssistantSessionList({ items: [rows[0], rows[0]] }), false);
  assert.equal(types.isAssistantSessionList(page(0, [], 100)), false);
  assert.equal(types.isAssistantSessionList(page(0, [...rows.slice(0, 99), { ...rows[0], id: rows[0].id.toUpperCase() }])), false);
} finally { await h.close(); }

// Independent pagination lifetime: failures preserve history/cursor; stale responses cannot win.
const r = await harness("sessions, selectedId, detail, nextOffset, moreLoading, listLoading, listError, loadSessions, selectSession");
try {
  await r.respond(page(0)); await r.respond(base(rows[0]));
  const stable = [...r.ui.sessions.value];
  const failed = r.ui.loadSessions(true); r.last().reject(new ApiError(503, "PRIVATE")); await failed;
  assert.equal(r.ui.moreLoading.value, false); assert.equal(r.ui.nextOffset.value, 100);
  assert.deepEqual(r.ui.sessions.value, stable); assert.doesNotMatch(r.ui.listError.value, /PRIVATE/);
  const malformed = r.ui.loadSessions(true);
  await r.respond({ ...page(100), next_offset: "200" }); await malformed;
  assert.deepEqual(r.ui.sessions.value, stable); assert.equal(r.ui.nextOffset.value, 100);
  const mismatch = r.ui.loadSessions(true); await r.respond(page(0)); await mismatch;
  assert.deepEqual(r.ui.sessions.value, stable);
  const stale = r.ui.loadSessions(true), staleRequest = r.last();
  const refresh = r.ui.loadSessions(); await r.respond(page(0)); await refresh;
  const current = r.ui.loadSessions(true), currentRequest = r.last();
  staleRequest.reject(new ApiError(401, "PRIVATE")); await stale;
  assert.equal(r.cleared(), 0); assert.equal(r.ui.moreLoading.value, true);
  currentRequest.resolve(page(100)); await current;
  assert.equal(r.ui.sessions.value.length, 200);
  const unmount = r.ui.loadSessions(true), unmountRequest = r.last();
  r.unmount(); unmountRequest.resolve(page(200)); await unmount;
  assert.equal(r.ui.sessions.value.length, 200, "unmounted requests cannot append");
} finally { await r.close(); }

const legacy = await harness("nextOffset, loadSessions");
try {
  await legacy.respond({ items: [] });
  assert.equal(legacy.ui.nextOffset.value, null);
  const before = legacy.requests.length; await legacy.ui.loadSessions(true);
  assert.equal(legacy.requests.length, before);
  assert.doesNotMatch(await legacy.html(), /加载更多会话/);
} finally { await legacy.close(); }

const mutationRace = await harness("sessions, selectedId, nextOffset, listLoading, moreLoading, loadSessions, deleteSession");
try {
  await mutationRace.respond(page(0)); await mutationRace.respond(base(rows[0]));
  const stale = mutationRace.ui.loadSessions(true), staleRequest = mutationRace.last();
  const deleting = mutationRace.ui.deleteSession(); mutationRace.confirmations.at(-1).resolve(); await tick();
  await mutationRace.respond(undefined); await deleting;
  const headRequest = mutationRace.last();
  staleRequest.resolve(page(100)); await stale;
  assert.equal(mutationRace.ui.sessions.value.length, 99, "deleted session cannot be restored by an older page");
  assert.equal(mutationRace.ui.listLoading.value, true, "stale finally cannot release the current loading state");
  headRequest.resolve(page(0, rows.slice(1, 101), 100)); await tick();
  await mutationRace.respond(base(rows[1]));
  const expired = mutationRace.ui.loadSessions(true);
  mutationRace.last().reject(new ApiError(401, "PRIVATE")); await expired;
  assert.equal(mutationRace.cleared(), 1);
  assert.deepEqual(mutationRace.routes, [{ name: "login" }]);
  assert.equal(mutationRace.ui.sessions.value.length, 0);
  assert.equal(mutationRace.ui.nextOffset.value, null);
  assert.equal(mutationRace.ui.moreLoading.value, false);
} finally { await mutationRace.close(); }

const reconcile = await harness("sessions, selectedId, detail, content, nextOffset, loadSessions, selectSession, deleteSession");
try {
  await reconcile.respond(page(0)); await reconcile.respond(base(rows[0]));
  const second = reconcile.ui.loadSessions(true); await reconcile.respond(page(100)); await second;
  assert.equal(reconcile.ui.sessions.value.length, 200);
  assert.equal(reconcile.ui.nextOffset.value, 200);
  await reconcile.choose(base(rows[50]));
  const deleting = reconcile.ui.deleteSession(); reconcile.confirmations.at(-1).resolve(); await tick();
  await reconcile.respond(undefined); await deleting;
  assert.equal(reconcile.ui.nextOffset.value, 199, "known deletion shifts the loaded range cursor");
  const remaining = rows.filter(item => item.id !== rows[50].id);
  await reconcile.respond(page(0, remaining.slice(0, 100), 100));
  await reconcile.respond(base(remaining[0]));
  assert.equal(reconcile.ui.sessions.value.length, 199, "truncated head preserves loaded older history");
  assert.equal(reconcile.ui.nextOffset.value, 199);
  const final = reconcile.ui.loadSessions(true);
  assert.equal(reconcile.last().url, "/assistant/sessions?offset=199&limit=100");
  await reconcile.respond(page(199, remaining.slice(199), null)); await final;
  assert.deepEqual(reconcile.ui.sessions.value.map(item => item.id), remaining.map(item => item.id),
    "all 214 remaining rows are reachable without concurrent updates, including the shifted boundary row");

  await reconcile.choose(base(rows[210]));
  reconcile.ui.content.value = "Preserve this old session draft";
  const detail = reconcile.ui.detail.value, streams = reconcile.streams.length;
  // A legacy capped response cannot prove that absent older sessions were deleted.
  const legacyRefresh = reconcile.ui.loadSessions(); await reconcile.respond({ items: [] }); await legacyRefresh;
  assert.equal(reconcile.ui.sessions.value.length, 214);
  assert.equal(reconcile.ui.nextOffset.value, null, "legacy missing next offset remains exhausted");

  const fewer = reconcile.ui.loadSessions();
  await reconcile.respond(page(0, rows.slice(0, 3), null)); await fewer;
  assert.deepEqual(reconcile.ui.sessions.value.map(item => item.id), rows.slice(0, 3).map(item => item.id),
    "explicit exhaustive head removes externally deleted rows");
  assert.equal(reconcile.ui.selectedId.value, rows[210].id);
  assert.equal(reconcile.ui.detail.value.id, detail.id);
  assert.equal(reconcile.ui.content.value, "Preserve this old session draft");
  assert.equal(reconcile.streams.length, streams, "reconciliation does not navigate or reconnect");

  const empty = reconcile.ui.loadSessions(); await reconcile.respond(page(0, [], null)); await empty;
  assert.deepEqual(reconcile.ui.sessions.value, [], "authoritative empty clears every stale list entry");
  assert.equal(reconcile.ui.nextOffset.value, null);
  assert.equal(reconcile.ui.selectedId.value, rows[210].id);
  assert.equal(reconcile.ui.detail.value.id, detail.id);
  assert.equal(reconcile.ui.content.value, "Preserve this old session draft");
  const reloaded = reconcile.ui.loadSessions(); await reconcile.respond(page(0)); await reloaded;
  assert.equal(reconcile.ui.nextOffset.value, 100, "exhaustive refresh resets the old paging state");
  assert.equal(reconcile.ui.sessions.value.length, 100);
} finally { await reconcile.close(); }
console.log("Assistant session pagination regression passed");
