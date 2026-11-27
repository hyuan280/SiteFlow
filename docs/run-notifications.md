# Durable Run Notifications

Terminal run transitions register one encrypted notification intent per run in
the same database transaction. The original task notification is sent
immediately when the task finishes: it does not wait for takeover and does not
make an extra model call. Explicit notification requests are idempotent.
Startup resumes existing intents only; it does not create notifications for
historical runs that have no intent.

## Immediate Task Notification

Before the original task notification is sent, the host performs local takeover
planning and same-failure deduplication. This step uses only the stored run/job,
policy, snapshot, signature and conclusion facts; it does not call the model and
does not open a browser.

Before reusing an already queued action, registration rechecks the current policy
revision and authorizing owner, site history identity/origin, exact source job
binding and eligibility. A changed or revoked delegation marks that action
`revoked` instead of reusing a conclusion or adding a reviewing annotation.
Active jobs, source eligibility and the current dispatch snapshot are also
checked before historical reuse; a matching signature alone grants no authority.
Unrelated historical unknown records do not block diagnostic planning or reuse
of a valid conclusion. Reuse dispatches no new actions and does not clear mutation
or import safety gates.

- A job that needs a fresh takeover investigation prefixes its original sanitized
  site message with `助手复查中，结果稍后发出。原任务结果：`. The original task
  layout, status, counts, other messages and channel filters remain unchanged.
- A valid reusable historical conclusion is included directly in that site's
  outgoing message with its historical-source wording. It is not replaced by the
  fresh-investigation message and does not produce a second investigation notice.
- A job without an eligible takeover keeps its original sanitized message.

If local planning fails, notification through the eligible channels falls back
to the original run result and the original execution is not rolled back.
The event, rendered message and eligible delivery targets are encrypted and
frozen at registration in a `ready` intent. Dispatch uses that snapshot rather
than rereading later model results or settings. A task that needs a model turn
therefore already has the reviewing annotation in its frozen event.

Original task notifications have no image attachment. For HTTP targets with an
image field configured, they use `multipart/form-data` without a file part.

## Separate Takeover Result Notification

When a fresh takeover later finishes with `completed_analysis` or `updated` and
a valid concise conclusion, the host sends a **separate** takeover-result
notification. It is not a rewrite or resend of the original task notification.

Only channels that are enabled and have the per-channel `notify_assistant` switch
on participate. `notify_assistant` defaults to true and is independent for email
and HTTP. It does not depend on the original task notification's success/manual
filters. A channel that is disabled or has the switch off receives no
takeover-result notification. Channel delivery is independent: one channel
failing does not block another, and no channel is retried automatically.

Each email/HTTP channel card has a switch labeled "接收助手调查通知", on by
default. Its description distinguishes the original task's success/manual filters
from the separate concise investigation result and explains that no takeover
means no claim of investigation. It changes only that channel's separate result
delivery, not takeover authorization or the original task layout. The API field
is `notify_assistant` in each channel view/update through the existing
notification-settings API; login and CSRF checks still apply. Reading or saving
these settings makes no model calls. It is not a model-callable delivery request.

`NotificationService.investigation_notice()` builds its own plain-text title and
brief body, not the user's task-notification layout, even when that layout uses
HTML. The title is `[SiteFlow] 助手调查结果 #<run_id>`. The body contains the site,
source run/job IDs, original job status, investigation status ("仅调查" or
"已更新配置但未重新验证"), and concise conclusion. It contains no full chat transcript.

HTTP uses `event: "assistant.investigation"`, with the configured title and body
fields (default `title` and `content`), `format: "text"`, `run_id`, `run_status`, `job_id`, `job_status`,
`site_id`, `site_name`, `conclusion` and `action_status` at the top level. The
original `run.finished` event retains its existing custom layout and nested
`run` payload.

When an HTTP image field is configured, the investigation form can attach one
existing failure screenshot from the exact source job. The file is read at
dispatch time. If no eligible screenshot is available, the conclusion is still
sent without an attachment. See [HTTP notification settings](notifications.md#发送图片).

An `updated` result means the configuration was saved; it explicitly says the
workflow was not rerun to verify the fix. A `completed_analysis` result is a
conclusion, not a successful execution. Failed, unknown, revoked, queued,
missing, invalid or otherwise unconcluded takeovers send no separate notice.

The source run/job keeps its original status, message, counts and filters.
`fail`/`error` stays failed and `waiting_human` stays `waiting_human`; takeover
never rewrites the task as `success`.

## Takeover Conclusions

The takeover-only `submit_takeover_conclusion` tool submits a plain-text candidate
of at most 240 characters, not a delivery request or execution permission. The
first accepted candidate is immutable and encrypted. Promotion validates the exact
action/request, owner, source run/job, site history identity, and determinate turn
outcome; an `updated` action also needs a valid import receipt. A valid short final
reply can be a fallback; a confirmed import alone can preserve only the host fact
that configuration was updated without a verification rerun. Unknown outcomes do
not promote model claims. The chat may still contain a longer explanation.

## Output Language

New model-generated final replies, visible tool titles, proposal explanations and
the `submit_takeover_conclusion` model body follow explicit human language requests
first, actual human-message language second, then the installation
`Settings.scheduler_timezone` (`SITEFLOW_SCHEDULER_TIMEZONE`) allowlist default.
Shanghai defaults to Simplified Chinese; Taipei/Hong Kong/Macau to Traditional
Chinese; Tokyo to Japanese; Seoul to Korean; selected UK/US zones to English;
Paris to French; Berlin to German. Invalid, unavailable or unmapped zones,
including multilingual Singapore, retain Simplified Chinese. Exact zone names
are listed in [reply language](ai-assistant.md#回复语言). This is an installation
default, not a claim of human-confirmed nationality or location. The site's
timezone groups source task days; it must not switch output language.

Autogenerated English takeover directives, even stored as `role=user`, website
content, tools, logs and assistant output are not human language preferences.
The fixed greeting, host UI, host-generated Chinese notification prefixes/titles
and separate conversation-title generator remain unchanged. Historical/reused
conclusions and frozen notices are not translated or backfilled; there is no
extra translator or model call. This is a prompt instruction, not a language
validator or delivery guarantee. Notification freezing and dispatch remain as
described below.

## Investigation Evidence

A source task's authentication-selector failure and a later live-browser failure
are separate facts. Unavailable observation does not establish the cause of the
source failure or change its status or notification delivery. See
[browser lifecycle troubleshooting](troubleshooting.md#生命周期诊断).

## Takeover Eligibility

Takeover covers terminal `fail`, `error`, and `waiting_human` source runs/jobs
from manual or scheduled execution, created after the saved policy's effective
time, only for enabled sites in its authorized scope. `unknown`, `interrupted`,
canceled/cancel-requested, `deferred`, unfinished, and assistant-origin tasks are
excluded; historical tasks are not backfilled. A `waiting_human` status alone
does not itself authorize mutation or replay. A valid live grant permits supported
same-origin single steps under current builtin category policy despite historical
unknown outcomes; it does not promise that every challenge can be handled or that
website side effects are absent. Historical partial consumption is not
automatically replayed. Historical failures or unrelated historical unknown
records alone do not block enqueueing or analyzing a new eligible source.
Configuration-write/import and full-workflow uncertainty guards remain in force;
diagnostic admission and category permission do not clear them. Exact dynamic
checks and available operations are determined by the server. An unknown, interrupted,
canceled or deferred source itself still does not trigger takeover.

Periodic discovery has a separate process-start cutoff: the takeover manager
captures UTC `_scan_started_at` before startup recovery, and `reconcile()` requires
`Run.finished_at >= _scan_started_at` as well as policy-effective creation and
the normal eligibility checks. A run started before this process but finished
after the cutoff can qualify; old finished runs without an action are not
backfilled. Existing queued actions and completion recovery are unaffected, as
are historical conclusion/dedup lookups. This cutoff applies only to periodic
discovery, not `_eligible` or the explicit terminal-transaction notification
planner, which still checks current policy and source eligibility. The planner
is not a periodic historical backfill path. See [takeover admission](ai-assistant.md#失败接管).

For example, Piggo is only a generic illustration: old failed/unknown records
are not proof that a new failure was investigated. Only a valid same-version,
same-failure conclusion can replace a fresh analysis. This is not a claim that
Piggo production configuration, selectors or a repair have been verified.

All jobs eligible under these status and safety criteria are planned locally
before the immediate task notification is sent; `deferred` does not plan a
takeover action. Only a valid promoted conclusion for a completed takeover can
produce the separate takeover-result notification. Timeout, failure, missing,
rejected, revoked, uncertain or unfinished conclusions produce no separate
notice and do not change the original task message.

Conclusions are carried in the separate takeover-result notification. Persisted
run and job statuses, messages, summaries, counts, and success/manual channel
filters still describe the original execution. A configuration update is not a
successful rerun. Known integration keys and credential patterns are sanitized
from outgoing job messages; this is not general-purpose PII removal.

An updated-workflow conclusion explicitly says configuration was updated and was
not rerun for verification. The source execution keeps its original status:
`fail`/`error` stays failed and `waiting_human` stays `waiting_human`, never
rewritten to `success`. Neither a saved fix nor an analysis conclusion turns it
into a successful execution.

"Not rerun" means no automatic **full-workflow** rerun. With explicitly saved
`live_access`, takeover may have browsed, logged in, signed in, clicked, or performed
other supported single steps, even while the owner was offline. `analysis` forbids
configuration saves, not website mutations. Conclusions must distinguish observed
single-step results from full-workflow verification and must not claim there were
no site-side effects. The configuration-write and full-run restrictions above still
apply. These observations do not change the source job's original status or the
original task notification.

## Reused Historical Conclusions

Same-configuration/same-failure deduplication remains available for `fail`/`error`.
`waiting_human` does not initially reuse old challenge analyses: a new dynamic
challenge under the same configuration still needs current evidence. Reusing a
daily conversation is separate from reusing a conclusion or replaying an action.

`assistant_takeover_dedup.py` can reuse a completed analysis only after current
policy, source eligibility, activity checks, and the actual dispatch snapshot
have been checked. Unrelated historical unknown records do not prevent this
no-action reuse of a valid conclusion. The identity is owner + site history UUID + canonical origin +
full actual-dispatch configuration fingerprint + configuration revision + v2
failure hash. The encrypted snapshot must still match current configuration,
attempt, and actual lane; a later reconstruction is not dispatch evidence.

The v2 SHA-256 input includes lane, job status, and all failed steps sorted by
step key: key, feature, status, reason/code/cause, field or field path, stage, and
whitespace-normalized message when available. The code whitelist also includes
`errorcode` and `provider_error`. The failure-identity safety requirement is to
distinguish the complete Run/Job terminal cause and result, including explicit
false results, alongside failed-step facts even when failed steps exist. Matching
step failures alone must not merge different terminal failures.
Non-whitelisted timestamps, durations, counts, URLs, and screenshot
hashes are ignored, but other message differences deliberately prevent matching.
Dedup metadata stores hashes and bindings, never raw messages or field paths;
the original execution records retain their existing storage rules. Missing or
malformed evidence, stage/field alone, or a generic error without an identifying
message or stable failure reason conservatively does not deduplicate.

A signature is not a successful-handling marker. The source must be
`completed_analysis` or `updated`, with a valid decryptable, exactly bound promoted
conclusion and a typed successful turn outcome (`turn_outcome.ok === true`). An
updated source also requires a valid import receipt. Queued, failed, unknown,
missing/corrupt conclusions, and chat text alone do not suppress a new analysis.
The full fingerprint covers local/resolved configuration, ancestor dependencies,
and feature schema/defaults/policy, not just workflow steps. A normal site resave
that advances revision invalidates reuse even with unchanged content; any actual
snapshot-field change, including non-step metadata, or dependency/policy change
also invalidates it. A previous `updated` action describes the pre-fix failure
version and cannot suppress analysis of a newly failed post-fix version. Resaving
takeover policy or switching modes alone is not a new failure/configuration version
and still requires current authorization.

Reuse also requires trustworthy analysis provenance: an analysis performed against
configuration different from the failed dispatch version must never count as that
version's completed analysis, even if configuration later returns to the old
content/fingerprint. A completed status, an overwritten stale reason, or a current
fingerprint match alone is not proof of what configuration the analysis used.
Known-unaccepted busy/session-running/model-unavailable sends must remain eligible
queued work, not a success marker or a reason to suppress later eligible analysis;
known non-acceptance is distinct from an uncertain acknowledgement. These are
safety requirements, not claims of new public fields or proof from status alone.

Each repeated source job keeps its own audit action with `status="reused"`,
`reason="same_config_same_failure"`, and `attempts=0`. It dispatches no new model
turn, opens no browser, performs no new repair/import, and creates no fresh promoted
conclusion. A direct link points to the original valid action, never a reuse chain.
An existing same-owner source chat is linked; a deleted chat is not recreated.
This is separate from daily conversation routing and can span source local days.

Cross-day reuse is bounded, not indefinite. `TakeoverValues.conclusion_ttl_days`
is a strict integer 1-30, default 7, exposed in the takeover UI. Each new reuse
decision uses the currently saved TTL and the **original promoted conclusion's**
`AssistantTakeoverConclusion.created_at`, with rolling UTC bounds:
`now - timedelta(days=conclusion_ttl_days) < created_at <= now`. Exact expiry and
future timestamps are invalid; source job finish, action update, dedup-link creation
and daily-chat dates do not set or renew age. Reuse never creates a new conclusion
or extends its lifetime. After expiry, the next eligible new source follows normal
investigation, subject to current admission; there is no automatic backfill,
requeue of existing reused actions, or record deletion.

TTL applies only to new reuse decisions. Existing history, reused projections and
frozen notification snapshots remain readable regardless of age, with their
existing integrity/binding checks. Expiry alone does not suppress a committed
historical projection or rewrite a frozen message. Changing TTL follows the
unchanged policy-save CAS, revision increment and effective_from update; it does
not retroactively change prior decisions. See [reuse lifetime and policy](ai-assistant.md#相同失败去重).

The UI label is "相同失败，复用已有分析", with the conclusion marked
"历史结论（复用，非本次重新分析）". The outgoing message explicitly starts with
"复用此前分析（任务 #<source Run ID>）：当前未重新分析/未修复。". Any nested old
update/observation claim is historical, not a claim of new analysis, repair, or
verification. Only the outgoing message changes; the current failed execution,
counts, and channel filters remain unchanged. Reuse is resolved by the local
plan/dedup step before the immediate task notification is sent; it does not start
a fresh takeover or produce a second investigation notice. The original
conclusion is revalidated for that projection. Invalid or missing evidence at
planning time prevents reuse; fresh planning still requires current admission.
If evidence becomes invalid **after the action has committed as `reused`** (not
merely older than TTL), the historical conclusion is suppressed, leaving the
original sanitized message;
the action is not automatically requeued for investigation. It must not present
an unverified historical conclusion.

## Dispatch Guarantee

The original task notification and the separate takeover-result notification
each freeze their event, rendered title/body, selected channels and resolved
delivery settings once, encrypted with the application's CryptoService, before
dispatch. Both SMTP and HTTP use that snapshot, including HTTP's structured run
payload. Delivery outcomes retain channel names and boolean outcomes, not secrets.

For an eligible completed investigation with a valid conclusion, completion,
promoted conclusion and notice registration are committed atomically. Its notice
can be claimed only after the original task intent reaches `completed` or
`unknown`, not while it is `ready` or `dispatched`. If the source intent is
missing, it is registered first. An ambiguous original attempt is not retried
before releasing the investigation notice. This is durable attempt ordering, not
a promise of successful delivery or recipient arrival order.

Completion recovery is distinct from network-delivery recovery. On startup,
`_recover` leaves an action `dispatched` only when its exact session/request has
a persisted accepted `role=user` message and the session still belongs to the
action's owner. Claimed/unaccepted actions or invalid session bindings keep the
restart fallback: `updated` with an import receipt, otherwise `unknown`.
Periodically, `_recover_finalizations` runs off the event loop via `_phase`
before `reconcile`, selecting at most 100 actions per batch with same-owner idle
sessions, no active tasks, and a 5-second `updated_at` backoff. Recovery-mode
`_complete` rechecks action/session/request and owner bindings and session
idleness. It requires a persisted outcome whose `ok` is a typed boolean;
missing/malformed outcomes become `unknown` unless an import receipt proves an
update. Completion, promoted conclusion and notice registration commit in the
same transaction; a failure rolls back, leaving `dispatched` for a later
finalization retry. Recovery never requeues an investigation, resends its user
message, calls the model or replays site actions, even for a determinate failure.
It does not infer a successful outcome from chat text. See
[conclusions and recovery](ai-assistant.md#结论与通知).

The separate takeover-result notification has its own ledger,
`assistant_takeover_notices`, keyed uniquely by action ID, independent of the
original task notification's `run_notification_intents`. Both paths commit an
at-most-once claim **before** any network I/O. On restart, a claimed notification in an ambiguous state becomes
`unknown` and is **not retried** or resent. This is an at-most-once attempt
policy, not exactly-once delivery: a crash can lose the notice, and a crash
between SMTP and HTTP can leave only one channel delivered. A timeout can also
mean delivery occurred without an acknowledgement. Per-channel retries and
automatic resend are deliberately absent. A completed claim means every selected
channel was processed, not that every delivery succeeded; transport failures are
logged and recorded as unsuccessful outcomes.

Existing legacy `waiting` intents become immediately `ready` on upgrade while
preserving their frozen event/messages; missing rendered messages or targets are
filled locally. They are not retroactively annotated as fresh investigations.
An unreadable legacy payload becomes `unknown`, not a new send. This is recovery
of existing intents, not historical backfill: no intents are created for older
runs and ambiguous sends are not recreated.

## Records And Knowledge

Records live in the configured `DATABASE_URL` database. Compose defaults to SQLite
at `/app/data/siteflow.db` in the API's `siteflow_data` volume, not browser storage
or a provider-side knowledge base.

| Table | Purpose |
| --- | --- |
| `run_notification_intents` | One immediate task-notification intent per run, dispatch state, encrypted event/rendered content/delivery settings |
| `assistant_takeover_notices` | One independent investigation notice per action ID, dispatch state, encrypted event/plain-text brief/delivery targets, and per-channel boolean outcomes |
| `assistant_takeover_actions` | Owner, source run/job/site, authorization revision, request/session binding, outcome and import receipt audit |
| `assistant_job_config_snapshots` | Encrypted actual dispatch configuration and attempt/lane binding |
| `assistant_takeover_daily_conversations` | `DailyConversation` routing by owner + site history UUID + source completion local date |
| `assistant_takeover_conclusion_candidates` | Encrypted candidates keyed by action + request |
| `assistant_takeover_conclusions` | Independently retained encrypted promoted conclusions |
| `assistant_takeover_failure_signatures` | Owner/site-history/origin, dispatch configuration revision/fingerprint, signature version and failure SHA-256; no raw failure message |
| `assistant_takeover_dedup_links` | Direct reused-action to original-action link and reuse timestamp; no copied conclusion |
| `assistant_takeover_analysis_provenance` | Evidence binding the analysis's inspected configuration to the failed dispatch configuration; missing evidence disqualifies historical reuse |

`assistant_takeover_notices` is separate persistence, not a sent flag on the
original task or disposable chat. Include it with the other notification and
takeover records in database backups.

Daily routing uses the source **SiteJob.finished_at** in the site's timezone,
falling back to scheduler timezone then UTC if invalid. The same owner/site/local
day reuses one canonical chat, not one chat per weekday or processing date.
Cross-day evidence is bounded to three prior conclusions and field-level fix
diffs, excluding the current local day. It is historical evidence, never authority
or proof that a fix was rerun. Promoted conclusions and takeover audit survive chat
deletion; a later new action can replace a deleted daily chat mapping without
replaying old actions. Restore requires the database and a separately secured
`SITEFLOW_MASTER_KEY`; routing/status/index metadata is not all encrypted.

Takeover uses a separate persistent delegation, not global assistant authorization
defaults. A new policy has `enabled=false`, strict boolean `live_access=true`,
`mode="analysis"`, `site_ids=[]`, `max_attempts=1` (1-3), and
`conclusion_ttl_days=7` (strict integer 1-30). The live-access checkbox
is the only browser/verification switch; checking a draft grants nothing until
the owner confirms and saves it. Exact legacy stored shapes may lack TTL,
`live_access`, or both: missing TTL reads as 7, missing live_access as false,
without a database write, revision/timestamp change or permission expansion.
Other malformed/partial shapes fail closed; existing users must check and save
to newly authorize live access. Enabling via API requires an explicit
`values.live_access` decision. Opening, refreshing, or saving policy does not call
the model or start a browser or task.

With live access, the autonomous turn may browse, capture input-masked screenshots
when vision is supported, and execute up to the frozen 18 supported same-origin
single-step features with available private credentials. Saved `proxy_general` or
`proxy_interaction` uses `proxy_interaction`; other saved lane policies use
`interaction`, with no fallback. Strict original-origin restrictions apply from
the first navigation; navigation cannot expand scope or approve other origins.
For a selected-site live policy, saving atomically freezes each selected site's
history UUID and canonical origin in the separate AppSetting record
`assistant.takeover.site_grants`, keyed by policy revision and the complete selected
ID set. Admission rechecks those bindings. An older selected-site live policy with
missing or invalid bindings fails closed and requires explicit policy resaving;
reading never reconstructs consent from current numeric IDs. Explicit
all-enabled-and-future scope permits future enabled sites without prebinding them,
but each actual action freezes its specific site history identity, origin, policy
revision, and source job. Strict same-origin authority is passed in the first
Worker open request and enforced before its first navigation, not tightened later.

`autofix` still permits only one workflow import when the actual failed dispatch
snapshot remains current. The autonomous turn cannot run a full workflow, execute
`flow.jump` or the four `post.*` features as isolated steps, or request retry approval.
The autonomous gate still denies `retry_step`; permitted new `execute_step` calls
follow category policy and do not imply that the retry tool is available.
Plugins and plugin providers remain denied. Secrets
and capability tokens stay out of model input and logs; DOM input omission and
live screenshot masks remain in force. Site/template/global edits and cancellation
require normal explicit human requests and host review; a human-requested consuming
full run still needs fresh review for every new dispatch.

Only code-owned builtin `Feature.category` determines category authority: `flow`,
`auth`, `challenge`, `sign`, `action`, `post`, never a key prefix, description,
model argument or plugin metadata. Auth login/password-verification attempts and
retries share `max_auth_uses_per_turn` (default 3, strict integer 1-20) per real
user turn, reserved before credential resolution with failures charged. Internal
model rounds and target/browser changes do not reset it. `challenge.2fa` is
challenge, not auth-budgeted, and its OTP remains private. Supported
flow/challenge/sign/action steps remain allowed with same-origin consent despite
historical unknowns. That is user-authorized policy, not a claim that only post
can change the website; physical clicks, navigation and scripts can still write.

In ordinary human turns, `retry_step` automatically executes the exact stored
non-post step with one-use host authority and a fresh UUID, without a UI prompt;
auth retries charge another use. Legacy manual retry approval APIs remain for
compatibility, not a mandatory prerequisite. Original unknown provenance is not
erased. Each enabled post step instead needs a fresh owner dialog before a new
saved full run, all dialogs approved before dispatch, with no per-date or
internal-click popup. The four post features remain unavailable as isolated steps.
Plugins may declare the six categories, but their metadata is unverified and
dangerous actual implementations need review; this does not enable assistant access.

Per-action authority ends after the autonomous turn, but valid live-granted browser,
execution, available credential, and screenshot permissions remain in that same
session for human follow-up. Disabling takeover policy does not revoke that saved
human consent. Human follow-up also retains `workflow_consent=true` for a live grant
regardless of autonomous analysis/autofix mode.
It does not allow autonomous analysis to save or run workflows. On an explicit
human request, normal import/full-run behavior applies, including fresh consumption
review and uncertainty gates. Without a live grant, obtain missing permissions
through normal explicit interactions; global automatic defaults remain suppressed.
Changed identity, origin, or bindings require reconfirmation. Asking about capability
alone grants nothing.

With a valid explicit saved-site binding, a new live browser can privately seed
saved cookies before its first navigation, in any credential mode. The workflow
destination binding takes precedence over the credential binding; a stale preferred
binding fails closed, without fallback or hostname lookup. This grants no auth,
OTP or full-run authority and does not export login-browser cookies or save them
back. Cookies belong to the lane's shared context, not an isolated assistant tab;
raw values never enter model messages, tool arguments/results or saved assistant
sessions. These facts do not change notification planning, conclusions or delivery.

`clear_bound_site_cookies` is one of the **38 ordinary tools** (plus one
takeover-only conclusion tool, **39 registered**), but is **unavailable to autonomous
takeover**, even with live access. A human in the daily chat may explicitly request
it with a fresh UUID4 and fresh internal `cookie_clear` scope review, only after an
original-bound browser is already READY; it never automatically opens a browser
for clearing. It deletes only the bound site's saved Cookie data and the current
shared lane's exact host/.host cookies on all paths, with no reload. Other same-host
tabs on that lane may log out; parent/sibling domains, other lanes, account/password
and localStorage remain. Active site runs block clearing, saved/browser outcomes
are separate, and partial/unknown results are never automatically retried or
re-executed by same-ID replay. Later successful tasks may save cookies again.
See [bound-site Cookie contracts](ai-assistant.md#绑定站点-cookie) for transaction
ordering, generation invalidation of queued/in-flight seeds, limits and privacy
boundaries. API, Worker and Web must be upgraded together; Cookie operations do
not change the normal browser TTL or the separate notification decision.

An autonomous takeover releases its own page and lease in the turn's `finally`
block **before the session becomes idle**, on success, error, or cancellation. It
does not retain the browser until the 15-minute TTL or wait for last-message idle.
History and valid human-follow-up permissions remain. On an explicit follow-up
request, `browser_open` can reuse those still-valid grants to open a **new page**
without repeat consent; it does not promise the original page, DOM, or transient
state. Changed identity/origin/bindings still require reconfirmation. Observation
or authorized single-step verification does not require manually running an entire
workflow first.

Autonomous `browser_open` uses `wait_for_lane: true`, a nonblocking
Worker response and host polling of `queued` / `opening` / `ready`, FIFO per lane,
and a **1200-second (20-minute)** maximum queue wait. Only queue waiting is excluded
from the active turn budget. The same tool call awaits once, with no extra LLM
calls while waiting. Timeout, cancellation, or policy revocation cleans up only
that request's queued ticket, page, and lease, never another owner's resources.
Manual assistant browsers, active manual/scheduled jobs, pickers, and viewers are
not preempted; source jobs and pickers share the lane execution semaphore. Per-lane
queues are independent, but the current primary autonomous consumer is serial:
there are no two concurrent autonomous browser takeovers or promised cross-lane
parallel takeovers.

Human assistant browser leases remain **fixed at 15 minutes from admission/creation**,
not renewed by messages or observations and not measured from the last message.
Queue waiting does not consume the post-admission TTL. The separate configurable
lane `idle_stop_seconds` defaults to **300 seconds** and counts down only after
there are no leases or viewers. It is neither the assistant TTL nor takeover
notification timing. Queue waiting, browser reopening, and single-step work do
not change the separate notification decision.
See [Queue and Ownership](ai-assistant.md#排队与所有权).

Database backups must include both AppSetting records:
`assistant.takeover.settings` holds the canonical authorizing owner, revision, and
authorization/effective timestamps; `assistant.takeover.site_grants` holds the
matching revision and selected-site history UUID/origin bindings. Owner, canonical
daily-chat routing, and binding metadata are not all encrypted. Do not restore
consent by looking up current numeric site IDs. New record tables are created by
`create_all()` after model registration; live-access fields and bindings use existing
JSON/AppSetting storage, not new columns on old tables. `create_all()` is not a
column-migration mechanism, and historical development schemas are not migrated.

## Performance Scope

Static inspection shows the takeover coordinator using `_phase` with
`asyncio.to_thread` for startup recovery (`_recover`), periodic completion recovery
(`_recover_finalizations`), background reconciliation,
selection (`_select`), model-runtime preflight, session creation
(`create_takeover_session`), dispatch claim (`_claim`), send-failure handling
(`_send_failed`), and completion (`_complete`). Database helpers own their sessions
inside the worker thread; only IDs, scalars, and serialized evidence cross phases,
not ORM sessions/objects. Model sends, task waits, and wakeups stay on the event
loop. Cancellation drains a started thread phase, with post-claim unsent cleanup
handled through the corresponding phase rather than abandoning database work.

Executor run preparation/finalization (including terminal notification
registration), takeover policy saves, assistant SSE authentication/detail/JSON,
and notification polling/delivery also offload their synchronous work. This is
phase-level thread isolation, not a claim that every assistant tool/database
operation is asynchronous.

`TakeoverPhaseCache` in `assistant_takeover_performance.py` is instantiated separately
by notification registration's `plan_run_takeovers()`, reconciliation, selection,
and claim. It reuses plain run/site facts within one synchronous transaction phase,
database session, and policy view only. Discard it before await, commit, or
authority/configuration changes; it is not an execution/import authorization cache.
Claim rechecks policy and source with a fresh cache instead of borrowing the
selection phase's authority view.

Unchanged assistant SSE snapshots emit heartbeat even while running, with auth and
ownership still rechecked on each poll. The frontend caches message JSON parsing
and defers full pretty-printing/rendering until a tool result is expanded; original
results, proposals, approval/stop controls, and safety gates remain available.
These boundaries do not guarantee stall-free operation under every load.
