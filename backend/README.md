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

## Container

```bash
./ops/build.sh
docker compose -f ops/docker-compose.yml up -d
```

See `docs/authentication.md` for bootstrap credentials and security notes.
