import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { readFile } from "node:fs/promises";
import { harness, base, id, message } from "./helpers/assistant-harness.mjs";

const h = await harness("detail, interactionValid, canUseBrowser, canScreenshot, screenshotPreview, selectSession, respondInteraction, takeScreenshot, toolResult, toggleMessage");
const { ui, types, requests, choose, respond, emit } = h;
const dates = Array.from({ length: 365 }, (_, index) => new Date(Date.UTC(2025, 10, 7 + index)).toISOString().slice(0, 10));
const purchase = { feature_type: "post.buy_card", step_key: "buy", label: "购买补签卡",
  purchase_quantity: 0, maximum_quantity: 365, max_price: 7, maximum_spend: 2555 };
const resign = feature => ({ feature_type: feature, step_key: feature.replaceAll(".", "-"), label: "补签" });
const xss = '<img src=x onerror="alert(1)">';
const review = { name: `Example ${xss}`, base_url: "https://example.com/?a=1&b=2", site_id: 12,
  steps: [purchase, ...["post.resign", "post.resign_click", "post.resign_dialog"].map(resign)],
  dates, date_count: dates.length, timezone: "Asia/Shanghai", today: "2026-11-07",
  warnings: ["仅授权本次完整运行；不会开启自动重试。", "所列为配置上限，不是网站实时报价。", `条件步骤也列入审阅 ${xss}`] };
const prompt = (data = review, fields = {}) => ({ id: randomUUID(), kind: "authorization", purpose: "consumption",
  title: "确认本次购卡与补签消费", description: "请核对全部日期、数量和费用上限。", expires_at: "2026-11-07T12:03:00Z",
  options: [], fields: [], multiple: false, review: data, ...fields });
const state = pending_interaction => base({ status: "running", pending_interaction });
const section = html => html.match(/<section[^>]*aria-label="授权核对内容"[^>]*>[\s\S]*?<\/section>/)?.[0];
try {
  await respond({ items: [] });
  const first = prompt();
  assert.equal(types.isAssistantPendingInteraction(first), true);
  assert.equal(types.isAssistantDetail(state(first)), true);
  await choose(state(first));
  assert.equal(ui.interactionValid.value, true);
  let html = section(await h.html()); assert.ok(html);
  for (const label of ["网站名称", "网站地址", "运行站点", "Asia/Shanghai", "2026-11-07", "购买补签卡", "历史日历补签", "点击补签", "弹窗补签",
    "购买数量策略", "配置数量 0", "最多购买", "365 张", "单张价格上限", "7（网站积分或计价单位）", "本步骤总消费上限", "2555",
    "全部批准补签日期（365 天）", "改变网站状态", "消耗积分、费用或补签卡", "一次性许可", "不可复用", "全局默认授权也不能跳过", "不是网站实时报价"]) assert.ok(html.includes(label), label);
  assert.equal((html.match(/<time /g) ?? []).length, 365);
  for (const day of dates) assert.ok(html.includes(`>${day}</time>`), `date never omitted: ${day}`);
  assert.match(html, /https:\/\/example.com\/\?a=1&amp;b=2/);
  assert.match(html, /&lt;img src=x onerror=&quot;alert\(1\)&quot;&gt;/);
  assert.match(html, /role="region" aria-label="全部批准补签日期" tabindex="0"/);
  assert.doesNotMatch(html, /<pre|<a\b|href=|<img|\[object Object\]|maximum_spend|purchase_quantity|feature_type|&quot;steps&quot;/);
  const approving = ui.respondInteraction("approve");
  assert.equal(h.last().url, `/assistant/sessions/${id}/interactions/${first.id}/response`);
  assert.deepEqual(JSON.parse(h.last().body), { decision: "approve" });
  await respond(state(null)); await respond(state(null)); await approving;
  emit(state(first)); assert.equal(ui.detail.value.pending_interaction, null, "one-use decision cannot reopen via replay");

  const fixed = { ...review, dates: [], date_count: 0, steps: [{ ...purchase, purchase_quantity: 2, maximum_quantity: 2, maximum_spend: 14 }] };
  await choose(state(prompt(fixed)));
  html = section(await h.html());
  assert.match(html, /固定购买 2 张，即使没有待补签日期也会购买/);
  assert.match(html, /无待补签日期（0 天）/);
  const denying = ui.respondInteraction("deny");
  assert.deepEqual(JSON.parse(h.last().body), { decision: "deny" });
  await respond(state(null)); await respond(state(null)); await denying;
  for (const feature of ["post.resign", "post.resign_click", "post.resign_dialog"]) {
    assert.equal(types.isAssistantPendingInteraction(prompt({ ...fixed, steps: [resign(feature)] })), true, "no purchase limits required on resign-only steps");
  }
  assert.equal(types.isAssistantPendingInteraction(prompt({ ...fixed, steps: [{ ...purchase, maximum_quantity: 0, maximum_spend: 0 }] })), true, "automatic purchase can have a zero bound");

  // Full-size public scopes bypass only the generic review's 50-item/500-node limits.
  const maximum = { ...review, steps: Array.from({ length: 365 }, (_, index) => ({ ...purchase, step_key: `buy-${index}`, label: "购".repeat(128) })),
    warnings: Array(3).fill("提".repeat(4096)) };
  assert.ok(Buffer.byteLength(JSON.stringify(state(prompt(maximum)))) > 200000);
  assert.equal(types.isAssistantDetail(state(prompt(maximum))), true, "large complete snapshot is accepted without partial review");
  await choose(state(prompt(maximum)));
  html = section(await h.html());
  assert.equal((html.match(/class="review-change"/g) ?? []).length, 365);
  assert.equal((html.match(/<time /g) ?? []).length, 365);
  assert.equal(ui.interactionValid.value, true);

  const bad = [null, {}, ...["name", "base_url", "site_id", "steps", "dates", "date_count", "timezone", "today", "warnings"].map(key => ({ ...review, [key]: undefined })),
    { ...review, name: " " }, { ...review, base_url: "javascript:alert(1)" }, { ...review, base_url: "https://user:secret@example.com/" },
    { ...review, timezone: "invalid/zone" }, { ...review, today: "2026-02-30" }, { ...review, today: "today" },
    { ...review, date_count: 364 }, { ...review, date_count: "365" }, { ...review, dates: [...dates, "2024-01-01"], date_count: 366 },
    { ...review, dates: [dates[0], dates[0]], date_count: 2 }, { ...review, dates: ["2026-02-30"], date_count: 1 },
    { ...review, dates: [review.today], date_count: 1 }, { ...review, dates: ["2026-11-08"], date_count: 1 },
    { ...review, dates: [{ code: "PRIVATE" }], date_count: 1 }, { ...review, warnings: [] }, { ...review, warnings: [""] },
    { ...review, warnings: ["a", "b", "c", "d"] }, { ...review, warnings: [{ solver: "PRIVATE" }] }, { ...review, warnings: ["a".repeat(4097)] },
    { ...review, steps: [] }, { ...maximum, steps: [...maximum.steps, { ...purchase, step_key: "extra" }] },
    { ...review, steps: [purchase, purchase] }, { ...review, steps: [{ ...purchase, feature_type: "action.click" }] },
    { ...review, steps: [{ ...purchase, step_key: "__proto__" }] }, { ...review, steps: [{ ...purchase, label: " " }] },
    ...["purchase_quantity", "maximum_quantity", "max_price", "maximum_spend"].flatMap(key => [undefined, -1, "7", NaN, Infinity, 1.5, true, Number.MAX_SAFE_INTEGER + 1].map(value =>
      ({ ...review, steps: [{ ...purchase, [key]: value }] }))),
    { ...review, steps: [{ ...purchase, max_price: 0 }] }, { ...review, steps: [{ ...purchase, purchase_quantity: 366 }] },
    { ...review, steps: [{ ...purchase, maximum_quantity: 364 }] }, { ...review, steps: [{ ...purchase, maximum_spend: 1 }] },
    { ...review, steps: [{ ...purchase, purchase_quantity: 2 }] },
    ...["permit", "run_plan_id", "binding", "fingerprint", "private_runtime", "solver", "cookie"].map(key => ({ ...review, [key]: "PRIVATE" })),
    ...["config", "solver", "secret"].map(key => ({ ...review, steps: [{ ...purchase, [key]: "PRIVATE" }] })),
    { ...review, steps: [{ ...resign("post.resign"), maximum_spend: 1 }] }];
  for (const data of bad) {
    assert.equal(types.isAssistantPendingInteraction(prompt(data)), false, JSON.stringify(data));
    assert.equal(types.isAssistantDetail(state(prompt(data))), false);
  }
  for (const fields of [{ kind: "choice", options: [{ id: "a", label: "A" }] }, { kind: "input", fields: [{ id: "a", label: "A", required: true }] }, { multiple: true }]) {
    assert.equal(types.isAssistantPendingInteraction(prompt(review, fields)), false);
  }
  assert.equal(types.isAssistantPendingInteraction(prompt({ list: Array(51).fill("x") }, { purpose: "question" })), false, "generic 50-item cap unchanged");
  assert.equal(types.isAssistantPendingInteraction(prompt({ dates }, { purpose: "question" })), false, "365-item exception only for consumption");

  // Revalidate immediately before approval even if local state is changed after publication.
  await choose(state(prompt()));
  ui.detail.value.pending_interaction.review.steps[0].max_price = undefined;
  assert.equal(ui.interactionValid.value, false);
  const before = requests.length; await ui.respondInteraction("approve"); assert.equal(requests.length, before);
  html = await h.html(); assert.match(html, /消费核对内容格式无效/);
  assert.match(html, /<button\b[^>]*\bdisabled[^>]*>确认并继续<\/button>/);
  assert.doesNotMatch(section(html), /maximum_spend|2555|PRIVATE/);
  const rejectInvalid = ui.respondInteraction("deny");
  assert.deepEqual(JSON.parse(h.last().body), { decision: "deny" }, "invalid scope can still be denied using the existing endpoint");
  await respond(state(null)); await respond(state(null)); await rejectInvalid;

  // These fixtures are backend-projected tool JSON, not raw worker/provider responses.
  for (const feature of ["challenge.2fa", "challenge.turnstile", "challenge.image_captcha", "challenge.grid_captcha", "sign.iframe.image_captcha", "sign.image_quiz", "flow.result"]) {
    const projected = { ok: true, operation_id: randomUUID(), feature_type: feature, status: "success", code: "step_success",
      before: { snapshot_id: "before-safe", url: "https://example.com/", elements: [{ ref: "n1", tag: "input", selectors: ["#code"] }] },
      after: { snapshot_id: "after-safe", url: "https://example.com/", elements: [{ ref: "n2", tag: "button", text: `Continue ${xss}`, selectors: ["#continue"] }] },
      result: feature === "flow.result" ? { value: false } : { submitted: true, verified: true } };
    for (const tool_name of ["execute_step", "retry_step"]) {
      const toolMessage = message(1, "tool", JSON.stringify(projected), { tool_name, status: "success" });
      assert.deepEqual(JSON.parse(ui.toolResult(toolMessage)), projected, "complete curated result stays visible for both execution tools");
      await choose(base({ messages: [toolMessage] }));
      assert.doesNotMatch(await h.html(), /<pre|before-safe|after-safe/, "tool history starts collapsed without rendering its JSON");
      ui.toggleMessage(toolMessage.id, { currentTarget: { open: true } });
      html = await h.html();
      assert.match(html, /before-safe/); assert.match(html, /after-safe/);
      assert.match(html, /#code/); assert.match(html, /#continue/);
      assert.match(html, /&lt;img src=x onerror=\\&quot;alert\(1\)\\&quot;&gt;/);
      assert.doesNotMatch(html, /<img|私有执行|仅展示状态|关闭并重新打开浏览器|solver|raw_response|provider_headers|input_value/);
      if (feature === "flow.result") assert.match(html, /&quot;value&quot;: false/);
      assert.equal(ui.canUseBrowser.value, true, "private-feature history does not restrict further browser use");
      assert.equal(ui.canScreenshot.value, true);
      assert.match(html, /<button(?![^>]*disabled)[^>]*>截图预览<\/button>/);
    }
  }
  const screenshot = { mime_type: "image/jpeg", image_base64: btoa("\xff\xd8\xff\xe0\xff\xd9"), width: 800, height: 600, masked: true };
  const capture = ui.takeScreenshot();
  assert.equal(h.last().url, `/assistant/sessions/${id}/browser/screenshot`);
  await respond(screenshot); await capture;
  assert.equal(ui.screenshotPreview.value.src, `data:image/jpeg;base64,${screenshot.image_base64}`);
  assert.equal(types.isAssistantScreenshot({ ...screenshot, masked: false }), false);
  const metadata = { ok: true, mime_type: screenshot.mime_type, width: 800, height: 600, masked: true, image_id: randomUUID(), url_redacted: false };
  const imageMessage = message(2, "tool", JSON.stringify({ ...screenshot, ...metadata, image_url: "https://example.com/model-only", token: "PRIVATE" }), { tool_name: "browser_screenshot" });
  assert.deepEqual(JSON.parse(ui.toolResult(imageMessage)), metadata, "screenshot logs still show only safe metadata");
  emit({ ...ui.detail.value, target: { ...ui.detail.value.target, screenshot_consent: false } });
  assert.equal(ui.canScreenshot.value, false);
  const noConsentCount = requests.length; await ui.takeScreenshot(); assert.equal(requests.length, noConsentCount);
  assert.equal(types.isAssistantDetail(base({ target: { ...base().target, execution_features: [...types.assistantExecutionFeatures] } })), true, "current backend execution feature contract remains readable");
  const component = await readFile(new URL("../src/components/AssistantInteractionReview.vue", import.meta.url), "utf8");
  assert.match(component, /\.review-scope \{[^}]*max-height: 240px; overflow: auto/);
  assert.doesNotMatch(component, /v-html|innerHTML|JSON.stringify|text-overflow|line-clamp|\.slice\(/);
  assert.ok(requests.every(request => !request.url.endsWith("/target") && !request.url.endsWith("/settings") && !request.url.endsWith("/messages")));
  console.log("Assistant consumption review regression passed");
} finally { await h.close(); }
