import assert from "node:assert/strict";
import { harness, base, message } from "./helpers/assistant-harness.mjs";

// Independent of the frontend allowlist: the 23 builtins returned by takeover's
// _target -> grant projection, including workflow-only and consumption features.
const builtins = [
  "action.click", "action.fill", "action.navigate", "action.wait",
  "auth.form_login", "auth.password_verify", "challenge.2fa", "challenge.altcha",
  "challenge.grid_captcha", "challenge.image_captcha", "challenge.slider", "challenge.turnstile",
  "flow.jump", "flow.open", "flow.result", "post.buy_card", "post.resign", "post.resign_dialog",
  "post.resign_loop", "sign.button", "sign.iframe.image_captcha", "sign.image_quiz", "sign.unit3d_claim",
];
const title = "\u5931\u8d25\u63a5\u7ba1\uff1a\u7ad9\u70b9 \u5929\u96ea 2026-11-24";
const takeover = base({ title, browser: null,
  target: { name: "\u5929\u96ea", base_url: "https://example.com/", credential_mode: "manual",
    confirmed: true, authorized: true, proxy: false, site_id: 7, workflow_site_id: 7,
    run_lane_policy: "general", workflow_consent: false, screenshot_consent: true,
    execution_consent: true, credential_consent: true, allow_sign: true,
    trusted_origins: [], execution_features: builtins },
  messages: [message(1, "user", "Investigate the failed check-in"), message(2, "assistant", "Takeover investigation recorded")],
});
const withTarget = fields => ({ ...takeover, target: { ...takeover.target, ...fields } });
const h = await harness("detail, error, connection, canUseBrowser, canScreenshot, selectSession");
const { ui, types } = h;
try {
  await h.respond({ items: [] });
  assert.equal(types.isAssistantDetail(takeover), true, "the complete takeover response must not be rejected as an invalid HTTP 200 snapshot");
  assert.deepEqual([...types.assistantExecutionFeatures].sort(), builtins);
  for (const credential_mode of ["manual", "bitwarden", "site", "cookie", "none"]) {
    assert.equal(types.isAssistantDetail(withTarget({ credential_mode })), true);
  }

  // Exercise the real view's HTTP publication and rendering, not just the guard.
  await h.choose(takeover);
  assert.equal(h.last().url, `/assistant/sessions/${takeover.id}`);
  assert.equal(ui.error.value, "");
  assert.equal(ui.detail.value.title, title);
  assert.deepEqual(ui.detail.value.messages, takeover.messages);
  let html = await h.html();
  assert.ok(html.includes(title));
  assert.match(html, /Takeover investigation recorded/);
  assert.equal(ui.canUseBrowser.value, false, "reading retained grants never opens a browser");
  assert.equal(ui.canScreenshot.value, false);

  const plugins = [...builtins, "plugin.example.work", "plugin.vault-provider.sign.check_in"];
  const site = withTarget({ credential_mode: "site", execution_features: plugins });
  await h.choose(site);
  assert.equal(ui.error.value, "");
  assert.equal(ui.detail.value.target.credential_mode, "site");
  assert.deepEqual(ui.detail.value.target.execution_features, plugins);
  h.emit({ ...site, messages: [...site.messages, message(3, "assistant", "Live takeover update")] });
  assert.equal(ui.connection.value, "live");
  html = await h.html();
  assert.match(html, /Live takeover update/);

  const maximum = Array.from({ length: 1024 }, (_, index) => `plugin.example.feature_${index}`);
  assert.equal(types.isAssistantDetail(withTarget({ execution_features: maximum })), true);
  assert.equal(types.isAssistantDetail(withTarget({ execution_features: [] })), true);
  assert.equal(types.isAssistantDetail(withTarget({ execution_features: undefined })), true, "legacy snapshots can omit feature metadata");
  const boundaryKey = `plugin.${"a".repeat(63)}.${"b".repeat(57)}`;
  assert.equal(boundaryKey.length, 128);
  assert.equal(types.isAssistantDetail(withTarget({ execution_features: [boundaryKey] })), true);

  const invalid = [null, true, "action.wait", {}, [true], [null], [1], [{}], [[]],
    [""], ["action.unknown"], ["unknown.work"], ["plugin.private"], ["plugin..work"],
    ["plugin.Example.work"], ["plugin.example."], ["plugin.example.bad key"],
    ["plugin.example.work\n"], ["plugin.example.\u0000work"], ["plugin.example.\u007fwork"],
    [`plugin.${"a".repeat(64)}.work`], [`${boundaryKey}b`],
    ["action.wait", "action.wait"], ["plugin.example.work", "plugin.example.work"],
    [...maximum, "plugin.example.extra"]];
  for (const execution_features of invalid) {
    const snapshot = withTarget({ execution_features });
    assert.equal(types.isAssistantDetail(snapshot), false, JSON.stringify(execution_features));
    const stable = ui.detail.value;
    h.emit(snapshot);
    assert.equal(ui.detail.value, stable, "invalid SSE metadata must not replace the published snapshot");
    await h.choose(snapshot);
    assert.equal(ui.detail.value, null, "invalid HTTP metadata must not publish partial history");
    assert.notEqual(ui.error.value, "");
    await h.choose(site);
  }
  for (const credential_mode of [undefined, null, true, 1, "plugin.example", "unknown", "Site"]) {
    assert.equal(types.isAssistantDetail(withTarget({ credential_mode })), false);
  }
  for (const fields of [{ authorized: false }, { confirmed: false }, { authorized: undefined, confirmed: undefined },
    { execution_consent: "true" }, { credential_consent: 1 }, { screenshot_consent: null }, { _credential_binding: {} }]) {
    assert.equal(types.isAssistantDetail(withTarget(fields)), false, "metadata compatibility does not loosen consent or private-field validation");
  }

  // Even a full feature list is not unified authorization or screenshot consent.
  const legacy = withTarget({ authorized: undefined, screenshot_consent: false, execution_consent: false,
    credential_consent: false, allow_sign: false, execution_features: plugins });
  legacy.browser = base().browser;
  await h.choose(legacy);
  assert.equal(ui.detail.value.target.authorized, undefined);
  assert.equal(ui.detail.value.target.execution_consent, false);
  assert.equal(ui.detail.value.target.credential_consent, false);
  assert.equal(ui.detail.value.target.allow_sign, false);
  assert.equal(ui.canScreenshot.value, false);
  assert.ok(h.requests.every(request => !request.method || request.method === "GET"), "opening HTTP/SSE history never authorizes, executes or reruns anything");
  console.log("Assistant detail contract regression passed");
} finally { await h.close(); }
