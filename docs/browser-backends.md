# Browser Backends

## Ownership

SiteFlow is the lifecycle and profile policy owner. Manager is only the browser
execution service: SiteFlow calls its APIs to create/configure profiles, start
and stop browsers. Manager does not schedule tasks, choose idle deadlines or
decide when a SiteFlow lane should run. Its container stays available while
individual profile browsers can be stopped.

SiteFlow is MIT licensed. Its worker executes workflows and plugins but contains
no browser binary, Manager, VNC server or bundled fonts. Compose pulls the official
Manager image directly; its upstream license applies independently.

`worker/browser_backend.py` defines the provider boundary:

- `prepare()` stops recorded leftover profiles after Worker restart and disables
  Manager auto-launch/session restore for them. Unknown profiles are untouched.
- `BrowserBackend.start(key, proxy)` returns a `BrowserSession` with a Playwright
  context, disconnect status, display metadata and optional `Desktop` transport.
- `stop(session)` releases the owned provider session. Task completion/cancellation
  closes only task-owned tabs; it never stops a shared lane profile.
- `vnc_connection(session)` supplies the backend's normalized viewer connection.
- `Desktop.screenshot/click` is optional and independent of browser launch.

Only `cloak-manager` is implemented today. Another backend must implement this
contract, including persistent state, input behavior, viewer protocol and cleanup;
changing a URL alone does not make an arbitrary CDP server fully compatible.

## Manager Versions and Licensing

Both Compose files default to the official image
`cloakhq/cloakbrowser-manager@sha256:d3d9b86cfd94347f7356811cf8222c8bc48152725b053dc6ce2e68f541e00c14`;
`SITEFLOW_BROWSER_IMAGE` can override it. The installed image was observed with
wrapper 0.7.1 / Playwright 1.64.1. Upstream's v0.2.0 changelog requires a license
even for the free tier and removes the bundled browser; v0.2.1 has also been
released. The observed `/api/status` reported tier `none` and an empty binary
version without a key, and the old bundled binary path did not exist. This is
inspection evidence, not a successful operational test of the current image.

Set `CLOAKBROWSER_LICENSE_KEY` to an upstream-issued free or paid license key.
Get a free key at https://cloakbrowser.dev/free. Set the variable in the deployment
`.env`, or leave it empty/unset and use the
[SiteFlow license card](#siteflow-license-card) to save a key in Manager.
Both Compose files explicitly map `CLOAKBROWSER_LICENSE_KEY: '${CLOAKBROWSER_LICENSE_KEY:-}'`
into `api`, `worker` and `browser`, keeping shell interpolation consistent across
all three rather than relying on API/Worker `env_file` values alone. API and Worker
independently enforce their own nonempty environment locks. Adding, changing or
removing the environment key requires recreating **all three services** using the
same Compose file used for deployment; `docker compose restart` does not load
changed environment values. Wait for active jobs/viewers to finish first:
recreation disconnects CDP/VNC.

```bash
# GHCR deployment
docker compose up -d --force-recreate api worker browser
# Source deployment: use this instead, not both Compose files
docker compose -f ops/docker-compose.yml up -d --force-recreate api worker browser
```

A nonempty environment key takes precedence on Manager startup. Remove it from
the deployment inputs (including any exported shell override) and recreate all
three services before relying on Manager-saved settings. Hot updates apply to
subsequent launches, not a promise to relicense live browsers.
Without a valid key from either source the current browser cannot run; there is no keyless bypass for
this image. The adapter accepts `keyless`, `free` and `pro` tiers without an exact
binary-version gate, and rejects `none` or unknown tiers with static errors.
Launch HTTP 402 is treated as profile-capacity rejection and HTTP 403 as license
rejection, with static errors that do not expose the provider response body.
API, CDP and VNC routes remain unchanged, but actual
CDP, humanized input, desktop and VNC operation on this image remains unverified
pending a valid license.

The opt-in isolated `backend/tests/test_demo_e2e.py` fixture still defaults to the
previous license-free image. To validate a current image, set
`SITEFLOW_DEMO_BROWSER_IMAGE` and a dedicated `SITEFLOW_DEMO_CLOAK_LICENSE_KEY`;
`SITEFLOW_DEMO_BROWSER_BINARY_PATH` is optional and defaults empty for an override
image. The key is forwarded privately to the disposable Manager, not reused from
production. Never run `remote_browser_smoke.py` against the deployed Worker.

The image digest pins Manager, **not** the separately downloaded browser.
`CLOAKBROWSER_AUTO_UPDATE=false` disables automatic updates, not the initial
download or license/version checks. Both Compose files use the fixed browser-service
environment `CLOAKBROWSER_CACHE_DIR=/var/cache/cloakbrowser` and mount
`browser_cache:/var/cache/cloakbrowser`; this is internal Compose wiring, not a new
deployment `.env` option. `CLOAKBROWSER_BINARY_PATH` stays empty by default so current
images use that custom cache. `/root/.cloakbrowser` is left **unmounted** to preserve
the old image's bundled binary path for an explicit legacy rollback.
The health check allows ten minutes for the initial download before counting
startup failures; a healthy Manager still does not prove a profile can launch.
Do not set the old bundled path with the current image. The worker's independent
SDK `cloakbrowser==0.6.3` and `playwright==1.64.0` pins remain unchanged; they are
not a claim that Manager's wrapper or downloaded browser has those versions.

The free license permits one concurrently running profile. SiteFlow's finite
profile queue automatically switches lanes safely; operators do not need to stop
every other lane manually. Multiple task tabs within one lane share its profile
and do not consume additional seats. Different lanes have different profiles, so
task concurrency and running-profile capacity are separate limits. Active work
and viewers can delay a switch; they are never evicted to satisfy the queue.

### Cache Mount Transition

Only the container mount target changes; reuse the **same actual named volume**
in either Compose deployment. Both files default to project name `siteflow`, hence
`siteflow_browser_cache`; preserve any existing `-p` / `COMPOSE_PROJECT_NAME`
override and volume-name overrides rather than accidentally selecting a new volume.
Existing contents, including hidden markers and `license.key`, remain unchanged.
No cache copy, directory migration, separate fresh cache or volume deletion is
needed. Profiles and Manager settings remain at `/data` (`browser_data`), with
lane/profile mappings in `worker_data`; Worker still contains no browser binaries.

1. Wait for active tasks and viewers to finish. Back up `browser_data` and
   `worker_data` together and preserve `browser_cache` separately. Cache and Manager
   backups can contain license secrets: restrict backup files to mode `0600` and
   never put keys in logs or shared diagnostics.
2. Apply the updated Compose cache environment and mount together. Remove or adjust
   obsolete local overrides that still mount `/root/.cloakbrowser`: an additional
   root-path mount still masks the bundled binary even with the new cache mount.
   Keep the deployment's Compose file, project name and actual volume names unchanged.
3. Recreate `browser`, not just restart it, to apply the mount/environment change:

```bash
# GHCR deployment; retain existing project-name overrides if any
docker compose up -d --no-deps --force-recreate browser
# Source deployment: alternative, not both
docker compose -f ops/docker-compose.yml up -d --no-deps --force-recreate browser
```

This cache-only change does not require recreating API/Worker; license environment
changes still require all three services as described above. These are operator
instructions, not an executed deployment, download or runtime verification.

Generate `SITEFLOW_BROWSER_TOKEN` using `openssl rand -hex 32` and configure it
in `.env`. It authenticates Worker-to-Manager HTTP/CDP/VNC; it is not a license key.
Never publish Manager/CDP or Worker ports to the host. SiteFlow's existing
administrator session and Origin checks remain the external viewer boundary.
The API authenticates its internal Worker viewer connection with this token;
Worker rejects missing/incorrect tokens and direct browser-Origin connections.
Other Worker control endpoints remain trusted-network-only; do not attach
untrusted containers or expose the internal Compose network externally.

Configuration:

```text
SITEFLOW_BROWSER_BACKEND=cloak-manager
SITEFLOW_BROWSER_URL=http://browser:8080
SITEFLOW_BROWSER_TOKEN=<random secret>
SITEFLOW_BROWSER_DESKTOP=x11
SITEFLOW_BROWSER_MAX_ACTIVE_PROFILES=1
CLOAKBROWSER_LICENSE_KEY=<upstream-issued license key>
```

Each lane gets a dedicated profile. Opaque IDs are atomically saved in
`worker_data/browser-profiles.json`; browser state lives in `browser_data`.
The adapter never imports, deletes or adopts unrelated Manager profiles. On
reconnection it may stop/restart only a previously mapped SiteFlow profile.
Use a dedicated Manager instance per worker, not one with independently operated
user profiles. Losing the mapping can leave an orphan profile, but does not cause
automatic adoption by name or deletion. Back up both data volumes together.

### Finite Profile Admission

Worker setting `SITEFLOW_BROWSER_MAX_ACTIVE_PROFILES` is an integer from 1 through
4, default **1**, not an unlimited mode. Both Compose files explicitly map
`SITEFLOW_BROWSER_MAX_ACTIVE_PROFILES: "${SITEFLOW_BROWSER_MAX_ACTIVE_PROFILES:-1}"`
into Worker, using the same shell/`.env` interpolation. Free tier always has an
effective cap of 1 even if configured higher. Manager provides no reliable seat
metadata for pro/unknown capacity, so the default is conservatively 1; legacy
keyless also defaults to 1. An operator-configured higher finite budget is not a
license entitlement or proof Manager will permit that many profiles. Unknown
license tiers still fail closed; do not infer unlimited seats from missing data.

The backend registers all initially nonempty lanes of a Run in one batch. Worker
grants dispatch reservations with non-preemptive priority `general`, `interaction`,
`proxy_general`, `proxy_interaction`; only granted tickets create lane execution
tasks, which then compete for global job slots. Pending tickets consume no global
job slots. An effective cap of 2 permits two lanes of the same Run to overlap;
free tier's cap of 1 keeps different lanes sequential. Other Runs, assistants and
viewers contend for the same profile pool. Same-lane concurrency is shared across
Runs and assistants: general defaults to 5, proxy general to 3 (each configurable
1..10), and interaction lanes are fixed at 1. The short foreground-input gate
still serializes input, not whole jobs. Requiring several lanes is not itself a
capacity check failure.

A granted ticket pins its profile for the entire lane batch, including jobs
waiting for global slots. Cross-lane fallback releases its source global task
slot and queues for a target batch after the source batch joins; a finished target
batch can reopen with a fresh ticket. Existing business retry/fallback budgets are
not expanded. Tickets have a **60-second TTL**, renewed every **1 second**, with
owner/Run/lane binding and a Worker-generation/session fence. Owner cleanup and
expiry release dispatch pins only, not ordinary job leases, quarantined cleanup
or viewer protection; released tickets cannot be revived by delayed renewals.
Lost reservations or generation changes fail closed without automatic replay.
Public `in_use` includes dispatch pin and job/cleanup leases, not an exact count
of running tasks.

Ordinary runtime lease acquisition uses a bounded **120-second** capacity wait;
pending dispatch tickets can retry capacity acquisition while renewed, so this
is not a total dispatch queue deadline. Under pressure it can immediately stop an
idle, mapped SiteFlow profile to free a slot,
without waiting for its idle timer, even when idle shutdown is disabled with 0.
It never deletes the profile: IDs, cookies and persistent data are reused on the
next start. Active leases, viewers, task pages and quarantined ownership are never
evicted; unconfirmed stop or launch cleanup continues to consume capacity. A
viewer pins its profile; disabling timed idle shutdown alone does not. Unrelated
Manager profiles are never adopted, stopped or deleted.

UI manual lane start uses the same capacity protection but fails fast rather than
waiting in the runtime seat queue. Before full-run dispatch, the assistant closes
only its own discovery browser session and releases that lease, not other
conversations' browsers, viewers or jobs. Admission cannot guarantee that another
client will not take capacity before the full run acquires it.

### First-Profile Preflight

Automatic scheduling uses authenticated `POST worker:/browser/preflight` with
`{"lanes":[...required lane keys...]}` before creating any Run, SiteJob or job
steps. Worker checks queue eligibility for all required lanes, but starts/reuses
**only the first required profile** in canonical order, verifies CDP and its local
`about:blank` home page; the remaining lanes await runtime dispatch admission.
It performs no site navigation, login, click or other business action. Remote
HTTP 402/capacity rejection or any blocked/failed preflight means no Run; merely
needing more lanes than the cap does not. The browser-token Bearer authentication
rejects requests with an Origin header; this is an internal control endpoint, not
a browser-facing API. Preflight has a 120-second outer Worker budget but fails
fast on unavailable seats rather than waiting for active users to drain.

The existing authenticated `GET /browser/readiness` remains read-only metadata
only; it never launches profiles and is not launch proof or the production
scheduler gate. Preflight can warm the first browser, without acquiring a lease
or reserving a future slot. Later contention, backend loss or another lane's
launch failure remains possible. Neither a healthy status nor successful
preflight guarantees all future lanes or business actions; capacity waits do not
authorize automatic business retries after steps have executed. Detailed scheduler
deadlines, proof validation and deferral behavior are in
[Scheduled Browser Admission](execution.md#scheduled-browser-admission).

### SiteFlow License Card

The top of SiteFlow's browser/lanes page (`BrowserView`) contains the CloakBrowser
Manager settings card. It shows connection status, license source
(`env`, `manager`, `unset` or `unknown`), read-only/editable state, configured
status, license tier and browser binary version. **Configured does not mean a
valid license** or a completed download. Neither existing keys nor provider key
masks are returned or displayed; the password input is only for a new key.

The card uses authenticated `GET /api/v1/lanes/browser-settings` and CSRF-protected
`PUT` on the same route. Requests go only through SiteFlow API -> authenticated
Worker `/browser/settings` -> authenticated Manager. Reads project metadata from
Manager's `/api/status` and `/api/settings`; writes forward only
`{"license_key":"<new nonempty key>"}` to `PUT /api/settings`, never other Manager
settings such as `release_channel`. The separate **Clear License** button asks for
confirmation and sends exactly `{"license_key":""}`. Leaving the save input blank
does not clear anything; whitespace-only requests are rejected. Manager persists
the saved key under `/data` (`browser_data`); API and Worker do not store duplicate
license records in SQLite or Worker data. Reading/saving does not create, modify,
start, stop or delete profiles, change lane/viewer leases, launch a browser, or
replay jobs. Manager can validate the key and download a binary during a save.

A nonempty `CLOAKBROWSER_LICENSE_KEY` locks both saving and clearing in the card,
API and Worker; empty/unset permits Manager-saved key updates when the backend is
connected and supports settings. Clearing removes only Manager's saved setting,
not environment variables or the wrapper's separate
`/var/cache/cloakbrowser/license.key` file. It does
not stop live browsers, delete profiles/cookies, or guarantee relicensing them.
Current Manager builds require a key for subsequent launches, so clearing can
make the current installation unable to start a browser.

The card compares numeric four/five-component browser versions against the known
legacy reference `146.0.7680.177.5`. Higher versions show a key-required hint;
versions at or below it can work without a key only with a Manager/binary that
actually supports legacy keyless mode. This is not a universal licensing cutoff
or a guarantee that every older build is supported. `free`/`pro`/`none` tier
evidence takes precedence over an old version number, and unknown or conflicting
metadata never establishes keyless compatibility. These hints do not relax the
server's launch checks.
For a custom external Manager, operators must supply the **same key environment
value to API, Worker and Manager**, or leave it empty/unset in all three for
Manager-managed settings. Manager cannot report whether its configured key came
from its environment. The `env` source and locks reflect local API/Worker
configuration, **not remote inspection of Manager's actual environment**; an
environment key set only on an external Manager cannot be reliably detected.
Keep Manager/CDP private; the card does not embed or proxy Manager's Settings page.

Saving hot-updates Manager without restarting it, but license checks and the
initial browser download can take several minutes. Wait for completion; there is
no automatic write retry. A timeout, canceled request or invalid response after a
dispatched PUT can leave its result unknown while Manager continues working.
Worker then retains a sticky uncertain-write guard: `license.editable=false`
persists across GET refreshes, even if a tier/version appears healthy. Refreshing
the card or reloading the page does not clear that guard or prove completion.

First verify through private Manager status/logs that the original write and
download have actually finished; do not share keys or raw provider responses.
Then drain active tasks and wait for viewers to finish before restarting Worker
to clear the guard. **Do not restart just to bypass an active download**, and do
not resubmit a write until its result has been reconciled. With no environment
change, use the matching deployment command below, then refresh the card and
decide manually whether another save is needed:

```bash
# GHCR deployment, only after Manager completion and task/viewer drain
docker compose restart worker
# Source deployment: alternative
docker compose -f ops/docker-compose.yml restart worker
```

If environment inputs changed, recreate `api worker browser` as described above
instead. Older Manager versions that lack the settings API remain read-only in
the card and require an explicit upgrade for this feature; the legacy keyless
rollback and current-image operational verification limits still apply.

### Legacy Keyless Rollback

The previously shipped image remains an explicit compatibility option. Set both
variables together to use its bundled keyless Chromium 146.0.7680.177.5:

```dotenv
SITEFLOW_BROWSER_IMAGE=cloakhq/cloakbrowser-manager@sha256:04b82e42fe74ec34352580da47b096695e6a8d08745f88e3f5fd821be5ebf989
CLOAKBROWSER_BINARY_PATH=/root/.cloakbrowser/chromium-146.0.7680.177.5/chrome
```

That legacy image does not require `CLOAKBROWSER_LICENSE_KEY`; this does not
remove the license requirement from current images. Before upgrading or rolling
back, stop jobs/viewers and back up `browser_data` plus `worker_data` (including
`browser-profiles.json`) together, and preserve `browser_cache` separately.
Browser profile/data downgrades are not guaranteed compatible; restoring the
matching pre-upgrade backups may be necessary; moving the cache mount does not solve
profile/data downgrade compatibility. With the updated mount, the explicit legacy
binary path bypasses `/var/cache/cloakbrowser` and uses the unmounted image path;
the same existing cache cannot mask it. An obsolete override mounting
`/root/.cloakbrowser` still can: remove or adjust that override, not the cache
contents. No separate cache or cache migration is required for this mount move.

An unset environment key alone does not establish keyless mode: Manager's saved
key/data in `/data` and the wrapper's cached `license.key` can still affect it.
Review both explicitly; the license card clears only Manager's saved value, not
the wrapper file. Do not silently delete the entire cache to clear licensing state.
Keep secret-bearing backups at `0600` and never log keys. Follow the cache mount
transition above and recreate `browser` only after tasks/viewers finish; if license
environment inputs also change, recreate all three services. This configuration
change does not perform a rollout, rollback, download or runtime verification.

### Input and Lifecycle

SiteFlow configures Windows/Intel persona, zh-CN, Asia/Shanghai, 1920x1080,
and humanized input settings. Remote CDP clients do not inherit Manager's Python
wrappers, so the adapter uses the upstream MIT Python SDK's humanization helpers.
Input actions share a reentrant per-context foreground guard with tab creation
and explicit focus changes, not `Page.close()`. A complete public input action
foregrounds its target and holds the guard so another guarded operation does not
background it mid-action and stall CDP mouse movement. This serializes input and
short lifecycle transitions, not whole jobs; navigation and observations remain
parallel. Lane/global limits, current input and real browser resources can still
make another job wait. Multiple tabs do not imply simultaneous foreground input.
The SDK remains an adapter dependency; no downloader or launch helper is used.
`worker/browser_input.py` restores native Playwright `input_value`, `text_content`,
`inner_text`, `inner_html` and `get_attribute` on Page, Frame, Locator and
ElementHandle after installing the SDK. Clicks, fills and other interactions
remain humanized. Waits, visibility/count checks, evaluation and screenshots
already use native Playwright. The class policy also covers future pages,
popups and plugin broker reads; it never toggles humanization across awaits.
The adapter is pinned to SDK 0.6.3 and uses its original-method accessor;
SDK upgrades require repeating the read/interaction contract tests.
It also guards the SDK's pointer hit check: when an approach opens a hover menu
over the target, the cursor retreats sideways before the SDK retries resolution
and hit-testing. This never forces a click through an overlay or replays a click.
Removing it today would remove the verified remote humanization behavior; setting
`humanize=True` in Manager alone does not wrap the Worker's CDP objects.

A profile browser starts on manual start, lane lease or first-profile scheduler
preflight. Timed idle stop defaults to 300 seconds; zero disables only that timer,
not safe pressure-driven stop for the finite profile queue. Dispatch pins, task
leases, active viewers, task pages and quarantined ownership prevent idle/pressure stop;
releasing a lease or viewer refreshes the timer. Profile data and identity survive
browser stop. A failed provider stop is reported as an error, not falsely
acknowledged as stopped or a freed seat.

## Job Lifecycle

Job setup reports `cookies` (when supplied), `new_page` and `focus` stages in
progress and `job.log`, with artifact metadata available before workflow steps.
Each setup stage has a 10-second caller budget. Separately, guarded creation/focus
has one 10-second budget for gate acquisition plus the native call, not a fresh
10 seconds for each part or a limit on the entire input action. Static setup
diagnostics use `browser_setup_timeout` / `browser_setup_failed` and the stage;
they do not copy native exception text or cookie values.

A timeout/cancel cannot prove a dispatched native call stopped remotely. Pending
creation/focus quarantines the context's input guard: the lock is freed, but later
conflicting input/creation/focus fails closed with `Browser lifecycle pending`
until the native call settles. Reads and close cleanup do not take this guard.
Dispatched page creation is retained uncanceled until its late page can be owned
and cleaned up, rather than losing ownership at a setup timeout. Quarantine can
block future input; this is not an unconditional throughput/recovery guarantee.
The existing 600-second execution HTTP read timeout remains unchanged: it limits
stream inactivity, not job/step-wide duration or lifecycle quarantine settlement.
Native guard child tasks are registered with their exact originating job owner,
so shutdown can track pending native work after its setup caller exits without
canceling unrelated lifecycle tasks across the context.

`JobPages` closes only a job's root and popup descendants, with 3 seconds for native
close followed by 3 seconds for CDP fallback per page. CDP must obtain the exact
owned page's target identity before `Target.closeTarget`. Job cleanup never closes
context siblings, refocuses the home page or calls `browser.close()` on a shared
lane. Unconfirmed closure/creation retains its owner/listeners and lane lease;
the reaper retries after 5 seconds between rounds. Logs distinguish
`browser_cleanup_quarantined` from `browser_cleanup_released`. The execution slot
can become available while `in_use` still includes that cleanup lease; it must not
be reported as zero without release. Idle/manual stop remain blocked by leases.

Completed business results are handed off **before optional Cookie export** and
cleanup. Export is a read-only cookie query using `JobPages.setup()`'s 10-second
budget; static `cookie_export_timeout` / `cookie_export_failed` warnings do not
change the completed business result. Successful export adds cookies and refreshes
the retained snapshot; interruption before it completes does not require fresh
cookies to preserve success. A completed successful Claim stays successful in the
worker even if export or cleanup is interrupted or cleanup is quarantined.
This neither rewrites old database results nor automatically replays any workflow.
Graceful `LaneManager.stop()` retains a single shielded shutdown task, reused across
stop calls. Caller cancellation during the prelude cannot bypass teardown;
nested `finally` blocks still attempt profile stop, owner reconciliation and
backend close. A canceled caller waits within a bounded settlement budget, while
the retained task can continue if it outlives that wait. Shutdown brackets
mapped-profile teardown with bounded owner/reaper cancellation and final
reconciliation (1-second cancellation waits,
5-second final wait), including each owner's registered native guard children.
Pages returned by a late creator during/after shutdown stay with that owner and
receive one bounded close through final reconciliation or a late-return callback,
not a new job or indefinite reaper. Only confirmed owners release leases; local
cancellation or provider teardown alone is not an all-clear, and the full stop has separate provider
deadlines. Remote creation that never returns a page/target identity can leave a
retained lease requiring operator reconciliation, not arbitrary-target cleanup.

The motivating production observations are investigation evidence only. Existing
mocked regression scenarios do not constitute a production retest or prove native
remote cancellation/recovery. This is workspace documentation, not a deployed fix
or automatic repair of old database results. Full semantics, evidence limits and
test references are in [Cancellation and Owned Pages](execution.md#cancellation-and-owned-pages).

## Desktop and Viewer

`browser_x11` is a dedicated socket volume shared only between browser and worker.
It is not the host `/tmp/.X11-unix`, requires no host networking or Docker socket,
and is mounted read-only in worker. Read-only mounting does not prevent X11 input:
it is a trusted browser-control channel, not an isolation boundary between those
two containers. Do not mount it into plugins or untrusted containers.

mss captures the actual display including browser chrome and visible native
dialogs; xdotool supplies OS-level clicks for the interaction-lane CF fallback.
Disabling desktop with `SITEFLOW_BROWSER_DESKTOP=none` retains CDP/page operations
and VNC, but desktop fallback fails explicitly. A future network-only desktop
adapter can replace X11 without changing workflow execution.

VNC path: noVNC -> authenticated SiteFlow API -> worker -> authenticated Manager
profile VNC -> local KasmVNC. Manager handles its protocol translation; SiteFlow
does not modify RFB frames again. Viewer leases still prevent idle stop.

## Failure and Deployment

Backend disconnects cancel in-flight work and revoke broker tokens; owned-page
cleanup still requires closure confirmation before releasing its lease. An
unfinished job reports interruption according to its policy; a completed business
result already handed off before cleanup is preserved. The worker does not replay
side effects. Subsequent jobs can reconnect after prior leases finish, but an
unconfirmed owner can require operator reconciliation. Requests already submitted
cannot be undone.
Graceful worker shutdown stops its mapped active lane profiles. Abrupt crashes
may leave profiles running until Worker startup reconciliation or explicit stop.

Existing internal-browser profiles are not migrated or removed. API-stored site
cookies remain available, but the new backend starts with fresh browser state.
Do not share the old profile directory simultaneously with Manager. No data
migration scripts or automatic database changes are required.

The old `.build-resources` cache is no longer used and remains Git-ignored; this
change does not delete local downloads or existing Docker images/volumes.

Deploy API and worker together: the viewer transport changed from direct Kasm
ports to the worker lane WebSocket. Stop active jobs/viewers before updating.
Provider launch/lease requests have a longer API deadline than routine status
polls, so a cold remote browser launch is not limited by the status timeout.

Opt-in isolated validation:

```bash
SITEFLOW_DEMO_E2E=1 SITEFLOW_DEMO_FAULTS=1 \
SITEFLOW_DEMO_WORKER_IMAGE=siteflow-worker:remote-browser \
  .venv/bin/pytest -q backend/tests/test_demo_e2e.py
```

The suite creates its own official Manager, worker, X11 volume and temporary API
database; it never uses production profiles or real site credentials. Its coverage includes
remote CDP, persistent-profile reuse, humanized input, desktop capture/click,
VNC handshake, workflows, plugin cancellation/revocation and backend loss.
Historical results do not validate the current licensed image. Before using this
suite to validate that image, ensure its isolated Manager is configured with a
valid license and sufficient profile capacity; the command alone does not supply
a license or prove current-image compatibility.
