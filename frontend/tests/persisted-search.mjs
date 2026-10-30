import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { effectScope } from "vue";
import { createServer } from "vite";

const server = await createServer({
  optimizeDeps: { noDiscovery: true, include: [] },
  server: { middlewareMode: true },
});
const scopes = [];
const previousWindow = globalThis.window;
try {
  const { usePersistedSearch } = await server.ssrLoadModule("/src/utils/persistedSearch.ts");
  const stored = new Map();
  globalThis.window = { localStorage: {
    getItem: key => stored.get(key) ?? null,
    setItem: (key, value) => stored.set(key, value),
    removeItem: key => stored.delete(key),
  } };
  function open(key) {
    const scope = effectScope();
    scopes.push(scope);
    return { search: scope.run(() => usePersistedSearch(key)), leave: () => scope.stop() };
  }

  for (const [view, name, key] of [
    ["SitesView", "siteSearch", "siteflow:sites:search"],
    ["TemplatesView", "templateSearch", "siteflow:templates:search"],
  ]) {
    const source = await readFile(new URL(`../src/views/${view}.vue`, import.meta.url), "utf8");
    assert.ok(source.includes(`const ${name} = usePersistedSearch("${key}")`));
    const first = open(key);
    assert.equal(first.search.value, "");
    first.search.value = `${view} query`;
    assert.equal(stored.get(key), `${view} query`, "save immediately before navigation");
    first.leave();
    const returned = open(key);
    assert.equal(returned.search.value, `${view} query`);
    returned.search.value = "";
    assert.equal(stored.has(key), false);
    returned.leave();
    assert.equal(open(key).search.value, "");
  }
  const sites = open("siteflow:sites:search");
  const templates = open("siteflow:templates:search");
  sites.search.value = "site query";
  templates.search.value = "template query";
  assert.equal(open("siteflow:sites:search").search.value, "site query");
  assert.equal(open("siteflow:templates:search").search.value, "template query");

  Object.defineProperty(globalThis.window, "localStorage", {
    get() { throw new Error("Storage blocked"); },
  });
  const blocked = open("blocked");
  assert.equal(blocked.search.value, "");
  blocked.search.value = "still filters";
  assert.equal(blocked.search.value, "still filters");
  blocked.search.value = "";
  assert.equal(blocked.search.value, "");
  console.log("Persisted search regression passed");
} finally {
  for (const scope of scopes) scope.stop();
  if (previousWindow === undefined) delete globalThis.window;
  else globalThis.window = previousWindow;
  await server.close();
}
