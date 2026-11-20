export interface PluginCheck {
  key: string;
  label: string;
  ok: boolean;
  skipped: boolean;
  message: string;
}

export type PluginFeatureCategory = "flow" | "auth" | "challenge" | "sign" | "action" | "post";

export interface PluginFeatureView {
  key: string;
  name: string;
  declared_category: PluginFeatureCategory;
  category: PluginFeatureCategory;
  category_overridden: boolean;
  risk_index: 1 | 2 | 3 | 4 | 5;
  risk_label: string;
  assistant_allowed: boolean;
  assistant_policy_revision: number;
}

export interface PluginSecretProviderView extends PluginFeatureView {
  provider_id: string;
  capability: "secret.credentials" | "secret.totp";
  declared_category: "auth" | "challenge";
  category: "auth" | "challenge";
  category_overridden: false;
  risk_index: 5;
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
  permission_epoch: number;
  features: PluginFeatureView[];
  captcha_methods: PluginFeatureView[];
  secret_providers: PluginSecretProviderView[];
  approved_capabilities: string[];
  approved_at: string | null;
  approved_by: string | null;
  registered_at: string;
  last_heartbeat_at: string;
  updated_at: string;
}
