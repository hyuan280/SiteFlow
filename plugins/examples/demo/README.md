# Local Demo

For disposable cancellation, revocation, timeout and unavailable-plugin tests, see
[FAULTS.md](FAULTS.md). Fault configuration is not part of the normal import.

Build/start: `docker compose -f ops/docker-compose.yml up -d --build plugin-demo demo-site`.
Review the registration and permissions in Plugin Management. The deployed plugin
must remain unapproved until you approve it. No external accounts are required.

The worker URL is `http://demo-site:8099`; the host URL is
`http://127.0.0.1:8099` (loopback-only publication).

Import `plugins/examples/demo/sites.import.json` using Sites > Import after
approval. This is the actual `siteflow.export`, version 1, `kind: sites` format;
the UI maps the item to backend `SiteWrite`. It creates exactly one disabled site,
`Demo Plugin End-to-End`, without credentials or template dependencies. Enable it
 explicitly to run all 19 steps sequentially. Four login steps at positions
 100–400 precede the existing scenarios at positions 1000–15000.

| Sequential Steps | Success Assertion |
| --- | --- |
| 1-4: login | Open `/login`, fill remote OTP, resolve credentials and submit, assert exact `Demo login verified: credentials and 2FA` |
| 5-6: checkin | Open `/checkin`, exact `Demo checkin successful` text |
| 7-9: form | Open `/form`, fill message, POST form, exact `Demo form submitted` text |
| 10-11: popup | Open `/popup`, follow popup, click checkin and assert exact text within one plugin step |
| 12-13: top-level frame | Explicitly navigate the active job page to `/frame`, assert exact checkin text |
| 14-16: captcha 150x40 | Open `/captcha`, built-in `challenge.image_captcha` with `plugin.demo.gd5`, POST and assert exact `Captcha verified` |
| 17-19: captcha 151x41 | Open `/captcha?scaled=1`, recognize, POST and assert the same exact server result |

| Endpoint | Scenario |
| --- | --- |
| `/login` | Combined server-validated credentials and 2FA; success issues an HttpOnly, SameSite=Strict session cookie |
| `/checkin` | `#sign` sets `#success`, safe and repeatable |
| `/form` | Fill `#message`, submit `#sign`, read `#success` |
| `/popup` | `#popup` opens same-origin `/checkin` |
| `/iframe` | Embeds `/frame`; use `/frame` top-level for automation |
| `/captcha` | Six-character GD5 image, input `#answer`, submit `#sign` |
| `/captcha?scaled=1` | Same captcha rendered at 151x41 instead of 150x40 |
| `/captcha.png` | Deterministic seed-5 PNG, no answer in HTTP metadata |
| `/verify` | POST `answer`; correct = 200, incorrect = 422 |
| `/health` | Health JSON |

The demo plugin advertises `plugin.demo.gd5` and implements RecognizeCaptcha
using the GD5 template recognizer. Docker copies the standalone GD5 package
from worker assets at build time; neither service imports the worker at runtime.
Captchas are intentionally deterministic test fixtures, not security controls.

Iframe traversal is not a broker operation. Popup following is limited to the
same origin and the current plugin step; subsequent steps still use the original
page. The optional `popup_selector` keeps the opener and checkin in one RPC.
Use top-level `/frame` instead of claiming broker iframe support. The `/iframe`
fixture contains no image captcha, so it is not a valid built-in iframe image
captcha scenario. `/`, `/health`, `/captcha.png` and `/verify` are respectively
the login alias, health probe and captcha transport/verification endpoints,
not additional workflow capabilities.

Every imported plugin step sets `expected_text`; empty or mismatching text fails
the step and job. No conditional `flow.result` is used: a skipped conditional
success result alone would not guarantee failure. Existing configs that omit
`expected_text` retain their previous optional-text behavior.

Isolated browser verification (does not approve or call the deployed plugin):

```sh
.venv/bin/python plugins/examples/demo/verify_browser.py
```

Requires the existing `siteflow-worker` image, Docker on Linux (host networking),
the project `.venv` with backend test dependencies and `frontend/node_modules`.
The test starts its own disposable worker container, local HTTP fixture and real
authenticated gRPC control/execution runtime. A temporary TestClient API database
registers and approves only that isolated plugin, imports via the actual UI
parser/payload expression and backend SiteWrite/workflow resolver, then enables
only that temporary copy and executes via ExecutionManager. It checks one persisted
job with all 19 steps successful in import order and all seven exact result assertions.
A second run deliberately mismatches the final captcha result: the first 18 steps
must succeed and the final step, job and run must persist failure.
A third run returns a wrong OTP through the real remote provider RPC; the server
must reject login and the job must not succeed, even with an existing session.
There are no captcha or plugin RPC mocks. The deployed plugin and production
database are never used. Worker data lives only inside the removed test container.
Port 16300 must be free. The UI adapter extracts the current SitesView payload
expression rather than maintaining a second backend-only import approximation.

## Secret Providers

The public fixture factors are username `admin`, password `password`, OTP `123456`.
They are intentionally fixed demo data, not real credentials or a real TOTP account.
The import stores only `credential_provider_id: "plugin.demo"` at site level,
and `totp_provider_id: "plugin.demo"` in the existing `challenge.2fa` step.
All fixture pages except login and health require a valid server-side session.

Sites > Login credentials > Credential provider offers Manual, Bitwarden, and
approved/enabled credential plugins. Existing Bitwarden matching controls remain.
The existing 2FA step and its global feature defaults offer a dynamic TOTP provider
selector. `service_hint` is needed only for built-in 2FAuth; plugins need no account.
Unavailable saved provider IDs remain visible rather than silently changing source.

The demo now declares `secret.credentials` and `secret.totp`. A capability change
invalidates prior approval. Explicitly review and reapprove in Plugin Management;
deployment and tests never autoapprove the live registration.
The backend redeems a 30-second one-use scoped lease, then calls authenticated
`ResolveCredential` / `GenerateTotp` on the approved registration endpoint.
Plugin step config cannot choose that endpoint. The remote request carries scope
metadata only, not worker capabilities, leases, or provider tokens in its body.
The worker receives only the resolved factors, checks the page origin before fill,
and does not place them in step results. Provider failures use generic errors.
Legacy sites with a null provider retain manual/`bitwarden_enabled` selection;
old 2FA configs retain the 2FAuth provider and saved service hints.

Additional regression: `node plugins/examples/demo/provider_ui_test.mjs`.
