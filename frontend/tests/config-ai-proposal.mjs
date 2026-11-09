import assert from "node:assert/strict";
import { createServer } from "vite";

// The standalone proposal parser/UI is gone; retain the shared API error contract.
const server = await createServer({
  optimizeDeps: { noDiscovery: true, include: [] },
  server: { middlewareMode: true, hmr: false },
});
const originalFetch = globalThis.fetch;
const originalDocument = Object.getOwnPropertyDescriptor(globalThis, "document");
try {
  const { ApiError, apiRequest, setCsrfToken } = await server.ssrLoadModule("/src/api/client.ts");
  const requests = [];
  let response;
  globalThis.document = { cookie: "unrelated=1; siteflow_csrf=cookie%20token" };
  globalThis.fetch = async (url, options) => { requests.push({ url, ...options }); return response.clone(); };
  const respond = (body, status = 400) => { response = new Response(typeof body === "string" ? body : JSON.stringify(body), { status }); };
  const apiFailure = async (detail, expectedCode, expectedReason) => {
    respond({ detail });
    await assert.rejects(apiRequest("/assistant/sessions/example/messages", { method: "POST", body: "{}" }), error => {
      assert.ok(error instanceof ApiError);
      assert.equal(error.name, "ApiError");
      assert.equal(error.status, 400);
      assert.equal(error.message, typeof detail === "string" ? detail : "请求失败（HTTP 400）");
      assert.equal(error.code, expectedCode);
      assert.equal(error.reason, expectedReason);
      return true;
    });
  };
  await apiFailure({ code: "invalid_response", message: "PRIVATE provider secret", nested: { secret: true } }, "invalid_response");
  for (const code of ["UPPER", "bad-code", "bad code", "x".repeat(129), "", null, 123, {}, "bad\n"]) {
    await apiFailure({ code, message: "PRIVATE" }, undefined);
  }
  await apiFailure({ code: "a".repeat(128) }, "a".repeat(128));
  for (const reason of ["no_tool_calls", "truncated", "too_many_parallel_tools", "submission_not_alone",
    "invalid_tool_function", "invalid_argument_json", "arguments_not_object", "unknown_reason", "a", "_", "a".repeat(128)]) {
    await apiFailure({ code: "invalid_response", reason, message: "PRIVATE provider secret" }, "invalid_response", reason);
  }
  for (const reason of ["UPPER", "bad-reason", "bad reason", "bad1", "x".repeat(129), "", null,
    123, true, {}, [], ["truncated"], "bad\n", "bad\r\n", "bad\t", "bad\u0000", "截断",
    'PRIVATE {"secret":"value"}<script>']) {
    await apiFailure({ code: "invalid_response", reason, message: "PRIVATE provider secret" }, "invalid_response");
  }
  await apiFailure({ reason: "truncated", message: "PRIVATE" }, undefined, "truncated");
  await apiFailure({ code: "bad-code", reason: "truncated" }, undefined, "truncated");
  await apiFailure({ code: "invalid_response", nested: { reason: "truncated" } }, "invalid_response");
  await apiFailure("Existing server error", undefined);
  for (const detail of [[{ msg: "PRIVATE", input: "secret" }], null, 1, false, { message: "PRIVATE" }]) await apiFailure(detail);
  assert.equal(requests.at(-1).headers.get("X-CSRF-Token"), "cookie token");
  assert.equal(requests.at(-1).headers.get("Content-Type"), "application/json");
  assert.equal(requests.at(-1).credentials, "include");
  assert.equal(requests.at(-1).url, "/api/v1/assistant/sessions/example/messages");
  setCsrfToken("memory token");
  respond({ ok: true }, 200);
  const abort = new AbortController();
  assert.deepEqual(await apiRequest("/ok", { method: "PUT", signal: abort.signal, headers: { "X-Custom": "yes" }, body: "{}" }), { ok: true });
  assert.equal(requests.at(-1).headers.get("X-CSRF-Token"), "memory token");
  assert.equal(requests.at(-1).headers.get("X-Custom"), "yes");
  assert.equal(requests.at(-1).signal, abort.signal);
  await apiRequest("/ok");
  assert.equal(requests.at(-1).headers.has("X-CSRF-Token"), false);
  respond("PRIVATE non-JSON error", 502);
  await assert.rejects(apiRequest("/bad"), error => error.message === "请求失败（HTTP 502）" && error.code === undefined && error.reason === undefined);
  respond("plain success", 200);
  assert.equal(await apiRequest("/plain"), "plain success");
  response = new Response(null, { status: 204 });
  assert.equal(await apiRequest("/empty"), null);
  assert.equal(new ApiError(403, "legacy").code, undefined);
  assert.equal(new ApiError(403, "legacy").reason, undefined);
  assert.equal(new ApiError(502, "legacy code", "invalid_response").reason, undefined);
  const reasonError = new ApiError(502, "safe message", "invalid_response", "truncated");
  assert.equal(reasonError.status, 502);
  assert.equal(reasonError.message, "safe message");
  assert.equal(reasonError.code, "invalid_response");
  assert.equal(reasonError.reason, "truncated");
  console.log("Shared API error contract regression passed");
} finally {
  globalThis.fetch = originalFetch;
  if (originalDocument) Object.defineProperty(globalThis, "document", originalDocument);
  else delete globalThis.document;
  await server.close();
}
