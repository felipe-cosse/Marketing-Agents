# API-01 verification

Status: verified locally

`GET /health/live` is an exact process-only signal. It returns the stable service body
without resolving readiness and remains available when the readiness probe is missing,
slow, malformed, or raising. Both health routes use `Cache-Control: no-store`.

`GET /health/ready` recomputes a fixed ordered report for database connectivity,
migration currency, catalog release/parity, deterministic provider registry,
deterministic connector registry, and worker-facing schema compatibility. The same
typed body is declared for `200` and `503`; only an exact, internally consistent report
with every check ready can produce `200`. Missing dependencies, timeouts, malformed
reports, and exceptions fail closed with bounded stable codes and never reflect raw
database URLs, paths, catalog diagnostics, credentials, or exception text.

The default local probe is read-only. It refuses to create an absent SQLite database,
does not migrate or seed, verifies an existing connection and mapped worker columns,
types, defaults, keys, constraints, and indexes, compiles the exact
5-department/12-function/36-template/43-instance catalog metadata, and validates mock
provider and connector registries without invoking an adapter. File hashes and directory
contents remain unchanged across repeated probes and a new probe instance.

DEL-04 owns the missing Alembic revisions, deployed-head comparison, catalog projection
tables, transactional seed, and database/source catalog parity. Consequently, the local
probe deliberately reports migration and persisted-catalog parity as not ready until
that owner supplies those checks. Current mapped-table inspection is not migration
currency, and the endpoint never repairs a dependency to manufacture readiness.

Machine authority: [`API-01.json`](API-01.json).
