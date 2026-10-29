import { execFile } from "node:child_process";
import { createHash, timingSafeEqual } from "node:crypto";
import http from "node:http";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);
const PORT = Number(process.env.BW_PORT || 8090);
const API_TOKEN = (process.env.BW_API_TOKEN || "").trim();
const CACHE_SECONDS = Math.max(0, Number(process.env.BW_CACHE_SECONDS || 60));
const BW_SERVER = (process.env.BW_SERVER || "").trim();
const CLIENT_ID = (process.env.BW_CLIENTID || "").trim();
const CLIENT_SECRET = (process.env.BW_CLIENTSECRET || "").trim();
const MASTER_PASSWORD = process.env.BW_PASSWORD || "";

const MULTI_PART_SUFFIXES = new Set([
  "co.uk",
  "org.uk",
  "ac.uk",
  "gov.uk",
  "com.cn",
  "net.cn",
  "org.cn",
  "gov.cn",
  "com.hk",
  "org.hk",
  "com.tw",
  "org.tw",
  "co.jp",
  "co.kr",
  "com.au",
  "net.au",
  "org.au",
  "co.nz",
  "com.br",
  "com.sg",
]);

let session = "";
let version = "";
let lastError = "";
let ready = false;
let itemsCache = { at: 0, items: [] };
let chain = Promise.resolve();
let serverConfigured = false;

function log(level, message) {
  const line = `${new Date().toISOString()} ${level} ${message}`;
  if (level === "ERROR") console.error(line);
  else console.log(line);
}

function configured() {
  return Boolean(CLIENT_ID && CLIENT_SECRET && MASTER_PASSWORD);
}

function cleanOutput(value) {
  return String(value || "")
    .replace(/\u001b\[[0-9;?]*[ -/]*[@-~]/g, "")
    .trim();
}

function runBw(args, { useSession = false, timeout = 120000 } = {}) {
  const env = { ...process.env };
  if (useSession && session) env.BW_SESSION = session;
  return execFileAsync("bw", args, {
    env,
    timeout,
    maxBuffer: 64 * 1024 * 1024,
  }).then(({ stdout }) => cleanOutput(stdout));
}

async function bwStatus() {
  const raw = await runBw(["status", "--raw"], { timeout: 30000 });
  try {
    return JSON.parse(raw);
  } catch {
    return { status: "unknown" };
  }
}

function normalizeServer(value) {
  return String(value || "").trim().replace(/\/+$/, "");
}

async function ensureServerConfigured() {
  if (serverConfigured || !BW_SERVER) return;
  const desired = normalizeServer(BW_SERVER);
  let status = {};
  try {
    status = await bwStatus();
  } catch {
    status = {};
  }
  if (normalizeServer(status.serverUrl) === desired) {
    log("INFO", `Bitwarden server 已配置: ${desired}`);
    serverConfigured = true;
    return;
  }
  if (status.status && status.status !== "unauthenticated") {
    log("INFO", "Bitwarden server 变更，退出旧登录");
    await runBw(["logout"], { timeout: 30000 }).catch(() => undefined);
    session = "";
    itemsCache = { at: 0, items: [] };
    ready = false;
  }
  try {
    await runBw(["config", "server", BW_SERVER], { timeout: 30000 });
  } catch (error) {
    if (!/Logout required/i.test(String(error.message || error))) throw error;
    log("INFO", "Bitwarden CLI 要求先退出登录，重试 server 配置");
    await runBw(["logout"], { timeout: 30000 }).catch(() => undefined);
    session = "";
    itemsCache = { at: 0, items: [] };
    ready = false;
    await runBw(["config", "server", BW_SERVER], { timeout: 30000 });
  }
  serverConfigured = true;
  log("INFO", `Bitwarden server 已切换: ${desired}`);
}

async function ensureSession(force = false) {
  await ensureServerConfigured();
  if (session && !force) return session;
  if (!configured()) {
    throw new Error("Bitwarden wrapper 未配置 BW_CLIENTID、BW_CLIENTSECRET 和 BW_PASSWORD");
  }

  let status = await bwStatus();
  if (status.status === "unauthenticated") {
    log("INFO", "Bitwarden CLI 未登录，使用 API Key 登录");
    await runBw(["login", "--apikey"], { timeout: 120000 });
    status = await bwStatus();
  }
  if (status.status === "unlocked") {
    await runBw(["lock"], { timeout: 30000 }).catch(() => undefined);
    status = await bwStatus();
  }
  if (status.status !== "locked") {
    throw new Error(`Bitwarden 状态异常: ${status.status || "unknown"}`);
  }
  log("INFO", "解锁 Bitwarden 保险库");
  session = await runBw(["unlock", "--passwordenv", "BW_PASSWORD", "--raw"], {
    timeout: 120000,
  });
  if (!session) throw new Error("Bitwarden 解锁失败：未返回会话");
  return session;
}

function queue(task) {
  const next = chain.then(task, task);
  chain = next.catch(() => undefined);
  return next;
}

function loadItems(force = false) {
  return queue(async () => {
    const now = Date.now();
    if (!force && itemsCache.at > now && CACHE_SECONDS > 0) {
      return itemsCache.items;
    }
    await ensureSession();
    log("INFO", "同步 Bitwarden 保险库");
    await runBw(["sync", "--force"], { useSession: true, timeout: 180000 });
    const raw = await runBw(["list", "items"], { useSession: true });
    let parsed;
    try {
      parsed = JSON.parse(raw);
    } catch {
      throw new Error("Bitwarden 返回了无效的条目 JSON");
    }
    const items = (Array.isArray(parsed) ? parsed : [])
      .filter((item) => item && item.type === 1 && item.login)
      .map((item) => ({
        id: String(item.id || ""),
        name: String(item.name || ""),
        username: String(item.login.username || ""),
        password: String(item.login.password || ""),
        revisionDate: String(item.revisionDate || ""),
        uris: (Array.isArray(item.login.uris) ? item.login.uris : [])
          .map((entry) => (entry && typeof entry.uri === "string" ? entry.uri.trim() : ""))
          .filter(Boolean),
      }))
      .filter((item) => item.username);
    itemsCache = { at: now + CACHE_SECONDS * 1000, items };
    lastError = "";
    ready = true;
    log("INFO", `Bitwarden 登录条目 ${items.length} 条`);
    return items;
  });
}

function hostOf(uri) {
  const value = String(uri || "").trim();
  if (!value) return "";
  try {
    const url = new URL(value.includes("://") ? value : `https://${value}`);
    return url.hostname.toLowerCase().replace(/\.$/, "");
  } catch {
    return "";
  }
}

function rootDomain(host) {
  const labels = String(host || "").toLowerCase().replace(/\.$/, "").split(".").filter(Boolean);
  if (labels.length <= 2) return labels.join(".");
  const suffix = labels.slice(-2).join(".");
  if (MULTI_PART_SUFFIXES.has(suffix)) {
    return labels.slice(-3).join(".");
  }
  return suffix;
}

function scoreItem(item, mode, host, custom) {
  const uris = item.uris || [];
  const itemHosts = uris.map(hostOf).filter(Boolean);
  if (mode === "domain") {
    return itemHosts.includes(host) ? 100 : 0;
  }
  if (mode === "root_domain") {
    const root = rootDomain(host);
    if (itemHosts.includes(host)) return 100;
    return itemHosts.some((candidate) => rootDomain(candidate) === root) ? 80 : 0;
  }
  const needle = String(custom || "").toLowerCase();
  if (!needle) return 0;
  const name = item.name.toLowerCase();
  const username = item.username.toLowerCase();
  if (name === needle) return 100;
  if (itemHosts.includes(needle) || uris.some((uri) => uri.toLowerCase() === needle)) return 95;
  if (itemHosts.some((candidate) => rootDomain(candidate) === rootDomain(needle))) return 90;
  if (name.includes(needle)) return 80;
  if (uris.some((uri) => uri.toLowerCase().includes(needle))) return 70;
  if (username.includes(needle)) return 60;
  return 0;
}

async function lookup(body) {
  const mode = String(body.mode || "");
  const host = String(body.host || "").trim().toLowerCase().replace(/\.$/, "");
  const custom = String(body.custom || "").trim();
  if (!["domain", "root_domain", "custom"].includes(mode)) {
    const error = new Error("mode 必须是 domain、root_domain 或 custom");
    error.statusCode = 400;
    throw error;
  }
  if (!host && mode !== "custom") {
    const error = new Error("host 不能为空");
    error.statusCode = 400;
    throw error;
  }
  if (mode === "custom" && !custom) {
    const error = new Error("custom 不能为空");
    error.statusCode = 400;
    throw error;
  }
  const items = await loadItems();
  let best = null;
  let bestScore = 0;
  for (const item of items) {
    const score = scoreItem(item, mode, host, custom);
    if (score > bestScore) {
      best = item;
      bestScore = score;
    }
  }
  if (!best) {
    const error = new Error("没有找到匹配的 Bitwarden 登录条目");
    error.statusCode = 404;
    throw error;
  }
  log("INFO", `Bitwarden 匹配成功 item=${best.id} mode=${mode} host=${host || "-"}`);
  return {
    item_id: best.id,
    name: best.name,
    username: best.username,
    password: best.password,
    matched_by: mode,
    uri: (best.uris || [])[0] || null,
  };
}

function authorized(request) {
  if (!API_TOKEN) return true;
  const header = String(request.headers.authorization || "");
  const expected = `Bearer ${API_TOKEN}`;
  const a = createHash("sha256").update(header).digest();
  const b = createHash("sha256").update(expected).digest();
  return timingSafeEqual(a, b);
}

function send(response, statusCode, payload) {
  const body = JSON.stringify(payload);
  response.writeHead(statusCode, {
    "content-type": "application/json; charset=utf-8",
    "content-length": Buffer.byteLength(body),
    "cache-control": "no-store",
  });
  response.end(body);
}

function readBody(request) {
  return new Promise((resolve, reject) => {
    let size = 0;
    const chunks = [];
    request.on("data", (chunk) => {
      size += chunk.length;
      if (size > 1024 * 1024) {
        reject(new Error("请求体过大"));
        request.destroy();
        return;
      }
      chunks.push(chunk);
    });
    request.on("end", () => {
      if (!chunks.length) return resolve({});
      try {
        resolve(JSON.parse(Buffer.concat(chunks).toString("utf8")));
      } catch {
        reject(new Error("请求体不是有效 JSON"));
      }
    });
    request.on("error", reject);
  });
}

const server = http.createServer(async (request, response) => {
  const url = new URL(request.url || "/", "http://localhost");
  try {
    if (request.method === "GET" && url.pathname === "/health") {
      if (url.searchParams.get("deep") === "1" && configured()) {
        try {
          await ensureSession();
          ready = true;
          lastError = "";
        } catch (error) {
          ready = false;
          lastError = String(error.message || error);
        }
      }
      return send(response, 200, {
        status: "ok",
        configured: configured(),
        ready,
        unlocked: Boolean(session),
        version: version || null,
        user_email: null,
        error: lastError || null,
      });
    }
    if (!authorized(request)) {
      return send(response, 401, { detail: "invalid Bitwarden wrapper token" });
    }
    if (request.method === "POST" && url.pathname === "/check") {
      if (!configured()) {
        return send(response, 503, {
          detail: "BW_CLIENTID、BW_CLIENTSECRET 和 BW_PASSWORD 未配置",
        });
      }
      try {
        await ensureSession();
        ready = true;
        lastError = "";
      } catch (error) {
        ready = false;
        lastError = String(error.message || error);
        throw error;
      }
      return send(response, 200, {
        status: "ok",
        configured: true,
        ready: true,
        unlocked: true,
        version: version || null,
        user_email: null,
      });
    }
    if (request.method === "POST" && url.pathname === "/lookup") {
      const body = await readBody(request);
      const result = await lookup(body);
      return send(response, 200, result);
    }
    if (request.method === "POST" && url.pathname === "/sync") {
      await loadItems(true);
      return send(response, 200, { status: "ok", count: itemsCache.items.length });
    }
    return send(response, 404, { detail: "not found" });
  } catch (error) {
    const statusCode = Number(error.statusCode || 502);
    lastError = String(error.message || error);
    log("ERROR", lastError);
    return send(response, statusCode, { detail: lastError });
  }
});

async function warmup() {
  try {
    version = cleanOutput(await runBw(["--version"], { timeout: 30000 }));
  } catch (error) {
    lastError = String(error.message || error);
  }
  if (!configured()) {
    log("INFO", "Bitwarden wrapper 未配置保险库凭据，仅提供健康检查");
    return;
  }
  try {
    await ensureSession();
    await loadItems(true);
    lastError = "";
    ready = true;
    log("INFO", "Bitwarden wrapper 已就绪");
  } catch (error) {
    ready = false;
    lastError = String(error.message || error);
    log("ERROR", `Bitwarden 初始化失败: ${lastError}`);
  }
}

server.listen(PORT, "0.0.0.0", () => {
  log("INFO", `Bitwarden wrapper listening on :${PORT}`);
  void warmup();
});
