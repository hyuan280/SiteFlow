import type { IntegrationTestResult } from "@/types/integrations";
import type { ModelProbeConfig, ModelProbeDraft } from "@/types/model-registry";

const SAMPLE_IMAGE = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4//8/AAX+Av4N70a4AAAAAElFTkSuQmCC";
const CONTROLS = /[\u0000-\u001f\u007f-\u009f\u200b-\u200f\u202a-\u202e\u2060-\u2069\ufeff]/;
const INVALID_BASE_URL = "请填写有效的 HTTP(S) /v1 根地址，不得包含用户信息、查询参数、片段或控制字符。";
const PROVIDER_ERRORS: Record<string, string> = {
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
const object = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

export function validateModelProbe(draft: ModelProbeDraft, requireKey: boolean, unchangedCap?: number | null): ModelProbeConfig | string {
  if (CONTROLS.test(draft.base_url)) return INVALID_BASE_URL;
  const base = draft.base_url.trim();
  try {
    const url = new URL(base);
    if (base.length > 2048 || !/^https?:\/\//i.test(base) ||
      !["http:", "https:"].includes(url.protocol) || !url.hostname || url.username || url.password ||
      /[?#\\]/.test(base) || base.split("/")[2]?.includes("@") || !url.pathname.replace(/\/+$/, "").endsWith("/v1")) throw new Error();
  } catch { return INVALID_BASE_URL; }
  const model = draft.model.trim();
  if (!model || model.length > 256 || CONTROLS.test(draft.model)) return "请填写模型名称（最多 256 个字符，不含控制字符）。";
  const key = draft.api_key.trim();
  if (requireKey && !key) return "新增模型或尚未保存密钥的模型必须填写 API Key 才能测试。";
  if (key.length > 4096 || CONTROLS.test(draft.api_key)) return "API Key 格式无效。";
  const cap = draft.max_tokens;
  const retainedCap = typeof unchangedCap === "number" && Number.isSafeInteger(unchangedCap) &&
    unchangedCap >= 0 && cap === String(unchangedCap);
  if (typeof cap !== "string" || !/^\d*$/.test(cap.trim()) || !Number.isSafeInteger(Number(cap)) || Number(cap) > 2147483647 && !retainedCap) {
    return "新增或修改的 max_tokens 必须为 0 到 2147483647 的非负整数；未修改的有效旧上限可沿用测试。";
  }
  return { base_url: base.replace(/\/+$/, ""), model, api_key: key,
    supports_vision: draft.supports_vision, max_tokens: cap.trim() === "" ? null : Number(cap) };
}

// Backend diagnostic prose still needs masking; provider prose is never displayed.
export function safeModelDiagnostic(value: unknown, secrets: string[] = []): string | null {
  if (typeof value !== "string" || value.length > 16384) return null;
  let text = value;
  const masks = new Set<string>();
  for (const secret of secrets.filter(Boolean)) {
    masks.add(secret);
    try { masks.add(encodeURIComponent(secret)); } catch { return null; }
    masks.add(JSON.stringify(secret).slice(1, -1));
    const bytes = new TextEncoder().encode(secret);
    const base64 = btoa(Array.from(bytes, byte => String.fromCharCode(byte)).join(""));
    const base64url = base64.replace(/\+/g, "-").replace(/\//g, "_");
    masks.add(base64); masks.add(base64.replace(/=+$/, ""));
    masks.add(base64url); masks.add(base64url.replace(/=+$/, ""));
    const hex = Array.from(bytes, byte => byte.toString(16).padStart(2, "0")).join("");
    masks.add(hex); masks.add(hex.toUpperCase());
    try {
      const url = new URL(secret);
      masks.add(url.href); masks.add(url.origin); masks.add(url.host);
      if (url.pathname !== "/") masks.add(url.pathname);
    } catch { /* Not every private field is a URL. */ }
  }
  for (const secret of [...masks].sort((a, b) => b.length - a.length)) text = text.split(secret).join("[已隐藏]");
  if (CONTROLS.test(text) || /[<>{}]|https?:\/\/|base64|data:|[A-Za-z0-9+/=_-]{64,}|\b(?:authorization|bearer|cookie|set-cookie|api[_ -]?key|headers?|request[_ -]?(?:body|dump)|password|secret)\b/i.test(text)) return null;
  return text.replace(/\s+/g, " ").trim().slice(0, 400) || null;
}

export function safeModelTestResult(value: unknown, secrets: string[] = []): IntegrationTestResult {
  const invalid = { ok: false, message: "模型测试响应格式无效。", data: { stage: "protocol", provider_error: null } };
  if (!object(value) || Object.keys(value).some(key => !["ok", "message", "data"].includes(key)) ||
    typeof value.ok !== "boolean" || typeof value.message !== "string" ||
    !(value.data == null || object(value.data))) return invalid;
  const data: Record<string, unknown> = {};
  if (object(value.data)) {
    if (value.data.mode !== undefined) {
      if (value.data.mode !== "text" && value.data.mode !== "vision") return invalid;
      data.mode = value.data.mode;
    }
    if (value.data.http_status !== undefined) {
      if (!Number.isInteger(value.data.http_status) || (value.data.http_status as number) < 100 || (value.data.http_status as number) > 599) return invalid;
      data.http_status = value.data.http_status;
    }
    if (typeof value.data.supports_vision === "boolean") data.supports_vision = value.data.supports_vision;
    if (["validation", "cancelled", "redirect", "response_limit", "invalid_json", "http", "protocol", "refused", "truncated", "empty", "complete", "timeout", "network",
      "invalid_response", "output_length", "content_filter", "refusal", "connection"].includes(value.data.stage as string)) data.stage = value.data.stage;
    if (["connect", "read", "write", "pool", "total"].includes(value.data.timeout_phase as string)) data.timeout_phase = value.data.timeout_phase;
    if (typeof value.data.timeout_seconds === "number" && Number.isFinite(value.data.timeout_seconds) && value.data.timeout_seconds > 0) data.timeout_seconds = value.data.timeout_seconds;
    const error = value.data.provider_error;
    if (object(error)) {
      const recognized: Record<string, string> = {};
      for (const field of ["code", "type"] as const) {
        const code = error[field];
        if (typeof code !== "string" || !Object.hasOwn(PROVIDER_ERRORS, code) || safeModelDiagnostic(code, secrets) !== code) continue;
        recognized[field] = code;
        recognized.message ??= PROVIDER_ERRORS[code]!;
      }
      data.provider_error = Object.keys(recognized).length ? recognized : null;
    }
  }
  let message = safeModelDiagnostic(value.message, secrets) ?? "模型测试已完成，但返回的诊断信息不可安全显示。";
  if (data.http_status && !message.includes(`HTTP ${data.http_status}`)) message = `HTTP ${data.http_status}：${message}`;
  const detail = data.provider_error as Record<string, string> | undefined;
  if (detail) {
    const extra = [detail.message, detail.type, detail.code].filter(part => part && !message.includes(part));
    if (extra.length) message += `（${extra.join("；")}）`;
  }
  return { ok: value.ok, message, data };
}

export async function probeModelInBrowser(config: ModelProbeConfig, signal?: AbortSignal): Promise<IntegrationTestResult> {
  const mode = config.supports_vision ? "vision" : "text";
  const result = (ok: boolean, stage: string, message: string, http_status?: number, provider_error: unknown = null): IntegrationTestResult =>
    ({ ok, message, data: { mode, stage, provider_error, ...(http_status ? { http_status } : {}) } });
  // The form enforces int32 for new/changed caps; the transport also accepts retained legacy safe integers.
  const validated = validateModelProbe({ ...config, max_tokens: config.max_tokens == null ? "" : String(config.max_tokens) }, true, config.max_tokens);
  if (typeof validated === "string") return result(false, "validation", validated);
  const controller = new AbortController();
  let timedOut = false;
  const cancel = () => controller.abort();
  signal?.addEventListener("abort", cancel, { once: true });
  if (signal?.aborted) controller.abort();
  // The registry does not expose the global timeout. Browser probes use a fixed 30s budget, including reading the body.
  const timer = setTimeout(() => { timedOut = true; controller.abort(); }, 30000);
  let status: number | undefined;
  try {
    if (controller.signal.aborted) return result(false, "cancelled", "测试已取消。");
    const response = await fetch(`${validated.base_url}/chat/completions`, {
      method: "POST", headers: { "Content-Type": "application/json", Authorization: `Bearer ${validated.api_key}` },
      credentials: "omit", referrerPolicy: "no-referrer", cache: "no-store", redirect: "error", signal: controller.signal,
      body: JSON.stringify({ model: validated.model, stream: false, messages: [{ role: "user", content: config.supports_vision
        ? [{ type: "text", text: "Describe this image in a few words." }, { type: "image_url", image_url: { url: SAMPLE_IMAGE } }]
        : "Reply with the single word OK." }], ...(validated.max_tokens ? { max_tokens: validated.max_tokens } : {}) }),
    });
    if (controller.signal.aborted) throw new Error();
    status = response.status;
    if (response.redirected || status >= 300 && status < 400) {
      controller.abort();
      return result(false, "redirect", `HTTP ${status}：测试不允许重定向。`, status);
    }
    const reader = response.body?.getReader();
    let text = "";
    let size = 0;
    const decoder = new TextDecoder();
    if (reader) {
      try {
        while (true) {
          const chunk = await reader.read();
          if (controller.signal.aborted) throw new Error();
          if (chunk.done) break;
          size += chunk.value.byteLength;
          if (size > 65536) {
            controller.abort();
            void reader.cancel().catch(() => {});
            return result(false, "response_limit", `HTTP ${status}：响应超过 64 KiB，已终止读取。`, status);
          }
          text += decoder.decode(chunk.value, { stream: true });
        }
        text += decoder.decode();
      } finally { reader.releaseLock(); }
    }
    let body: unknown;
    try { body = JSON.parse(text); }
    catch { return result(false, response.ok ? "invalid_json" : "http", `HTTP ${status}：${response.ok ? "响应不是有效 JSON。" : "请求失败，未返回可安全显示的 JSON 错误。"}`, status); }
    if (!response.ok || object(body) && body.error != null) {
      const error = object(body) && object(body.error) && size <= 16384 ? body.error : {};
      return safeModelTestResult(result(false, "http", `HTTP ${status}：模型服务返回错误。`, status, error),
        [validated.api_key, validated.base_url, validated.model]);
    }
    if (!object(body) || !Array.isArray(body.choices) || !object(body.choices[0]) || !object(body.choices[0].message)) {
      return result(false, "protocol", `HTTP ${status}：响应不符合 chat/completions 协议。`, status);
    }
    const choice = body.choices[0];
    const message = choice.message as Record<string, unknown>;
    if (message.refusal || choice.finish_reason === "content_filter" || Array.isArray(message.content) &&
      message.content.some(part => object(part) && part.type === "refusal")) return result(false, "refused", `HTTP ${status}：模型拒绝了测试请求。`, status);
    if (choice.finish_reason === "length") return result(false, "truncated", `HTTP ${status}：模型输出被截断，请检查 Token 上限。`, status);
    const content = Array.isArray(message.content) ? message.content.filter(part => object(part) && part.type === "text" && typeof part.text === "string")
      .map(part => part.text).join("") : message.content;
    if (typeof content !== "string") return result(false, "protocol", `HTTP ${status}：响应未包含文本内容。`, status);
    const output = content.replace(/<think>.*?<\/think>/gis, "");
    if (!output.trim() || /<\/?think\b/i.test(output)) return result(false, "empty", `HTTP ${status}：模型返回空内容或仅推理内容。`, status);
    if (CONTROLS.test(output.replace(/[\n\r\t]/g, ""))) return result(false, "protocol", `HTTP ${status}：响应文本包含无效控制字符。`, status);
    return result(true, "complete", `HTTP ${status}：${mode === "vision" ? "视觉图片" : "文本"}请求成功。`, status);
  } catch {
    if (timedOut) return result(false, "timeout", "测试超时（30 秒），请求已终止。", status);
    if (controller.signal.aborted) return result(false, "cancelled", "测试已取消。", status);
    return result(false, "network", "浏览器无法完成请求，可能是 CORS、HTTPS 混合内容、网络或重定向限制；浏览器无法区分这些原因。不会自动转交后端。", status);
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener("abort", cancel);
  }
}
