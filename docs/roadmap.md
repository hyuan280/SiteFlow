# Release Scope

The first release includes:

- Single-admin authentication, CSRF protection and encrypted configuration.
- Sites, inherited templates, schema-driven features, signals and grouped ordering.
- Four browser lanes, VNC, element picking, cancellation and concurrency limits.
- Scheduling, notifications, task logs, screenshots and retention controls.
- Run analytics, cookie import/export and credential-provider integrations.
- Plugin runtime, capability approval, scoped authorization and security audit.
- Docker Compose deployment with fresh SQLite schema creation.

Historical database upgrades, old-engine imports and database-porting tools are
not part of v1. Current site/template import and export remain supported.

Out of scope: automatic site disabling or root-cause inference, plaintext
password storage in the web client, shared browser profiles across workers,
and multi-instance SQLite operation.
