export interface PluginCheck {
  key: string;
  label: string;
  ok: boolean;
  skipped: boolean;
  message: string;
}

export interface PluginRuntimeView {
  removed_at?: string | null;
  removed_by?: string | null;
  plugin_id: string;
  name: string;
  version: string;
  protocol_version: number;
  endpoint: string;
  capabilities: string[];
  feature_count: number;
  captcha_method_count: number;
  checks: PluginCheck[];
  status: string;
  enabled: boolean;
  generation: number;
  approved_capabilities: string[];
  approved_at: string | null;
  approved_by: string | null;
  registered_at: string;
  last_heartbeat_at: string;
  updated_at: string;
}
