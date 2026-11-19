# SiteFlow backend

Current scope: single-admin authentication, encrypted integration
configuration, and the site/template/feature workflow model.

## Run

From `backend/`:

```bash
../.venv/bin/uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000
```

The app reads `SITEFLOW_*` from the process environment or the repository
`.env`. `SITEFLOW_MASTER_KEY` is required.

If no admin env variables are configured and no database user exists, the API
prints a random admin password to stdout on every restart. Change it in the web
UI to persist it in SQLite.

## Test

```bash
../.venv/bin/pytest
```

## Endpoints

```text
GET    /api/v1/health
POST   /api/v1/auth/login
GET    /api/v1/auth/me
POST   /api/v1/auth/logout
POST   /api/v1/auth/password

GET    /api/v1/lanes
POST   /api/v1/lanes/{key}/start
POST   /api/v1/lanes/{key}/stop
WS     /api/v1/lanes/{key}/vnc
GET    /api/v1/lane-settings
PUT    /api/v1/lane-settings

GET    /api/v1/integrations
PUT    /api/v1/integrations/proxy
DELETE /api/v1/integrations/proxy
PUT    /api/v1/integrations/twofauth
DELETE /api/v1/integrations/twofauth
POST   /api/v1/integrations/proxy/test
POST   /api/v1/integrations/twofauth/test
PUT    /api/v1/integrations/cfbox
DELETE /api/v1/integrations/cfbox
POST   /api/v1/integrations/cfbox/test

GET    /api/v1/features

GET    /api/v1/templates
POST   /api/v1/templates
GET    /api/v1/templates/{id}
PUT    /api/v1/templates/{id}
DELETE /api/v1/templates/{id}
GET    /api/v1/templates/{id}/resolved

GET    /api/v1/sites
POST   /api/v1/sites
GET    /api/v1/sites/{id}
PUT    /api/v1/sites/{id}
DELETE /api/v1/sites/{id}
GET    /api/v1/sites/{id}/resolved
POST   /api/v1/sites/validate
POST   /api/v1/sites/{id}/validate
POST   /api/v1/sites/config/export
POST   /api/v1/sites/{id}/config/export
POST   /api/v1/sites/{id}/config/import
GET    /api/v1/sites/{id}/credential
PUT    /api/v1/sites/{id}/credential
DELETE /api/v1/sites/{id}/credential

GET    /api/v1/runs
POST   /api/v1/runs
GET    /api/v1/runs/{id}
POST   /api/v1/runs/{id}/cancel
```

All workflow endpoints require a session; mutations also require
`X-CSRF-Token`. See `docs/feature-model.md` for resolution semantics.

## Site Configuration Transfer

Export accepts the current unsaved `SiteWrite` body, not just a saved site ID,
and returns `SiteWrite`. Use the ID route for an existing site's draft so its
`expected_revision` and `expected_history_key` are checked against that site;
use the route without an ID for a new draft. Export uses read-only draft
resolution, returns `Cache-Control: no-store`, and does not write history or
refresh authentication activity. Both routes require authentication and CSRF.

The result materializes the complete template chain plus local overrides and
effective feature defaults. It has `template_id=null` and null CAS fields, strips
resolved-only metadata and system signals, and preserves group anchors, effective
positions, disabled entries and ordering. System signal references in conditions
remain valid because the server supplies those signals again on import. Like the
manual editor, exported configuration can contain private authored values; treat
it as sensitive rather than model-safe data.

Import accepts `SiteWrite`, returns `SiteDetail`, and requires non-null
`expected_revision` and `expected_history_key` for the destination. `steps`,
`groups` and `signals` must each be explicitly supplied, including empty arrays
when clearing them. All three local collections are replaced, not merged with
old local entries. A null group `step_anchor` clears its old value. Omitted
metadata uses `SiteWrite` defaults, including an empty `trusted_urls` list.
`template_id` may remain bound for ordinary template-plus-overrides imports,
or be null for standalone configuration. Inheritance still merges configuration
objects, replaces arrays and removes keys marked null. Import uses the existing
CAS/history transaction; conflicts return 409, invalid input returns 422, and
failed writes leave the previous configuration and history intact.

For import previews, call either site validation route with
`?replace_local=true` (default is false). This also requires all three collections
and returns a normalized complete `SiteWrite`-compatible `local` object without
CAS fields. Fill the form from `local`, add the current destination CAS fields,
and save existing sites through `/config/import`. New sites still save through
normal `POST /api/v1/sites`. Ordinary PUT/validation preservation rules are unchanged.
When exporting an imported draft before saving, pass `?replace_local=true` to
the export route too, so old null-anchor preservation cannot affect the export.

Assistant imports call `import_site_config(site_id, payload, _commit=False)`
directly, retaining the outer consent/binding/receipt transaction and protected
metadata checks. They preserve template binding; model tools cannot unbind a
template. Import standalone `template_id=null` configuration manually instead.

## Container

```bash
./ops/build.sh
docker compose -f ops/docker-compose.yml up -d
```

See `docs/authentication.md` for bootstrap credentials and security notes.
