import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { harness, base, message } from "./helpers/assistant-harness.mjs";

const h = await harness("detail, interactionValid, respondInteraction, selectSession");
const xss = '<img src=x onerror="bad()">';
const prompt = (purpose, review, fields = {}) => ({ id: randomUUID(), kind: "authorization", purpose, title: "核对变更", description: "只批准所列范围", expires_at: "2026-11-07T12:03:00Z", review, ...fields });
const state = pending_interaction => base({ status: "running", pending_interaction });
const section = html => html.match(/<section[^>]*aria-label="授权核对内容"[^>]*>[\s\S]*?<\/section>/)?.[0];
const change = (field, before, after) => ({ field, label: "HOST LABEL", before, after });
const template = { source_site: { id: 7, name: `来源 ${xss}` }, destination: { id: 9, name: `模板 ${xss}`, operation: "update" },
  parent: { id: 3, name: "父模板" }, counts: { steps: 10, groups: 2, signals: 4 }, warnings: [`替换本地数组 ${xss}`] };
const scheduler = { scope: "global_scheduler", timezone: "Asia/Shanghai", revision: 4, changes: [
  change("enabled", false, true), change("mode", "interval", "random"), change("cron", "", "0 8 * * *"),
  change("interval_seconds", 3600, 7200), change("ranges", [], ["08:00-09:00", "18:00-19:00"]),
], warnings: ["影响所有站点"] };
const cancel = { run_id: 42, status: "running", site_count: 8, warnings: ["业务操作不可撤销"] };
try {
  await h.respond({ items: [] });
  for (const [purpose, review, expected] of [
    ["template_import", template, ["来源站点", "目标模板", "更新已有模板", "父模板", "步骤：10", "分组：2", "信号：4", "不复制站点配置", "不会运行任务", "无详细基线", "仅有范围摘要"]],
    ["scheduler_config", scheduler, ["全局调度器", "Asia/Shanghai", "当前修订", "变更前", "变更后", "启用全局调度", "Cron 表达式", "间隔秒数", "随机时间范围", "3600", "7200", "08:00-09:00", "18:00-19:00"]],
    ["run_cancel", cancel, ["目标任务", "#42", "运行中", "涉及站点数", "8", "请求取消任务", "不代表任务已停止", "不会撤销"]],
  ]) {
    const pending = prompt(purpose, review);
    assert.equal(h.types.isAssistantPendingInteraction(pending), true);
    assert.equal(h.types.isAssistantDetail(state(pending)), true);
    await h.choose(state(pending)); assert.equal(h.ui.interactionValid.value, true);
    const before = h.requests.length;
    const html = section(await h.html()); assert.ok(html);
    for (const label of expected) assert.ok(html.includes(label), label);
    assert.doesNotMatch(html, /<img|<pre|\[object Object\]|HOST LABEL|&quot;changes&quot;|source_site|site_count/);
    assert.equal(h.requests.length, before, "rendering must never mutate or start a task");
    for (const malformed of [null, {}, { ...review, warnings: undefined }, { ...review, warnings: [{}] }, { ...review, private_data: "PRIVATE" }]) {
      assert.equal(h.types.isAssistantPendingInteraction(prompt(purpose, malformed)), false);
    }
    assert.equal(h.types.isAssistantPendingInteraction(prompt(purpose, review, { kind: "input", fields: [{ id: "a", label: "A", required: true }] })), false);
    const approving = h.ui.respondInteraction("approve"); assert.deepEqual(JSON.parse(h.last().body), { decision: "approve" });
    const settled = state(null); await h.respond(settled); await h.respond(settled); await approving;
  }
  for (const review of [
    { ...template, destination: { name: "Missing ID", operation: "update" } }, { ...template, parent: { id: 0, name: "Bad" } },
    { ...template, counts: { steps: -1, groups: 2, signals: 3 } }, { ...template, counts: { steps: 1, groups: 2, signals: 3, secret: 4 } },
    { ...template, source_site: { id: 7, name: "Source", credentials: "PRIVATE" } }, { ...template, steps: [{ private: true }] },
  ]) assert.equal(h.types.isAssistantPendingInteraction(prompt("template_import", review)), false);
  for (const review of [
    { ...scheduler, scope: "site" }, { ...scheduler, timezone: "Invalid/Zone" }, { ...scheduler, revision: "4" },
    { ...scheduler, changes: [] }, { ...scheduler, changes: [scheduler.changes[0], scheduler.changes[0]] },
    ...[change("enabled", false, "true"), change("interval_seconds", 3600, 59), change("cron", "", {}), change("mode", "cron", "invalid"),
      change("ranges", [], ["08:00-09:00", {}]), change("password", "", "PRIVATE")].map(item => ({ ...scheduler, changes: [item] })),
  ]) assert.equal(h.types.isAssistantPendingInteraction(prompt("scheduler_config", review)), false);
  for (const review of [{ ...cancel, run_id: 0 }, { ...cancel, status: {} }, { ...cancel, site_count: -1 }, { ...cancel, jobs: [] }]) {
    assert.equal(h.types.isAssistantPendingInteraction(prompt("run_cancel", review)), false);
  }
  await h.choose(state(prompt("template_import", { ...template, destination: { name: "New", operation: "create" }, parent: null })));
  assert.match(section(await h.html()), /创建模板/); assert.match(section(await h.html()), /无父模板/);
  const hostTemplate = { source_site_id: 7, destination_template_id: 9, name: "Host template", description: "Public", enabled: true,
    parent_id: 3, steps: [{ step_key: "open", feature_type: "flow.open", config: { url: "https://example.com" } }], groups: [], signals: [], warnings: ["Local arrays are replaced"] };
  const hostCancel = { run_id: 42, status: "running", mode: "parallel", trigger: "manual", created_at: "2026-11-07T12:00:00+00:00", jobs: [
    { id: 12, site_id: 7, site_name: xss, status: "running" }, { id: 13, site_id: null, site_name: "Deleted", status: "canceled" },
  ], jobs_truncated: true };
  for (const [purpose, review] of [["template_import", hostTemplate], ["run_cancel", hostCancel]]) {
    assert.equal(h.types.isAssistantPendingInteraction(prompt(purpose, review)), true, "actual backend payload remains readable");
    await h.choose(state(prompt(purpose, review))); const html = section(await h.html());
    // Native details are lazy: raw candidate/config text is absent until opened.
    assert.doesNotMatch(html, /<pre|<img|\[object Object\]|&quot;config&quot;/);
    if (purpose === "template_import") {
      assert.match(html, /无详细基线/);
      assert.match(html, /完整本地候选/);
      assert.doesNotMatch(html, /<summary>本地工作流差异/);
    }
    for (const invalid of [{ ...review, extra: true }, { ...review, run_id: "42", source_site_id: "7" }]) {
      assert.equal(h.types.isAssistantPendingInteraction(prompt(purpose, invalid)), false);
    }
  }
  assert.match(section(await h.html()), /取消范围是整个指定任务/);
  assert.match(section(await h.html()), /无法确定总数/);
  assert.equal(h.types.isAssistantPendingInteraction(prompt("template_import", { ...hostTemplate, steps: [{ step_key: "open", config: { password: "PRIVATE" } }] })), false);
  assert.equal(h.types.isAssistantPendingInteraction(prompt("template_import", { ...hostTemplate, steps: [{
    step_key: "login", feature_type: "auth.form_login", config: { username_selector: ["#username"], password_selector: ["#password"], submit_selector: ["#submit"] },
  }] })), true, "login selectors are configuration metadata, not credential values");
  assert.equal(h.types.isAssistantPendingInteraction(prompt("run_cancel", { ...hostCancel, jobs: [{ ...hostCancel.jobs[0], private: "PRIVATE" }] })), false);
  for (const [name, receipt, expected] of [
    ["create_template_from_site", { ok: true, purpose: "template_import", persisted: true, template_id: 9, name: xss, operation: "updated", version: 3 }, "仅导入工作流；未运行任务"],
    ["update_scheduler_settings", { ok: true, purpose: "scheduler_config", persisted: true, scope: "global_scheduler", revision: 8 }, "已保存全局调度变更"],
    ["cancel_run", { ok: true, run_id: 42, cancel_requested: true, finished: false }, "不代表任务已停止"],
  ]) {
    await h.choose(base({ messages: [message(1, "tool", JSON.stringify(receipt), { tool_name: name, status: "success" })] }));
    const html = await h.html(); assert.ok(html.includes(expected)); assert.doesNotMatch(html, /<img/);
    await h.choose(base({ messages: [message(1, "tool", JSON.stringify({ ...receipt, ok: false }), { tool_name: name, status: "success" })] }));
    assert.doesNotMatch(await h.html(), /class="proposal-notice"/);
  }
  console.log("Assistant operation reviews regression passed");
} finally { await h.close(); }
