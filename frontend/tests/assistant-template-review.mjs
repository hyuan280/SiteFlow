import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { createServer } from "vite";
import { createRenderer, createSSRApp, nextTick, reactive, ssrContextKey } from "vue";
import { renderToString } from "@vue/server-renderer";

const server = await createServer({ optimizeDeps: { noDiscovery: true, include: [] }, server: { middlewareMode: true, hmr: false },
  plugins: [{ name: "template-review-harness", enforce: "pre", transform(code, path) {
    if (path.endsWith("/AssistantInteractionReview.vue")) return code.replace("</script>", "defineExpose({ toggleTemplateSection, differences });\n</script>");
  } }] });
let app;
try {
  const { default: Review } = await server.ssrLoadModule("/src/components/AssistantInteractionReview.vue");
  const { templateImportReview, templateWorkflowDifferences, templateReviewValue } = await server.ssrLoadModule("/src/utils/assistant-review.ts");
  const { isAssistantPendingInteraction } = await server.ssrLoadModule("/src/types/assistant.ts");
  const xss = '<img src=x onerror="bad()"><script>bad()</script>';
  const step = (key, config = {}, position = 10) => ({ step_key: key, feature_type: "action.click", group_key: "main", position, config });
  const group = (key, position = 10) => ({ group_key: key, name: key, position, step_anchor: position });
  const signal = (key, label = key) => ({ signal_key: key, label, include_texts: ["ok"], enabled: true });
  const baseline = { parent_id: 3, steps: [step("open", { selector: "#old", nested: { b: 2, a: 1 } }), step("keep")],
    groups: [group("main")], signals: [signal("removed"), signal("stable"), signal("changed", "Old signal")] };
  const payload = { source_site_id: 7, destination_template_id: 9, name: `Template ${xss}`, description: `Description ${xss}`, enabled: false,
    parent_id: 4, steps: [step("open", { selector: `#new ${xss}`, nested: { a: 1, b: 2 } }, 20), step("inserted", { text: "ADDED-CONFIG" }, 25), step("keep", {}, 30)],
    groups: [group("main", 20), group("added", 30)], signals: [signal("stable"), signal("changed", "New signal"), signal("added")],
    before: baseline, warnings: [`References affected ${xss}`] };
  const pending = review => ({ id: randomUUID(), kind: "authorization", purpose: "template_import", title: "Review", description: "Scope",
    expires_at: "2026-11-07T12:03:00Z", review });
  assert.equal(isAssistantPendingInteraction(pending(payload)), true);
  const review = templateImportReview(payload);
  assert.deepEqual(review.candidate.steps, payload.steps, "candidate is the actual source array, never merged");
  assert.deepEqual(review.before, baseline);
  const diff = templateWorkflowDifferences(review);
  const item = (section, key) => diff.find(entry => entry.section === section && entry.key === key);
  assert.equal(item("steps", "inserted").status, "added");
  assert.equal(item("signals", "removed").status, "removed");
  assert.equal(item("signals", "stable").status, "unchanged");
  assert.equal(item("signals", "changed").status, "updated");
  assert.deepEqual(item("parent_id", "parent_id").fields, [{ field: "parent_id", before: 3, after: 4 }]);
  assert.equal(item("steps", "open").fields.find(field => field.field === "config").before.selector, "#old");
  assert.equal(diff.some(entry => entry.fields.some(field => field.field === "array_order")), false, "insertions and renumbering are not reordering");
  const reordered = templateWorkflowDifferences(templateImportReview({ ...payload, steps: [payload.steps[2], payload.steps[0], payload.steps[1]] }));
  assert.equal(reordered.filter(entry => entry.fields.some(field => field.field === "array_order")).length, 2);
  assert.equal(templateReviewValue({ b: 1, a: { d: 2, c: 3 } }, false), templateReviewValue({ a: { c: 3, d: 2 }, b: 1 }, false));

  const props = reactive({ purpose: "template_import", review: payload });
  let bindings, ui;
  const renderer = createRenderer({ createComment: () => ({}), insert() {}, remove() {}, parentNode() {}, nextSibling() {} });
  app = renderer.createApp({ setup() { bindings = Review.setup(props, { expose(value) { ui = value; } }); return () => null; } });
  app.provide(ssrContextKey, { modules: new Set() }); app.mount({});
  const html = () => renderToString(createSSRApp({ ...Review, setup: () => bindings }, props));
  const toggle = async (section, open = true) => { ui.toggleTemplateSection(section, { currentTarget: { open } }); await nextTick(); };
  const setReview = async value => { props.review = value; await nextTick(); };
  const safeHtml = rendered => {
    assert.doesNotMatch(rendered, /<img|<script|\[object Object\]/);
    assert.match(rendered, /&lt;img/);
  };
  const originalStringify = JSON.stringify;
  let prettyCalls = 0;
  JSON.stringify = function (value, replacer, space) { if (space === 2) prettyCalls++; return originalStringify(value, replacer, space); };
  try {
    let rendered = await html(); safeHtml(rendered);
    assert.match(rendered, /<details/); assert.doesNotMatch(rendered, /<pre|#new|#old|ADDED-CONFIG/);
    assert.equal(prettyCalls, 0, "closed details never pretty-print candidate or diff configs");
    for (const label of ["整体替换", "不会隐式合并", "原有效分组", "原有效步骤", "相对顺序", "position 数值相等", "所有引用它的站点"]) assert.ok(rendered.includes(label), label);
    await toggle("candidate"); rendered = await html(); safeHtml(rendered);
    for (const label of ["#new", "ADDED-CONFIG", "step_anchor", "group_key", "parent_id", "include_texts", "position", "config"]) assert.ok(rendered.includes(label), label);
    assert.doesNotMatch(rendered, /#old|Old signal/);
    const candidateCalls = prettyCalls; await html(); assert.equal(prettyCalls, candidateCalls, "candidate formatting is cached");
    await toggle("differences"); rendered = await html(); safeHtml(rendered);
    for (const label of ["旧值", "新值", "#old", "#new", "Old signal", "New signal", "新增", "移除", "更新", "未变更", "仅比较原目标模板的本地工作流"]) assert.ok(rendered.includes(label), label);
    await toggle("candidate", false); await toggle("differences", false);
    assert.doesNotMatch(await html(), /<pre|#new|#old/);
  } finally { JSON.stringify = originalStringify; }

  const unchanged = { ...payload, ...baseline, steps: [step("open", { nested: { a: 1, b: 2 }, selector: "#old" }), step("keep")] };
  await setReview(unchanged); await toggle("differences");
  assert.match(await html(), /无差异：本地工作流未变更/);
  assert.equal(ui.differences.value.every(entry => entry.status === "unchanged"), true);
  const created = { ...payload, destination_template_id: null, before: null };
  assert.equal(isAssistantPendingInteraction(pending(created)), true);
  await setReview(created); await toggle("differences");
  assert.match(await html(), /创建模板：无原目标工作流/);
  assert.equal(ui.differences.value.filter(entry => entry.section === "steps").every(entry => entry.status === "added"), true);
  const { before, ...legacy } = payload;
  assert.equal(isAssistantPendingInteraction(pending(legacy)), true, "missing before is a persisted legacy approval");
  assert.equal(templateWorkflowDifferences(templateImportReview(legacy)), null, "never invent a legacy diff");
  await setReview(legacy);
  assert.match(await html(), /无详细基线/); assert.doesNotMatch(await html(), /<summary>本地工作流差异/);
  const summary = { source_site: { id: 7, name: "Old source" }, destination: { id: 9, name: "Old destination", operation: "update" },
    parent: null, counts: { steps: 1, groups: 0, signals: 0 }, warnings: [] };
  assert.equal(isAssistantPendingInteraction(pending(summary)), true);
  await setReview(summary); assert.match(await html(), /仅有范围摘要/); assert.doesNotMatch(await html(), /<details/);

  for (const invalid of [undefined, {}, [], "before", { ...baseline, parent_id: undefined }, { ...baseline, extra: "PRIVATE" },
    { ...baseline, steps: undefined }, { ...baseline, groups: [{}] }, { ...baseline, signals: [{ signal_key: "bad", label: "Bad", extra: "PRIVATE" }] }]) {
    assert.equal(isAssistantPendingInteraction(pending({ ...payload, before: invalid })), false, `invalid explicit baseline ${String(invalid)}`);
  }
  assert.equal(templateImportReview({ ...payload, before: null }), null, "updates require an object baseline when supplied");
  assert.equal(templateImportReview({ ...created, before: baseline }), null, "creates require a null baseline when supplied");
  for (const privateKey of ["password", "cookie", "token", "secret", "credential", "authorization", "constructor", "prototype", "api_token"]) {
    for (const side of ["candidate", "before"]) {
      const invalidStep = step("private", { nested: { [privateKey]: "PRIVATE-MARKER" } });
      const invalid = side === "before" ? { ...payload, before: { ...baseline, steps: [invalidStep] } } : { ...payload, steps: [invalidStep] };
      assert.equal(isAssistantPendingInteraction(pending(invalid)), false, `${side} private key ${privateKey}`);
      await setReview(invalid); const rendered = await html();
      assert.match(rendered, /不能批准/); assert.doesNotMatch(rendered, /PRIVATE-MARKER|<details/);
    }
  }
  const metadata = { username_selector: ["#username"], password_selector: ["#password"], token_selector: ["#token"],
    username_field: "user", password_field: "pass", credential_provider_id: "builtin.manual", totp_provider_id: "builtin.totp" };
  const metadataReview = { ...payload, steps: [step("login", metadata)], before: { ...baseline, steps: [step("login", metadata)] } };
  assert.equal(isAssistantPendingInteraction(pending(metadataReview)), true, "selector/provider metadata is allowed on both sides");
  await setReview(metadataReview); await toggle("candidate"); assert.match(await html(), /#password/);
  for (const invalid of [
    { ...payload, extra: "PRIVATE" }, { ...payload, steps: [step("open"), step("open")] },
    { ...payload, steps: [step("open", { text: "x".repeat(4097) })] },
    { ...payload, before: { ...baseline, steps: Array.from({ length: 501 }, (_, i) => step(`s${i}`)) } },
  ]) assert.equal(templateImportReview(invalid), null);

  const large = { ...created, steps: Array.from({ length: 500 }, (_, i) => step(`s${i}`, { text: i === 499 ? "LAST-CONFIG-END" : "ok" })),
    groups: Array.from({ length: 500 }, (_, i) => group(`g${i}`)), signals: Array.from({ length: 500 }, (_, i) => signal(`n${i}`)) };
  assert.ok(templateImportReview(large), "500 items in each array remain supported");
  await setReview(large); assert.doesNotMatch(await html(), /LAST-CONFIG-END/);
  await toggle("candidate"); assert.match(await html(), /LAST-CONFIG-END/);
  await toggle("differences");
  assert.equal(ui.differences.value.length, 1501, "no diff entries silently truncated");
  const largeHtml = await html(); assert.match(largeHtml, /LAST-CONFIG-END/); assert.match(largeHtml, /n499/); assert.match(largeHtml, /g499/);
  console.log("Assistant complete template candidate and diff review regression passed");
} finally { app?.unmount(); await server.close(); }
