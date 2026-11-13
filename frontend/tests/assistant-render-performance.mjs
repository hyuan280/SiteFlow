import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { harness, id, otherId, base, session, message, date } from "./helpers/assistant-harness.mjs";

const h = await harness("detail, sessions, connection, error, selectSession, refreshDetail, toggleMessage, openMessageIds, messageCache, approvalSeconds, pendingApproval");
const { ui, respond, choose, emit } = h;
const originalParse = JSON.parse, originalStringify = JSON.stringify;
let prettyCalls = 0, contentParses = 0, frameParses = 0;
const largeContent = JSON.stringify({ ok: true, text: "LARGE-CLOSED-LOG".repeat(20000), literal: '<script>alert("unsafe")</script>' });
const log = message(1, "tool", largeContent, { tool_name: "browser_observe", status: "running" });
let frame;
JSON.parse = function (value, ...args) {
  if (value === largeContent) contentParses++;
  if (value === frame) frameParses++;
  return originalParse(value, ...args);
};
JSON.stringify = function (value, replacer, space) {
  if (space === 2) prettyCalls++;
  return originalStringify(value, replacer, space);
};
try {
  await respond({ items: [] });
  const loading = ui.selectSession(id);
  await respond(base({ status: "running", messages: [log] })); await loading;
  const listing = h.requests.length;
  ui.sessions.value = [session({ status: "running" })];
  const sessions = ui.sessions.value;
  let html = await h.html();
  assert.doesNotMatch(html, /<pre|LARGE-CLOSED-LOG|<script>/);
  assert.equal(prettyCalls, 0, "large closed results never run JSON pretty printing");
  assert.equal(contentParses, 1, "trust metadata parses the content once");
  await h.html();
  assert.equal(contentParses, 1, "parent renders reuse parsed content");

  const stream = h.streams.at(-1);
  frame = originalStringify(base({ status: "running", messages: [log] }));
  stream.update({ data: frame });
  const detail = ui.detail.value;
  for (let i = 0; i < 50; i++) stream.update({ data: frame });
  assert.equal(frameParses, 1, "identical SSE data is skipped before JSON.parse");
  assert.equal(ui.detail.value, detail, "duplicate frames do not apply snapshots");
  assert.equal(ui.sessions.value, sessions, "message-only updates retain list identity");
  assert.equal(h.requests.length, listing, "duplicates never initiate reads or writes");
  await h.html();
  assert.equal(prettyCalls, 0);
  assert.equal(contentParses, 1, "new message references with identical content share parsing");

  ui.toggleMessage(1, { currentTarget: { open: true } });
  html = await h.html();
  assert.ok(/<pre\b[^>]*>\{/.test(html), "expanded results render a pre element");
  assert.match(html, /LARGE-CLOSED-LOG/);
  assert.match(html, /&lt;script&gt;/); assert.doesNotMatch(html, /<script>/);
  assert.equal(prettyCalls, 1);
  await h.html(); assert.equal(prettyCalls, 1, "expanded results also reuse pretty output");
  ui.toggleMessage(1, { currentTarget: { open: false } });
  assert.doesNotMatch(await h.html(), /<pre|LARGE-CLOSED-LOG/);
  emit(base({ status: "running", messages: [{ ...log, status: "success" }] }));
  ui.toggleMessage(1, { currentTarget: { open: true } });
  await h.html();
  assert.equal(contentParses, 1, "status-only updates retain the parsed result");
  assert.equal(prettyCalls, 1, "status-only updates retain the formatted result");
  assert.equal(ui.sessions.value, sessions);
  ui.toggleMessage(1, { currentTarget: { open: false } });

  // A completed tool cannot regress to running, even in a long history.
  const history = Array.from({ length: 1500 }, (_, i) => message(i + 1, "tool", "{}", { status: "success" }));
  emit(base({ status: "running", messages: history }));
  emit(base({ status: "running", messages: history.map(item => ({ ...item, status: "running" })) }));
  assert.equal(ui.detail.value.messages.at(-1).status, "success");
  frame = originalStringify(ui.detail.value);
  stream.update({ data: frame });
  const beforeError = ui.detail.value;
  stream.onerror();
  assert.equal(ui.connection.value, "offline");
  const pendingRead = h.last();
  stream.update({ data: frame });
  assert.equal(ui.connection.value, "live", "duplicate heartbeat restores live state");
  await respond(base({ status: "idle", messages: history.map(item => ({ ...item, content: "stale-http-content" })), title: "New independent title", title_revision: 2,
    context: { revision: 2, summary_through_message_id: 1, compressed: true } }));
  assert.equal(ui.detail.value.messages.length, beforeError.messages.length, "network error reads preserve progress");
  assert.equal(ui.detail.value.messages[0].content, "{}", "even a same-ID read cannot replace streamed content on network error");
  assert.equal(ui.detail.value.status, "running");
  assert.equal(ui.detail.value.title, "New independent title");
  assert.equal(ui.detail.value.context.revision, 2);
  assert.equal(pendingRead.method, undefined, "connection errors only read; no hidden resend");

  const approval = { id: randomUUID(), operation_id: randomUUID(), status: "pending", created_at: date,
    feature_type: "sign.button", title: "Check retry", expires_at: new Date(Date.parse(date) + 60000).toISOString() };
  // Use a selection reset so this is a fresh pending approval, not an older progress frame.
  await choose(base({ status: "running", messages: [], pending_approval: approval }));
  assert.ok(ui.pendingApproval.value);
  assert.equal(ui.approvalSeconds.value, 60);
  const approvalFrame = originalStringify(ui.detail.value);
  h.streams.at(-1).update({ data: approvalFrame });
  await h.advance(1000);
  h.streams.at(-1).update({ data: approvalFrame });
  assert.equal(ui.approvalSeconds.value, 59, "SSE dedup does not suppress approval clock updates");
  assert.match(await h.html(), /剩余授权时间 59 秒/);

  await choose(base({ messages: [log] }));
  await h.html(); assert.equal(contentParses, 2, "selection resets clear parsed logs");
  const changed = { ...log, content: '{"ok":true,"text":"updated-content"}', status: "success" };
  emit(base({ messages: [changed] }));
  ui.toggleMessage(1, { currentTarget: { open: true } });
  html = await h.html();
  assert.match(html, /updated-content/); assert.doesNotMatch(html, /LARGE-CLOSED-LOG/);
  assert.equal(prettyCalls, 2, "content changes invalidate pretty results");
  // Closing, reselection and snapshot removal all discard expansion state.
  await choose(base({ id: otherId, messages: [changed] }));
  assert.equal(ui.openMessageIds.size, 0);
  assert.doesNotMatch(await h.html(), /<pre/);
  ui.toggleMessage(1, { currentTarget: { open: true } });
  emit(base({ id: otherId, messages: [message(2)] }));
  assert.equal(ui.openMessageIds.size, 0);

  const probe = message(100, "tool", largeContent);
  ui.messageCache.parse(probe);
  const beforeEviction = contentParses;
  for (let i = 0; i < 201; i++) ui.messageCache.parse(message(1000 + i, "tool", "{}"));
  ui.messageCache.parse(probe);
  assert.equal(contentParses, beforeEviction + 1, "the memory cache holds at most 200 results");
  ui.messageCache.prune([]);
  ui.messageCache.parse(probe);
  assert.equal(contentParses, beforeEviction + 2, "snapshots prune absent cached messages");
  assert.equal(h.stored.size, 0, "logs and parsed caches are never persisted");
  h.unmount();
  assert.equal(ui.openMessageIds.size, 0);
  console.log("Assistant render performance regression passed");
} finally {
  JSON.parse = originalParse; JSON.stringify = originalStringify;
  await h.close();
}
