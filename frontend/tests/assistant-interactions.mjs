import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { harness, id, otherId, base, message, ApiError, tick, date } from "./helpers/assistant-harness.mjs";

const h = await harness("detail, action, error, content, pendingInteraction, interactionKey, interactionBusy, interactionExpired, interactionSeconds, interactionSelections, interactionValues, interactionValid, selectSession, refreshDetail, respondInteraction, cancel");
const { ui, types, requests, last, respond, choose, emit } = h;
const prompt = (fields = {}) => ({ id: randomUUID(), kind: "authorization", purpose: "target", title: "Confirm <script>literal</script>",
  description: "Review the public target", expires_at: "2026-11-07T12:03:00Z", options: [], multiple: false, fields: [],
  review: { name: "Example target", base_url: "https://example.com/", confirmed: true, browser_consent: true,
    credential_mode: "none", execution_features: ["action.click"], site_id: 12, run_lane_policy: "general" }, ...fields });
const state = (pending_interaction = null, fields = {}) => base({ status: "running", messages: [message(1), message(2, "tool", "", { status: "running", tool_name: "request_user_interaction" })], pending_interaction, ...fields });
const settled = fields => state(null, fields);
try {
  await respond({ items: [] });
  const first = prompt();
  for (const value of [undefined, null, first, prompt({ description: "" }), prompt({ options: undefined, fields: undefined, review: undefined })]) {
    assert.equal(types.isAssistantDetail(state(value)), true);
  }
  const choice = prompt({ kind: "choice", purpose: "question", options: [{ id: "a", label: "A" }, { id: "b", label: "B" }] });
  const input = prompt({ kind: "input", purpose: "question", fields: [{ id: "name", label: "Display name", required: true }, { id: "note", label: "Note", required: false }] });
  for (const value of [choice, input]) assert.equal(types.isAssistantPendingInteraction(value), true);
  const invalid = [false, [], {}, { ...first, token: "PRIVATE" }, { ...first, status: "pending" }, { ...first, kind: "unknown" },
    ...["id", "kind", "purpose", "title", "description", "expires_at"].map(key => ({ ...first, [key]: undefined })),
    prompt({ title: "x".repeat(257) }), prompt({ description: "x".repeat(4097) }), prompt({ expires_at: "2026-11-07" }),
    prompt({ review: { token: "PRIVATE" } }), prompt({ review: { _private: "PRIVATE" } }), prompt({ review: { nested: { password: "PRIVATE" } } }),
    prompt({ review: { huge: "x".repeat(4097) } }), prompt({ review: [] }), prompt({ multiple: true }),
    { ...choice, options: [] }, { ...choice, options: Array.from({ length: 21 }, (_, i) => ({ id: `a${i}`, label: "A" })) },
    { ...choice, options: [choice.options[0], choice.options[0]] }, { ...choice, options: [{ id: "a", label: "A", secret: true }] },
    { ...choice, options: [{ id: "__proto__", label: "A" }] }, { ...choice, options: [{ id: "x".repeat(65), label: "A" }] },
    { ...input, fields: [] }, { ...input, fields: [{ id: "name", label: "Name", required: "yes" }] },
    { ...input, fields: Array.from({ length: 9 }, (_, i) => ({ id: `a${i}`, label: "A", required: true })) }];
  for (const value of invalid) {
    assert.equal(types.isAssistantPendingInteraction(value), false);
    assert.equal(types.isAssistantDetail(state(value)), false);
  }
  await choose(state(first));
  assert.equal(ui.pendingInteraction.value.id, first.id); assert.equal(ui.interactionSeconds.value, 180); assert.equal(h.timers.size, 1);
  const before = requests.length;
  emit(state(first)); emit(state(first)); assert.equal(requests.length, before); assert.equal(h.timers.size, 1);
  let html = await h.html();
  assert.match(html, /剩余 180 秒/);
  assert.doesNotMatch(html, /role="dialog"/); assert.match(html, /Confirm &lt;script&gt;literal/);
  assert.match(html, /https:\/\/example.com/); assert.match(html, /<button[^>]*>停止<\/button>/);
  const reviewHtml = html.match(/<section[^>]*aria-label="授权核对内容"[^>]*>[\s\S]*?<\/section>/)?.[0];
  assert.ok(reviewHtml); assert.match(reviewHtml, /网站名称/); assert.match(reviewHtml, /Example target/); assert.match(reviewHtml, /网站地址/);
  assert.doesNotMatch(reviewHtml, /<pre|confirmed|browser_consent|credential_mode|execution_features|site_id|run_lane_policy|action.click|\{\s*&quot;/);
  assert.ok(html.indexOf('aria-label="助手交互请求"') < html.indexOf('class="composer"'));
  const stable = ui.detail.value; emit(state({ ...first, private: "PRIVATE" })); assert.equal(ui.detail.value, stable);
  ui.content.value = "keep draft";
  const approving = ui.respondInteraction("approve"), post = last();
  assert.equal(post.url, `/assistant/sessions/${id}/interactions/${first.id}/response`);
  assert.deepEqual(JSON.parse(post.body), { decision: "approve" }); assert.equal(ui.action.value, "");
  await ui.respondInteraction("deny"); assert.equal(last(), post, "decision double click is latched");
  const finished = settled({ status: "idle", messages: [message(1), message(2, "tool", "done", { status: "success" })] });
  emit(finished); assert.equal(ui.pendingInteraction.value, null); assert.equal(h.timers.size, 0);
  post.resolve(state(first)); await tick(); assert.equal(ui.detail.value.status, "idle");
  await respond(finished); await approving;
  emit(state(first)); assert.equal(ui.pendingInteraction.value, null, "terminal frame cannot reopen an interaction");
  assert.equal(ui.content.value, "keep draft");

  // Choice inputs are bound to the exact pending ID, never carried into its successor.
  await choose(state(choice)); assert.equal(ui.interactionValid.value, false);
  const beforeChoice = requests.length; await ui.respondInteraction("approve"); assert.equal(requests.length, beforeChoice);
  ui.interactionSelections.value = ["unknown"]; assert.equal(ui.interactionValid.value, false);
  ui.interactionSelections.value = ["a", "b"]; assert.equal(ui.interactionValid.value, false);
  ui.interactionSelections.value = ["a"]; assert.equal(ui.interactionValid.value, true);
  html = await h.html(); assert.match(html, /type="radio"/);
  const choosing = ui.respondInteraction("approve"), choicePost = last(); assert.deepEqual(JSON.parse(choicePost.body), { decision: "approve", selections: ["a"] });
  const multi = prompt({ ...choice, id: randomUUID(), multiple: true }); emit(state(multi));
  assert.deepEqual(ui.interactionSelections.value, []);
  choicePost.resolve(settled()); await tick(); assert.equal(ui.pendingInteraction.value.id, multi.id);
  const choiceRead = last(); emit(state(multi)); choiceRead.resolve(state(choice)); await choosing;
  assert.equal(ui.pendingInteraction.value.id, multi.id);
  emit(state(choice)); assert.equal(ui.pendingInteraction.value.id, multi.id, "settled older ID cannot replace current pending ID");
  ui.interactionSelections.value = ["a", "a"]; assert.equal(ui.interactionValid.value, false);
  ui.interactionSelections.value = ["a", "b"]; assert.equal(ui.interactionValid.value, true);
  assert.match(await h.html(), /type="checkbox"/);
  const selectingMany = ui.respondInteraction("approve"); assert.deepEqual(JSON.parse(last().body), { decision: "approve", selections: ["a", "b"] });
  await respond(settled()); await respond(settled()); await selectingMany;
  emit(state(multi)); assert.equal(ui.pendingInteraction.value, null);

  await choose(state(input)); assert.deepEqual(ui.interactionValues.value, {}); assert.equal(ui.interactionValid.value, false);
  ui.interactionValues.value = { name: " ", note: "optional" }; assert.equal(ui.interactionValid.value, false);
  ui.interactionValues.value = { name: "public name", note: "x".repeat(2001) }; assert.equal(ui.interactionValid.value, false);
  ui.interactionValues.value = { name: "public name", unexpected: "x" }; assert.equal(ui.interactionValid.value, false);
  ui.interactionValues.value = { name: "public name" }; assert.equal(ui.interactionValid.value, true);
  html = await h.html(); assert.match(html, /type="text"/); assert.match(html, /autocomplete="off"/); assert.doesNotMatch(html, /type="password"/);
  const entering = ui.respondInteraction("approve"); assert.deepEqual(JSON.parse(last().body), { decision: "approve", values: { name: "public name", note: "" } });
  await respond(settled()); await respond(settled()); await entering;
  await choose(state(input)); ui.interactionValues.value = { name: "discard" };
  const denying = ui.respondInteraction("deny"); assert.deepEqual(JSON.parse(last().body), { decision: "deny" }, "deny never transmits typed values or selections");
  await respond(settled()); await respond(settled()); await denying;
  emit(state(input)); assert.equal(ui.pendingInteraction.value, null, "denial has no reprompt");

  // A removal received during a refresh wins even when message IDs do not change.
  const removed = prompt(); await choose(state(removed));
  const refreshing = ui.refreshDetail(), staleGet = last(); emit(settled());
  staleGet.resolve(state(removed)); await refreshing; assert.equal(ui.pendingInteraction.value, null);
  emit(state(removed)); assert.equal(ui.pendingInteraction.value, null, "SSE removal settles ID before old snapshot replay");
  const afterRemoval = prompt(); emit(state(afterRemoval)); assert.equal(ui.pendingInteraction.value.id, afterRemoval.id);
  emit(state(removed)); assert.equal(ui.pendingInteraction.value.id, afterRemoval.id);

  // Selection epochs protect pending writes even after returning to the same session.
  const abandoned = ui.respondInteraction("approve"), abandonedPost = last(), previousStream = h.streams.at(-1);
  await choose(state(null, { id: otherId })); await choose(state(afterRemoval));
  previousStream.emit(settled()); const beforeLate = requests.length;
  abandonedPost.reject(new ApiError(401, "PRIVATE")); await abandoned;
  assert.equal(h.cleared(), 0); assert.equal(requests.length, beforeLate); assert.equal(ui.pendingInteraction.value.id, afterRemoval.id);
  assert.equal(ui.interactionBusy.value, false);

  // Failed writes remain manual, sanitize errors and never leak entered values to storage.
  const failing = ui.respondInteraction("approve"), countFailure = requests.length;
  last().reject(new TypeError("PRIVATE credentials")); await failing;
  assert.equal(requests.length, countFailure); assert.doesNotMatch(ui.error.value, /PRIVATE|credentials/);
  const invalidWrite = ui.respondInteraction("approve"); last().resolve(state({ ...afterRemoval, token: "PRIVATE" })); await invalidWrite;
  assert.equal(ui.pendingInteraction.value.id, afterRemoval.id);
  assert.equal(h.stored.size, 0);

  // Stop is independent from interaction writes and the main action latch.
  const inFlight = ui.respondInteraction("approve"), inFlightPost = last(); ui.action.value = "send";
  const stopping = ui.cancel(), stopPost = last(); assert.equal(stopPost.url, `/assistant/sessions/${id}/cancel`);
  emit(finished); stopPost.resolve(finished); await tick(); await respond(finished); await stopping; await respond({ items: [] });
  ui.action.value = ""; inFlightPost.resolve(state(afterRemoval)); await tick(); await respond(finished); await inFlight;
  assert.equal(ui.pendingInteraction.value, null); assert.equal(ui.detail.value.status, "idle");

  // Timeout only performs one read. Backend owns timeout resolution; no synthetic denial.
  const expiring = prompt({ expires_at: "2026-11-07T12:00:02Z" }); await choose(state(expiring));
  await h.advance(1000); assert.equal(ui.interactionSeconds.value, 1);
  const beforeExpiry = requests.length; await h.advance(1000);
  assert.equal(ui.interactionExpired.value, true); assert.equal(h.timers.size, 0); assert.equal(requests.length, beforeExpiry + 1); assert.equal(last().method, undefined);
  await ui.respondInteraction("approve"); await ui.respondInteraction("deny"); assert.equal(requests.length, beforeExpiry + 1);
  assert.match(await h.html(), /交互等待已超时/);
  await respond(state(expiring)); emit(state(expiring)); assert.equal(requests.length, beforeExpiry + 1);
  emit(settled()); assert.equal(ui.pendingInteraction.value, null);
  const expired = prompt({ expires_at: date }); await choose(state(expired)); assert.equal(ui.interactionExpired.value, true); assert.equal(h.timers.size, 0);
  await respond(settled());

  await choose(state(prompt()));
  const unauthorized = ui.respondInteraction("deny"); last().reject(new ApiError(401, "PRIVATE")); await unauthorized;
  assert.equal(h.cleared(), 1); assert.deepEqual(h.routes.at(-1), { name: "login" }); assert.equal(h.timers.size, 0);
  await choose(state(prompt())); const unmounted = ui.respondInteraction("approve"), unmountedPost = last();
  h.unmount(); unmountedPost.reject(new ApiError(401, "PRIVATE")); await unmounted;
  assert.equal(ui.pendingInteraction.value, null); assert.equal(h.timers.size, 0); assert.equal(h.cleared(), 1);
  assert.ok(requests.every(request => !request.url.endsWith("/messages") && !request.url.endsWith("/target")), "decisions never forge chat, authority or tool execution");
  console.log("Assistant interactions regression passed");
} finally { await h.close(); }
