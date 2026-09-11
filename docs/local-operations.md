# DEL-05 local operations

Claim: **Implemented and verified** within the historical
[DEL-05 evidence](verification/requirements/DEL-05.md), not a claim that current
full-release acceptance is green. [Operations](operations.md) is the operator
entry point; [verification](verification.md) records current results and limits.

## Startup and storage

`make up` builds the digest-pinned Python/Node/nginx images from frozen uv/pnpm
locks. `local-secret-init` owns first creation of the private installation key;
`migrate-seed` owns schema upgrade and exact repeatable catalog import. API,
run-worker, scheduler-worker, and web start only after these one-shot services
complete. No runtime process implicitly migrates the database.

The default Compose project is `marketing-agents-local`. Its volumes are
`marketing-agents-local_data`, `marketing-agents-local_local-secrets`, and
`marketing-agents-local_api-socket`. The last is disposable IPC, not durable data.
The first two must be backed up and restored together. Runtime UID/GID is
10001, with owner-only database/key directories; only initialization writes the
secret volume. The web process is UID101 with the socket group10001 and no
database/key mounts. Root filesystems are read-only with explicit temporary paths.

SQLite WAL may require transient shared-memory files even for a read-only query.
The secret-init stage therefore uses `--defer-database-check` on its read-only
data mount: it checks presence, path ownership, and private key permissions and
never replaces an existing key. The following migration owner verifies the
database fingerprint with writable WAL support **before any migration or seed
writes**. A mismatched key is not accepted; a missing key beside a database stops
at secret initialization. Native commands retain immediate fingerprint checking.

`make down` retains every named volume. Never use broad Docker pruning or copy a
live `.db` alone: recent state may still be in its WAL. There is deliberately no
destructive reset target. Missing keys cannot be regenerated to recover old data.

## Network and identity

Only `127.0.0.1:8080` is published. API, both workers, migration, and key init use
`network_mode: none`. The API binds a private Unix socket; it does not relax the
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

After activating Python3.12, Node24.20.0, pnpm11.24.0 and uv0.10.7:

```sh
make bootstrap
make dev
```

The supervisor uses `data/native/marketing_agents.db` and
`data/native/secrets/digest.key`, enforces owner-only storage, and logs to private
per-run directories beneath `data/native`. It starts API8000 and Vite5173,
validates readiness and both worker health files, and performs bounded cleanup
on Ctrl-C, termination, startup failure, or child failure. It does not kill
unrelated listeners. Override explicitly with:

```sh
.venv/bin/python scripts/dev.py --state-dir /absolute/private/new-installation \
  --api-port 18000 --web-port 15173
```

`--smoke` stops after successful readiness. Reusing the same state directory
preserves the key/database. The separate `make migrate seed seed-check` commands
default to `data/marketing_agents.db` and `data/digest.key`; set both path variables
explicitly when operating on the supervisor's installation.

## Paired backup and restore

Backups are **secret-bearing**. Keep their parent directory private, do not add
them to Git, and never upload them as test/CI evidence. Operator transport uses
the repository's `.venv` Python (`uv sync --frozen --python 3.12` if only Compose
was previously used). Compose operations need the local Docker daemon too.

```sh
make backup-local DESTINATION=/absolute/private/new-backup
make restore-local BACKUP=/absolute/private/new-backup \
  LOCAL_PROJECT=marketing-agents-restored \
  LOCAL_IMAGE=marketing-agents-local-backend:local
COMPOSE_PROJECT_NAME=marketing-agents-restored make up
```

The backup target's parent must exist; the target itself must not. Restore refuses
an existing destination project/paired volumes and starts no application service.
It creates only new scoped data/key volumes; startup creates a fresh socket
volume. Source installation data is never removed. Only the runtime image already
present locally is used; backup/restore never pulls arbitrary images.

Native equivalent (explicit existing database and key):

```sh
make backup-local LOCAL_MODE=native \
  DATABASE_URL=sqlite+aiosqlite:////absolute/private/native/marketing_agents.db \
  MARKETING_AGENTS_DIGEST_KEY_PATH=/absolute/private/native/secrets/digest.key \
  DESTINATION=/absolute/private/new-backup
make restore-local LOCAL_MODE=native BACKUP=/absolute/private/new-backup \
  DESTINATION=/absolute/private/new-restored-installation
```

SQLite's online backup API captures a consistent snapshot including WAL state.
The database, key, non-secret fingerprint/version, schema/catalog revisions, and
checksummed manifest are staged privately, verified, and published without
overwriting an existing target. Missing halves, incompatible schema, mismatched
keys, permissive permissions, unsafe links, or checksum failures fail closed.
Do not manually repair a failed bundle or generate a replacement key; restore a
known-good pair into new storage. This is a local-demo recovery procedure, not a
production availability claim or measured RPO/RTO.

## Verification and troubleshooting

With host Git and Python 3 available, `make verify-clean REF=HEAD` exports a
committed tree to an owned temporary
directory, creates a unique `marketing-agents-del05-*` project, acquires frozen
dependencies, and then verifies application/runtime behavior. Backend/frontend
test images run with `--network none`; the production browser shares only web's
namespace and blocks any request outside its exact local origin. The final JSON
contains sanitized phases, exit codes and hashes, never database/key bundles.
Cleanup checks ownership before deleting only its test containers, three volumes,
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
