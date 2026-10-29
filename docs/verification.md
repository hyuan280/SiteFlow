# Verification

Run automated checks from the repository root:

```bash
(cd backend && ../.venv/bin/pytest -q)
.venv/bin/pytest -q plugins/runtime/tests
(cd frontend && npm run build)
(cd frontend && node tests/feature-settings.mjs)
(cd frontend && node tests/plugin-management.mjs)
(cd frontend && node tests/workflow-ordering.mjs)
git diff --check
```

`backend/tests/test_fresh_install.py` exercises an empty temporary database,
application startup, authenticated access, current defaults, workflow fields,
credential columns and unapproved plugin registration. It also reopens the
current database without historical upgrade or backfill code.

Browser and container integration checks require their documented dependencies.
Use the isolated [demo](../plugins/examples/demo/README.md) and
[cancellation fault scenarios](../plugins/examples/demo/FAULTS.md), not a live
database or real credentials. Tests skipped for missing optional dependencies
are not evidence that those integrations passed.

For 2FAuth, configure a service/account hint explicitly and confirm the matching
account. Missing or ambiguous matches must not silently choose an account.
Keep credentials, real service addresses, screenshots and machine-specific
verification output out of version control.
