import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { harness, base, message, ApiError } from "./helpers/assistant-harness.mjs";

const h = await harness("detail, error, pendingInteraction, interactionValid, respondInteraction, selectSession");
const xss = '<img src=x onerror="bad()">';
const review = { site_id: 7, name: `Saved site ${xss}`, base_url: 'https://example.com/path?one=1&two="<test>"',
  lane_key: "interaction", saved_cookie_count: 12, warnings: [`Shared host ${xss}`] };
const prompt = (data = review, fields = {}) => ({ id: randomUUID(), kind: "authorization", purpose: "cookie_clear",
  title: "Clear Bound Site Cookies", description: "Exact host only", expires_at: "2026-11-07T12:03:00Z",
  options: [], fields: [], multiple: false, review: data, ...fields });
const state = pending_interaction => base({ status: "running", pending_interaction });
const section = html => html.match(/<section[^>]*aria-label="授权核对内容"[^>]*>[\s\S]*?<\/section>/)?.[0];
const receipt = { ok: true, purpose: "cookie_clear", operation_id: randomUUID(), saved_cookies_cleared: true,
  browser_cookies_cleared: true, browser_outcome: "cleared" };
try {
  await h.respond({ items: [] });
  for (const [lane_key, saved_cookie_count, warnings, name] of [
    ["interaction", 0, [], "a"], ["proxy_interaction", 300, Array(20).fill("Public warning"), "a".repeat(128)],
  ]) {
    const pending = prompt({ ...review, lane_key, saved_cookie_count, warnings, name });
    assert.equal(h.types.isAssistantPendingInteraction(pending), true);
    assert.equal(h.types.isAssistantDetail(state(pending)), true);
    await h.choose(state(pending));
    assert.equal(h.ui.interactionValid.value, true);
    const html = section(await h.html());
    assert.ok(html.includes(lane_key === "interaction" ? "交互车道（不使用代理）" : "代理交互车道（使用已配置代理）"));
    assert.ok(html.includes(`>${saved_cookie_count}</dd>`));
    for (const warning of warnings) assert.ok(html.includes(warning));
  }

  await h.choose(state(null));
  const pending = prompt(), before = h.requests.length;
  h.emit(state(pending));
  assert.equal(h.ui.pendingInteraction.value.id, pending.id, "valid SSE review must not drop the entire snapshot");
  assert.equal(h.ui.interactionValid.value, true);
  let html = section(await h.html());
  for (const label of ["当前已保存站点 ID", "#7", "网站名称", "网站地址", "当前共享浏览器车道", "当前已保存 Cookie 数量",
    "12", "host 和 .host", "所有路径", "其他标签页可能退出登录", "父域", "兄弟域", "其他车道", "账号、密码和 localStorage 保持不变",
    "不会刷新页面", "不会自动重试", "Cookie 清除风险提示"]) assert.ok(html.includes(label), label);
  assert.match(html, /Saved site &lt;img src=x onerror=&quot;bad\(\)&quot;&gt;/);
  assert.match(html, /https:\/\/example.com\/path\?one=1&amp;two=&quot;&lt;test&gt;&quot;/);
  assert.match(html, /Shared host &lt;img/);
  assert.doesNotMatch(html, /<img|<script|<a\b|href=|<pre|\[object Object\]|saved_cookie_count|lane_key/);
  assert.doesNotMatch(html, /type="checkbox"/);
  assert.equal(h.requests.length, before, "receiving and rendering approval never initiates navigation, deletion or retries");
  const stable = h.ui.detail.value;
  const malformed = [null, [], {}, ...Object.keys(review).map(key => ({ ...review, [key]: undefined })),
    ...[0, -1, 1.5, "7", true, NaN, Infinity].map(site_id => ({ ...review, site_id })),
    ...["", " ", "a".repeat(129), 1, {}].map(name => ({ ...review, name })),
    ...["general", "proxy_general", "auto", {}, null].map(lane_key => ({ ...review, lane_key })),
    ...[-1, 301, 1.5, "12", false, null, NaN, Infinity].map(saved_cookie_count => ({ ...review, saved_cookie_count })),
    ...[undefined, "Warning", [{}], [null], Array(21).fill("Warning")].map(warnings => ({ ...review, warnings })),
    ...["cookies", "cookie", "cookie_values", "password", "account", "localStorage", "operation_id", "extra"]
      .map(key => ({ ...review, [key]: "RAW_COOKIE_SECRET" })),
    ...["javascript:alert(1)", "file:///tmp/test", "ftp://example.com", "https://user:pass@example.com", "https://user@example.com",
      "https://127.0.0.1/", "http://10.0.0.1", "http://169.254.169.254", "http://[::1]/", "https://localhost/", "https://x.internal/",
      "https://singlehost/", "https://example.com./", "https://example.com:0/", "https://example.com\\@other.com/", " https://example.com/",
      "https://example.com/\n", `https://example.com/${"a".repeat(500)}`].map(base_url => ({ ...review, base_url }))];
  for (const data of malformed) {
    const invalid = prompt(data);
    assert.equal(h.types.isAssistantPendingInteraction(invalid), false, JSON.stringify(data));
    assert.equal(h.types.isAssistantDetail(state(invalid)), false);
    h.emit(state(invalid));
    assert.equal(h.ui.detail.value, stable, "malformed SSE must never publish raw review values");
  }
  for (const fields of [{ kind: "choice", options: [{ id: "a", label: "A" }] },
    { kind: "input", fields: [{ id: "cookie", label: "Cookie", required: true }] }]) {
    assert.equal(h.types.isAssistantPendingInteraction(prompt(review, fields)), false);
  }
  const approving = h.ui.respondInteraction("approve"), post = h.last();
  assert.equal(post.url, `/assistant/sessions/${stable.id}/interactions/${pending.id}/response`);
  assert.equal(post.method, "POST");
  assert.deepEqual(JSON.parse(post.body), { decision: "approve" }, "never send cookies, review data or operation ID");
  await h.ui.respondInteraction("approve"); assert.equal(h.last(), post);
  await h.respond(state(null)); await h.respond(state(null)); await approving;

  // Defense in depth: an invalid in-memory review is visibly invalid and cannot be approved.
  await h.choose(state(prompt()));
  h.ui.detail.value.pending_interaction.review = { ...review, cookie_values: "RAW_COOKIE_SECRET" };
  assert.equal(h.ui.interactionValid.value, false);
  html = await h.html();
  assert.match(section(html), /格式无效.*不能批准/);
  assert.doesNotMatch(html, /RAW_COOKIE_SECRET/);
  assert.match(html, /<button[^>]*disabled[^>]*>确认并继续<\/button>/);
  const blocked = h.requests.length;
  await h.ui.respondInteraction("approve"); assert.equal(h.requests.length, blocked);
  const denying = h.ui.respondInteraction("deny");
  assert.deepEqual(JSON.parse(h.last().body), { decision: "deny" });
  await h.respond(state(null)); await h.respond(state(null)); await denying;

  for (const status of ["success", "error"]) {
    const partial = { ...receipt, ok: false, browser_cookies_cleared: false, browser_outcome: "unknown", code: "browser_outcome_unknown" };
    const requestsBefore = h.requests.length;
    await h.choose(base({ messages: [message(1, "tool", JSON.stringify(partial), { tool_name: "clear_bound_site_cookies", status })] }));
    html = await h.html();
    assert.match(html, /class="proposal-notice"[^>]*>已清除站点保存的 Cookie；浏览器 Cookie 清除结果未知/);
    assert.match(html, /不代表全部清除成功.*未刷新页面.*不会自动重试/);
    assert.doesNotMatch(html, /和当前车道的精确主机浏览器 Cookie|允许重试一次/);
    assert.equal(h.requests.length, requestsBefore + 1, "partial receipt only reads selected session, never retries");
  }
  await h.choose(base({ messages: [message(1, "tool", JSON.stringify(receipt), { tool_name: "clear_bound_site_cookies", status: "success" })] }));
  assert.match(await h.html(), /已清除站点保存的 Cookie 和当前车道的精确主机浏览器 Cookie；未刷新页面/);
  for (const data of [{ ...receipt, saved_cookies_cleared: false }, { ...receipt, browser_cookies_cleared: "true" },
    { ...receipt, operation_id: "bad" }, { ...receipt, purpose: "other" }, { ...receipt, ok: false },
    { ...receipt, browser_cookies_cleared: false }]) {
    await h.choose(base({ messages: [message(1, "tool", JSON.stringify(data), { tool_name: "clear_bound_site_cookies", status: "success" })] }));
    assert.doesNotMatch(await h.html(), /class="proposal-notice"/);
  }
  await h.choose(base({ messages: [message(1, "tool", JSON.stringify(receipt), { tool_name: "clear_bound_site_cookies", status: "running" })] }));
  assert.doesNotMatch(await h.html(), /class="proposal-notice"/);

  // Target confirmation explains existing restore authority without adding another grant or checkbox.
  for (const credential_mode of ["manual", "bitwarden", "cookie", "none"]) {
    await h.choose(state(prompt({ name: "Target", base_url: "https://example.com", credential_mode, workflow_site_id: 7, site_id: 8 }, { purpose: "target" })));
    html = section(await h.html());
    for (const label of ["首次导航前自动恢复", "精确主机 Cookie", "所有凭据模式", "优先使用工作流绑定站点", "否则使用凭据绑定站点",
      "不会按主机查找", "绑定失效也不会回退", "不授权使用账号、密码或 OTP", "不会自动导出浏览器 Cookie"]) assert.ok(html.includes(label), label);
    assert.doesNotMatch(html, /type="checkbox"/);
  }
  await h.choose(state(prompt()));
  const failing = h.ui.respondInteraction("approve");
  h.last().reject(new ApiError(409, "RAW_COOKIE_SECRET", "cookie_clear_binding_changed"));
  await failing;
  assert.match(h.ui.error.value, /范围或绑定已改变.*原批准不可复用/);
  assert.doesNotMatch(h.ui.error.value, /RAW_COOKIE_SECRET/);
  console.log("Assistant cookie clear regression passed");
} finally { await h.close(); }
