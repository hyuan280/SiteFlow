import assert from "node:assert/strict";
import { createServer } from "vite";

const server = await createServer({ optimizeDeps: { noDiscovery: true, include: [] }, server: { middlewareMode: true, hmr: false } });
const originalFetch = globalThis.fetch;
const originalSetTimeout = globalThis.setTimeout;
const originalClearTimeout = globalThis.clearTimeout;
try {
  const { probeModelInBrowser, validateModelProbe, safeModelDiagnostic, safeModelTestResult } = await server.ssrLoadModule("/src/utils/model-test.ts");
  const draft = { base_url: "https://provider.example/v1", model: "private-model", api_key: "sk-private-unique-key", supports_vision: false, max_tokens: "" };
  const config = { ...draft, max_tokens: null };
  const success = (content = "OK", extra = {}) => ({ choices: [{ message: { content }, finish_reason: "stop", ...extra }] });
  const json = (value, status = 200) => new Response(JSON.stringify(value), { status });
  const requests = [];
  const reply = value => { globalThis.fetch = async (url, options) => { requests.push({ url, options }); return value; }; };

  for (const base_url of ["", "ftp://example.com/v1", "https://example.com/v2", "https://u:p@example.com/v1", "https://@example.com/v1", "https://example.com/v1?", "https://example.com/v1#", "https://example.com/v1?q=secret", "https://example.com/v1#fragment", "https://example.com:99999/v1", "https://exa\nmple.com/v1", "https://example.com/v1\u0000", "https://example.com\\v1", "/v1"]) {
    assert.equal(typeof validateModelProbe({ ...draft, base_url }, true), "string", base_url);
  }
  for (const control of ["\n", "\t", "\u0000", "\u007f", "\u0085", "\u200b", "\u202e", "\u2066", "\ufeff"]) {
    for (const base_url of [`${control}${draft.base_url}`, `${draft.base_url}${control}`, `https://provider.example/${control}v1`]) {
      assert.equal(typeof validateModelProbe({ ...draft, base_url }, true), "string", "raw URL controls are rejected before trimming or URL normalization");
    }
  }
  for (const base_url of ["https://example.com/v1", "http://localhost:8000/openai/v1/", "https://[::1]/v1//"]) {
    assert.equal(typeof validateModelProbe({ ...draft, base_url }, true), "object", base_url);
  }
  for (const [key, values] of Object.entries({ model: ["", " ", "a".repeat(257), "model\n"], api_key: ["", " ", "key\n", "x".repeat(4097)], max_tokens: ["-1", "1.1", "1e3", "2147483648", "Infinity", true, NaN] })) {
    for (const value of values) assert.equal(typeof validateModelProbe({ ...draft, [key]: value }, true), "string");
  }
  assert.equal(typeof validateModelProbe({ ...draft, api_key: "" }, false), "object");
  for (const [value, expected] of [["", null], [" ", null], ["0", 0], [" 00032 ", 32], ["2147483647", 2147483647]]) {
    assert.equal(validateModelProbe({ ...draft, max_tokens: value }, true).max_tokens, expected);
  }
  for (const cap of [2147483648, Number.MAX_SAFE_INTEGER]) {
    assert.equal(validateModelProbe({ ...draft, max_tokens: String(cap) }, true, cap).max_tokens, cap);
    for (const value of [String(cap), ` ${cap} `, `0${cap}`, "2147483649"]) {
      assert.equal(typeof validateModelProbe({ ...draft, max_tokens: value }, true), "string", "new caps remain int32");
      if (value !== String(cap)) assert.equal(typeof validateModelProbe({ ...draft, max_tokens: value }, true, cap), "string", "only the exact unchanged baseline is exempt");
    }
  }
  for (const cap of [-1, 1.5, Infinity, Number.MAX_SAFE_INTEGER + 1]) {
    assert.equal(typeof validateModelProbe({ ...draft, max_tokens: String(cap) }, true, cap), "string", "invalid baselines cannot permit invalid caps");
  }

  reply(json(success()));
  let result = await probeModelInBrowser(config);
  assert.equal(result.ok, true); assert.equal(result.data.stage, "complete"); assert.equal(result.data.mode, "text");
  const request = requests.at(-1);
  assert.equal(request.url, "https://provider.example/v1/chat/completions");
  assert.equal(request.options.method, "POST"); assert.equal(request.options.credentials, "omit");
  assert.equal(request.options.redirect, "error"); assert.equal(request.options.referrerPolicy, "no-referrer");
  assert.equal(request.options.cache, "no-store"); assert.ok(request.options.signal instanceof AbortSignal);
  assert.deepEqual(request.options.headers, { "Content-Type": "application/json", Authorization: `Bearer ${config.api_key}` });
  const payload = JSON.parse(request.options.body);
  assert.deepEqual(payload, { model: config.model, stream: false, messages: [{ role: "user", content: "Reply with the single word OK." }] });
  assert.doesNotMatch(JSON.stringify(payload), /api_key|sk-private|cookie|csrf/);
  reply(json(success())); await probeModelInBrowser({ ...config, max_tokens: 0 });
  assert.equal(JSON.parse(requests.at(-1).options.body).max_tokens, undefined);
  for (const cap of [2147483648, Number.MAX_SAFE_INTEGER]) {
    reply(json(success()));
    assert.equal((await probeModelInBrowser({ ...config, max_tokens: cap })).ok, true, "transport retains legacy safe-integer caps");
    assert.equal(JSON.parse(requests.at(-1).options.body).max_tokens, cap);
  }
  for (const cap of [-1, 1.5, Infinity, Number.MAX_SAFE_INTEGER + 1]) {
    const before = requests.length;
    assert.equal((await probeModelInBrowser({ ...config, max_tokens: cap })).data.stage, "validation");
    assert.equal(requests.length, before);
  }
  reply(json(success())); result = await probeModelInBrowser({ ...config, supports_vision: true, max_tokens: 64 });
  assert.equal(result.data.mode, "vision");
  const vision = JSON.parse(requests.at(-1).options.body);
  assert.equal(vision.max_tokens, 64); assert.equal(vision.stream, false);
  assert.deepEqual(vision.messages[0].content, [
    { type: "text", text: "Describe this image in a few words." },
    { type: "image_url", image_url: { url: "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4//8/AAX+Av4N70a4AAAAAElFTkSuQmCC" } },
  ]);

  const secrets = [config.api_key, config.base_url, config.model];
  for (const status of [401, 403, 429, 500]) {
    reply(json({ error: { message: `Invalid credential ${config.api_key} at ${config.base_url} for ${config.model}`, type: "authentication_error", code: "invalid_credential", request: { headers: { secret: config.api_key } } } }, status));
    result = await probeModelInBrowser(config);
    assert.equal(result.ok, false); assert.equal(result.data.http_status, status);
    assert.match(result.message, new RegExp(`HTTP ${status}.*认证失败，请检查密钥及其权限.*authentication_error`));
    for (const secret of secrets) assert.ok(!JSON.stringify(result).includes(secret));
    assert.doesNotMatch(JSON.stringify(result), /request|headers/);
    assert.doesNotMatch(JSON.stringify(result), /Invalid credential|invalid_credential/);
  }
  for (const message of ["<html>private</html>", "data:image/png;base64,private", "A".repeat(200), "request dump: Authorization: Bearer other-secret", "cookie=private", "invalid\u0000private", "{\"headers\":\"private\"}"]) {
    reply(json({ error: { message, type: "invalid_request_error" } }, 400));
    result = await probeModelInBrowser(config);
    assert.equal(result.ok, false); assert.match(result.message, /HTTP 400.*invalid_request_error/);
    assert.equal(result.data.provider_error.message, "服务拒绝请求，请检查模型、参数及视觉输入支持。");
    assert.ok(!JSON.stringify(result).includes(message), "arbitrary provider prose is never displayed");
  }
  assert.equal(safeModelDiagnostic(`Failure at ${encodeURIComponent(config.base_url)} with ${config.api_key}`, secrets), "Failure at [已隐藏] with [已隐藏]");
  reply(json({ error: { message: "small message", ignored: "x".repeat(17000) } }, 400));
  result = await probeModelInBrowser(config);
  assert.equal(result.data.provider_error, null, "error JSON above 16 KiB is not reflected");
  const knownErrors = {
    invalid_api_key: "密钥无效，请检查认证配置。",
    authentication_error: "认证失败，请检查密钥及其权限。",
    model_not_found: "模型不存在或当前密钥无权访问。",
    unsupported_model: "服务不支持此模型。",
    unsupported_parameter: "模型不支持请求中的参数。",
    unsupported_value: "模型不支持请求中的参数值或输入类型。",
    rate_limit_exceeded: "请求频率超过服务限制，请稍后再试。",
    rate_limit_error: "请求频率超过服务限制，请稍后再试。",
    insufficient_quota: "服务额度不足，请检查账户余额或配额。",
    quota_exceeded: "服务额度已用尽，请检查账户配额。",
    invalid_request_error: "服务拒绝请求，请检查模型、参数及视觉输入支持。",
  };
  for (const [code, reason] of Object.entries(knownErrors)) {
    for (const field of ["code", "type"]) {
      reply(json({ error: { [field]: code, message: "arbitrary quota phrase must not be echoed" } }, 429));
      result = await probeModelInBrowser(config);
      assert.equal(result.data.provider_error[field], code); assert.equal(result.data.provider_error.message, reason);
      assert.ok(result.message.includes("HTTP 429")); assert.ok(result.message.includes(reason));
      assert.doesNotMatch(JSON.stringify(result), /arbitrary quota phrase/);
      const backend = safeModelTestResult({ ok: false, message: "服务返回错误。", data: {
        http_status: 429, provider_error: { [field]: code, message: "arbitrary backend provider prose" },
      } }, secrets);
      assert.equal(backend.data.provider_error.message, reason);
      assert.doesNotMatch(JSON.stringify(backend), /arbitrary backend provider prose/);
    }
  }
  for (const code of ["unknown_error", "Authentication_error", "quota_exceeded ", "__proto__", "constructor", "toString"]) {
    reply(json({ error: { code, type: code, message: "arbitrary quota phrase" } }, 400));
    result = await probeModelInBrowser(config);
    assert.equal(result.data.provider_error, null);
    assert.equal(result.message, "HTTP 400：模型服务返回错误。");
  }
  const variants = secret => {
    const bytes = Buffer.from(secret, "utf8");
    const base64 = bytes.toString("base64");
    return [...new Set([secret, encodeURIComponent(secret), JSON.stringify(secret).slice(1, -1), base64,
      base64.replace(/=+$/, ""), base64.replace(/\+/g, "-").replace(/\//g, "_"),
      bytes.toString("base64url"), bytes.toString("hex"), bytes.toString("hex").toUpperCase()])];
  };
  assert.equal(Buffer.from(config.api_key).toString("base64"), "c2stcHJpdmF0ZS11bmlxdWUta2V5");
  for (const key of [config.api_key, "sk-ÿÿ?+/=短"]) {
    for (const encoded of variants(key)) {
      for (const field of ["message", "type", "code"]) {
        reply(json({ error: { [field]: encoded } }, 401));
        result = await probeModelInBrowser({ ...config, api_key: key });
        assert.equal(result.data.provider_error, null);
        assert.ok(!JSON.stringify(result).includes(encoded), `provider ${field} never reflects an encoded key`);
        const backend = safeModelTestResult({ ok: false, message: `Rejected ${encoded}`, data: {
          http_status: 401, provider_error: { [field]: encoded },
        } }, [key]);
        assert.ok(!JSON.stringify(backend).includes(encoded), `backend ${field} and message never reflect a known encoded key`);
      }
    }
  }
  for (const secret of secrets) {
    for (const encoded of variants(secret)) {
      const safe = safeModelDiagnostic(`Rejected ${encoded}`, secrets);
      assert.ok(safe === null || !safe.includes(encoded));
    }
  }
  reply(json({ error: { code: "authentication_error", type: "authentication_error", message: "not safe" } }, 401));
  result = await probeModelInBrowser({ ...config, api_key: "authentication_error" });
  assert.equal(result.data.provider_error, null, "even a known code cannot echo a matching form secret");
  reply(new Response("<html>private-error</html>", { status: 502 }));
  result = await probeModelInBrowser(config); assert.equal(result.data.stage, "http"); assert.match(result.message, /HTTP 502/);
  assert.doesNotMatch(JSON.stringify(result), /private-error/);

  for (const [response, stage] of [
    [new Response("invalid JSON private"), "invalid_json"],
    [json({}), "protocol"], [json({ choices: [] }), "protocol"], [json(success(null)), "protocol"],
    [json(success("")), "empty"], [json(success("  ")), "empty"],
    [json(success("<think>private reasoning</think>")), "empty"],
    [json(success("<think>unfinished private reasoning")), "empty"],
    [json(success("OK\u0000")), "protocol"],
    [json(success([{ type: "refusal", refusal: "private refusal" }])), "refused"],
    [json(success("partial", { finish_reason: "length" })), "truncated"],
    [json(success("", { finish_reason: "content_filter" })), "refused"],
    [json(success(null, { message: { refusal: "private refusal" } })), "refused"],
    [new Response(null, { status: 204 }), "invalid_json"],
    [new Response(null, { status: 302, headers: { Location: "https://other.example" } }), "redirect"],
  ]) {
    reply(response); result = await probeModelInBrowser(config);
    assert.equal(result.ok, false); assert.equal(result.data.stage, stage);
    assert.doesNotMatch(JSON.stringify(result), /private refusal|invalid JSON private|other.example/);
  }
  reply(json(success(config.api_key)));
  result = await probeModelInBrowser(config); assert.equal(result.ok, true);
  assert.ok(!JSON.stringify(result).includes(config.api_key), "model output is never reflected");
  reply(json(success([{ type: "text", text: "OK" }])));
  assert.equal((await probeModelInBrowser(config)).ok, true);
  const redirected = json(success()); Object.defineProperty(redirected, "redirected", { value: true });
  reply(redirected); result = await probeModelInBrowser(config); assert.equal(result.data.stage, "redirect");

  let cancelledRead = false; let chunksRead = 0;
  const oversized = new ReadableStream({
    pull(controller) { chunksRead++; controller.enqueue(new Uint8Array(32769)); },
    cancel() { cancelledRead = true; },
  }, { highWaterMark: 0 });
  reply(new Response(oversized)); result = await probeModelInBrowser(config);
  assert.equal(result.data.stage, "response_limit"); assert.equal(cancelledRead, true);
  assert.equal(chunksRead, 2); assert.equal(requests.at(-1).options.signal.aborted, true);
  const exact = JSON.stringify(success("OK"));
  reply(new Response(exact + " ".repeat(65536 - new TextEncoder().encode(exact).length)));
  assert.equal((await probeModelInBrowser(config)).ok, true, "64 KiB limit is inclusive");
  reply(new Response(JSON.stringify(success("字".repeat(22000)))));
  assert.equal((await probeModelInBrowser(config)).data.stage, "response_limit", "limit counts bytes, not characters");

  for (const error of [new TypeError(`CORS ${config.api_key}`), new Error(`https://private.example/${config.model}`), new DOMException("private raw error", "NetworkError")]) {
    globalThis.fetch = async () => { throw error; };
    result = await probeModelInBrowser(config);
    assert.equal(result.data.stage, "network"); assert.match(result.message, /CORS.*HTTPS 混合内容.*网络.*重定向/);
    assert.doesNotMatch(JSON.stringify(result), /sk-private|private.example|private raw error|private-model/);
  }
  let timeout;
  globalThis.setTimeout = (callback, ms) => { assert.equal(ms, 30000); timeout = callback; return 1; };
  globalThis.clearTimeout = () => {};
  globalThis.fetch = (_url, options) => new Promise((_resolve, reject) => {
    options.signal.addEventListener("abort", () => reject(new Error("raw timeout secret")), { once: true });
  });
  let testing = probeModelInBrowser(config); timeout(); result = await testing;
  assert.equal(result.data.stage, "timeout"); assert.match(result.message, /30 秒/);
  const abort = new AbortController(); testing = probeModelInBrowser(config, abort.signal); abort.abort();
  assert.equal((await testing).data.stage, "cancelled");
  const preAborted = new AbortController(); preAborted.abort();
  let fetchCalled = false; globalThis.fetch = async () => { fetchCalled = true; throw new Error(); };
  assert.equal((await probeModelInBrowser(config, preAborted.signal)).data.stage, "cancelled");
  assert.equal(fetchCalled, false);
  let readSignal;
  globalThis.fetch = async (_url, options) => {
    readSignal = options.signal;
    return new Response(new ReadableStream({ start(controller) {
      options.signal.addEventListener("abort", () => controller.error(new Error("raw body error")), { once: true });
    } }));
  };
  testing = probeModelInBrowser(config); await Promise.resolve(); timeout(); result = await testing;
  assert.equal(result.data.stage, "timeout"); assert.equal(readSignal.aborted, true);

  for (const invalid of [null, [], {}, { ok: "yes", message: "private" }, { ok: false, message: "private", cookie: "private" }, { ok: false, message: "private", data: [] }, { ok: true, message: "private", data: { mode: "cookie" } }, { ok: true, message: "private", data: { http_status: "200" } }]) {
    const safe = safeModelTestResult(invalid, secrets);
    assert.equal(safe.ok, false); assert.match(safe.message, /响应格式无效/); assert.doesNotMatch(JSON.stringify(safe), /private/);
  }
  result = safeModelTestResult({ ok: false, message: `Rejected ${config.base_url} for ${config.model}`, data: {
    mode: "text", http_status: 401, stage: "private_cookie_secret", cookie: "private-cookie", api_key: config.api_key,
    provider_error: { message: "Credential expired", code: "expired", headers: "private-headers" },
  } }, secrets);
  assert.match(result.message, /HTTP 401.*Rejected/);
  assert.equal(result.data.provider_error, null);
  assert.doesNotMatch(JSON.stringify(result), /Credential expired|provider.example|private-model|sk-private|private-cookie|private-headers|private_cookie_secret/);
  console.log("Browser model probe regression passed");
} finally {
  globalThis.fetch = originalFetch; globalThis.setTimeout = originalSetTimeout; globalThis.clearTimeout = originalClearTimeout;
  await server.close();
}
