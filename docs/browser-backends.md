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

## Keyless Manager

The official image is pinned by digest in both Compose files. It contains SDK
0.6.3 and keyless Chromium 146.0.7680.177.5. Compose sets the existing binary path
and disables updates; no license key is supplied. The adapter checks Manager's
tier and binary version before launching a profile and rejects a different tier
or version rather than downloading/upgrading implicitly.

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
```

Each lane gets a dedicated profile. Opaque IDs are atomically saved in
`worker_data/browser-profiles.json`; browser state lives in `browser_data`.
The adapter never imports, deletes or adopts unrelated Manager profiles. On
reconnection it may stop/restart only a previously mapped SiteFlow profile.
Use a dedicated Manager instance per worker, not one with independently operated
user profiles. Losing the mapping can leave an orphan profile, but does not cause
automatic adoption by name or deletion. Back up both data volumes together.

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

The existing SiteFlow idle policy is unchanged: a profile browser starts only on
manual start or lane lease; idle stop defaults to 300 seconds and is disabled by
zero. Task leases and active viewers prevent idle stop; releasing either refreshes
the countdown. Profile data and identity survive browser stop. A failed provider
stop is reported as an error, not falsely acknowledged as stopped.

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
database; it never uses production profiles or real site credentials. It covers
remote CDP, persistent-profile reuse, humanized input, desktop capture/click,
VNC handshake, workflows, plugin cancellation/revocation and backend loss.
