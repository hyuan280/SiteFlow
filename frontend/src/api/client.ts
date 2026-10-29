import type { Feature, FeatureExecutionSettings } from "@/types/workflow";
import type { IntegrationTestResult, VisionUpdate, VisionView } from "@/types/integrations";

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

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
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
    const detail =
      typeof data === "object" && data !== null && "detail" in data
        ? String((data as { detail: unknown }).detail)
        : `请求失败（HTTP ${response.status}）`;
    throw new ApiError(response.status, detail);
  }

  return data as T;
}
