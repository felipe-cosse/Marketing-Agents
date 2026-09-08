# DEL-04 verification

DEL-04 supplies five frozen, schema-only Alembic revisions, an atomic catalog
import/check, normalized deployment trigger persistence, and native migration/seed
commands. File-backed SQLite and an isolated PostgreSQL 14.17 runtime are verified
for migration, seed, native installation, and readiness. Revisions never call
`metadata.create_all`, import current ORM definitions, or embed catalog seed data.

## Verification results

The combined PostgreSQL runtime gate passes all 31 cases: 15 migration/seed,
15 native installation/readiness, and one subprocess CLI round trip. The
session-owned cluster and separate per-test databases are exercised live, with
seven additional no-server lifecycle regressions for cleanup and failure paths.
The gate completed in 184.26 seconds on PostgreSQL 14.17.

The combined `make test-del-04-persistence test-del-04-regression` invocation
passes without network access: 115 persistence/native-command/fixture cases in
262.84 seconds and 81 affected API/demo regressions in 95.22 seconds. The full
backend run completed with 1,709 passed, 31 explicitly opt-in PostgreSQL cases
skipped, and four passed subtests in 658.51 seconds; the seven new fixture cases
were run separately after that suite's collection. Formatting, lint, type checks
(248 source modules), architecture boundaries, and an offline installed-wheel
SQLite smoke pass. The wheel smoke loads 131 application modules exclusively
from the installed wheel, with all five revisions, exact schema, seed/reseed/check,
key-loss recovery, and the maintenance lease constraint.

The positive trigger gate passes. Restoring the pre-DEL-04 configuration
repository produces the intended witness assertion: no normalized trigger rows
instead of `manual` and `webhook`, not an import/setup failure. Final branch
verification binds the feature tree to all four manifest gates and the witness;
pre-commit snapshot checks are not a substitute for that attestation.

This macOS host initially refused a 56-byte System V shared-memory allocation.
After explicit user approval, validation temporarily raised `kern.sysv.shmall`
from `1024` to `16384` through a guarded administrator command. PostgreSQL and
pytest still ran as the normal service user. The gate passed, its private server
stopped, and the original `1024` value was restored and verified. No persistent
system configuration or unrelated IPC objects were changed. Neither application
startup nor the committed test fixture changes kernel settings automatically.

## Native use

```sh
make migrate
make seed
make seed-check
```

Defaults are the ignored `data/marketing_agents.db`, `data/digest.key`, and the
versioned `catalog/v1` source. Override `DATABASE_URL`,
`MARKETING_AGENTS_DIGEST_KEY_PATH`, and `CATALOG_ROOT` explicitly for an isolated
installation. The key must have an owner-only directory and file permissions;
new directories/files are created with `0700`/`0600`. The initializer never
overwrites a key and refuses a missing key beside an existing database. Migration
calls the same initializer used by `make init-local-secret` and the
`marketing-agents-local-secret` CLI. Native commands accept file-backed SQLite or
a preprovisioned PostgreSQL database, not an in-memory SQLite database. PostgreSQL
requires the optional driver (`uv sync --frozen --extra postgresql`) and an
explicit `postgresql+asyncpg` URL; the private digest key remains local. These
commands do not provision or drop PostgreSQL databases. A new key is allowed only
when the provisioned database has no existing tables, views, materialized views,
or sequences; existing schema requires its original key. Credentials should be
supplied through the deployment environment, not committed or copied into logs.

The schema upgrade and non-secret key fingerprint/version binding commit in one
transaction. Existing unversioned tables are never stamped/adopted; existing
application data without a valid key identity is never rebound. Keep the database
and key together across restarts. Recovery of a lost/mismatched pair requires a
verified matching backup; these commands deliberately provide no destructive
reset or key-replacement option.

`marketing-agents-db seed --check` compiles the complete source and compares the
persisted projection without DML, DDL, commit, repair, or missing-file creation.
Check connections enforce SQLite query-only access or PostgreSQL
`default_transaction_read_only=on` and avoid ignoring active SQLite WAL data.
Errors return bounded JSON codes, not driver exceptions, database URLs, key
material, prompts, or admitted payloads.

## Frozen revision ownership

| Revision | Tables added | Dependency reason |
| --- | ---: | --- |
| `0001` | 13 | Catalog identities/releases, configuration, triggers, local key identity |
| `0002` | 12 | Brief revisions, work/runs/plans/selections/routing, steps/transitions, artifacts/parents |
| `0003` | 10 | Actions/dispatch/receipts, approvals/consumption, authorization sets, audit sequence |
| `0004` | 4 | Webhook receipts/deliveries, schedules/occurrences |
| `0005` | 6 | Execution controls/policies/attempts, rate windows, audit events, maintenance ledger; all query indexes |

The sequence adapts plan 04 to the already implemented runtime: audit events
reference schedules/occurrences and execution attempts, and attempts reference
rate windows, so those tables must finish in `0005`. Existing equivalent names
are retained: `run_plan_selected_instances` for run selections,
`execution_attempts` for tool attempts, `artifact_parent_edges` for provenance,
and `authorization_sets`/`authorization_set_members` for action authorization.
No duplicate parallel execution model is introduced. Campaign brief revisions
and maintenance rows are storage foundations, not a new public brief API or
maintenance worker in this requirement.

Every prior revision upgrades to its successor while preserving a historical
sentinel. Fresh head has exactly 45 application tables plus `alembic_version`,
with zero application rows when using the schema-only library. Exact metadata
comparison covers columns/types/nullability/defaults, named primary/unique/foreign
keys, checks, and indexes, including partial-index drift. Unknown/multiple
revisions, extra/unversioned tables, downgrade attempts, and head drift fail
closed. Injected failure after successful DDL rolls back both schema and revision
and permits a successful retry. SQLite uses explicit transactional DDL and one
serialized migration owner; PostgreSQL uses a transaction-scoped advisory lock.
PostgreSQL comparison reflects primary keys separately, uses dialect-truncated
constraint names, checks SERIAL defaults against their owned sequence, and
compares server-normalized expressions using `EXPLAIN (VERBOSE, FORMAT JSON)`
with `LIMIT 0`, never `ANALYZE`. Checks/defaults are not evaluated against table
rows and sequence defaults are not advanced. This is not a sandbox against a
database administrator: immutable schema functions can be evaluated during query
planning. Drift tests reject weakened or NOT VALID checks, missing primary keys
or sequence defaults, a different sequence, a partial replacement index, and a
foreign key redirected to a same-named table in another schema.
Maintenance lease checks explicitly require a non-null expiry when claimed;
regressions reject partial triples and reversed expiry, including SQL's nullable
CHECK-expression case.

All destructive downgrades are unsupported and raise before mutation. Restore a
verified backup into a separate installation instead. Raw Alembic is a schema
maintenance interface; deployed native usage goes through the paired command.
The only Ruff exception is generated migration SQL literal line length; other
lint rules remain enabled.

## Seed and deployment preservation

The compiled catalog is validated before a transaction. Stable IDs, relationships,
resolved prompts/schema hashes, capabilities, policies, trigger-kind edges, and
immutable release snapshots are persisted and exactly rechecked before commit.
The canonical release initially inserts 262 catalog projection rows and 43
instance-configuration defaults. Result catalog counts include releases/current
pointer; configuration insert/preserve counts are separate. Trigger definitions
are checked as the versioned configuration projection, not counted as catalog
source rows.

Reseeding preserves operator-controlled enabled flags, variant labels, connector
bindings, triggers, schedules, and optimistic revisions. A new content version
may change catalog-owned fields and display ordering, including valid order
swaps; it cannot silently remap/remove stable identities or reuse a version with
different content. Historical releases and stored work/run/plan snapshots remain
unchanged. Incompatible local overrides abort the entire new release.

Missing/changed catalog projection fields are repairable by explicit seed, while
`--check` reports them without repair. Corrupt release history or configuration
integrity fails closed. Injected catalog/default insertion failures roll back
all rows. Independent file-backed SQLite seeders and concurrent PostgreSQL
transactions converge on one exact release and 43 defaults.

Configuration compare-and-swap now maintains normalized trigger rows in the same
transaction, with stable binding IDs and real catalog-instance foreign keys.
Stale/invalid updates, post-flush failures, and tampered trigger projections are
covered by negative tests. Existing integration fixtures now seed real catalog
parents rather than bypassing the new FK.

Readiness checks actual head/schema, exact persisted catalog/configuration parity,
and deployment key identity without running migrations or repairing data. A
schema-only library database is not a fully initialized deployment. Native key
loss, replacement, unsafe permissions, or missing identity cannot report ready.

## Verification and boundaries

```sh
make test-del-04-persistence
make test-del-04-regression
# Optional server binaries and cached postgresql extra required:
make test-del-04-postgresql
make verify-architecture
make format-check lint typecheck
```

The causal witness retains new migrations, seed, and trigger tests while restoring
the pre-DEL-04 configuration repository. The trigger-projection gate must fail
because that repository cannot maintain or validate the new normalized rows.
This proves a runtime connection, not merely test-file presence.
The witness runner prepends the archived checkout's source to its import path;
run the evidence verifier with the bootstrapped `.venv/bin` on `PATH`. It reuses
installed pytest without dependency installation or an editable-import shortcut
back to the real checkout.

Offline PostgreSQL compilation checks all revision DDL without a server. The
separate live gate requires `initdb`/`pg_ctl` on `PATH` and the optional driver;
it explicitly sets `MARKETING_AGENTS_TEST_POSTGRES=1`. Ordinary backend tests
skip these server-dependent cases by default, while explicit opt-in fails if its
prerequisites are unavailable. One private cluster is owned by the pytest session;
each test creates and removes its own uniquely named database. TCP listeners are
disabled, the Unix socket directory is `0700`, and no existing database
destination is inherited. The session finalizer confirms server shutdown before
removing temporary data; an unconfirmed shutdown preserves the directory and
fails. A failed bootstrap is cached so later cases do not repeatedly retry it.
macOS sandboxed runners may require shared-memory and private-socket permission.

The live gate exercises fresh/prior revisions and restart, exact metadata,
injected DDL/seed rollback, seed races and preserved overrides, drift rejection,
native CLI installation, no-write checks, and key-loss/replacement/permissions.
This qualifies PostgreSQL 14.17 for those paths, not every runtime concurrency
path, hosted services, other server versions, or network/TLS authentication.
Container/process startup, supervision, backup/restore automation, and complete
operational release gates belong to later delivery requirements. Database/key
access and lack of encryption at rest remain residual risks.

Implementation references: [Alembic asynchronous cookbook](https://alembic.sqlalchemy.org/en/latest/cookbook.html)
and [SQLAlchemy SQLite transaction guidance](https://docs.sqlalchemy.org/en/20/dialects/sqlite.html),
[SQLAlchemy reflection](https://docs.sqlalchemy.org/en/20/core/reflection.html),
[PostgreSQL EXPLAIN](https://www.postgresql.org/docs/14/sql-explain.html), and
[PostgreSQL function volatility](https://www.postgresql.org/docs/14/xfunc-volatility.html).
Host prerequisite reference: [PostgreSQL kernel resources on macOS](https://www.postgresql.org/docs/14/kernel-resources.html).
Machine authority: [DEL-04.json](DEL-04.json).
