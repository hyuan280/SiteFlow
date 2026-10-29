# Cancellation and Page Lifecycle Fault Suite

Run from `backend`:

```sh
SITEFLOW_DEMO_E2E=1 SITEFLOW_DEMO_FAULTS=1 ../.venv/bin/pytest -q -s tests/test_demo_e2e.py
```

Requires the existing `siteflow-worker` image and frontend Node dependencies.
The harness creates a disposable worker, temporary API database, local HTTP fixture,
and local gRPC server. Only the isolated demo registration is approved. No live
provider or external website is contacted. `faults.json` is separate from the normal
single-site import; the normal 19-step demo is unchanged.

Coverage: normal ordered execution, wrong result, wrong OTP, popup followed by
built-in click (the opener has no matching button), long cooperative RPC timeout,
cancel, delayed broker click timeout, revoke during execution, and dropped plugin.
Fault cases assert terminal non-success, RPC handler settlement where applicable,
zero lane leases, zero broker sessions, zero task-page mappings, and at most the
lane home page remaining. Unit tests cover ownership isolation, cross-origin and
late popup closure, in-flight broker cancellation, and real gRPC future cancellation
for both ExecuteStep and RecognizeCaptcha.

Measured on 2026-10-28 in the disposable harness: cancellation to observed settled
handler and terminal state 0.241s; revocation 0.772s. These are observations, not
hard distributed-system latency guarantees. Authorization polling is every 0.5s
with a 2s HTTP timeout; transport scheduling, database contention and browser close
can extend total settlement time.
