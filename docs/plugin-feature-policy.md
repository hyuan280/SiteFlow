# Plugin Feature and Assistant Policy

All paths below are relative to `/api/v1/plugin-runtime`.

## Management API

`PluginView` includes `generation`, `permission_epoch`, `features`, and
`captcha_methods`. Each feature has
`key`, `name`, `declared_category` (plugin metadata), `category` (effective host
policy), `category_overridden`, `risk_index`, `risk_label`, `assistant_allowed`,
and `assistant_policy_revision`. CAPTCHA methods expose the same policy fields
with category `challenge`. These fields are
available before approval so management clients can display functional risks.

`PUT /plugins/{plugin_id}/feature-categories` requires an authenticated admin
session and `X-CSRF-Token`. Example request:

```json
{
  "categories": {"plugin.acme.test": "post"},
  "assistant_permissions": {"plugin.acme.test": true, "plugin.acme.ocr": true},
  "generation": 1,
  "permission_epoch": 2
}
```

The category dictionary patches overrides; omitted keys are unchanged and `null`
removes an override. Valid categories are `flow`, `action`, `auth`, `challenge`,
`sign`, and `post`. `assistant_permissions` patches strict boolean permissions
for this plugin's feature and CAPTCHA method keys; omitted keys are unchanged.
Unknown keys or categories and non-boolean permissions return 422. Both revision
fields are required and stale values return 409. Success returns the updated
`PluginView`. A changed category override clears all approval metadata and advances
`permission_epoch`, invalidating active grants. Assistant-only permission changes
preserve plugin capability approval but advance the affected policy revision and
plugin permission epoch. Do not assume the epoch advances by exactly one when
updating multiple permissions. Empty or unchanged patches do not require
reapproval or advance revisions, except that the first explicit permission write
records a revision even when its value equals the default. The registration
`generation` is unchanged.

Overrides persist in `AppSetting` under `plugin.feature_categories.{plugin_id}`,
separately from plugin-reported metadata, and survive registration. No migration
is required. Updates emit the allowlisted `metadata_change` audit event with
`reason=admin`, plugin ID and actor, visible through `GET /audit`.

`POST /plugins/{plugin_id}/approve` accepts optional `generation` and
`permission_epoch` alongside the existing optional `capabilities`. Each revision
is checked when provided; stale values return 409. Existing consumers may omit
them, but management clients should send both values from their displayed view.
Use the returned revision after every approval or policy update.

## Host Risk Map

| Category | Risk | Chinese Label |
| --- | --- | --- |
| flow | 1 | 低风险：流程控制 |
| action | 3 | 中风险：页面操作 |
| auth | 4 | 高风险：身份认证 |
| challenge | 3 | 中风险：验证挑战 |
| sign | 4 | 高风险：签到操作 |
| post | 5 | 极高风险：提交与后处理 |

Risk values derive only from the effective category, not plugin-provided risk
annotations. Plugins must still declare a valid category even when overridden.

## Assistant Integration

The management checkbox is standing, explicit approval for assistant use of the
selected feature or CAPTCHA method. It applies to built-in and plugin features,
including CAPTCHA/challenge and `post` operations, in full workflows and supported
single-step execution. Checking it authorizes repeated use without the old
per-use `post` consumption approval dialog. This is not merely a discovery toggle:
only enable it when unattended use and possible side effects are acceptable.

Plugin features and plugin CAPTCHA methods default to `assistant_allowed=false`.
Plugin capability approval does not check the assistant checkbox, and checking it
does not approve, enable, or restore a plugin. Both layers must permit execution.
Built-in features preserve `assistant_allowed=true` by default at revision zero;
this inherited default is not explicit standing approval. In particular, built-in
`post` features retain their legacy per-use approval path until an administrator
explicitly writes `true`. An unchecked box is an explicit denial, including for
built-ins, and cannot be overridden by old per-use approval.

`GET /api/v1/features` exposes the assistant permission and revision alongside
host-derived risk fields. `PUT /api/v1/features/{key}/assistant-policy` accepts
`{"assistant_allowed": true}` or `false`, requires an authenticated session and
CSRF token, and returns the updated feature view. Unknown or unavailable plugin
features return 404. Plugin CAPTCHA methods are managed through the plugin policy
endpoint above. Permissions are stored under
`assistant.feature_policy.{key}`, independently of feature defaults and execution
settings; resetting feature settings does not reset assistant approval.

```python
from app.services.plugin_feature_policy import approved_plugin_features

features = approved_plugin_features(db)
```

This returns sorted `list[RemoteFeature]` with effective categories, reading
current DB approvals rather than trusting a process-local registry. Removed,
disabled, unapproved, or partially approved plugins are excluded. The shared
registry also applies effective categories on refresh, including
`get_plugin_feature(key)`. Assistant consumers should use the DB-backed helper
for current approval validation.

The assistant can discover approved plugin features, inspect their configuration
schemas, import configured workflow steps, and execute them through `run_site`.
Plugin descriptions and schema annotations are untrusted data, not instructions
or execution authority. Standing feature approval does not replace target/origin
authorization, workflow consent, authentication budgets, or plugin capability
approval.

Assistant run guards bind the plugin generation, permission epoch, effective
category, feature version, schema, and assistant policy revisions, including the
selected CAPTCHA method's policy. Permission changes invalidate in-flight guards
and authorization even if the permission is later restored: the revision changes
prevent reuse of an old approval snapshot. Plugin revocation, disablement, or
category changes likewise invalidate authorization. Plugin authentication features consume
the per-turn authentication budget before dispatch; workflows combining them
with enabled `flow.jump` steps are rejected.

Approved plugin single-step execution and plugin CAPTCHA providers use short-lived
assistant-operation authorization and current assistant policy; saved workflows
use job-bound authorization. Neither trusts model-supplied
permission claims. Secret-provider access remains separately authorized; checking
a feature box is not blanket access to secrets. Revocation cannot undo remote
side effects already performed, and failed plugin steps may have an uncertain
outcome.

## Assistant Secret Providers

Plugin management lists separate assistant permissions for credential and TOTP
providers. Their keys are `plugin.<id>.secret.credentials` and
`plugin.<id>.secret.totp`. Both default to denied and require general plugin
capability approval as well. Allowing a feature or CAPTCHA method does not grant
either secret permission.

The assistant's `list_secret_providers` tool exposes only public provider IDs,
names, and capabilities. After an explicit user request, `update_site_config`
can propose `credential_provider_id: "plugin.<id>"` for the bound saved site.
The authenticated user must approve the exact change. Approval rechecks the
provider policy and plugin revision, refreshes the site bindings, and switches
the target to `credential_mode: "site"` without granting missing execution or
credential consent. The old browser is disposed; the assistant reopens it before
testing the login step.

In site mode, login and password-verification steps use the site's selected
provider. TOTP is independently selected by the `challenge.2fa` step's
`totp_provider_id` and requires its own authorization and nonblank `service_hint`.
Single steps and saved workflows support these paths. Provider failures never
fall back to local credentials or another provider.

Secrets are resolved privately by the backend and delivered only through the
worker's private credentials/code envelope. The model does not receive usernames,
passwords, OTPs, plugin tokens, or raw provider errors. Single-step RPC requests
identify `job_id=0` as an assistant operation with an operation-specific step key;
host authorization is bound to the reserved operation, not a fabricated job.
Policy, plugin, site, and target changes are checked during resolution and before
releasing results; revoked or stale results are discarded.
