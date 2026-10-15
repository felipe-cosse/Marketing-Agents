# DEL-05 local operations

Claim: **Implemented and verified** within the historical
[DEL-05 evidence](verification/requirements/DEL-05.md), not a claim that current
full-release acceptance is green. [Operations](operations.md) is the operator
entry point; [verification](verification.md) records current results and limits.

## Startup and storage

Current defaults use PostgreSQL as the primary database. This change does not
extend the historical DEL-05 verification claim above; current qualification
must be recorded separately.

`make up` builds the digest-pinned PostgreSQL/Python/Node/nginx images from frozen
uv/pnpm locks. PostgreSQL initializes its own empty cluster and restricted
application role first. `local-secret-init` verifies or creates the private
installation key;
`migrate-seed` owns schema upgrade and exact repeatable catalog import. API,
run-worker, scheduler-worker, and web start only after these one-shot services
complete. No runtime process implicitly migrates the database.

The default Compose project is `marketing-agents-local`. Its volumes are
`marketing-agents-local_postgres-data`, `marketing-agents-local_postgres-secrets`,
`marketing-agents-local_postgres-socket`, and `marketing-agents-local_api-socket`.
The two socket volumes are disposable IPC, not durable data. PostgreSQL data and
the installation key must be backed up and restored together using the logical
backup procedure below, not by copying the live cluster directory.
PostgreSQL runs as UID999; application processes use UID/GID10001 and the
non-superuser `marketing_agents` database role. Peer authentication maps the
application OS identity to that role; it cannot authenticate as the PostgreSQL
superuser. Only initialization writes the secret volume. The web process is
UID101 with the socket group10001 and no
database/key mounts. Root filesystems are read-only with explicit temporary paths.

PostgreSQL key initialization checks the database/key fingerprint immediately.
A populated database with a missing or mismatched key fails closed; keys are not
replaced to make startup succeed. The historical SQLite Compose initialization
used a deferred WAL-compatible check followed by migration-owner verification;
that is not the current PostgreSQL path. Existing legacy `data` and
`local-secrets` volumes are retained, not adopted or converted by the new default.

`make down` retains every named volume. Never use broad Docker pruning or copy a
live `.db` alone: recent state may still be in its WAL. There is deliberately no
destructive reset target. Missing keys cannot be regenerated to recover old data.

## Network and identity

Only `127.0.0.1:8080` is published. PostgreSQL, API, both workers, migration, and
key init use `network_mode: none`. PostgreSQL disables TCP listeners and uses a
private, group-scoped Unix socket. The API binds its separate private Unix
socket; it does not relax the
loopback-only local identity setting or trust forwarded identity headers. Nginx
forwards only explicitly allowlisted headers, including CSRF/Origin and signed
webhook headers; untrusted forwarding headers are stripped.

The tested Docker Desktop29.7.2 internal/isolated and internal/NAT bridges both
discarded the requested port publication. The compatibility fallback is one
normal NAT ingress bridge used only by web. It is not an OS-level web egress
firewall: Docker-host and external destinations remain reachable from that
container if its code/configuration is replaced. The committed proxy has no
external upstream, and backend processes cannot use that network. Local Docker
administrators remain trusted. Docker28+ is required for its corrected
loopback-publication boundary. The wrapper rejects remote Docker contexts.

Local identity is credential-free, fixed server-side, and includes administrative
and approval privileges. It is unsuitable for a shared/public endpoint. Mock
newsletter/CRM receipts are not actual delivery. Runtime entrypoints reject
non-local, real-adapter, or external-network-enabled settings.

## Native workflow

After activating Python3.12, Node24.20.0, pnpm11.24.0 and uv0.10.7, install
PostgreSQL14+ locally with matching-major `initdb`, `postgres`, `pg_ctl`, and
`psql` available on `PATH`. Native startup does not install tools or silently
fall back to SQLite when they are absent:

```sh
make bootstrap
make dev
```

The supervisor uses the persistent `data/native/postgres` cluster,
`data/native/native-postgresql.json` ownership/version marker, and paired
`data/native/secrets/digest.key`. It enforces owner-only storage and logs to private
per-run directories beneath `data/native`. PostgreSQL is a directly supervised
owned child, not a separately installed system service. It has no TCP listener;
a fresh private `/tmp/ma-pg-*` Unix-socket directory is removed after shutdown.
The application connects as a non-superuser role that owns only its application
database. Startup also launches API8000 and Vite5173,
validates readiness and both worker health files, and performs bounded cleanup
on Ctrl-C, termination, startup failure, or child failure. It does not kill
unrelated listeners. Override explicitly with:

```sh
.venv/bin/python scripts/dev.py --state-dir /absolute/private/new-installation \
  --api-port 18000 --web-port 15173
```

`--smoke` stops after successful readiness. Reusing the same state directory
preserves the key/database. Existing SQLite data causes an explicit error under
the PostgreSQL default: select the legacy backend or choose fresh storage; no
automatic conversion occurs. PostgreSQL major upgrades and adoption of unknown
clusters are also refused.

Explicit SQLite compatibility mode retains the existing file-backed behavior:

```sh
make dev LOCAL_DATABASE=sqlite LOCAL_STATE=/absolute/private/existing-sqlite
```

That mode uses `marketing_agents.db` and `secrets/digest.key` in the selected
directory and does not require PostgreSQL tools. It refuses a PostgreSQL state
directory rather than rebinding its key. The separate `make migrate`, `make seed`,
and `make seed-check` defaults use a PostgreSQL URL at `localhost:5432` and
`data/digest.key`; they do not provision or discover the supervisor's private
socket. Supply the exact existing database URL and matching key path before
using those low-level commands on any installation.

## Paired backup and restore

Backups are **secret-bearing**. Keep their parent directory private, do not add
them to Git, and never upload them as test/CI evidence. Operator transport uses
the repository's `.venv` Python (`uv sync --frozen --python 3.12` if only Compose
was previously used). Compose operations need the local Docker daemon too.

```sh
make backup-local DESTINATION=/absolute/private/new-backup
make restore-local BACKUP=/absolute/private/new-backup \
  LOCAL_PROJECT=marketing-agents-restored \
  LOCAL_IMAGE=marketing-agents-local-backend:local \
  LOCAL_POSTGRES_IMAGE=marketing-agents-local-postgres:local
COMPOSE_PROJECT_NAME=marketing-agents-restored make up
```

The backup target's parent must exist; the target itself must not. Restore refuses
an existing destination project/paired volumes and starts no application service.
It creates only new scoped PostgreSQL data/socket/key volumes. Restore temporarily
starts only its owned database to import and verify the pair, then stops it;
API, workers and web are not started. Source installation data is never removed.
Only explicitly selected backend/PostgreSQL images already present locally are
used; backup/restore never pulls arbitrary images.

For native PostgreSQL, stop the supervisor first. `pg_dump`/`pg_restore` must
match the server major. The wrapper locks the existing owned state, temporarily
starts only that database on a private socket, snapshots its pair, and stops it.
An active supervisor, unknown cluster, missing key, or mismatched pair is refused:

```sh
make backup-local LOCAL_MODE=native LOCAL_STATE=/absolute/private/native \
  DESTINATION=/absolute/private/new-postgresql-backup
make restore-local LOCAL_MODE=native BACKUP=/absolute/private/new-postgresql-backup \
  DESTINATION=/absolute/private/new-restored-postgresql
```

The equivalent direct command is:

```sh
.venv/bin/python scripts/postgresql_backup.py backup --mode native \
  --state-dir /absolute/private/native --destination /absolute/private/new-postgresql-backup
```

Native restore provisions and stops only a new owned PostgreSQL cluster; it does
not start the application. It rejects existing destinations. A failed fresh
native restore retains its private target for diagnosis rather than deleting
unknown or unconfirmed-live data. Source, client tools, and restored server must
have the same major version; this procedure is not a
PostgreSQL major upgrade or a SQLite/PostgreSQL conversion.

For an explicit legacy SQLite installation only:

```sh
make backup-local LOCAL_MODE=native LOCAL_DATABASE=sqlite \
  DATABASE_URL=sqlite+aiosqlite:////absolute/private/native/marketing_agents.db \
  MARKETING_AGENTS_DIGEST_KEY_PATH=/absolute/private/native/secrets/digest.key \
  DESTINATION=/absolute/private/new-backup
make restore-local LOCAL_MODE=native LOCAL_DATABASE=sqlite BACKUP=/absolute/private/new-backup \
  DESTINATION=/absolute/private/new-restored-installation
```

PostgreSQL uses a custom-format logical dump bound to an exported consistent
snapshot. Explicit SQLite compatibility backups use its online backup API,
including WAL state. The database, key, non-secret fingerprint/version,
schema/catalog revisions, and
checksummed manifest are staged privately, verified, and published without
overwriting an existing target. Missing halves, incompatible schema, mismatched
keys, permissive permissions, unsafe links, or checksum failures fail closed.
Do not manually repair a failed bundle or generate a replacement key; restore a
known-good pair into new storage. This is a local-demo recovery procedure, not a
production availability claim or measured RPO/RTO.

PostgreSQL sequence counters are not MVCC-transactional; see the official
[sequence semantics](https://www.postgresql.org/docs/17/functions-sequence.html).
The recovery verifier
compares quiescent owned installations and checks sequence stability; an online
Compose dump is not a universal atomic point-in-time sequence guarantee. Stop
application writers when an exact quiescent recovery comparison is required.

## Verification and troubleshooting

With host Git and Python 3 available, `make verify-clean REF=HEAD` exports a
committed tree to an owned temporary
directory, creates a unique `marketing-agents-del05-*` project, acquires frozen
dependencies, and then verifies application/runtime behavior. Backend/frontend
test images run with `--network none`; the production browser shares only web's
namespace and blocks any request outside its exact local origin. The final JSON
contains sanitized phases, exit codes and hashes, never database/key bundles.
Cleanup checks ownership before deleting only its test containers, scoped volumes,
network, and export. Developer volumes are never reused. Cached build images may
remain for reuse; no global image prune is performed.

- **Port conflict:** choose `MARKETING_AGENTS_WEB_PORT=18080 make up`. Native mode
  reports the occupied port before initializing storage; choose different ports.
- **Permissions/key loss:** preserve existing files and recover a known-good pair;
  do not chmod unrelated paths or overwrite keys to force startup.
- **Migration/catalog mismatch:** read the safe diagnostic, compare the selected
  commit/catalog, and recover separately. Unknown schema and downgrade fail closed.
- **Readiness timeout:** confirm supported tools and available CPU/memory. Heavy
  concurrent builds can exhaust the bounded readiness deadline; retry after load
  subsides. A timeout is not reported as a successful start.
- **Worker restart:** claims have bounded leases and fenced ownership. Graceful
  shutdown stops new claims; forced termination leaves durable state for recovery.
- **Configuration conflict:** refresh and retry against the new revision; startup
  and reseeding preserve operator changes.
- **Approval expired/changed/used:** review the current proposed action and create
  a fresh valid decision. Do not bypass the immutable action authorization barrier.
- **Unexpected real-adapter settings:** use the committed safe Compose settings;
  runtime rejects real/provider-enabled configurations rather than silently calling
  a provider or pretending a real action was successful.
