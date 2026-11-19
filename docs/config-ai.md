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
the local workflow of **one new site without a template**. It selects an eligible
OpenAI-compatible connection with registry usage `assistant` and runs read-only discovery,
validation and submission tools inside `POST /api/v1/config-ai/generate`.
Submission returns a proposal for human review, not a saved site or approval to
execute it.

There is no standalone generation/apply UI in the new-site editor. The backend
generation endpoint and shared discovery, guide, tools and model settings remain
available; removing the UI does not remove assistant capabilities.
In this endpoint there is no persisted chat, existing-site or template editing,
template inheritance, public standalone tool dispatcher, MCP server, saving,
scheduling, browser interaction or workflow execution. All **23 reviewed builtins**
can be selected for authoring, including `action.fill`, `post.buy_card`,
`post.resign`, `post.resign_loop` and `post.resign_dialog`. Plugin features,
aliases and unknown keys remain blocked. Authoring purchase/resign steps grants
no execution or consumption authority. The generic
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

The persistent assistant reuses this same flag and ordered model registry, selecting
usage `assistant` once per turn and inheriting that entry's `max_tokens` unless
assistant settings override it; it has no separate provider configuration. Deploy its Web,
API and Worker together. Unlike the single-request API, an accepted assistant turn continues
after tab closure or SSE disconnect and requires explicit cancellation. Its SSE
sends full snapshots, not provider tokens (`stream: false`); request UUID replay
does not rerun a turn, and the UI never automatically resends messages. Assistant
tool titles are short operation/reason summaries, not private chain-of-thought.
It shares the reviewed static builtin metadata and draft validator for all 23
builtins, not arbitrary administrator/plugin metadata; plugins remain excluded.
The proposal alone has no save/run authority. Separately authorized assistant
tools can import a saved proposal and fully test its bound site; see the
assistant guide for those permissions. Its separately authorized live steps
also perform real business operations, as described below.
Its limits differ from the single-request budgets below; use
[the assistant guide](ai-assistant.md).

The current catalog contains **23 reviewed builtins** and the ordinary assistant defines
**38 tools**, with one additional takeover-only conclusion tool (**39 registered**);
takeover sessions expose a restricted subset. Catalog/schema availability follows current code and server policy,
not historical test counts, and does not guarantee production-site compatibility
or business success.

### Persistent Assistant Differences

These capabilities belong only to `/assistant`, **not** `/config-ai/generate`:

- New session creation directly sends `{}`, with no creation dialog or model call. The server writes a fixed initial assistant greeting and the default title. Only successful main turns among the first three accepted user messages may trigger a separate, small, tool-free title request using the same configured model. Failed/cancelled user turns still consume that window; title failure preserves the title and is not retried. Manual rename permanently locks automatic updates. Title lock/revision/attempt fields use existing encrypted JSON, requiring no column migration; old sessions missing those fields are treated as locked. The title request has at most 10 seconds within the remaining 180-second turn budget, not an extra budget.
- `list_sites`, `get_site_config`, `list_templates`, `get_template_config`, `list_recent_runs`, `get_run_summary` and `get_system_status` return non-secret site/template metadata, sanitized local/resolved workflows and signals, run/job metadata, integration configuration sources and lane status. Inline fill/static-field values and sensitive config keys are omitted; URL credentials/query/fragment are stripped. This is a dedicated projection, not the raw administrator preview and not generic free-text DLP. These metadata tools do not return raw logs or screenshot pixels; separate historical artifact tools below do. No tool permits arbitrary SQL, shell, JavaScript, file paths or secret retrieval.
- Site list/detail metadata includes `history_key` (in `_SITE_FIELDS`), as does template metadata, for UUID-based historical discovery. `list_sites` / `list_templates` accept name `query`, `offset` (default 0), and `limit` 1-100 (default 100), ordered by name/id ascending. `list_recent_runs` accepts offset and limit 1-20 (default 20); `get_run_summary` accepts offset and limit 1-100 (default 100) to page **jobs**, retaining overall run metadata/counts. Responses include offset/limit/next_offset (null at the end) and `truncated` (`jobs_truncated` for summary), with no total guarantee. These are live offsets, not snapshots: concurrent changes can overlap/skip rows; deduplicate IDs and refresh. See [data contracts](ai-assistant.md#数据查询).
- In `get_system_status`, CFbox is configured with a URL alone; its API Key is optional. Non-Bitwarden integrations remain metadata-only, with `available: null` and `health_checked: false`, not a health verdict or integration decryption. Top-level model fields explicitly carry `model_config_scope: "environment_only"`, not the DB-resolved effective model or current assistant overrides. The tool does not enumerate registry entries, priority, names, usages, connections or all-model metadata. The separate safe Bitwarden health projection retains its documented boundary.
- `GET /api/v1/assistant/sessions` accepts offset>=0 (default 0), limit 1-100 (default 100), and returns the same paging metadata without a total guarantee. The UI can load more sessions, not just the first 100; updated_at/id descending offsets are live, with ID deduplication and refresh needed for concurrent changes. Detail/SSE still show the latest 200 messages, not old-message pagination. `browser_navigate` requires exactly one address: path up to 2048 or full URL up to 4096 characters, aligned in runtime and schema. These contract changes add no tools: 38 ordinary, 39 registered.
- Host target settings bind the origin, credential mode, proxy and optional saved `site_id`. The user confirms unified execution permission in the host UI; the API derives `execution_consent`, the frozen reviewed `execution_features`, credential-source-dependent `credential_consent`, and `allow_sign` for signing, clicks, public fills and challenges. Old targets retain only their saved permissions until the user reconfirms; neither an upgrade nor a retry expands the initial scope. Model text, chat approval, DOM, feature schemas and step conditions cannot grant permissions. Extra `trusted_origins` authorize navigation/observation only, never cross-origin execution or secret use.
- Manual stored-account tests require a persisted site with exactly the same origin (scheme, host and effective port). The API freezes its ID, history identity and configuration revision and rechecks them before execution; changes require reconfirmation. Bitwarden can privately resolve credentials for an unsaved new target using full-domain matching, not inherited root/custom matching, with explicit credential authorization. All credential modes may privately seed saved cookies from a validated explicit saved-site binding before a new browser's first navigation: `workflow_site_id` takes precedence over credential `site_id`, with no fallback from a stale preferred binding and no hostname lookup. This grants no auth, OTP or full-run permission; `cookie`, `none` and unbound manual targets still cannot authorize the two auth features or `challenge.2fa`. Manual login in a separate same-lane user tab remains an alternative, not the only Cookie-mode path.
- `clear_bound_site_cookies({operation_id})` adds destructive clearing, not secret retrieval or arbitrary Cookie writing. A fresh UUID4, explicit human request, already READY original-bound browser and fresh internal `cookie_clear` scope review are required; never automatically open a browser to clear. Only the exact bound saved Cookie row and current shared lane's host/.host cookies on all paths are cleared. Parent/sibling domains and other lanes, saved account/password and localStorage remain; same-host same-lane tabs may log out, with no reload. Active site runs block clearing; saved/browser outcomes are separate, partial/unknown outcomes are not automatically retried and same-ID replay returns the stored result only. Autonomous takeover cannot clear; human daily-chat follow-up can request normal review. Private seeding is backend-to-Worker only, never raw model/tool/session data, and does not export login-browser cookies or save them back. See [bound-site Cookie contracts](ai-assistant.md#绑定站点-cookie) for exact-origin/domain filtering, generation invalidation, 300-cookie/256-KiB limits, context-wide token fingerprints and residual privacy risks. API, Worker and Web must be upgraded together.
- Exactly **18 builtins** support isolated tests: all 23 except `flow.jump` and the four `post.*` features. This includes 2FA, Turnstile, character/grid captcha, iframe image-captcha sign and image quiz, plus public `action.fill` and boolean-only `flow.result`. See the [complete list](ai-assistant.md#单步验证). Isolated post execution is **not implemented**, because it needs real SiteJob consumption checkpoints, not because the user denied permission; use a saved full workflow. Plugins remain unavailable.
- `execute_step` runs one enabled, unconditional step on the existing current page, not workflow conditions/groups/jumps. Activity is bounded to 90 seconds, the shorter step timeout and remaining browser TTL/turn budget. Navigation retry is forced to zero; no automatic step redispatch occurs. Results contain sanitized status and available before/after **structured DOM state snapshots, not PNG screenshots**, HTML or raw runtime logs. CAPTCHA handling does not impose a summary-only evidence rule. Real sign-in/check-in submits to the website and requires explicit authorization; submission is not proof of success.
- All `challenge.*`, `sign.iframe.image_captcha` and `sign.image_quiz` use host-configured providers/runtime handling. TOTP is limited to `builtin.twofauth`, requires credential consent and a bound, credential-ready same-origin manual site or Bitwarden source. Actual OTP/2FA authentication codes, TOTP seeds, passwords, cookies, tokens, host-private prompts and API/provider keys remain private: never request or echo them. Image CAPTCHA OCR text and solver answers are not secrets merely because they solve a CAPTCHA. A user's “它不是秘密” can clarify a CAPTCHA answer, never declassify actual OTPs or credentials. A configured solver/vision provider may process challenge images; this is not a claim that no model processes them or permission to retrieve raw provider responses.
- CAPTCHA handling does **not lock the browser**: observe/inspect, selector checks, separately consented screenshots (including UI preview), navigation and authorized follow-up single steps remain available on the same page without reopening. Sequential CAPTCHA/login/signing tests do not require a saved full run merely because they include a CAPTCHA. All DOM/selector evidence still omits input values. Screenshots mask input controls in the main page and readable same-origin frames, and conservatively mask other frames in full. Visible page text may reflect a CAPTCHA answer, which is acceptable; masking is not complete pixel-level secret redaction, and visible personal information or other secrets may remain. Existing authorization, origin, uncertainty and operation-ledger rules still apply.
- Private isolated Turnstile requires explicit observation trust for `https://challenges.cloudflare.com` and one unique visible supported Cloudflare challenge iframe on the current assistant page (HTTPS, `/cdn-cgi/challenge-platform/` path). Worker pins its original node, source, document and bounds and rechecks before action. CFbox coordinates must fall inside and hit that pinned target; non-challenge coordinates cause no action. This is page-scoped challenge handling, not arbitrary full-page coordinate clicking, a full-page-challenge substitute, desktop/X11 or another-tab authority. Observation trust does not authorize sending account credentials to Cloudflare.
- Private provider responses echoing known provider keys are rejected rather than filled into the website or used for action. Secret input revalidates original targets after focus, then validates/inserts atomically to prevent focus handlers from redirecting input to replacement nodes. These controls are not generic DLP or a production success guarantee.
- `action.fill` accepts explicit public nonsecret literals only, never passwords, account credentials, cookies, tokens or OTP/TOTP authentication codes; host field/content checks still apply, and image CAPTCHA answers are distinct from OTPs. Isolated `flow.result` returns the configured boolean without model judgment; false is a failed step result, not proof the website operation failed, and must not be changed to true to hide failure. Full-workflow result judgment retains host policy; its runtime connection, prompts and provider keys are host-supplied privately to Worker. The assistant receives safe boolean results/status only, never raw model answers, judgment reasons or keys.
- API-resolved stored secrets go privately to the Worker, never to model messages, chat or proposals. Worker capability validation binds the original unique visible input/submit nodes to the same original form, same-origin action/formaction and current window; password targets must be editable password inputs. It revalidates before secret use. A form need not declare POST: existing page JS may prevent native GET submission and POST to another compliant path on the original origin. There is no per-form POST endpoint approval or model-authored JS. GET/HEAD remain the default; POST opens only during an authorized non-read-only step, never for navigation/wait. Cross-origin POST, PUT/DELETE and known secrets in any request URL remain blocked. Afterward POST closes and original-origin restrictions remain, even for trusted observation origins. No SSO/trusted-URL expansion is granted.
- The API persists a stable operation UUID before dispatch/credential release and rejects reuse; the Worker binds it to the complete request fingerprint and returns only in-progress/cached terminal state on replay. Unknown, timeout, cancellation or a lost response is not proof of no submission: inspect read-only within current permissions without exposing input values, never change UUIDs to bypass uncertainty. CAPTCHA handling does not require reopening; reopening does not clear uncertainty. Limits are 20 operations per browser and a persistent 256-ID ledger per chat session, unaffected by target changes or browser reopening.
- Only code-owned builtin `Feature.category` is trusted: `flow`, `auth`, `challenge`, `sign`, `action`, `post`. Key prefixes, descriptions, model arguments and plugin metadata confer no category authority. Plugins may declare these six values, but remain unavailable to the assistant: metadata is unverified and dangerous actual implementations need separate review.
- Assistant isolated steps and saved full runs share the originating real user turn's `max_auth_uses_per_turn` for auth login/password-verification attempts and retries: default **3**, strict integer **1-20**. Each attempt is reserved before credential resolution and failures are charged. Full runs charge once when each new runtime auth credential lease is issued, not again on redemption; conditionally skipped steps are free. Loop re-entry and executor restart/fallback attempts requesting new leases charge again. Background runs retain their originating turn and frozen limit even after chat ends or a new user turn begins; internal model rounds, browser reopening and target changes do not reset it. A new auth-containing `run_site` checks remaining capacity before dispatch without precharging every configured step; exhaustion at runtime denies further auth leases with `auth_budget_exhausted`, not unlimited full-workflow auth loops. Result reads and same-run replays are free; ordinary manual/scheduled jobs are outside this assistant quota. `challenge.2fa` is challenge, not auth-budgeted; its OTP remains private and credential consent remains required.
- Supported same-original-origin flow/challenge/sign/action single steps remain permitted with current host consent despite historical unknown outcomes. In ordinary chats, `retry_step` takes only the original `operation_id` and public `title`; the host automatically issues one-use exact-stored-step authority and executes with a fresh UUID, **without a UI approval prompt**. Auth retries charge another shared use. Current target/browser/credential bindings, authorization, duplicate-operation checks and secret boundaries remain enforced; no step edits or post isolated execution.
- Legacy idle **允许重试一次** and retry-approval APIs remain for compatibility and optional manual use, not a prerequisite or evidence that all retries require approval. Legacy grants remain five-minute, browser-bound and single-use; creating one alone does not execute. Legacy pending approval waits at most 180 seconds, denial/timeout/cancellation grants nothing, and restart does not resume execution.
- Original unknown history/ledger/safety provenance persists; unrelated success never clears it, and retry success does not prove the original failed. Current category policy and linked host resolutions take precedence over historical `no_retry` / `requires_host_approval` prose. Configuration-write/import and full-run uncertainty guards are unchanged. Category permission is user policy, **not proof of no website side effects**: physical clicks, navigation and scripts can still change the website, not only post features.
- Successful assistant draft validation/submission also returns `required_site_settings` next to the proposal: `credential_mode`, `credential_provider_id`, `lane_policy` and a notice. Users must apply these host-confirmed provider/proxy settings separately to reproduce the tested environment; workflow arrays do not carry credentials or Cookie values.

### Template, Scheduler And History Tools

The nine additional assistant tools are `create_template_from_site`,
`get_scheduler_settings`, `update_scheduler_settings`, `cancel_run`,
`list_workflow_versions`, `get_workflow_version`, `list_run_artifacts`,
`read_artifact_log` and `read_artifact_image`. They are **not** added to the four
tools of `/config-ai/generate`. All assistant calls also require a public `title`.

Manual template editing has **从站点导入配置**: select a site and confirm replacing
the current form's local `steps`, `groups`, `signals` and `parent_id` (mapped from
the source `template_id`). It does not flatten resolved inheritance, copy site
metadata/credentials, change the source, save automatically or execute anything.
The destination form's name, description and enabled state remain unchanged;
review, validate and save separately. The read-only candidate endpoint is
`GET /api/v1/templates/import-from-site/{site_id}`. Active/uncertain source jobs,
invalid/empty workflows and cyclic inheritance prevent import.

At an explicit user request, `create_template_from_site` instead saves after an
exact host review. Its source is only the saved, host-bound `workflow_site_id`,
never the credential `site_id` or an arbitrary model-supplied source ID. Arguments
are `operation_id`, `name`, optional `description`, and optional
`destination_template_id`, `expected_revision`, `expected_history_key`. Existing
destination replacement requires both current revision and history UUID. It
replaces the complete local arrays and parent, not a merge, preserving destination
name/description/enabled. It never copies site settings/secrets, changes the
source or runs a workflow. A valid configured source without active or uncertain
jobs **does not require a successful assistant full run**. Global authorization
never bypasses review. The host freezes source/destination snapshots and ancestor
dependencies, rechecks them on approval and uses conditional updates and CAS;
drift, denial or timeout writes nothing. History and encrypted action receipt
commit together. Exact UUID/request replays return the receipt while the source
binding remains valid, rather than saving twice.

Replacement must retain every original **resolved** group identity, name and
relative order, and every original resolved step identity, group and relative
order, including inherited items. The backend checks this at preparation and
apply; incompatible source content is rejected, never implicitly merged back.
Existing step configuration may change, and new groups/steps may be inserted
anywhere. Numeric `position` and `step_anchor` values may change as long as the
original relative order remains intact. The public review contains the exact
source-local `steps`/`groups`/`signals` arrays and parent, plus `before` containing
the previous destination's **local** arrays and parent (`null` for creation), not
flattened resolved workflows. The UI expands the full candidate and keyed
before/after differences; protected configuration fails closed, not as hidden
markers. Legacy persisted approvals without `before` report an unavailable
baseline rather than guessing. Manual editor import behavior is unchanged. See
[saving a template from a site](ai-assistant.md#从站点保存模板).

`get_scheduler_settings` reads **global** settings, source/environment lock,
revision, timezone and next-run time without a target. Only an explicit global
scheduling request permits `update_scheduler_settings({operation_id, patch})`.
Its nonempty sparse patch allows only `enabled`, `mode` (cron/interval/random),
five-field `cron` (up to 128 characters), `interval_seconds` (strict integer
60-2592000) and `ranges` (up to 20 same-day HH:MM-HH:MM ranges). Omission preserves,
null/unknown fields and no actual change are rejected. Every exact patch requires
authenticated host review, regardless of automatic authorization. Any scheduling
environment override locks editing of the global schedule; timezone is
environment-only and is not writable here. A timezone override alone does not
lock the other fields. The host freezes revision/raw configuration/environment,
rechecks locks and uses revision CAS with an encrypted receipt. Dynamic
`next_run_at` is excluded from the binding, so ordinary timer advancement does not
invalidate approval; a saved change recalculates it. Exact operation replays do
not write again; stale bindings, denial and timeout do not apply changes.

`cancel_run({run_id})` requires an explicit request and exact host-built run review,
not generic workflow consent. It can address historical/current UI-accessible
runs, not just conversation-created runs. Approval commits cancellation before
calling the real shared `executor.cancel` for a still-active run. Queued runs are
atomically canceled; running runs receive a cancellation request. Report actual
`status`, `cancel_requested`, `finished` and `worker_cancel_delivered`:
**requested/delivered is not proof the worker stopped** and does not undo effects.
Terminal runs return their current state. Stopping the chat turn is separate and
does not cancel a submitted run or clear uncertainty.

`list_workflow_versions({history_key, offset, limit})` and
`get_workflow_version({history_key, revision})` read sanitized current/archived
history by UUID, including local/resolved configuration, dependencies and feature
snapshots, without restoring anything. `list_run_artifacts({run_id, job_id?,
offset, limit})` lists database artifact IDs and metadata, not file paths. List
limits are 1-50. In ordinary chats these historical reads use the authenticated
management UI's read scope, not a conversation-created-run restriction.
`get_run_results` / `get_run_log` retain their conversation-run restriction and
curated projections; takeover has a host-bound source-job exception, not arbitrary
feedback access.

At the user's request, `read_artifact_log({artifact_id, offset, limit})` reads the
**actual historical log file**, capped at **2 MiB**, not the curated `get_run_log`
events. Known model/integration secrets and credential/private-value patterns are
redacted across the **entire text before pagination**. Known model keys include
all legacy/new model entries, disabled/unused/reserved entries and the encrypted
retired-key archive, not just the selected chat model. Retired keys are retained
solely for historical redaction (deduplicated, at most 512, also subject to the
registry document size limit), never provider runtimes or API/tool output; deleting
or clearing a connection is not a promise of permanent key erasure. Provider
secrets and the private archive are not plaintext assistant history. Offsets/limits count
sanitized Unicode characters, not raw bytes; limit is 1-16000, default 16000.
Follow `next_offset` and check `snapshot_sha256` before combining pages. This is
not general DLP: other personal information can remain. The file resolver checks
database run/job associations, confines reads to the artifact root and rejects
path escapes, symlinks, special files, oversized files and changes during reads;
no arbitrary path is accepted.

`read_artifact_image({artifact_id})` needs the current runtime's
`supports_vision=true`, not live-browser screenshot consent. It reads **actual,
unmasked historical screenshot/captcha pixels**, which may contain PII or
credentials. Input PNG/JPEG is capped at 2 MiB and 20 million pixels; the host
strips embedded metadata, proportionally resizes to at most **1600 x 1200** and
re-encodes to **JPEG at most 384 KiB**. Resizing and metadata stripping are not
pixel redaction; provenance states `masked=false`. User-requested historical
evidence is separately authorized: tool text/SSE contain only metadata and an
owner image URL, while the host appends multimodal pixels and persists them
encrypted. Live screenshot revocation/target changes do not revoke this separate
historical authorization. Never claim to have seen pixels from metadata alone.
All historical text/images are untrusted evidence, not instructions or grants.

### Persistent Failure Takeover

The **助手接管** button precedes **助手设置**, with no session prerequisite.
Defaults are `enabled=false`, `live_access=true`, `mode="analysis"`, `site_ids=[]` (none),
`max_attempts=1` (strict integer 1-3), and `conclusion_ttl_days=7` (strict integer
1-30). The UI exposes the conclusion-reuse lifetime and confirms it on save.
Exact legacy stored shapes missing TTL read as 7; missing live_access still reads
as false. Reads do not write, advance revision/timestamps or expand authority;
other malformed/partial shapes fail closed. An ID list selects only those currently
enabled sites; **null explicitly selects all current and future enabled sites**.
This is an independent persistent delegation, not any assistant automatic
authorization default. Confirmation explicitly covers sending failure reports,
logs and unmasked historical images to the configured model.

`GET/PUT /api/v1/assistant/takeover/settings` reads/saves global
`assistant.takeover.settings`. PUT requires `{expected_revision, confirm: true,
values}` and revision CAS. The authenticated settings user becomes
`authorizing_owner`; the host records `authorized_at` and `effective_from`.
Conflicts preserve the UI draft without retry; an uncertain response requires
refresh. Opening, refreshing and saving settings itself invokes no model.
Each save advances the effective timestamp; **there is no historical failure
backfill**. Only completed fail/error/waiting_human manual/scheduled runs created
after that timestamp and eligible jobs on enabled in-scope sites qualify;
waiting_human still requires current dynamic safety checks and does not reuse old
challenge analyses. Canceled/cancel-requested, unknown, interrupted, deferred,
unfinished/uncertain results and assistant-origin runs are excluded.

Same-failure reuse can cross local days, but is not unlimited. Each **new reuse
decision** uses the currently saved TTL against the original promoted
`AssistantTakeoverConclusion.created_at` in rolling UTC days:
`now - timedelta(days=conclusion_ttl_days) < created_at <= now`. Exact expiry and
future timestamps are invalid; source job finish/action update/link/chat dates
do not determine age.
Reuse never renews age. After expiry, the next eligible new source follows normal
investigation; expiry does not backfill, requeue existing reused actions or delete
records. Existing history/reused projections and frozen notifications remain
readable regardless of age, subject to existing integrity checks. Changing TTL
uses the unchanged policy-save CAS/revision/effective_from behavior, not a separate
retroactive update. See [same-failure reuse](ai-assistant.md#相同失败去重).

With persisted `live_access` and current host scope, either mode may browse and
perform supported same-origin non-post single steps under category policy and
the shared auth cap, despite historical unknown outcomes. Without live access,
neither mode can browse or execute. `analysis` forbids configuration saves, not
website side effects. Autonomous `retry_step` is currently denied; permitted new
`execute_step` calls do not make that retry tool available.

Analysis can inspect source evidence and propose a validated draft but never
save. Autofix can import one validated saved proposal into **only the existing
bound source site's workflow** when the actual failed dispatch snapshot still
matches current identity/revision/attempt/lane/configuration/dependencies/policy
and no active or uncertain work exists. Missing/stale snapshots downgrade to
analysis. It cannot fully rerun, cancel, create sites,
change credentials, site metadata, templates or global settings; **it never
automatically runs the saved fix**. Artifact reads are restricted to the source
run/job, and global mutation defaults are suppressed.

Every tool and import transaction rechecks persisted owner, policy revision,
scope and source authority; disabling or changing policy revokes old-revision
authority, without undoing committed writes or withdrawing already-sent data.
The 1-3 attempts bound model analysis, not business execution retries. Unknown
dispatch outcomes are not blindly retried. On restart, a dispatched action with
an exact accepted user message in a valid same-owner session remains eligible
for completion-only recovery from persisted evidence; no model or site action
is replayed. Without that binding, the restart fallback remains updated with an
import receipt, otherwise unknown. See [conclusions and recovery](ai-assistant.md#结论与通知)
for typed-outcome checks and atomic finalization. The dialog shows the
current owner's latest 20 audit records and links to their sessions;
`GET /api/v1/assistant/takeover/actions?limit=100` returns only the current owner's
records (limit 1-100). Updated means saved, not successfully tested; completed
analysis is not proof of a business fix. See the [assistant guide](ai-assistant.md)
for full contracts and the complete 38-tool ordinary list (39 registered).

Assistant full saved runs support all 23 builtins. Each actual new dispatch with
enabled `post.buy_card`, `post.resign`, `post.resign_loop` or `post.resign_dialog`
requires a fresh owner review dialog for EACH enabled post step, including
conditional steps. All step dialogs must be approved before the full workflow
starts; each approval covers that step's listed dates and bounded internal
clicks, with no popup per date or internal click. Global
auto-authorization cannot bypass it. The host binds the exact saved workflow,
configuration/policy fingerprint, all eligible past dates (up to a 365-day window),
timezone/day, bounded purchase quantity and a **positive `max_price`**. Quantity
is an integer 0-365; 0 auto-fills pending need minus inventory, still with a
reviewed maximum quantity/spend. Approval is one-use and rechecked before dispatch;
runtime dates and capabilities are private host data, not model parameters.
These consuming assistant runs have no automatic fallback/restart/retry attempts.
Same-configuration/proposal replay returns the original run ID without another
prompt or submission. Unknown full-run outcomes cannot be replayed through
another tool; permitted non-post single steps do not resolve the full-run or
configuration-write guards.

This gate covers assistant invocations only, not a new gate on normal manual or
scheduled runs, which retain existing policy. Import preserves an existing site's
enabled/scheduler state, so importing post steps into an already-enabled site can
affect later scheduled execution; review that impact before import. New sites
remain disabled. Generic clicks/navigation can have unknown business effects:
the prompt prohibits using them or fills/loops to bypass consumption review,
but this is not a guarantee that every website consumption action is detected.
See [purchase/resign review](ai-assistant.md#购买与补签审核).

The public assistant API adds `PUT /api/v1/assistant/sessions/{uuid}/title` with
`{title}` and returns `title`, `title_locked`, `title_revision` in session views.
Its `PUT /sessions/{uuid}/target` accepts required name/base URL/mode/proxy and
literal unified `authorized: true`; optional `site_id` defaults to null,
`trusted_origins` to `[]`, and separate `screenshot_consent` to false. The server
derives execution/credential/side-effect permissions; legacy granular fields
remain for existing callers, not the main UI flow. Legacy running retry decisions use
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

Generation resolves `IntegrationService.model_runtime("assistant")` once before
the loop, selecting the first saved entry that is enabled, configured and assigned
usage `assistant`. It does **not** call the legacy-entry compatibility resolver
`vision_runtime()`, use `feature_cards` or accept a registry ID/provider connection
in the generation request. Disabled/wrong-usage entries do not block
other eligible entries. No eligible entry or an unreadable/corrupt registry returns
static 503 `model_unavailable`, without provider calls. This endpoint remains
**text-only**: images are not sent and `supports_vision=false` is eligible.

The selected base URL, model, API Key and per-entry `max_tokens` are frozen for all
generation rounds; requests use global `SITEFLOW_MODEL_TIMEOUT` (default 30 seconds,
maximum 300). The provider URL
must be an HTTP(S) `/v1` root without credentials, query or fragment. Calls go to
`/chat/completions` with bearer authentication, `tool_choice: "auto"` and
`stream: false`. Positive `max_tokens` is sent unchanged; unset/null or 0 omits it,
using upstream defaults, not guaranteed unlimited output. Registry PUT omits a
field to keep it and uses null/0 to clear its cap (0-2147483647); the modal turns a
cleared input into null, while the legacy `/vision` PUT also accepts blank strings.
Persistent assistant Token overrides do not apply to this single-request API.

One optional ordinary environment entry uses `SITEFLOW_MODEL_BASE_URL`,
`SITEFLOW_MODEL_ID` (upstream model name), `SITEFLOW_MODEL_API_KEY`,
`SITEFLOW_MODEL_SUPPORTS_VISION` and `SITEFLOW_MODEL_MAX_TOKENS`. Any one present,
including false/0, locks **the entire connection**, not just that field. The row
has the UI source label **环境变量**, `source="env"` and `config_editable=false`;
it has no editable frontend modal and cannot be deleted. Order/enable state remain
saveable in the UI; name/usage metadata can only be updated through the API,
not edited in the UI. Even an
incomplete ENV connection must be repaired in deployment settings. Other entries
are DB-only and do not inherit these values. `SITEFLOW_MODEL_TIMEOUT` is global;
setting it alone does not create or lock an ENV entry. **Old `SITEFLOW_VISION_*`
names are ignored without alias fallback; rename deployment variables.**

The environment cap is an optional nonnegative integer output limit: 4096 sends
`max_tokens=4096`; 0 omits the parameter and uses provider defaults, not unlimited
output. Leave its line commented to retain the DB cap; an empty value becomes 0
and creates/locks the entry. Effective legacy values still merge per field with
DB, but all connection fields remain locked while any ENV connection setting
exists. Omit the vision flag unless an ENV entry is desired; it uses the saved DB
flag or defaults to true. To clear the DB cap in the UI, remove all five ENV
connection settings first, then save blank/0.

With no registry, reads expose revision 0 and an empty list unless legacy DB rows
or environment connection settings exist; those expose one ordinary enabled entry
for `feature_cards` and `assistant`, with MCP off. A missing ENV entry is appended
to an existing registry, not forced first. No migration writes or changes to
existing `vision.*` ciphertext/AAD or cookies occur. Internal ID `primary` and
legacy encryption keys are compatibility details, not a model role. Deleting the
legacy DB entry removes its active connection too, preventing resurrection; archived
keys are used only for log redaction, never runtime or entry recovery. There are no
new SQL columns; upgrade API, Worker and Web together and rename deployment variables.

The persistent assistant likewise selects once per accepted turn, reusing the
connection for conversation, title, summary and failure takeover. A text-only
assistant cannot auto-switch to another entry for live/historical images; current
vision and host-consent gates still apply. Host-private feature execution instead
selects `feature_cards` by each feature's vision need, and full workflows use a
private per-feature runtime map: text result judgement may use an earlier text
model while a later image CAPTCHA uses a visual entry. `ddddocr` invokes no LLM.
The `flow.result` model override remains only an upstream model-name string on the
selected provider, never a registry ID/provider selector.

Selection is not health checking or request-failure fallback. Provider HTTP/network
failures, timeouts or invalid output never try the next model automatically.
Thinking models may spend the budget on reasoning; reserve enough for final tool
calls. Reasoning is never an answer/proposal, and existing timeout/response-byte
budgets are unchanged. A successful image/text self-test does not establish the
required OpenAI-compatible tool-calling capability.

Registry usages do not grant tool or website permissions. `mcp` is reserved only,
not an implemented runtime/server, external identity or authorization grant. The
full-width Settings **大模型** card provides explicitly saved ordering/temporary
disable metadata and DB connection modals (**0-20 DB models including any legacy
DB entry, plus one optional environment model, at most 21 total**, with no
mandatory model). The environment model consumes no DB slot: all 21 rows remain
visible when it appears alongside 20 DB models, without truncation, dropped rows
or disabled list operations. DB additions are limited by DB count, not total rows.
The concise hint is
**保存前请测试连通性，测试不代表保存配置**. Row tests use a specific saved ID, not drafts
or the first selected model; modal tests can preview unsaved connection drafts.
New/typed keys use browser-direct tests with no backend fallback; blank existing
keys use the API's in-memory merge for that saved ID. Testing never saves, and
browser CORS/30-second limits and API-versus-Worker reachability boundaries apply.
Whole-list PUT uses revision CAS; conflicts preserve drafts with no automatic
retry, and confirmed refresh discards drafts before re-editing. Legacy-entry
PUT/DELETE also advance that revision. See [model registry](vision-feature-settings.md#model-registry)
for API, legacy clearing/deletion, encryption, repairable legacy field errors and retired-key
redaction limits. These contracts do not claim production deployment or live-site
validation.

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
| `allowed_features` | Required explicit selection of 1-23 unique canonical builtin feature keys, each permitted by generation policy |

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
`plugin_policy`. The directory includes all 23 reviewed builtins only;
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
authorization: the internal dispatcher enforces the explicit host selection,
reviewed provider/field restrictions and rejects protected changes. All 23
builtins may be selected for authoring, including `post.*` and public nonsecret
`action.fill`; catalog presence grants no execution or consumption authority.
An unavailable key returns a generic 404; malformed policy returns a generic 422
without reflecting its stored values.

Do not substitute the administrative `GET /api/v1/features` response: its
effective defaults, execution settings and other administrator data are not a
model-safe export. Catalog/schema exports must independently select safe content,
omit administrator effective default values and strip schema defaults/examples.
Removing defaults/examples alone does not make arbitrary descriptions,
annotations or plugin schemas trusted or secret-free.

## Replacement And Inheritance

The following are shared workflow/protocol semantics, **not capabilities of
`/config-ai/generate`**. That endpoint accepts no existing local state or template
binding and proposes only a new site's local arrays. The persistent assistant's
existing-site import and site-to-template tools are separate implemented paths
with host-held bindings, exact reviews and CAS as described above.

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

A future standalone UI must show the local draft, explanation, unresolved questions
and side effects for explicit human review. Clicks and navigation can have side
effects; purchase/resign authoring is allowed here, but this endpoint never saves
or executes any feature. Assistant purchase/resign execution is full-run-only with
fresh bounded consumption review. A separate user-confirmed save must
preserve host-owned metadata/secrets and revalidate current dependencies.
Validation does not lock dependencies, authorize execution or guarantee a later
save. Existing-site/template integrations need host-held target,
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
questions. Use runtime credential features for authentication and keep secret
provider results out of model context; image CAPTCHA OCR/solver answers are not
blanket-classified as secrets. Redacting known secret strings is not complete DLP;
schema validation and prompt instructions do not replace host-side controls.

## Verification Scope

Backend tests cover the tool/schema/policy boundaries, safe diagnostics,
read-only behavior, mocked model/tool rounds, HTTPX transport limits, timeouts,
authentication/CSRF, busy handling and cancellation/disconnect cleanup. Provider
traffic in these tests is mocked and external network connections are forbidden.
This is not evidence of a real-provider integration test, production model
tool-calling compatibility, live selector/success verification or a completed UI
review/save flow.
