// Exercise the actual UI parser and payload expression without a browser UI.
import { readFile } from "node:fs/promises";
import assert from "node:assert/strict";
import { createServer } from "../../../frontend/node_modules/vite/dist/node/index.js";

const root = new URL("../../../frontend/", import.meta.url).pathname;
const server = await createServer({ root, optimizeDeps: { noDiscovery: true, include: [] }, server: { middlewareMode: true } });
try {
  const { parseExportFile, importText, importArray } = await server.ssrLoadModule("/src/utils/exportFile.ts");
  const source = await readFile(root + "src/views/SitesView.vue", "utf8");
  const expression = source.slice(source.indexOf("body: JSON.stringify({", source.indexOf("async function importSitesFromFile")) + "body: JSON.stringify(".length);
  const payload = expression.slice(0, expression.indexOf("\n        }),") + "\n        }".length);
  const convert = new Function("entry", "templateMap", "importText", "importArray", "importBitwardenMode", `return (${payload});`);
  const rows = parseExportFile(await readFile(new URL("sites.import.json", import.meta.url), "utf8"), "sites", "sites");
  assert.equal(rows.length, 1);
  assert.equal(rows[0].enabled, false);
  assert.equal(rows[0].bitwarden_enabled, false);
  assert.equal(rows[0].credential_provider_id, "plugin.demo");
  assert.equal(rows[0].steps.length, 19);
  assert.equal(new Set(rows[0].steps.map(step => step.step_key)).size, 19);
  assert.deepEqual(rows[0].steps.map(step => step.position), [100, 200, 300, 400, ...Array.from({ length: 15 }, (_, i) => (i + 1) * 1000)]);
  console.log(JSON.stringify(rows.map(item => convert({ name: importText(item.name), templateName: item.template_name, item }, new Map(), importText, importArray, value => ["root_domain", "custom"].includes(importText(value)) ? importText(value) : "domain"))));
} finally {
  await server.close();
}
