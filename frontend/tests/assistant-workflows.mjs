import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { readFile } from "node:fs/promises";
import { harness, base, message, target } from "./helpers/assistant-harness.mjs";

const h = await harness("detail, selectSession, workflowResult, siteConfigResult, toolResult, isProposal");
const { ui, types, respond, choose, emit, requests } = h;
const source = await readFile(new URL("../src/views/AIAssistantView.vue", import.meta.url), "utf8");
const tool = (name, result, fields = {}) => message(1, "tool", JSON.stringify(result), { tool_name: name, status: "success", ...fields });
try {
  await respond({ items: [] }); await choose();
  for (const fields of [{ workflow_site_id: null }, { workflow_site_id: 12 }, { workflow_consent: false }, { workflow_consent: true },
    ...types.assistantRunLanePolicies.map(run_lane_policy => ({ run_lane_policy }))]) {
    assert.equal(types.isAssistantDetail(base({ target: { ...target, ...fields } })), true);
    emit(base({ target: { ...target, ...fields } })); assert.deepEqual(ui.detail.value.target, { ...target, ...fields });
  }
  for (const fields of [{ workflow_site_id: "12" }, { workflow_site_id: 0 }, { workflow_consent: "true" }, { run_lane_policy: "direct" },
    { _workflow_binding: "PRIVATE" }]) {
    const previous = ui.detail.value; emit(base({ target: { ...target, ...fields } })); assert.equal(ui.detail.value, previous);
  }
  const imported = { ok: true, site_id: 12, name: '<script>alert("x")</script>', history_key: randomUUID(), config_revision: 2, lane_policy: "general", operation: "updated" };
  const run = { ok: true, run_id: 42, status: "queued", lane: "general", trigger: "assistant", created_by: "assistant" };
  const before = requests.length;
  for (const operation of ["created", "updated"]) {
    const value = tool("import_workflow_draft", { ...imported, operation });
    assert.deepEqual(ui.workflowResult(value), { kind: "import", site_id: 12, name: imported.name, operation });
    assert.deepEqual(JSON.parse(ui.toolResult(value)), { ...imported, operation }); assert.equal(ui.isProposal(value), false);
  }
  assert.deepEqual(ui.workflowResult(tool("run_site", run)), { kind: "run", run_id: 42, status: "queued", lane: "general" });
  assert.equal(ui.isProposal(tool("submit_workflow_draft", { ok: true, submitted: true, proposal: {} })), true);
  for (const value of [tool("run_site", { ...run, ok: false }), tool("run_site", { ...run, run_id: "42" }), tool("run_site", { ...run, lane: "auto" }),
    tool("run_site", { ...run, status: {} }), tool("run_site", run, { role: "assistant" }), tool("run_site", run, { status: "running" }),
    tool("run_site", run, { status: "error" }), tool("run_site", run, { content: "null" }), tool("import_workflow_draft", { ...imported, site_id: 0 }),
    tool("import_workflow_draft", { ...imported, config_revision: "2" }), tool("import_workflow_draft", { ...imported, operation: "deleted" })]) {
    assert.equal(ui.workflowResult(value), null, "invalid tool evidence cannot form a link");
  }
  emit(base({ messages: [tool("import_workflow_draft", imported), { ...tool("run_site", run), id: 2 }] }));
  const html = await h.html();
  assert.match(html, /&lt;script&gt;alert/); assert.doesNotMatch(html, /<script>alert/);
  assert.match(html, /已触发完整测试任务，创建人：助手/); assert.match(source, /path: '\/runs', query: \{ run: String\(result.run_id\) \}/);
  assert.match(source, /<router-link to="\/sites">/);
  assert.equal(requests.length, before, "rendering workflow evidence never saves or runs anything");
  const updated = { ok: true, purpose: "site_config", operation_id: randomUUID(), persisted: true,
    site_id: 12, name: imported.name, history_key: randomUUID(), config_revision: 3,
    changed_fields: ["enabled", "lane_policy", "trusted_urls"] };
  assert.deepEqual(ui.siteConfigResult(tool("update_site_config", updated)), { site_id: 12, name: imported.name,
    fields: ["启用站点", "运行车道", "可信 URL 正则（仅本站点）"] });
  const changedFields = ["name", "enabled", "lane_policy", "trusted_urls", "support_resign", "resign_max_days",
    "checkin_after", "timezone", "tags", "notes", "observation.proxy", "observation.trusted_origins"];
  assert.equal(ui.siteConfigResult(tool("update_site_config", { ...updated, changed_fields: changedFields })).fields.length, 12);
  for (const value of [tool("update_site_config", updated, { role: "assistant" }), tool("update_site_config", updated, { status: "running" }),
    tool("update_site_config", updated, { status: "error" }), tool("run_site", updated),
    ...[{ ok: false }, { purpose: "target" }, { operation_id: "forged" }, { persisted: false },
      { site_id: "12" }, { site_id: -1 }, { config_revision: 0 }, { history_key: "-".repeat(36) },
      { changed_fields: ["password"] }, { changed_fields: [] }, { changed_fields: ["enabled", "enabled"] },
      { changed_fields: [...changedFields, "name"] },
      ...["base_url", "steps", "groups", "signals", "template_id", "credential_provider_id", "bitwarden_enabled", "bitwarden_match_mode", "bitwarden_custom_match", "proxy", "trusted_origins"]
        .map(field => ({ changed_fields: [field] })),
      { name: {} }].map(fields => tool("update_site_config", { ...updated, ...fields }))]) {
    assert.equal(ui.siteConfigResult(value), null, "titles and malformed tool results cannot forge a site update notice");
  }
  await choose(base({ messages: [tool("update_site_config", updated, { title: "FORGED TITLE: all URLs authorized" })] }));
  const updatedHtml = await h.html();
  assert.match(updatedHtml, /已更新站点[\s\S]*?「&lt;script&gt;alert/); assert.match(updatedHtml, /变更字段：启用站点、运行车道、可信 URL 正则（仅本站点）/);
  assert.doesNotMatch(updatedHtml, /<script>alert/);
  const updateNotice = updatedHtml.match(/<p[^>]*class="proposal-notice"[^>]*>[\s\S]*?<\/p>/)?.[0];
  assert.ok(updateNotice); assert.doesNotMatch(updateNotice, /FORGED TITLE/);
  const { name: _name, changed_fields: _fields, ...receipt } = updated;
  assert.deepEqual(ui.siteConfigResult(tool("update_site_config", receipt)), { site_id: 12, fields: [] }, "current backend receipt needs no invented title or field list");
  await choose(base({ messages: [tool("update_site_config", receipt, { title: "FORGED TITLE" })] }));
  const receiptHtml = await h.html();
  const receiptNotice = receiptHtml.match(/<p[^>]*class="proposal-notice"[^>]*>[\s\S]*?<\/p>/)?.[0];
  assert.ok(receiptNotice); assert.match(receiptNotice, /已更新站点[\s\S]*?#12/);
  assert.doesNotMatch(receiptNotice, /FORGED TITLE|变更字段|undefined/);
  assert.doesNotMatch(source, /targetForm|workflowCheckbox|\/sites\?view=options|v-html|innerHTML/);
  assert.match(source, /提案本身不保存，授权后的工具可实际保存配置、触发任务/);
  console.log("Assistant workflow regression passed");
} finally { await h.close(); }
