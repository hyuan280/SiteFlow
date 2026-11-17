import type { Feature, FeatureExecutionSettings } from "@/types/workflow";
import type { IntegrationTestResult, VisionUpdate, VisionView } from "@/types/integrations";
import { isModelRegistryView, type ModelDraftTest, type ModelRegistryUpdate } from "@/types/model-registry";

export const getModelRegistry = async () => {
  const value = await apiRequest<unknown>("/integrations/models");
  if (!isModelRegistryView(value)) throw new ApiError(502, "模型设置响应格式无效。", "invalid_model_registry_response");
  return value;
};
export const saveModelRegistry = async (payload: ModelRegistryUpdate) => {
  const value = await apiRequest<unknown>("/integrations/models", { method: "PUT", body: JSON.stringify(payload) });
  if (!isModelRegistryView(value)) throw new ApiError(502, "模型设置响应格式无效。", "invalid_model_registry_response");
  return value;
};
export const testModel = (id: string, draft?: ModelDraftTest) =>
  apiRequest<IntegrationTestResult>(`/integrations/models/${encodeURIComponent(id)}/test`, {
    method: "POST", ...(draft ? { body: JSON.stringify(draft) } : {}),
  });

export const saveFeatureSettings = (key: string, defaults: Record<string, unknown>, required_fields: string[], execution_settings: FeatureExecutionSettings | null) =>
  apiRequest<Feature>(`/features/${encodeURIComponent(key)}/settings`, {
    method: "PUT", body: JSON.stringify({ defaults, required_fields, execution_settings }),
  });

export const resetFeatureSettings = (key: string) =>
  apiRequest<Feature>(`/features/${encodeURIComponent(key)}/settings`, { method: "DELETE" });

export const saveVisionSettings = (payload: VisionUpdate) =>
  apiRequest<{ vision: VisionView }>("/integrations/vision", { method: "PUT", body: JSON.stringify(payload) });
export const clearVisionSettings = () =>
  apiRequest<{ vision: VisionView }>("/integrations/vision", { method: "DELETE" });
export const testVisionSettings = () =>
  apiRequest<IntegrationTestResult>("/integrations/vision/test", { method: "POST" });

export class ApiError extends Error {
  readonly status: number;
  readonly code?: string;
  readonly reason?: string;

  constructor(status: number, message: string, code?: string, reason?: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.reason = reason;
  }
}

const API_BASE = "/api/v1";
let csrfToken = "";

export function setCsrfToken(token: string | null): void {
  csrfToken = token ?? "";
}

function readCookie(name: string): string {
  const prefix = `${name}=`;
  for (const part of document.cookie.split(";")) {
    const value = part.trim();
    if (value.startsWith(prefix)) {
      return decodeURIComponent(value.slice(prefix.length));
    }
  }
  return "";
}

export async function apiRequest<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers);
  const method = (options.method ?? "GET").toUpperCase();

  if (options.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  if (!["GET", "HEAD", "OPTIONS"].includes(method)) {
    const token = csrfToken || readCookie("siteflow_csrf");
    if (token) {
      headers.set("X-CSRF-Token", token);
    }
  }

  const response = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers,
    credentials: "include",
  });

  const text = await response.text();
  let data: unknown = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = text;
    }
  }

  if (!response.ok) {
    const detail: unknown =
      typeof data === "object" && data !== null && "detail" in data
        ? (data as { detail: unknown }).detail
        : undefined;
    const rawCode = typeof detail === "object" && detail !== null && !Array.isArray(detail) && "code" in detail
      ? (detail as { code: unknown }).code : undefined;
    const code = typeof rawCode === "string" && rawCode.length <= 128 && /^[a-z]/.test(rawCode) && !/[^a-z_]/.test(rawCode)
      ? rawCode : undefined;
    const rawReason = typeof detail === "object" && detail !== null && !Array.isArray(detail) && "reason" in detail
      ? (detail as { reason: unknown }).reason : undefined;
    const reason = typeof rawReason === "string" && /^[a-z_]{1,128}$/.test(rawReason) ? rawReason : undefined;
    throw new ApiError(response.status, typeof detail === "string" ? detail : `请求失败（HTTP ${response.status}）`, code, reason);
  }

  return data as T;
}
