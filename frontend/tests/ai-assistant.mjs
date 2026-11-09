import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { readFile } from "node:fs/promises";
import { harness, id, otherId, base, session, message, ApiError, tick, date } from "./helpers/assistant-harness.mjs";

const source = await readFile(new URL("../src/views/AIAssistantView.vue", import.meta.url), "utf8");
const h = await harness("detail, sessions, content, action, error, createError, creating, detailLoading, titleDialog, titleInput, settingsOpen, connection, usageOpen, contextPercentText, contextCircle, safetyVisible, canSend, tokenText, dismissSafety, createSession, selectSession, loadSessions, refreshDetail, send, cancel, openTitle, saveTitle, deleteSession, toolResult, trustResult, isContextLimit");
const { ui, types, last, respond, choose, emit, finish, requests } = h;
const counters = ["prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens", "reasoning_tokens"];
const call = (kind = "conversation", fields = {}) => ({ id: randomUUID(), kind, created_at: date, prompt_tokens: 120, completion_tokens: 30,
  total_tokens: 150, cached_tokens: 80, reasoning_tokens: 10, ...fields });
const usage = (revision = 1, fields = {}) => ({ revision, last_context_tokens: 150, last_context_budget_tokens: 1000,
  last_context_prompt_tokens: 120, last_context_completion_tokens: 30, last_context_cached_tokens: 80, calls: [call()],
  totals: { request_count: 1, prompt_tokens: 120, completion_tokens: 30, total_tokens: 150, cached_tokens: 80, reasoning_tokens: 10 },
  unreported_usage_count: 0, ...fields });
const context = (revision, compressed = true) => ({ revision, summary_through_message_id: compressed ? 1 : null, compressed });
try {
  assert.equal(last().url, "/assistant/sessions");
  await respond({ items: [] });
  const failedCreate = ui.createSession();
  last().reject(new ApiError(503, "PRIVATE", "assistant_busy")); await failedCreate;
  assert.match(ui.createError.value, /繁忙/); assert.doesNotMatch(ui.createError.value, /PRIVATE/);
  const creating = ui.createSession(), creation = last();
  assert.deepEqual(JSON.parse(creation.body), {});
  await ui.createSession(); await ui.selectSession(otherId);
  assert.equal(last(), creation, "create and selection are latched");
  creation.resolve(base({ messages: [message(1, "assistant", "Welcome")] })); await creating;
  assert.equal(ui.detail.value.messages[0].content, "Welcome");
  assert.equal(h.streams.at(-1).url, `/api/v1/assistant/sessions/${id}/events`);
  await respond({ items: [session()] });
  ui.content.value = "   "; await ui.send(); assert.equal(last().url, "/assistant/sessions");
  ui.content.value = "x".repeat(12001); assert.equal(ui.canSend.value, false);
  ui.content.value = "x".repeat(12000); assert.equal(ui.canSend.value, true, "no checkbox or confirmed target gate");
  ui.content.value = "  configure  ";
  const sending = ui.send(), sendPost = last(), payload = JSON.parse(sendPost.body);
  assert.equal(sendPost.url, `/assistant/sessions/${id}/messages`);
  assert.deepEqual(Object.keys(payload).sort(), ["consent_to_send", "content", "request_id"]);
  assert.equal(payload.content, "configure"); assert.equal(payload.consent_to_send, true);
  assert.match(payload.request_id, /^[0-9a-f-]{36}$/);
  await ui.send(); assert.equal(last(), sendPost);
  const done = base({ messages: [message(1), message(2, "assistant", "done")] });
  emit(done); sendPost.resolve({ accepted: true }); await tick();
  const sendGet = last();
  emit({ ...done, messages: [...done.messages, message(3, "tool", "{}", { status: "success" })] });
  sendGet.resolve(base({ status: "running", messages: [message(1)] })); await sending;
  await respond({ items: [session()] });
  assert.equal(ui.detail.value.messages.length, 3, "HTTP never regresses SSE progress");
  emit(base({ status: "running", messages: [...done.messages, message(3, "tool", "{}", { status: "running" })] }));
  assert.equal(ui.detail.value.messages[2].status, "success", "terminal tools never regress");
  const stable = ui.detail.value;
  for (const invalid of [null, {}, { ...stable, id: otherId }, { ...stable, status: "bad" }, { ...stable, messages: [message(1), message(1)] },
    { ...stable, browser: { lane_key: "general", status: "ready", expires_at: date } }, { ...stable, target: {} },
    { ...stable, title_locked: undefined }, { ...stable, title_revision: -1 }]) {
    emit(invalid); assert.equal(ui.detail.value, stable);
  }
  h.streams.at(-1).update({ data: "invalid JSON" }); assert.match(ui.error.value, /格式无效/);

  // Independent title/context/usage highwaters remain independent of message progress.
  emit({ ...stable, title: "Auto", title_revision: 1 });
  assert.equal(ui.sessions.value[0].title, "Auto");
  ui.openTitle(); ui.titleInput.value = " "; const beforeTitle = requests.length;
  await ui.saveTitle(); assert.equal(requests.length, beforeTitle);
  ui.titleInput.value = "x".repeat(129); await ui.saveTitle(); assert.equal(requests.length, beforeTitle);
  ui.titleInput.value = "  Manual <script>literal</script>  ";
  const naming = ui.saveTitle(), titlePut = last();
  assert.equal(titlePut.method, "PUT"); assert.equal(JSON.parse(titlePut.body).title, "Manual <script>literal</script>");
  const manual = { ...stable, title: "Manual", title_locked: true, title_revision: 3 };
  emit(manual); titlePut.resolve({ ...manual, title: "older", title_revision: 2 }); await tick();
  await respond(base()); await naming; await respond({ items: [session()] });
  assert.equal(ui.detail.value.title, "Manual"); assert.equal(ui.sessions.value[0].title, "Manual");
  emit({ ...base(), context: context(2), usage: usage(2) });
  assert.equal(ui.detail.value.messages.length, 3); assert.equal(ui.detail.value.title_revision, 3);
  assert.equal(ui.detail.value.context.revision, 2); assert.equal(ui.detail.value.usage.revision, 2);
  emit({ ...ui.detail.value, context: context(1), usage: usage(1) });
  assert.equal(ui.detail.value.context.revision, 2); assert.equal(ui.detail.value.usage.revision, 2);
  const read = ui.refreshDetail(), oldGet = last();
  emit({ ...ui.detail.value, context: context(3), usage: usage(3) });
  oldGet.resolve({ ...base(), context: context(4), usage: usage(1) }); await read;
  assert.equal(ui.detail.value.context.revision, 4); assert.equal(ui.detail.value.usage.revision, 3);
  assert.equal(ui.detail.value.messages.length, 3);
  const list = ui.loadSessions(); await respond({ items: [session({ context: context(5), title: "Latest", title_revision: 4 })] }); await list;
  assert.equal(ui.detail.value.title, "Latest"); assert.equal(ui.detail.value.context.revision, 5);
  emit({ ...ui.detail.value, context: context(5, false) }); assert.equal(ui.detail.value.context.compressed, false);

  assert.equal(ui.contextPercentText.value, "15%", "numerator includes 120 prompt + 30 completion, not 120 or cumulative totals");
  emit({ ...ui.detail.value, usage: usage(4, { last_context_tokens: 1500, last_context_completion_tokens: 1380 }) });
  assert.equal(ui.contextPercentText.value, "150%"); assert.equal(ui.contextCircle.value, "100%");
  const known = ui.detail.value.usage;
  for (const kind of ["summary", "title"]) {
    emit({ ...ui.detail.value, usage: { ...known, revision: ui.detail.value.usage.revision + 1, calls: [...known.calls, call(kind, { prompt_tokens: 9000 })] } });
    assert.equal(ui.contextPercentText.value, "150%", "auxiliary calls never supply the numerator");
  }
  emit({ ...ui.detail.value, usage: { ...known, revision: 7, last_context_usage_missing: true,
    calls: [...known.calls, call("conversation", { completion_tokens: null })], unreported_usage_count: 1 } });
  assert.equal(ui.contextPercentText.value, "150%", "incomplete call retains the last valid numerator and budget");
  assert.equal(ui.detail.value.usage.last_context_prompt_tokens, known.last_context_prompt_tokens);
  assert.equal(ui.detail.value.usage.last_context_completion_tokens, known.last_context_completion_tokens);
  assert.equal(ui.detail.value.usage.last_context_budget_tokens, known.last_context_budget_tokens);
  let staleHtml = await h.html();
   assert.match(staleHtml, /aria-label="上次已知（非当前）用量 150%，输入 \+ 输出 1500 \/ 预算 1000 Token；最近调用未返回完整用量，不代表当前上下文，查看用量明细"/);
   const ring = staleHtml.match(/<button[^>]*class="context-button"[^>]*>[\s\S]*?<\/button>/)?.[0];
   assert.ok(ring);
   assert.match(ring, /title="上次已知（非当前）用量 150%/);
   assert.equal(ring.replace(/<[^>]*>/g, "").trim(), "", "usage ring contains no visible percentage, placeholder or label");
   assert.doesNotMatch(staleHtml, /class="usage-stale"|>上次已知（非当前）；最近调用未返回完整用量/);
  ui.usageOpen.value = true;
  staleHtml = await h.html();
  assert.match(staleHtml, /上次已知（非当前）占用 150%/);
  assert.match(staleHtml, /保留上次有效的计数和预算，不代表当前上下文/);
  assert.match(staleHtml, /aria-label="上次已知（非当前）用量"/);
  assert.doesNotMatch(staleHtml, /本次上下文占用|aria-label="当前上下文用量"/);
  ui.usageOpen.value = false;
  emit({ ...ui.detail.value, usage: usage(8, { last_context_usage_missing: false }) });
  assert.equal(ui.contextPercentText.value, "15%", "next complete measurement replaces historical counts");
  assert.doesNotMatch(await h.html(), /上次已知（非当前）/);
  emit({ ...ui.detail.value, context: context(6), usage: usage(9, { last_context_usage_missing: false,
    last_context_tokens: null, last_context_budget_tokens: null, last_context_prompt_tokens: null,
    last_context_completion_tokens: null, last_context_cached_tokens: null }) });
  assert.equal(ui.contextPercentText.value, "--", "compaction clears the old ratio rather than carrying it forward");
  assert.doesNotMatch(await h.html(), /上次已知（非当前）/);
  const legacyUsage = usage(10); delete legacyUsage.last_context_budget_tokens; delete legacyUsage.last_context_prompt_tokens; delete legacyUsage.last_context_completion_tokens;
  assert.equal(types.isAssistantDetail(base({ usage: legacyUsage })), true);
  emit({ ...ui.detail.value, usage: legacyUsage }); assert.equal(ui.contextPercentText.value, "--", "old input-only usage is never mislabeled");
  emit({ ...ui.detail.value, usage: usage(11, { last_context_tokens: null }) }); assert.equal(ui.contextPercentText.value, "--");
  assert.equal(ui.tokenText(null), "--"); assert.equal(ui.tokenText(0), "0");
  for (const fields of [{ last_context_budget_tokens: -1 }, { last_context_prompt_tokens: "120" }, { last_context_completion_tokens: Infinity },
    ...[null, "true", 0, 1, {}, []].map(last_context_usage_missing => ({ last_context_usage_missing })),
    { raw_usage: "PRIVATE" }, { calls: Array.from({ length: 101 }, () => call()) }, { calls: [call("invalid")] },
    { calls: [call("conversation", { raw: "PRIVATE" })] }, { calls: [call("conversation", { prompt_tokens: -1 })] }]) {
    assert.equal(types.isAssistantDetail(base({ usage: usage(1, fields) })), false);
  }
  const nullableCall = call("conversation", Object.fromEntries(counters.map(key => [key, null])));
  assert.equal(types.isAssistantDetail(base({ usage: usage(1, { calls: [nullableCall] }) })), true);
  for (const last_context_usage_missing of [undefined, false, true]) {
    assert.equal(types.isAssistantDetail(base({ usage: usage(1, { last_context_usage_missing }) })), true, "missing flag is optional for persisted ledgers, otherwise strictly boolean");
  }
  emit({ ...ui.detail.value, usage: usage(12, { last_context_usage_missing: true, last_context_tokens: null,
    last_context_prompt_tokens: null, last_context_completion_tokens: null, last_context_budget_tokens: null }) });
  assert.equal(ui.contextPercentText.value, "--");
   assert.match(await h.html(), /aria-label="上次已知（非当前）用量 --，输入 \+ 输出 -- \/ 预算 -- Token；最近调用未返回完整用量，不代表当前上下文，查看用量明细"/, "first incomplete call cannot invent counts");
  assert.equal(types.isAssistantDetail(base({ usage: usage(1, { last_context_revision: 0, last_context_target_revision: 1,
    calls: [call("conversation", { context_budget_tokens: 64000, context_revision: 0, target_revision: 1 })] }) })), true, "current backend usage metadata is accepted");
  assert.equal(types.isAssistantSession({ ...session(), usage: usage() }), false);
  for (const invalid of [null, { revision: 0 }, { ...context(1), summary: "PRIVATE" }, { ...context(1), summary_through_message_id: 0 }]) {
    assert.equal(types.isAssistantDetail(base({ context: invalid })), false);
    assert.equal(types.isAssistantSession(session({ context: invalid })), false);
  }
  const beforeDetails = requests.length;
  ui.usageOpen.value = true;
  const rendered = await h.html();
  assert.match(rendered, /上下文与累计用量/); assert.match(rendered, /会话累计消耗，不是当前上下文大小/);
  assert.equal(requests.length, beforeDetails, "usage details never invoke models");
  ui.usageOpen.value = false;
  const trust = message(4, "tool", JSON.stringify({ ok: true, text: "page retained", warnings: [{ code: "browser_trust_required", source: "blocked_request", target_origin: "https://auth.example.com" }] }), { tool_name: "browser_observe" });
  assert.equal(ui.trustResult(trust).target_origin, "https://auth.example.com");
  assert.match(ui.toolResult(trust), /page retained/);
  emit({ ...ui.detail.value, messages: [...ui.detail.value.messages, trust] });
  assert.equal(requests.length, beforeDetails, "trust warnings never forge a user message or target write");
  assert.match(await h.html(), /请在聊天中要求助手发起交互式可信来源确认/);
   const limit = message(5, "system", "[context_limit]"); assert.equal(ui.isContextLimit(limit), true);
   assert.equal(ui.isContextLimit({ ...limit, role: "assistant" }), false);

   const markdown = "## 回复\n\n**重要**\n\n- 第一项\n- 第二项\n\n<script>alert(1)</script>";
   emit({ ...ui.detail.value, messages: [message(1, "assistant", markdown), message(2, "user", markdown),
     message(3, "tool", markdown, { tool_name: "browser_observe", status: "success" }),
     message(4, "system", markdown), message(5, "assistant", markdown, { status: "error" })] });
   const messageHtml = await h.html();
   assert.equal((messageHtml.match(/class="assistant-markdown"/g) ?? []).length, 1, "only non-error assistant replies use Markdown");
   assert.match(messageHtml, /<h2>回复<\/h2>/); assert.match(messageHtml, /<strong>重要<\/strong>/);
   assert.match(messageHtml, /<li>第一项<\/li>/); assert.doesNotMatch(messageHtml, /<script>/);
   assert.equal((messageHtml.match(/## 回复/g) ?? []).length, 4, "user, tool, system and error records remain plaintext");

  ui.content.value = "preserved";
  ui.settingsOpen.value = true; await tick();
  assert.equal(requests.length, beforeDetails); assert.equal(ui.content.value, "preserved"); ui.settingsOpen.value = false;
  assert.equal(ui.safetyVisible.value, true);
  ui.dismissSafety(); ui.dismissSafety();
  assert.deepEqual([...h.stored], [["siteflow.assistant.safety.v1", "dismissed"]], "only a versioned dismissal is stored, never session data");
  h.blockStorage(true); ui.safetyVisible.value = true; ui.dismissSafety(); assert.equal(ui.safetyVisible.value, false);

  ui.content.value = "late"; const late = ui.send(), latePost = last(), oldStream = h.streams.at(-1);
  const switching = ui.selectSession(otherId), switchingGet = last();
  const returning = ui.selectSession(id), returningGet = last();
  switchingGet.resolve(base({ id: otherId })); await switching;
  assert.equal(ui.detailLoading.value, true);
  returningGet.resolve(base()); await returning;
  assert.equal(ui.content.value, ""); assert.equal(oldStream.closed, true);
  oldStream.emit(base({ messages: [message(100)] })); latePost.reject(new ApiError(401, "PRIVATE")); await late;
  assert.equal(h.cleared(), 0); assert.equal(ui.detail.value.messages.length, 0); assert.equal(ui.detail.value.usage, undefined);
  for (const [code, pattern] of [["secret_input", /敏感信息/], ["context_limit", /上下文仍超过/], ["authorization_required", /交互式授权/], ["unknown_code", /操作失败/]]) {
    ui.content.value = "retry manually"; const failed = ui.send(), count = requests.length;
    last().reject(new ApiError(503, "PRIVATE provider", code)); await failed;
    assert.match(ui.error.value, pattern); assert.doesNotMatch(ui.error.value, /PRIVATE|provider/); assert.equal(requests.length, count);
  }
  const disconnectPosts = requests.filter(item => item.method === "POST").length;
  h.streams.at(-1).onerror(); assert.equal(ui.connection.value, "offline"); await respond(base());
  assert.equal(requests.filter(item => item.method === "POST").length, disconnectPosts);
  const deleting = ui.deleteSession(); h.confirmations.at(-1).resolve(); await tick();
  assert.equal(last().method, "DELETE"); await respond(undefined); await deleting; await respond({ items: [] });
  assert.equal(ui.detail.value, null);
  await choose(); const authRead = ui.refreshDetail(); last().reject(new ApiError(401, "PRIVATE")); await authRead;
  assert.equal(h.cleared(), 1); assert.deepEqual(h.routes.at(-1), { name: "login" });
  await choose(); ui.content.value = "unmount"; const unmountSend = ui.send(), unmountPost = last();
  h.unmount(); unmountPost.resolve({ accepted: true }); await unmountSend; assert.equal(ui.detail.value, null);
  assert.ok(requests.every(item => item.url.startsWith("/assistant/sessions")), "no sites options or direct site/run APIs");
  assert.doesNotMatch(source, /targetDialog|targetForm|openTarget|saveTarget|requestTrust|target-summary|v-html|innerHTML|sessionStorage/);
  assert.doesNotMatch(source, /const consent|v-model="consent"|<label for="assistant-message">/);
  assert.match(source, /aria-label="消息"/);
   assert.match(source, /class="title-line"><h2>[\s\S]*?<button v-if="detail"[^>]*class="context-button"/);
   assert.match(source, /\.context-button \{[^}]*width: 24px; height: 24px;/);
   assert.match(source, /<AssistantMarkdown v-else-if="message.role === 'assistant' && message.status !== 'error'" :content="message.content"/);
  assert.match(source, /<AssistantSettingsDialog v-model="settingsOpen"/);
  assert.match(source, /<AssistantInteractionReview v-if="pendingInteraction.review"/);
  assert.doesNotMatch(source, /JSON.stringify\(pendingInteraction.review/);
  const layout = await readFile(new URL("../src/components/layout/MainLayout.vue", import.meta.url), "utf8");
  assert.match(layout, /'assistant-shell': route.path === '\/assistant'/); assert.match(layout, /\.assistant-shell \.app-content \{ overflow: hidden/);
  console.log("AI assistant regression passed");
} finally { await h.close(); }
