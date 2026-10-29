import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { createServer } from "../../../frontend/node_modules/vite/dist/node/index.js";

const root = new URL("../../../frontend/", import.meta.url).pathname;
const server = await createServer({ root, optimizeDeps: { noDiscovery: true, include: [] }, server: { middlewareMode: true } });
try {
  const { missingRequiredFields } = await server.ssrLoadModule("/src/utils/schema.ts");
  const schema = { required: ["service_hint"], properties: {
    totp_provider_id: { type: "string", default: "builtin.twofauth" },
    service_hint: { type: "string" },
  } };
  assert.deepEqual(missingRequiredFields(schema, {}, null), ["service_hint"]);
  assert.deepEqual(missingRequiredFields(schema, { totp_provider_id: "plugin.demo" }, null), ["service_hint"]);
  assert.deepEqual(missingRequiredFields(schema, {}, { totp_provider_id: "plugin.demo" }), ["service_hint"]);
  assert.deepEqual(missingRequiredFields(schema, { service_hint: "legacy" }, null), []);
  const source = await readFile(root + "src/views/SitesView.vue", "utf8");
  assert.match(source, /credential_provider_id: detail.credential_provider_id/);
  assert.match(source, /credential_provider_id: importText\(entry.item.credential_provider_id\)/);
  assert.match(source, /credential_provider_id: form.credential_provider_id/);
  assert.doesNotMatch(source, /label="Credential provider"/);
  assert.match(source, /pluginCredentialProviders.value/);
  assert.match(source, /@update:model-value="setExternalProvider"/);
  assert.match(source, /v-model="form.bitwarden_match_mode"/);
  const selector = await readFile(root + "src/components/SecretProviderSelect.vue", "utf8");
  assert.match(selector, /\/secret-providers/);
  assert.match(selector, /current \/ unavailable/);
  console.log("Provider tabs and universal required 2FA regression passed");
} finally {
  await server.close();
}
