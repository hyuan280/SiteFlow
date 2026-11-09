import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { readFile } from "node:fs/promises";
import { harness, base } from "./helpers/assistant-harness.mjs";

const h = await harness("detail, selectSession, respondInteraction, interactionValid");
const { ui, types, respond, choose, requests } = h;
const pattern = String.raw`(?i)^https://auth\.example\.com/(?P<path>login|totp)(?:[?#].*)?$`;
const longPattern = `^https://example\\.com/${"a".repeat(900)}$`;
const xss = '<img src=x onerror="alert(1)">';
const change = (field, before, after) => ({ field, label: field, before, after });
const review = {
  name: `Example ${xss}`, base_url: "https://example.com/?a=1&b=2", site_id: 12,
  changes: [change("enabled", false, true), change("lane_policy", "auto", "proxy_general"),
    change("trusted_urls", { count: 2 }, [pattern, longPattern, xss]), change("checkin_after", null, "08:00"),
    change("resign_max_days", 3, 4), change("tags", [], ["public", "review"])],
  warnings: [`凭据使用范围将扩展 ${xss}`, "宽泛正则可能泄露凭据"], successful_run_id: 42,
};
const prompt = (purpose = "site_config", data = review) => ({ id: randomUUID(), kind: "authorization", purpose,
  title: "核对站点变更", description: "批准将保存所列变更，不会自动执行。", expires_at: "2026-11-07T12:03:00Z", review: data });
const state = item => base({ status: "running", pending_interaction: item });
const reviewSection = html => html.match(/<section[^>]*aria-label="授权核对内容"[^>]*>[\s\S]*?<\/section>/)?.[0];
try {
  await respond({ items: [] });
  assert.equal(types.isAssistantPendingInteraction(prompt()), true);
  const before = requests.length;
  await choose(state(prompt()));
  let html = reviewSection(await h.html()); assert.ok(html);
  for (const label of ["网站名称", "网站地址", "启用站点", "运行车道", "变更前", "变更后", "是（已启用）", "否（未启用）",
    "代理通用车道（使用已配置代理）", "自动选择", "数量：2", "补签最大天数", "未设置", "08:00", "空列表（0 项）", "public", "成功测试任务：#42"]) {
    assert.ok(html.includes(label), label);
  }
  assert.ok(html.includes(pattern.replaceAll("<", "&lt;").replaceAll(">", "&gt;"))); assert.ok(html.includes(longPattern), "long regex is never truncated or normalized");
  assert.match(html, /https:\/\/example.com\/\?a=1&amp;b=2/);
  assert.match(html, /&lt;img src=x onerror=&quot;alert\(1\)&quot;&gt;/);
  assert.match(html, /仅作用于本站点/); assert.match(html, /TOTP/); assert.match(html, /全匹配完整 HTTP\(S\) URL/);
  assert.match(html, /变更风险提示/); assert.match(html, /宽泛正则可能泄露凭据/);
  assert.doesNotMatch(html, /<img|<script|<a\b|href=|<pre|\[object Object\]|&quot;changes&quot;|step_count|successful_run_id/);
  assert.equal(requests.length, before + 1, "rendering only reads the chosen session, never saves anything");

  // Bounds fail closed; no partial diff can be approved when changes/warnings are omitted.
  const tags = Array.from({ length: 50 }, (_, index) => `public-tag-${index}`);
  const uniqueChanges = [change("name", "Old name", "New name"), change("enabled", true, false),
    change("lane_policy", "general", "proxy_general"), change("trusted_urls", "0 项（旧内容不展示）", Array(20).fill(pattern)),
    change("support_resign", false, true), change("resign_max_days", 30, 60), change("checkin_after", null, "08:00"),
    change("timezone", "UTC", "Asia/Shanghai"), change("tags", "0 项（旧内容不展示）", tags), change("notes", "", "Public notes"),
    change("observation.proxy", false, true), change("observation.trusted_origins", [], Array.from({ length: 20 }, (_, index) => `https://auth${index}.example.com`))];
  const maximum = { ...review, changes: uniqueChanges, warnings: ["核对完整凭据正则", "观察来源不授权转发凭据", "观察代理不改变保存运行车道"] };
  delete maximum.successful_run_id;
  const maximumPrompt = prompt("site_config", maximum);
  assert.equal(types.isAssistantPendingInteraction(maximumPrompt), true);
  assert.equal(types.isAssistantDetail(state(maximumPrompt)), true);
  await choose(state(maximumPrompt));
  assert.equal(ui.detail.value.pending_interaction.id, maximumPrompt.id);
  assert.equal(ui.interactionValid.value, true, "maximum backend review remains eligible for a user response");
  html = reviewSection(await h.html());
  assert.equal((html.match(/class="review-change"/g) ?? []).length, 12);
  for (const tag of tags) assert.ok(html.includes(`>${tag}</span>`), `all 50 tags remain visible: ${tag}`);
  for (const warning of maximum.warnings) assert.ok(html.includes(warning));
  assert.doesNotMatch(html, /格式无效|<pre|<a\b/);
  const approving = ui.respondInteraction("approve");
  assert.deepEqual(JSON.parse(h.last().body), { decision: "approve" });
  const settled = base({ pending_interaction: null });
  await respond(settled); await respond(settled); await approving;
  assert.equal(types.isAssistantPendingInteraction(prompt("site_config", { ...review, changes: [change("trusted_urls", 20, Array(20).fill(pattern))] })), true);
  for (const data of [{ ...review, changes: [] }, { ...maximum, changes: [...uniqueChanges, change("name", "Old", "New")] },
    { ...review, changes: [review.changes[0], review.changes[0]] }, { ...review, changes: [change("unknown", 1, 2)] },
    { ...review, changes: [change("trusted_urls", 20, Array(21).fill(pattern))] },
    { ...review, changes: [change("trusted_urls", null, ["x".repeat(1025)])] },
    { ...review, changes: [change("notes", { browser_metadata: "PRIVATE" }, "public")] },
    { ...review, changes: [{ ...review.changes[0], secret: "PRIVATE" }] },
    { ...maximum, changes: uniqueChanges.map(item => item.field === "tags" ? change("tags", [], [...tags, "extra-tag"]) : item) },
    { ...maximum, changes: [change("observation.trusted_origins", [], Array(21).fill("https://auth.example.com"))] },
    ...["base_url", "steps", "groups", "signals", "template_id", "credential_provider_id", "bitwarden_enabled", "bitwarden_match_mode", "bitwarden_custom_match", "proxy", "trusted_origins"]
      .map(field => ({ ...review, changes: [change(field, "PRIVATE_UNSUPPORTED_BEFORE", "PRIVATE_UNSUPPORTED_AFTER")] })),
    { ...review, warnings: ["a", "b", "c", "d"] }, { ...review, warnings: [{}] }, { ...review, warnings: undefined },
    { ...review, successful_run_id: "42" }, { ...review, browser_metadata: "PRIVATE" }, null]) {
    assert.equal(types.isAssistantPendingInteraction(prompt("site_config", data)), false);
    assert.equal(types.isAssistantDetail(state(prompt("site_config", data))), false, "invalid reviews cannot publish unsupported raw values");
  }
  assert.equal(types.isAssistantPendingInteraction({ ...prompt(), review: undefined }), false);
  const observation = { name: "Example", base_url: "https://example.com", changes: [
    change("observation.proxy", false, true), change("observation.trusted_origins", [], ["https://auth.example.com"]),
  ], warnings: ["观察代理不改变保存运行车道", "观察来源不授权转发凭据"] };
  assert.equal(types.isAssistantPendingInteraction(prompt("site_config", observation)), true);
  await choose(state(prompt("site_config", observation)));
  html = reviewSection(await h.html());
  assert.match(html, /观察浏览器使用代理/); assert.match(html, /观察浏览器可信来源/);
  assert.match(html, /https:\/\/auth.example.com/); assert.match(html, /不转发凭据/);
  assert.match(html, /观察代理不改变保存运行车道/); assert.doesNotMatch(html, /observation\.|<a\b|<pre/);

  // Existing standalone proxy/trust/auth reviews remain semantic and scoped.
  for (const scope of ["browser", "workflow", "both"]) {
    await choose(state(prompt("proxy", { name: "Example", base_url: "https://example.com/", scope,
      suggested_lane: "proxy_general", current_browser_lane: "interaction", current_run_lane_policy: "general",
      browser_implementation: { general: "PRIVATE browser internals" }, run_lane_policy: { general: "PRIVATE mapping" } })));
    html = reviewSection(await h.html());
    assert.match(html, /适用范围/); assert.match(html, /建议车道（非授权）/); assert.match(html, /当前观察浏览器车道/);
    assert.match(html, /当前运行车道/); assert.doesNotMatch(html, /PRIVATE|browser_implementation|\[object Object\]|<pre/);
  }
  await choose(state(prompt("trusted_origins", { base_url: "https://example.com", origins: ["https://auth.example.com", xss] })));
  html = reviewSection(await h.html()); assert.match(html, /待信任来源/); assert.match(html, /https:\/\/auth.example.com/);
  assert.match(html, /不转发凭据/); assert.match(html, /不授权完整运行/); assert.doesNotMatch(html, /<a\b|<img/);
  await choose(state(prompt("authorization", { name: "Example", base_url: "https://example.com", capability: "execution",
    credential_mode: "bitwarden", execution_features: ["action.click", "auth.form_login"], credential_consent: true, allow_sign: true })));
  html = reviewSection(await h.html());
  for (const label of ["授权能力", "真实单步执行", "凭据来源", "Bitwarden", "允许执行的功能", "点击", "表单登录", "允许使用私有凭据"]) assert.ok(html.includes(label));
  await choose(state(prompt("legacy-purpose", { name: "Legacy", base_url: "javascript:alert(1)", required: false,
    browser_metadata: "PRIVATE", payload: { nested: "PRIVATE" } })));
  html = reviewSection(await h.html()); assert.match(html, /Legacy/); assert.match(html, /javascript:alert\(1\)/);
  assert.doesNotMatch(html, /PRIVATE|browser_metadata|<a\b|href=|<pre/);
  await choose(state(prompt("authorization", { credential_mode: "constructor", feature_type: "__proto__" })));
  html = reviewSection(await h.html()); assert.match(html, /constructor/); assert.match(html, /__proto__/);
  assert.doesNotMatch(html, /function|\[object Object\]/);

  const component = await readFile(new URL("../src/components/AssistantInteractionReview.vue", import.meta.url), "utf8");
  const view = await readFile(new URL("../src/views/AIAssistantView.vue", import.meta.url), "utf8");
  assert.doesNotMatch(component, /v-html|innerHTML|JSON.stringify|text-overflow|line-clamp|href=/);
  assert.match(component, /white-space: pre-wrap; overflow-wrap: anywhere/);
  assert.match(component, /grid-template-columns: repeat\(2, minmax\(0, 1fr\)\)/);
  assert.match(component, /@media \(max-width: 720px\).*grid-template-columns: minmax\(0, 1fr\)/);
  assert.match(component, /var\(--el-bg-color/); assert.match(component, /var\(--el-text-color-primary/);
  assert.match(view, /\.inline-prompt \{[^}]*max-height: min\(38dvh, 360px\)/);
  assert.match(view, /\.prompt-body \{[^}]*overflow: auto/);
  console.log("Assistant site config review regression passed");
} finally { await h.close(); }
