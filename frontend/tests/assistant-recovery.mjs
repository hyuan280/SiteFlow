import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { harness, id, otherId, base, target, message, date, ApiError, tick } from "./helpers/assistant-harness.mjs";

const h = await harness("detail, content, action, error, legacyRetry, recoveryOperations, canScreenshot, screenshotOpen, screenshotPreview, selectSession, takeScreenshot, refreshBrowser, authorizeRetry, confirmLegacyRetry, send, toolResult, uncertaintyStatus");
const { ui, types, last, respond, choose, emit, finish, requests, confirmations } = h;
const op = randomUUID(), otherOp = randomUUID();
const unresolved = (operation_id = op, fields = {}) => ({ operation_id, feature_type: "auth.form_login", status: "unknown", code: "outcome_uncertain", can_authorize: true, ...fields });
const state = (fields = {}) => base({ unresolved_operations: [unresolved()], ...fields });
const grant = (fields = {}) => ({ operation_id: op, retry_operation_id: null, expires_at: date, status: "approved", target_revision: 1, ...fields });
const screenshot = (fields = {}) => ({ mime_type: "image/jpeg", image_base64: btoa("\xff\xd8\xff\xe0\xff\xd9"), width: 1600, height: 1200, masked: true, ...fields });
try {
  await respond({ items: [] }); await choose(state());
  for (const value of [unresolved(op, { can_authorize: "true" }), unresolved("bad"), { ...unresolved(), step: "PRIVATE" }]) {
    assert.equal(types.isAssistantDetail(state({ unresolved_operations: [value] })), false);
  }
  for (const value of [grant({ target_revision: -1 }), grant({ status: "pending" }), grant({ retry_operation_id: "bad" }), { ...grant(), token: "PRIVATE" }]) {
    assert.equal(types.isAssistantDetail(state({ retry_authorizations: [value] })), false);
  }
  for (const fields of [{ target: { ...target, screenshot_consent: false } }, { target: { ...target, screenshot_consent: undefined } },
    { browser: null }, { browser: { status: "expired", lane_key: "interaction", expires_at: date } }, { status: "running" }]) {
    await choose(state(fields)); const before = requests.length; await ui.takeScreenshot(); assert.equal(requests.length, before);
  }
  await choose(state());
  for (const invalid of [screenshot({ mime_type: "image/svg+xml" }), screenshot({ masked: false }), screenshot({ width: 1601 }), screenshot({ height: 1201 }),
    screenshot({ image_base64: "AAAA" }), screenshot({ image_base64: btoa("<svg/>") }), screenshot({ image_base64: "A".repeat(524292) }), screenshot({ image_base64: "AB==" })]) {
    assert.equal(types.isAssistantScreenshot(invalid), false);
    const pending = ui.takeScreenshot(); await respond(invalid); await pending;
    assert.equal(ui.screenshotOpen.value, false); assert.equal(ui.screenshotPreview.value, null);
  }
  const capture = ui.takeScreenshot(), imagePost = last();
  assert.equal(imagePost.url, `/assistant/sessions/${id}/browser/screenshot`); assert.equal(imagePost.body, "{}");
  await ui.takeScreenshot(); await ui.refreshBrowser(); await ui.authorizeRetry(op); assert.equal(last(), imagePost);
  await respond(screenshot()); await capture;
  assert.equal(ui.screenshotPreview.value.src, `data:image/jpeg;base64,${screenshot().image_base64}`);
  ui.screenshotOpen.value = false; assert.equal(ui.screenshotPreview.value, null);
  const closed = ui.takeScreenshot(); ui.screenshotOpen.value = false; await respond(screenshot()); await closed;
  assert.equal(ui.screenshotPreview.value, null);
  const changed = ui.takeScreenshot(); emit(state({ target: { ...target, base_url: "https://example.com/new" } }));
  await respond(screenshot()); await changed; assert.equal(ui.screenshotPreview.value, null);
  emit(state());
  const refreshing = ui.refreshBrowser();
  assert.match(confirmations.at(-1).args[0], /GET.*不会重提 POST.*副作用/);
  confirmations.at(-1).resolve(); await tick(); assert.equal(last().url, `/assistant/sessions/${id}/browser/refresh`);
  await finish(refreshing, state());
  const deniedRefresh = ui.refreshBrowser(); confirmations.at(-1).reject(); await deniedRefresh; assert.equal(ui.action.value, "");
  const racedRefresh = ui.refreshBrowser(); emit(state({ browser: null })); confirmations.at(-1).resolve(); await racedRefresh;
  assert.equal(ui.action.value, "");

  await choose(state({ unresolved_operations: [unresolved(), unresolved(otherOp)] })); assert.equal(ui.recoveryOperations.value.length, 2);
  const unknown = message(1, "tool", JSON.stringify(unresolved()), { tool_name: "execute_step", status: "error" });
  emit(state({ unresolved_operations: undefined, messages: [unknown] })); assert.equal(ui.recoveryOperations.value.length, 1);
  assert.equal(ui.uncertaintyStatus(unknown), "pending");
  emit(state({ unresolved_operations: [], messages: [unknown], retry_authorizations: [grant()] }));
  assert.equal(ui.recoveryOperations.value.length, 0); assert.equal(ui.uncertaintyStatus(unknown), "historical");
  const noAuthCount = requests.length; await ui.authorizeRetry(op); assert.equal(ui.legacyRetry.value, null); assert.equal(requests.length, noAuthCount);
  await choose(state()); ui.content.value = "keep my draft";
  const before = requests.length, confirmationsBefore = confirmations.length;
  await ui.authorizeRetry(op);
  assert.equal(requests.length, before); assert.equal(confirmations.length, confirmationsBefore, "legacy retry is inline, not a confirmation modal");
  assert.equal(ui.legacyRetry.value.operationId, op);
  const html = await h.html();
  assert.match(html, /允许重试一次/); assert.match(html, /重复登录.*签到.*费用.*副作用/);
  assert.match(html, /5 分钟有效.*不会立即执行/);
  assert.doesNotMatch(html, /role="dialog"/);
  await ui.send(); assert.equal(requests.length, before, "staged legacy review does not forge a message");
  const approving = ui.confirmLegacyRetry(), post = last(), payload = JSON.parse(post.body);
  assert.equal(post.url, `/assistant/sessions/${id}/retry-authorizations`);
  assert.deepEqual(Object.keys(payload).sort(), ["confirmed", "operation_id", "request_id"]);
  assert.equal(payload.confirmed, true); assert.equal(payload.operation_id, op);
  post.reject(new TypeError("PRIVATE credentials")); await approving;
  assert.equal(requests.length, before + 1); assert.equal(ui.content.value, "keep my draft"); assert.doesNotMatch(ui.error.value, /PRIVATE/);
  emit(state({ retry_authorizations: [grant({ status: "expired" })] }));
  await ui.authorizeRetry(op); assert.match(await h.html(), /同一请求 ID.*不新建或续期授权/);
  const repeated = ui.confirmLegacyRetry(); assert.equal(JSON.parse(last().body).request_id, payload.request_id);
  await finish(repeated, state({ retry_authorizations: [grant()] }));
  assert.equal(ui.recoveryOperations.value[0].authorization.status, "approved"); assert.equal(ui.content.value, "keep my draft");
  emit(state({ retry_authorizations: [grant({ status: "used", retry_operation_id: randomUUID() })] }));
  await ui.authorizeRetry(op); assert.match(await h.html(), /先前重试授权已使用.*更多重复操作/);
  const renewal = ui.confirmLegacyRetry(); assert.notEqual(JSON.parse(last().body).request_id, payload.request_id);
  await finish(renewal, state({ retry_authorizations: [grant()] }));
  for (const fields of [{ unresolved_operations: [] }, { unresolved_operations: [unresolved(op, { can_authorize: false })] },
    { target: { ...target, base_url: "https://example.com/changed" } }, { status: "running", messages: [message(1)] }]) {
    await choose(state()); await ui.authorizeRetry(op); emit(state(fields));
    assert.equal(ui.legacyRetry.value, null, "state changes invalidate staged inline review");
    const count = requests.length; await ui.confirmLegacyRetry(); assert.equal(requests.length, count);
  }
  await choose(state()); await ui.authorizeRetry(op); await choose(state({ id: otherId }));
  assert.equal(ui.legacyRetry.value, null);
  const lateImage = ui.takeScreenshot(), latePost = last(); await choose(state()); latePost.reject(new ApiError(401, "PRIVATE")); await lateImage;
  assert.equal(h.cleared(), 0); assert.equal(ui.screenshotPreview.value, null);
  await ui.authorizeRetry(op); const lateRetry = ui.confirmLegacyRetry(), oldRetry = last(); await choose(state({ id: otherId }));
  oldRetry.reject(new ApiError(401, "PRIVATE")); await lateRetry; assert.equal(h.cleared(), 0);
  const modelImage = message(1, "tool", JSON.stringify({ ...screenshot(), image_id: randomUUID(), token: "PRIVATE", image_url: "javascript:alert(1)" }), { tool_name: "browser_screenshot" });
  assert.doesNotMatch(ui.toolResult(modelImage), /image_base64|PRIVATE|javascript|image_url/); assert.match(ui.toolResult(modelImage), /image_id/);
  const pendingImage = ui.takeScreenshot(), finalPost = last(); h.unmount(); finalPost.resolve(screenshot()); await pendingImage;
  assert.equal(ui.screenshotPreview.value, null); assert.equal(ui.screenshotOpen.value, false);
  assert.ok(requests.every(item => item.url.startsWith("/assistant/sessions")), "no target editor, sites options or direct execution writes");
  assert.equal(requests.filter(item => item.url.endsWith("/messages")).length, 0);
  assert.equal(h.stored.size, 0, "screenshots, grants, drafts and recovery state remain memory-only");
  console.log("Assistant recovery regression passed");
} finally { await h.close(); }
