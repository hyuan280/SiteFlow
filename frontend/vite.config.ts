import { execFileSync } from "node:child_process";
import { existsSync } from "node:fs";
import { fileURLToPath, URL } from "node:url";

import vue from "@vitejs/plugin-vue";
import { defineConfig } from "vite";

const buildInfoScript = fileURLToPath(new URL("../ops/build-info.sh", import.meta.url));
let version = process.env.SITEFLOW_BUILD_VERSION;
let commit = process.env.SITEFLOW_BUILD_COMMIT;
if (version === undefined && existsSync(buildInfoScript)) {
  [version, commit] = execFileSync("bash", [buildInfoScript, "web"], { encoding: "utf8" })
    .trimEnd().split(" ");
}

export default defineConfig({
  plugins: [vue()],
  define: {
    __SITEFLOW_WEB_BUILD__: JSON.stringify({ version: version || "v0.0.0-dev", commit: commit || null }),
  },
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  build: {
    target: "es2022",
  },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
      "/cookiecloud": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
});
