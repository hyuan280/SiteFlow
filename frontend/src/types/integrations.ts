export interface ValueView {
  value: string | null;
  source: "env" | "db" | "auto" | "unset";
  editable: boolean;
  error: string | null;
}

export interface TokenView {
  configured: boolean;
  source: "env" | "db" | "auto" | "unset";
  editable: boolean;
  masked: string | null;
}

export interface BooleanView {
  value: boolean;
  source: "env" | "db" | "auto" | "unset";
  editable: boolean;
}

export interface VisionView {
  base_url: ValueView;
  model: ValueView;
  api_key: TokenView;
  supports_vision: BooleanView;
  max_tokens: {
    value: number | null;
    source: "env" | "db" | "unset";
    editable: boolean;
    error: string | null;
  };
  available: boolean;
}

export interface VisionUpdate {
  base_url?: string;
  model?: string;
  api_key?: string;
  supports_vision?: boolean;
  max_tokens?: number | null;
}

export interface IntegrationTestResult {
  ok: boolean;
  message: string;
  data?: Record<string, unknown> | null;
}
