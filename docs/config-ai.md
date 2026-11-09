# Config AI Generation

This page describes the **retained single-request generation API and shared
authoring protocol**. The standalone site-editor AI generation button and
generation/review dialog have been removed; use **智能助手** (`/assistant`) for
AI-assisted configuration. The assistant stores encrypted, username-owned conversations and can
read sanitized system data, observe an approved website and execute explicitly
authorized single steps. See [智能助手](ai-assistant.md) for titles, host permissions,
manual login, API, budgets, lifecycle and security boundaries.
Statements below about no persisted chat/browser and disconnect cancellation
apply to `/config-ai/generate`, not `/assistant`.

The backend implements an opt-in, bounded, text-only model/tool loop for proposing
the local workflow of **one new site without a template**. It reuses the configured
Vision/OpenAI-compatible model connection and runs read-only discovery,
validation and submission tools inside `POST /api/v1/config-ai/generate`.
Submission returns a proposal for human review, not a saved site or approval to
execute it.

There is no standalone generation/apply UI in the new-site editor. The backend
generation endpoint and shared discovery, guide, tools and model settings remain
available; removing the UI does not remove assistant capabilities.
In this endpoint there is no persisted chat, existing-site or template editing,
template inheritance, public standalone tool dispatcher, MCP server, saving,
scheduling, browser interaction or workflow execution. Plugin features, aliases,
`post.*` (including `post.resign_dialog`) and `action.fill` are blocked even when explicitly selected. The generic
guide and schemas also describe inheritance and editing semantics for future
integrations; those descriptions do not enable them in this endpoint.

## Enablement

For AI-assisted configuration in the frontend, use **智能助手** and follow its
target, review and authorization flow. Normal manual site/template editing is
unchanged. The retained single-request API is documented below; it has no
frontend entry point or automatic editor handoff.

`SITEFLOW_CONFIG_AI_ENABLED=false` is the default; `.env.example` includes the
commented setting. Set it to `true` to opt in. Discovery GET routes remain
available when generation is disabled.

The persistent assistant reuses this same flag and Vision connection, including
global `max_tokens`; it has no separate provider configuration. Deploy its Web,
API and Worker together. Unlike the single-request API, an accepted assistant turn continues
after tab closure or SSE disconnect and requires explicit cancellation. Its SSE
sends full snapshots, not provider tokens (`stream: false`); request UUID replay
does not rerun a turn, and the UI never automatically resends messages. Assistant
tool titles are short operation/reason summaries, not private chain-of-thought.
It shares the reviewed static builtin metadata and draft validator, still excluding
plugins, `post.*` and `action.fill`, not arbitrary administrator/plugin metadata.
The proposal alone has no save/run authority. Separately authorized assistant
tools can import a saved proposal and fully test its bound site; see the
assistant guide for those permissions. Its separately authorized live steps
also perform real business operations, as described below.
Its limits differ from the single-request budgets below; use
[the assistant guide](ai-assistant.md).

### Persistent Assistant Differences

These capabilities belong only to `/assistant`, **not** `/config-ai/generate`:

- New session creation directly sends `{}`, with no creation dialog or model call. The server writes a fixed initial assistant greeting and the default title. Only successful main turns among the first three accepted user messages may trigger a separate, small, tool-free title request using the same configured model. Failed/cancelled user turns still consume that window; title failure preserves the title and is not retried. Manual rename permanently locks automatic updates. Title lock/revision/attempt fields use existing encrypted JSON, requiring no column migration; old sessions missing those fields are treated as locked. The title request has at most 10 seconds within the remaining 180-second turn budget, not an extra budget.
- `list_sites`, `get_site_config`, `list_templates`, `get_template_config`, `list_recent_runs`, `get_run_summary` and `get_system_status` return non-secret site/template metadata, sanitized local/resolved workflows and signals, run/job metadata, integration configuration sources and lane status. Inline fill/static-field values and sensitive config keys are omitted; URL credentials/query/fragment are stripped. This is a dedicated projection, not the raw administrator preview and not generic free-text DLP. It provides no arbitrary SQL, shell, JavaScript, files, keys, raw run logs or screenshot artifacts.
- Host target settings bind the origin, credential mode, proxy and optional saved `site_id`. The user confirms one unified `authorized: true` grant in the host UI; the API derives `execution_consent`, the frozen reviewed `execution_features`, credential-source-dependent `credential_consent`, and `allow_sign` for signing, generic clicks, ALTCHA and sliders. Old targets retain only their saved permissions until the user reconfirms; neither an upgrade nor a retry expands the initial scope. Model text, chat approval, DOM, feature schemas and step conditions cannot grant permissions. Extra `trusted_origins` authorize navigation/observation only, never cross-origin execution or secret use.
- Manual stored-account tests require a persisted site with exactly the same origin (scheme, host and effective port). The API freezes its ID, history identity and configuration revision and rechecks them before execution; changes require reconfirmation. Bitwarden can privately resolve credentials for an unsaved new target using full-domain matching, not inherited root/custom matching, with explicit credential authorization. Cookie mode uses manual login in a separate user-controlled tab on the same lane, followed by assistant navigation/reload; it does not import stored Cookie values for auth tests. `cookie` and `none` cannot authorize auth features.
- The reviewed live list is exactly `flow.open`, `action.navigate`, `action.wait`, `auth.form_login`, `auth.password_verify`, `sign.button`, `sign.unit3d_claim`, `action.click`, `challenge.altcha`, `challenge.slider`. It is narrower than the proposal catalog. No live image-captcha, Turnstile, 2FA, iframe sign, purchase/resign, plugin, arbitrary fill or screenshot-recognition integration exists yet. Unsupported challenges need manual handling, not an assumed success.
- `execute_step` runs one enabled, unconditional step on the existing current page, not workflow conditions/groups/jumps. Activity is bounded to 90 seconds, the shorter step timeout and remaining browser TTL/turn budget. Navigation retry is forced to zero; no automatic step retries occur. The page stays open afterward. Results contain static logs, safe flags and before/after **structured DOM state snapshots, not PNG screenshots**, HTML or raw runtime logs. Real sign-in/check-in submits to the website and requires explicit authorization; submission is not proof of success.
- API-resolved stored secrets go privately to the Worker, never to model messages, chat or proposals. Worker capability validation binds the original unique visible input/submit nodes to the same original form, same-origin action/formaction and current window; password targets must be editable password inputs. It revalidates before secret use. A form need not declare POST: existing page JS may prevent native GET submission and POST to another compliant path on the original origin. There is no per-form POST endpoint approval or model-authored JS. GET/HEAD remain the default; POST opens only during an authorized non-read-only step, never for navigation/wait. Cross-origin POST, PUT/DELETE and known secrets in any request URL remain blocked. Afterward POST closes and original-origin restrictions remain, even for trusted observation origins. No SSO/trusted-URL expansion is granted.
- The API persists a stable operation UUID before dispatch/credential release and rejects reuse; the Worker binds it to the complete request fingerprint and returns only in-progress/cached terminal state on replay. Unknown, timeout, cancellation or a lost response is not proof of no submission: inspect read-only first, never change UUIDs to bypass uncertainty. Reopening a browser does not clear the gate and is not a mandatory retry step. Limits are 20 operations per browser and a persistent 256-ID ledger per chat session, unaffected by target changes or browser reopening.
- When the user explicitly requests a retry, the model calls `retry_step` with the original `operation_id` and public `title`, not a changed step or private authority. A non-read-only `execute_step` whose normalized step exactly matches one eligible uncertain original under the current binding is also intercepted by the host into this approval flow. A changed/ambiguous step is not executed; `retry_step_mismatch` directs the model to the original ID. The host **pauses that same tool invocation**, keeps the turn/tool `running`, and publishes safe `pending_approval` metadata through detail/SSE. The running dialog lets the owner approve, deny or stop the turn. Approval requires login/ownership/CSRF and immediately resumes the original invocation to execute the exact stored normalized step once; no new user message or second model execution call is needed. The model must not refuse the request or ask for a new message after approval.
- Approval waits at most **180 seconds** independently of the remaining active turn budget. Human event-wait time pauses the outer turn timeout and advances its deadline by the elapsed waiting duration; approval processing, model requests and execution still consume the active budget (default 180 seconds). An already exhausted turn cannot open a new prompt. Denial returns `retry_denied`, expiry `retry_approval_timeout`, and invalidated authority/binding `retry_approval_invalidated`, all without execution. Decisions/results for the same original operation are cached per turn, so repeated calls do not spam dialogs or dispatch twice. Stop/shutdown still cancels immediately; restart cancels pending approvals with no automatic execution/resume. A later turn may request fresh approval only when the user explicitly asks again.
- Optional legacy idle preauthorization remains available through **允许重试一次**, issuing a five-minute, browser-bound, single-use grant for the stored step. This idle action alone does not execute/send a message; a later `retry_step` can consume it. It is not the main flow or a required prerequisite to running approval. Five minutes bounds grant admission, not approval waiting or extra turn time; an admitted, consumed retry can return after expiry if bindings remain valid. The host consumes authority before credential release/dispatch and rechecks original origin, browser/target revision, credential binding and current permissions. There is no always-retry permission; broad initial scope, cross-origin and secret boundaries remain unchanged.
- Only a linked, actually dispatched terminal `success` retry clears its uncertainty gate, not proof that the original failed. Failure, missing elements or approval alone do not resolve the original outcome; a still-unknown retry needs approval for that new uncertain record. Original history/ledger/safety facts persist. Historical `no_retry: true` and current `requires_host_approval: true` mean **no autonomous repeat; host approval required**, not a permanent ban. Current host authorization/resolution is authoritative, never chat or summary claims.
- Successful assistant draft validation/submission also returns `required_site_settings` next to the proposal: `credential_mode`, `credential_provider_id`, `lane_policy` and a notice. Users must apply these host-confirmed provider/proxy settings separately to reproduce the tested environment; workflow arrays do not carry credentials or Cookie values.

The public assistant API adds `PUT /api/v1/assistant/sessions/{uuid}/title` with
`{title}` and returns `title`, `title_locked`, `title_revision` in session views.
Its `PUT /sessions/{uuid}/target` accepts required name/base URL/mode/proxy and
literal unified `authorized: true`; optional `site_id` defaults to null,
`trusted_origins` to `[]`, and separate `screenshot_consent` to false. The server
derives execution/credential/side-effect permissions; legacy granular fields
remain for existing callers, not the main UI flow. Running retry decisions use
`POST /sessions/{uuid}/retry-approvals/{approval_id}/decision` with
`{"decision":"approve"}` or `{"decision":"deny"}`. Detail/SSE `pending_approval`
contains only `id`, `operation_id`, `feature_type`, `status`, `created_at`,
`expires_at`, `title`, never stored steps, capabilities or fingerprints.
See the [SetTarget table](ai-assistant.md#api-与排查) for combinations and constraints.

The internal **`POST /assistant-browser/{session_id}/execute`** is API-to-Worker
only. The frontend saves permissions and sends messages; it never calls
`/execute` directly. The API checks ownership, target/revision and permissions,
normalizes config, records the UUID and privately resolves credentials. The
Worker checks the capability token, origin, feature/request binding and secret
form targets, then executes once on the leased page. Model tool arguments contain
only `title`, `operation_id` and `step`, never Worker tokens or credentials.

Browser routing is not a complete sandbox: independent Chromium DNS leaves
rebinding risk, shared-profile service workers can bypass page routing, popups
may send an initial request before closing, and even GET can cause side effects.
Proxy lanes still require Worker-local DNS. Keep network-layer egress policies.

For a disabled, standalone example, see
[NexusPHP workflow JSON](examples/nexusphp-assistant-workflow.json) and the
[evidence-discovery guide](assistant-discovery.md). It is a sanitized adaptation
of the actual `NexusPHP站点登录签到` template and parent `NexusPHP站点登录`, not a live
browser verification or mutation of those templates. Purchase/resign steps are
removed entirely; selectors and conditions require target-specific review.

### Model Connection

Generation resolves the existing Vision connection through
`IntegrationService.vision_runtime()`, using its configured base URL, model, API
key, global `max_tokens` and `vision_timeout`. No separate provider configuration is accepted in the
request. The provider URL must be an HTTP(S) `/v1` root without URL credentials,
query or fragment. Requests go to its `/chat/completions` endpoint with bearer
authentication, `tool_choice: "auto"` and `stream: false`. Every round sends the
same positive global `max_tokens` integer unchanged; unset/null, blank (normalized
on update), or 0 omits the parameter, using upstream defaults rather than
guaranteeing unlimited output. See [Vision settings](vision-feature-settings.md#vision-integration)
for update/clear semantics and the optional `SITEFLOW_VISION_MAX_TOKENS` override;
even environment value 0 overrides and locks the DB setting. Thinking models may
consume the budget on reasoning, so leave enough for final tool calls. Reasoning
is never treated as an answer or proposal, and the model is never switched
automatically. Existing timeout and response-byte budgets are unchanged.
The model must support OpenAI-compatible function/tool calling; a successful
ordinary text or image test does not establish that capability. Images are not
sent, and `supports_vision=false` does not block this text-only use.

## Generation API

`POST /api/v1/config-ai/generate` requires an authenticated session and the
session-bound CSRF cookie/header pair (`X-CSRF-Token`). Authentication is
non-touching: it does not update session activity. Responses, including expected
errors, use `Cache-Control: no-store`.

The `GenerateSiteDraftRequest` contract forbids unknown fields, including inside
`site`:

| Field | Contract |
| --- | --- |
| `site` | Only `name` (1-128 characters) and `base_url` (1-512 characters, normalized HTTP(S) URL with a hostname); both required |
| `requirements` | Required text, 1-8000 characters |
| `evidence` | Optional text, at most 16000 characters; defaults to `""` |
| `consent_to_send` | Required literal JSON `true`; `1` and `"true"` are not consent |
| `allowed_features` | Required explicit selection of 1-22 unique canonical feature keys, each permitted by generation policy |

The host binds the new site's name/base URL and the feature selection. Neither
the request nor tool arguments accept owner IDs, revision/history preconditions,
template bindings, credentials, schedules, `trusted_urls`, lane policy or
owner-level `enabled` metadata. The site name stays server-side. Model context
includes only the sanitized site base URL (scheme, host/port and path, without
userinfo, query or fragment), allowed features and code-owned constraints and
warnings. No existing site/template configuration is exported.

Example JSON body, sent with the authenticated session and CSRF header:

```json
{
  "site": {"name": "Wait Example", "base_url": "https://example.com"},
  "requirements": "Propose only a two-second wait. Do not add clicks, form fills or success claims.",
  "evidence": "I observed a brief asynchronous page update after loading the page. No selectors or success state have been verified.",
  "consent_to_send": true,
  "allowed_features": ["action.wait"]
}
```

Requirements and evidence are sent as untrusted user data alongside trusted
code-owned instructions. Consent covers sending that text to the configured
provider; it does not sanitize it. Manually redact secrets before sending it,
including secrets in URLs, page text and copied configuration.

The response model is `GenerationResult`:

```json
{
  "status": "proposal",
  "draft": {"steps": [], "groups": [], "signals": []},
  "explanation": "No workflow actions are proposed until the remaining question is resolved.",
  "unresolved_questions": ["Is a fixed delay necessary rather than a state-based wait?"],
  "requires_review": true
}
```

This illustrates the response shape, not a guaranteed answer to the example
request. All three local draft arrays are present, even when empty. A successful
submit has passed server-side draft validation, including when questions remain,
but the response is **not** an administrator validation preview or a saved site.
It contains the submitted local draft serialized through the shared input models
(which can populate ordinary entry defaults), not the validator's local/resolved
output or materialized administrator feature defaults. `status` is always
`"proposal"` and `requires_review` is always `true`. There is no saved identifier
or approval to run; separately review, explicitly save and revalidate through the
normal authorized workflow before execution.

## Execution Bounds

| Boundary | Limit/behavior |
| --- | --- |
| Incoming raw body | 128 KiB, counted from actual streamed bytes rather than trusting `Content-Length`; 10-second asynchronous body-read timeout |
| Generation loop | 120-second overall asynchronous timeout, starting inside the generation service |
| Provider I/O | HTTPX timeout set to `min(vision_timeout, 120)` seconds, also subject to the loop's asynchronous timeout |
| Model rounds | At most 8 provider requests |
| Tool calls | At most 16 total and 4 per provider response; calls in a batch execute sequentially |
| Draft entries | At most 50 steps, 20 groups and 30 signals |
| Provider response | At most 256 KiB, read incrementally; `Accept-Encoding: identity` is requested and compressed responses are rejected |
| Outgoing provider body | At most 512 KiB per serialized request, including guide, tools, context and accumulated messages |
| Tool arguments | At most 64 KiB per call, checked before and after canonical JSON serialization and again by the dispatcher |
| Tool result | At most 128 KiB per serialized tool response |
| Concurrency | One generation per API process; no queue or cluster-wide lock |

The body-read timeout is separate from the generation timeout. Authentication,
feature-selection preflight and runtime resolution occur before the generation
service's 120-second budget. Local catalog/schema checks and full workflow
validation run synchronously on the caller's thread, with bounded argument sizes
and entry counts. These asynchronous deadlines **cannot hard-preempt synchronous
work or a database stall**, and a blocked event loop also delays disconnect
detection. After each tool dispatch the loop yields and checks its monotonic
deadline before returning a proposal, so an overdue synchronous dispatch cannot
successfully return a late proposal once control resumes. This is not an absolute
wall-clock deadline for every synchronous operation or the entire HTTP request.

A busy process returns `429` with `Retry-After: 5`. Client disconnects are polled
approximately every 0.25 seconds during generation; observed disconnect cancels
the generation task. Parent-task cancellation propagates, pending generation and
monitor tasks are cancelled/awaited, and the process slot is released during
cleanup. A disconnected client may not receive the `499` response.

There are no automatic provider retries, followed redirects or environment proxy
use (`trust_env=false`, `follow_redirects=false`); no SiteFlow proxy lane is used.
Provider errors fail closed, without forwarding the provider's response body.

## Failure Contract

Generation body-validation and generation-service failures return static codes
as `{"detail": {"code": "..."}}`, not raw input, Pydantic error details,
provider output or exception text. Authentication/CSRF retain their ordinary
`401`/`403` responses. GET query validation retains FastAPI's ordinary error
shape; the static body-error contract applies to POST generation.

Protocol failures also include an optional static `detail.reason`, such as
`no_tool_calls` (ordinary answer rather than tool calls), `truncated` (output
budget exhausted), `too_many_parallel_tools` or `submission_not_alone`.
These describe the provider response format, not a safety finding about the
user's request. Logs record this reason with stage, round and provider status;
raw responses, user evidence and reasoning text are not persisted. Historical
failures logged before reason-level diagnostics cannot be reconstructed from
the status code alone.

| HTTP status | Codes |
| --- | --- |
| `503` | `generation_disabled`, `model_unavailable` (missing, unusable or failed runtime resolution) |
| `429` | `generation_busy`, with `Retry-After: 5` |
| `422` | `invalid_request` (malformed JSON/request schema), `invalid_selection`, `secret_input`, `request_limit` (outgoing provider body), `tool_response_limit` |
| `502` | `invalid_runtime`, `provider_http`, `provider_network`, `invalid_response`, `invalid_arguments`, `response_limit`, `argument_limit`, `tool_limit`, `round_limit`, `secret_output`, `generation_failed` |
| `504` | `provider_timeout` (provider I/O or overall generation timeout) |
| `499` | `generation_cancelled` (observed client disconnect) |
| `413` | `request_limit` (incoming raw body exceeds 128 KiB) |
| `408` | `request_timeout` (incoming body-read timeout) |

Schema/policy/workflow failures inside a dispatched tool instead return safe
`ok: false` diagnostics to the model so it can correct the draft within the same
budgets. Transport/protocol failures and exhausted budgets terminate the loop.

## Python Interface

```python
from app.services.config_ai_protocol import generation_guide, tool_definitions

guide = generation_guide()  # {"version": 1, "guide": str, "limitations": list[str]}
tools = tool_definitions()  # list of OpenAI-compatible function definitions
```

Each tool has the shape
`{"type": "function", "function": {"name": ..., "description": ..., "parameters": ...}}`.
Calls return fresh containers. The guide is code-owned trusted text, never built
from administrator settings, page content or plugin descriptions.

| Tool | Arguments | Behavior inside generation |
| --- | --- | --- |
| `list_features` | Optional `category`: `flow`, `auth`, `challenge`, `sign`, `action`, `post` (or `null`) | Read the safe catalog filtered to the host's allowed features |
| `get_feature_schema` | `feature_type`: string, 1-128 characters | Read a selected feature's generation-restricted schema; record that it was consulted |
| `validate_workflow_draft` | `draft`: `{steps, groups, signals}` | Validate the new-site candidate and return bounded, value-free diagnostics with safe locations, never a preview |
| `submit_workflow_draft` | Same `draft`, `explanation`, `unresolved_questions` | Revalidate and return a proposal for human review, not save it |

All three draft arrays are required, including empty arrays. `explanation` is
required and bounded to 1-4000 characters. `unresolved_questions` is required,
contains at most 20 strings of 1-1000 characters each, and is empty when there
are no open questions.

Argument schemas are generated by Pydantic. `WorkflowProposal` directly reuses
`StepInput`, `GroupInput` and `SignalInput` from `app.schemas.workflow`; there is
no parallel copy of the workflow schema. `ListFeaturesArgs`,
`GetFeatureSchemaArgs`, `ValidateWorkflowDraftArgs` and
`SubmitWorkflowDraftArgs` expose the corresponding Python validation models.
Every tool's top-level argument object and the draft wrapper forbid additional
properties. The reused entry models also forbid unknown fields. Defaults and
examples are removed recursively at JSON Schema positions, including `$defs`
and composition branches, without deleting properties literally named `default`
or `examples` or traversing arbitrary annotation values.

These are ordinary OpenAI-compatible schemas, not `strict: true` schemas.
`StepInput.config` is intentionally an open feature-specific object. The
request-scoped dispatcher validates it against the selected feature's restricted
override schema and feature rules, then validates the full new-site candidate
under current server policy/defaults. Structural argument validation alone is
not a secret filter or authorization check. The generation session specializes
the generic tool schemas with feature enums and draft entry bounds.

Every proposed feature's schema must have been consulted in that session before
validation/submission. Protected and unknown config keys are rejected even with
`null` values. Generation additionally withholds image-captcha static `fields`
and restricts credential/TOTP provider references to reviewed builtin values.
Each submission is freshly validated via `validate_draft("site", candidate,
owner_id=None)`; a separate successful validate call is not a substitute. The
model must finish with `submit_workflow_draft` as the only call in its response.
Plain prose/JSON, refusals, legacy function calls, malformed tool batches,
duplicate call IDs/JSON keys and non-finite JSON numbers do not become proposals.
The entire call batch is checked before dispatch, and discarded assistant prose
or reasoning is not appended to subsequent model messages.

There are no target IDs, owner metadata, revision/history preconditions,
credentials, `trusted_urls`, schedules or owner-level `enabled` fields in tool
arguments. Existing step/signal `enabled` fields are legitimate workflow controls
and do not authorize enabling a site or template. Freeform `config` and text
fields are not permission to smuggle protected values or metadata.

### Validation Diagnostics

Both `validate_workflow_draft` and the revalidation inside
`submit_workflow_draft` return a top-level `ok: false` and a `diagnostics` array
on validation failure. At most **10 diagnostics** are returned from the **first
failing validation stage**. This is not an exhaustive scan of every step and
every stage: argument validation, feature selection/schema consultation,
per-step override checks and full effective-workflow validation can stop the
call before later checks run. Fix the reported issues using the safe schema,
validate again, and iterate within the original budgets before submitting.

| Diagnostic field | Contract |
| --- | --- |
| `code` | Allowlisted machine-readable category or known semantic rule |
| `message` | Code-owned static repair guidance, never raw validator/exception text |
| `path` | Safe field-name/zero-based-index array, normally rooted at `draft`; may be empty when no safe location is available; unknown property names are not echoed |
| `step_key` | Optional syntactically safe local draft step key when identifiable, not an administrator/database identifier |
| `feature_type` | Optional reviewed feature key when safely identifiable |
| `field` | Optional schema-owned config field name, not its value |
| `hint` | Optional allowlisted numeric/length constraints such as `ge=0` or `min_length=1`, not defaults or submitted values |

For example, an undefined signal referenced by a step condition can produce:

```json
{
  "ok": false,
  "diagnostics": [{
    "code": "signal_missing",
    "message": "Define the signal referenced by this condition or change the condition.",
    "path": ["draft", "steps", 0, "condition"],
    "step_key": "test_login",
    "feature_type": "auth.form_login"
  }]
}
```

If current policy requires `submit_selector` and it remains absent from the
effective configuration, a separate validation attempt can return:

```json
{
  "ok": false,
  "diagnostics": [{
    "code": "field_required",
    "message": "Supply this required field; omission or null cannot satisfy the current policy.",
    "path": ["draft", "steps", 0, "config", "submit_selector"],
    "step_key": "test_login",
    "feature_type": "auth.form_login",
    "field": "submit_selector"
  }]
}
```

These hypothetical responses contain no actual condition, selector, signal
evidence, administrator default or secret value. They are not a promise that
both errors appear in one response or that an omitted override is always invalid;
current effective configuration and required-field policy determine that.
Known semantic validators supply specific guidance as well: a `flow.jump`
target containing a colon yields `flow_jump_target_invalid`, locates
`draft.steps[index].config.target_step_key`, and says to remove colons and use a
workflow step key, without echoing the offending target. Missing jump evidence
or target have their own `flow_jump_evidence_required` and
`flow_jump_target_required` codes. Unknown validation rules retain static
`field_invalid` guidance; unrecognized workflow/tool errors use static fallbacks
such as `workflow_invalid` or `tool_failed`, possibly with `path: []`. The host
does not parse arbitrary exception messages to guess paths or repair details.

Administrator-readable error strings remain separate from this model-facing
projection. Neither diagnostics nor tool-failure text/logs should expose raw
Pydantic `input`/messages/context, submitted field values, private defaults,
credentials, raw exceptions or local/resolved administrator previews. Safe
locations and bounded numeric/length constraints are metadata, not permission to
export administrator configuration. Do not forward administrator responses or
their errors directly to the model.

## Read-Only Discovery

Authenticated, read-only discovery is implemented under `/api/v1/config-ai`.
These GET routes use non-touching session authentication and return
`Cache-Control: no-store`, including expected error responses. No CSRF token is
needed for GET discovery. They do not invoke the model or execute tools.

| GET path | Purpose |
| --- | --- |
| `/api/v1/config-ai/guide` | Versioned trusted guide and limitations |
| `/api/v1/config-ai/features?category=auth` | Model-safe catalog, optionally filtered |
| `/api/v1/config-ai/features/{key}` | Model-safe schema and reviewed metadata for one feature |
| `/api/v1/config-ai/tools` | The four tool definitions, not tool execution |

The tools response includes `version: 1`, `execution_enabled: false`, `tools`,
`generation_endpoint: "/api/v1/config-ai/generate"` and `execution_scope`.
`execution_enabled: false` means there is no public standalone dispatcher, not
that the internal generation tools are unimplemented: they execute only inside
an enabled, authenticated/CSRF-authorized new-site generation request.
The feature list response contains `version`, compact `features` and
`plugin_policy`. The initial directory includes all 22 reviewed builtins only;
plugin features and plugin captcha methods, descriptions and enum annotations
are excluded. Catalog presence is not a connectivity or credential check.

Feature detail includes `generation_hints`, `config_schema`,
`override_config_schema`, `required_fields`, `excluded_fields`,
`user_input_required`, `defaults_policy` and `schema_scope` alongside its summary.
`config_schema` describes effective configuration; `override_config_schema`
allows omitted top-level fields and `null` clearing markers for local overrides.
Nested required constraints remain intact. The full draft validator remains
authoritative after inheritance and defaults.

Current required-field names are read from server policy, but default values and
execution settings are never exported. Code-owned schemas are used directly to
avoid dynamic plugin metadata injection. Defaults/examples are stripped from all
schema positions. Hidden, read-only, write-only, password-format and model
judgment control fields are withheld. This is an authoring subset, not field-level
authorization: the internal dispatcher applies the narrower generation policy
and rejects protected changes. GET discovery is broader than the generation
allowlist; catalog presence does not authorize `post.*` or `action.fill`.
An unavailable key returns a generic 404; malformed policy returns a generic 422
without reflecting its stored values.

Do not substitute the administrative `GET /api/v1/features` response: its
effective defaults, execution settings and other administrator data are not a
model-safe export. Catalog/schema exports must independently select safe content,
omit administrator effective default values and strip schema defaults/examples.
Removing defaults/examples alone does not make arbitrary descriptions,
annotations or plugin schemas trusted or secret-free.

## Replacement And Inheritance

The following are shared workflow/protocol semantics for future editing and
template integrations, **not current generation capabilities**. Today's endpoint
accepts no existing local state or template binding and proposes only a new
site's local arrays.

A proposal is the complete desired **local** `steps`, `groups` and `signals`, not
a PATCH and not the resolved workflow. Preserve unchanged local entries. Empty
arrays remove local entries; inherited entries can still remain. Preserve
existing `step_key` values when overriding template steps, and preserve group and
signal keys and references. A new key is a new step, not a rename of an inherited
step. Removing a local override restores inheritance; disabling an inherited
step uses its existing key and `enabled=false`.

The resolved view merges the template chain, local overrides and current server
defaults. It is a preview, never a replacement payload to flatten back into
local storage. Inside feature `config`, objects deep-merge, arrays replace,
omission inherits and `null` clears an inherited value. Other fields follow
their own rules: for example an existing local group's `step_anchor` can be
preserved when submitted as `null`. See [workflow drafts](workflow-drafts.md)
and [the feature model](feature-model.md) for existing save/validation semantics.

## Review Boundary

The implemented loop supplies trusted guide/host constraints at the system level
and requirements/evidence as untrusted user data. Request-scoped tools enforce
the host's feature selection, schema consultation, protected-field checks and
full draft validation. Failures expose only allowlisted static codes/messages
and bounded value-free location/constraint metadata as described in
[Validation Diagnostics](#validation-diagnostics), never raw validation errors,
submitted field values, private administrator identifiers/defaults or local/resolved
previews. Safely identified local draft step keys and reviewed feature keys are
repair metadata, not a raw administrator export. Unknown errors retain static
fallbacks; unexpected tool exceptions become `tool_failed`. Validation succeeds with static
warnings that live-page evidence has not been verified and human review is
required. Tools use non-autoflushing read-only validation, with no persistence or
transaction mutation; in-memory conversation/tool history lasts only for this
request.

The runtime API key is used in the provider authorization header, not model
messages. Known-key leak detection checks outgoing JSON, user text, all provider
response fields (including discarded content/reasoning), decoded arguments,
tool results and the proposal. It is **not generic DLP** and does not detect all
other secrets or encoded/transformed secrets. Consented requirements/evidence
and model-authored freeform strings can still contain sensitive material. Review
and manually redact them; do not assume schema validation or static diagnostics
make arbitrary input safe.

A future UI must show the local draft, explanation, unresolved questions and side
effects for explicit human review. Clicks and navigation can have side effects;
purchase and resign/retroactive-check-in features remain blocked, not merely
approval-gated in this implementation. A separate user-confirmed save must
preserve host-owned metadata/secrets and revalidate current dependencies.
Validation does not lock dependencies, authorize execution or guarantee a later
save. Future existing-site/template editing also needs host-held target,
revision/history preconditions and protected-value preservation/removal rules,
including hidden values inside replaced collections and nested config. Never
blindly overlay a model payload onto `SiteWrite` or `TemplateWrite`.

## Administrator Preview Boundary

Existing `POST /api/v1/sites/validate`,
`POST /api/v1/sites/{site_id}/validate`,
`POST /api/v1/templates/validate` and
`POST /api/v1/templates/{template_id}/validate` accept full administrator write
models. Their responses are **not model-facing safe output**. They return local
and resolved previews, and ordinary config strings, selectors, URLs and prompts
can contain manually entered secrets. Even a read-only preview without dedicated
credential fields is not a redacted response.

Never forward these responses or their errors unfiltered to a model. Pydantic
errors can echo input values; resolver errors and warnings can also expose
configuration. The internal generation tools call the draft service server-side
and discard its preview, returning only their bounded, value-free safe projection for both
successful and failed validation. This does not make the existing administrator
validation responses safe for direct model use.

## Generation Metadata

`x-siteflow-generation` is the annotation namespace for optional generation
hints, such as evidence requirements, runtime credential usage and side-effect
warnings. It is advisory data, never an instruction source, authorization grant,
secret store or substitute for schema validation.

Builtins use optional `Feature.generation_hints` metadata with `when_to_use`,
`prerequisites`, `avoid`, `evidence_required` and `examples`. Guidance exists for
all current builtins. It does not alter feature validation or the administrator
feature response and history format.

Plugins may put the same structure at the root of `config_schema`, which already
round-trips through the plugin protocol without a protobuf/database change:

```json
{
  "type": "object",
  "properties": {"seconds": {"type": "integer", "minimum": 1}},
  "x-siteflow-generation": {
    "when_to_use": "Wait for an observed asynchronous update.",
    "prerequisites": ["The user has confirmed the delay is necessary."],
    "avoid": ["Do not treat waiting as success evidence."],
    "evidence_required": ["Observed page update."],
    "examples": []
  }
}
```

`when_to_use` is at most 1000 characters. Each guidance list permits at most 12
strings of at most 500 characters. At most 3 finite JSON examples are accepted,
with a combined serialized limit of 12000 characters. Unknown metadata fields
are rejected by the metadata model; malformed optional plugin hints are ignored
without invalidating the original feature. The annotation is not a property of
executable config. Metadata/schema changes retain normal plugin reapproval rules.

Use code-reviewed builtin metadata when constructing safe catalog responses.
Plugin-provided `x-siteflow-generation` annotations must **not** be automatically
exported, promoted to trusted guide text or treated as approval. Installed or
enabled plugins are not automatically trusted for model instructions. A future
exporter must explicitly review/allowlist plugin fields and annotations and keep
their provenance clear; arbitrary plugin descriptions and schema extensions
remain untrusted. Parsing plugin hints does not enable them in the model-facing
catalog. No external Agent or MCP skills are required by this backend loop.

No runtime secrets or freeform secret values belong in a proposal, including
`action.fill.value`, static form fields, URLs, prompts, labels, explanations and
questions. Use runtime credential features for authentication and keep provider
results out of model context. Redacting known secret strings is not complete DLP;
schema validation and prompt instructions do not replace host-side controls.

## Verification Scope

Backend tests cover the tool/schema/policy boundaries, safe diagnostics,
read-only behavior, mocked model/tool rounds, HTTPX transport limits, timeouts,
authentication/CSRF, busy handling and cancellation/disconnect cleanup. Provider
traffic in these tests is mocked and external network connections are forbidden.
This is not evidence of a real-provider integration test, production model
tool-calling compatibility, live selector/success verification or a completed UI
review/save flow.
