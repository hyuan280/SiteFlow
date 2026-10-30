import type { JsonSchema } from "@/utils/schema";

export interface FeatureExecutionSettings {
  llm_prompt: string;
  llm_enabled?: boolean;
  use_vision?: boolean;
  judgment_scope?: "success_only" | "all";
  model?: string;
}

export interface Feature {
  key: string;
  name: string;
  category: string;
  cardinality: string;
  requires: string[];
  description: string;
  version: number;
  config_schema: JsonSchema;
  defaults: Record<string, unknown>;
  required_fields: string[];
  base_required_fields: string[];
  customized: boolean;
  execution_settings: FeatureExecutionSettings | null;
  source: string;
}

export interface GroupView {
  step_anchor?: number | null;
  group_key: string;
  name: string;
  position: number;
}

export interface ResolvedGroupView extends GroupView {
  source: string;
}

export interface StepView {
  step_key: string;
  feature_type: string;
  feature_version: number;
  label: string | null;
  category: string;
  group_key: string | null;
  position: number | null;
  enabled: boolean | null;
  config: Record<string, unknown>;
  condition: string | null;
  on_error: string | null;
  timeout_seconds: number | null;
  source: string | null;
}

export interface ResolvedStep {
  step_key: string;
  feature_type: string;
  feature_version: number;
  label: string;
  category: string;
  group_key: string | null;
  position: number;
  enabled: boolean;
  config: Record<string, unknown>;
  condition: string;
  condition_unknown_action: "needs_human";
  on_error: string;
  timeout_seconds: number | null;
  requires: string[];
  source: string;
}

export interface ResolvedWorkflow {
  owner_type: "template" | "site";
  owner_id: number;
  name: string;
  lane: string | null;
  requires: string[];
  steps: ResolvedStep[];
  groups: ResolvedGroupView[];
  signals: ResolvedSignalView[];
  warnings: string[];
}

export interface SignalView {
  signal_key: string;
  label: string;
  description: string;
  include_texts: string[];
  include_selectors: string[];
  exclude_texts: string[];
  exclude_selectors: string[];
  match_mode: string;
  enabled: boolean;
}

export interface ResolvedSignalView extends SignalView {
  source: string;
}

export interface SignalForm extends SignalView {
  uid: string;
}

export interface TemplateSummary {
  id: number;
  name: string;
  description: string;
  parent_id: number | null;
  enabled: boolean;
  version: number;
  step_count: number;
  signal_count: number;
  lane: string | null;
  valid: boolean;
  validation_error: string | null;
  created_at: string;
  updated_at: string;
}

export interface TemplateDetail extends TemplateSummary {
  steps: StepView[];
  groups: GroupView[];
  groups_locked: boolean;
  dependent_site_count: number;
  dependent_template_count: number;
  signals: SignalView[];
  resolved_signals: ResolvedSignalView[];
  resolved: ResolvedWorkflow | null;
}

export interface SiteCredential {
  configured: boolean;
  username_masked: string | null;
  cookies_configured: boolean;
  cookie_count: number;
  updated_at: string | null;
  cookies_updated_at: string | null;
  last_verified_at: string | null;
  last_error: string | null;
  error: string | null;
}

export interface SiteSummary {
  id: number;
  name: string;
  base_url: string;
  enabled: boolean;
  template_id: number | null;
  template_name: string | null;
  lane_policy: string;
  lane: string | null;
  support_resign: boolean;
  resign_max_days: number;
  bitwarden_enabled: boolean;
  credential_provider_id?: string | null;
  bitwarden_match_mode: "domain" | "root_domain" | "custom";
  bitwarden_custom_match: string | null;
  checkin_after: string | null;
  timezone: string;
  tags: string[];
  notes: string;
  config_revision: number;
  step_count: number;
  signal_count: number;
  credential: SiteCredential;
  valid: boolean;
  validation_error: string | null;
  created_at: string;
  updated_at: string;
}

export interface SiteOption {
  id: number;
  name: string;
  base_url: string;
  enabled: boolean;
  template_id: number | null;
  template_name: string | null;
  lane_policy: string;
  lane: string | null;
  valid: boolean | null;
  validation_error: string | null;
}

export interface SiteDetail extends SiteSummary {
  steps: StepView[];
  groups: GroupView[];
  signals: SignalView[];
  resolved_signals: ResolvedSignalView[];
  resolved: ResolvedWorkflow | null;
}

export type StepEnabled = "inherit" | "on" | "off";

export interface GroupForm extends GroupView {
  uid: string;
  isNew?: boolean;
}

export interface StepForm {
  uid: string;
  step_key: string;
  feature_type: string;
  label: string;
  group_key: string;
  position: number | undefined;
  enabled: StepEnabled;
  config: Record<string, unknown>;
  condition: string;
  on_error: string;
  timeout_seconds: number | undefined;
  isNew?: boolean;
}

export interface RunSummary {
  id: number;
  mode: string;
  trigger: string;
  status: string;
  force: boolean;
  cancel_requested: boolean;
  requested_sites: number[];
  summary: Record<string, number>;
  site_count: number;
  site_names: string[];
  created_by: string;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface JobStepView {
  step_key: string;
  feature_type: string;
  label: string;
  status: string;
  message: string;
  data: Record<string, unknown>;
  started_at: string | null;
  finished_at: string | null;
}

export interface ArtifactView {
  id: number;
  step_key: string | null;
  kind: string;
  label: string;
  content_type: string;
  size_bytes: number;
  sha256: string;
  created_at: string;
}

export interface SiteJobView {
  id: number;
  site_id: number | null;
  site_name: string;
  lane: string;
  attempt: number;
  status: string;
  message: string;
  result: Record<string, unknown>;
  started_at: string | null;
  finished_at: string | null;
  steps: JobStepView[];
  artifacts: ArtifactView[];
}

export interface RunDetail extends RunSummary {
  jobs: SiteJobView[];
}
