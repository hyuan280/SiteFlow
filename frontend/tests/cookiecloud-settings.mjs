import assert from "node:assert/strict";
import { randomUUID, webcrypto } from "node:crypto";
import { test } from "node:test";
import { createServer } from "vite";
import { createRenderer, ssrContextKey } from "vue";

test("CookieCloud can select all current sites, adjust the selection, and clear it", async () => {
  const siteRows = Array.from({ length: 1001 }, (_, index) => ({
    name: `站点 ${index + 1}`,
    history_key: randomUUID(),
  }));
  const mutations = [];
  const servers = [];
  const endpoint = "/cookiecloud/servers";
  let endpointBase = "https://siteflow.example.com";
  globalThis.__cookieCloudMocks = {
    crypto: webcrypto,
    window: { location: { origin: "http://localhost:5173" } },
    ElMessage: { success() {} },
    async apiRequest(path, options) {
      if (path === "/sites") return structuredClone(siteRows);
      if (path === `${endpoint}/endpoint-base`) return { base_url: endpointBase };
      if (options?.method) {
        const body = JSON.parse(options.body);
        mutations.push({ method: options.method, body });
        const { id, name, site_keys, all_sites } = body;
        const server = { id: id ?? path.split("/").at(-1), name, site_keys, all_sites };
        servers[0] = server;
        return structuredClone(server);
      }
      if (path === endpoint) return structuredClone(servers);
      if (path === `${endpoint}/bulk/connection`) {
        const { uuid, password } = mutations[0].body;
        return { uuid, password };
      }
      assert.fail(`Unexpected request: ${path}`);
    },
  };

  const vite = await createServer({
    optimizeDeps: { noDiscovery: true, include: [] },
    server: { middlewareMode: true, ws: false },
    plugins: [{ name: "cookiecloud-settings-harness", enforce: "pre", transform(code, id) {
      if (!id.endsWith("/CookieCloudSettingsCard.vue")) return;
      return code.replace('import { ElMessage, ElMessageBox } from "element-plus";',
        'const { ElMessage, crypto, window } = globalThis.__cookieCloudMocks;')
        .replace('import { ApiError, apiRequest } from "@/api/client";',
          'class ApiError extends Error {}\nconst { apiRequest } = globalThis.__cookieCloudMocks;')
        .replace("</script>", `defineExpose({ form, siteOptions, servers, load, openEditor,
          clearEditor, dialogOpen, selectAllSites, clearSites, urlFor, save });\n</script>`);
    } }],
  });
  let app;
  try {
    const { default: Card } = await vite.ssrLoadModule("/src/components/CookieCloudSettingsCard.vue");
    const renderer = createRenderer({ createComment: () => ({}), insert() {}, remove() {} });
    let ui;
    app = renderer.createApp({ setup() {
      Card.setup({}, { expose(value) { ui = value; }, emit() {} });
      return () => null;
    } });
    app.provide(ssrContextKey, { modules: new Set() }).mount({});
    await new Promise(resolve => setImmediate(resolve));

    assert.equal(ui.urlFor("bulk"), "https://siteflow.example.com/cookiecloud/bulk");
    await ui.openEditor();
    Object.assign(ui.form.value, { id: "bulk", name: "批量配置" });
    const keys = siteRows.map(site => site.history_key);
    ui.selectAllSites();
    assert.deepEqual(ui.form.value.site_keys, keys);
    ui.selectAllSites();
    assert.deepEqual(ui.form.value.site_keys, keys, "select all is idempotent");
    ui.form.value.site_keys = ui.form.value.site_keys.filter(key => key !== keys[0]);
    assert.equal(mutations.length, 0, "selection changes are drafts until saved");
    await ui.save();
    assert.deepEqual(mutations[0].body.site_keys, keys.slice(1));
    assert.equal(mutations[0].body.all_sites, false);

    ui.clearEditor();
    await ui.openEditor(ui.servers.value[0]);
    assert.deepEqual(ui.form.value.site_keys, keys.slice(1));
    ui.clearSites();
    assert.equal(ui.servers.value[0].site_keys.length, keys.length - 1, "clearing does not change saved state");
    await ui.save();
    assert.equal(mutations[1].method, "PUT");
    assert.deepEqual(mutations[1].body.site_keys, []);

    siteRows.length = 0;
    await ui.load();
    await ui.openEditor();
    ui.selectAllSites();
    assert.equal(ui.siteOptions.value.length, 0);
    assert.deepEqual(ui.form.value.site_keys, []);

    Object.assign(ui.form.value, { id: "all", name: "所有站点", all_sites: true });
    await ui.save();
    assert.equal(mutations[2].body.all_sites, true);
    assert.deepEqual(mutations[2].body.site_keys, []);
    assert.equal(ui.servers.value.at(-1).all_sites, true);

    endpointBase = "";
    await ui.load();
    assert.equal(ui.urlFor("all"), "http://localhost:5173/cookiecloud/all");
  } finally {
    app?.unmount();
    await vite.close();
    delete globalThis.__cookieCloudMocks;
  }
});
