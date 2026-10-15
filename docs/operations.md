# Local operations

This is the operator entry point for the credential-free, single-user local
installation. **Implemented and verified (historical local evidence):** startup,
native supervision, paired backup/restore, and restart behavior are recorded in
[DEL-05](verification/requirements/DEL-05.md) and exercised by the
[runtime tests](../tests/integration/runtime/test_del_05_process_composition.py),
[native tooling tests](../tests/tooling/test_del_05_native.py), and
[backup tests](../tests/integration/db/test_del_05_backup.py).
Those records are not a fresh health check of an existing installation.
The current primary database is PostgreSQL; historical SQLite evidence is not
a claim that the PostgreSQL change has passed fresh full-release qualification.

**Deterministic mock behavior:** the running application supports the five
[demos](demos.md); it does not contact real model or connector providers.
**Operational status recorded 2026-09-10:** CI is paused at the operator's request,
and live execution is disabled. Refer to the [verification record](verification.md)
for the known failure state. Do not resume remote workflows or enable real
execution simply to use these local instructions.

## Choose one installation

The canonical path requires Make and a local Docker Engine 28+ with Compose:

```sh
make up
```

The wrapper builds, starts detached services, waits for bounded readiness, and
prints [http://127.0.0.1:8080](http://127.0.0.1:8080). It does not require `.env`,
cloud credentials, an external database service, or a manual seed step. It owns a
private local PostgreSQL service. Initial acquisition
may contact image/package registries. It rejects remote Docker daemons and pins
builds to the selected local daemon.

For a different project or port, keep both values consistent across operations:

```sh
COMPOSE_PROJECT_NAME=marketing-agents-demo MARKETING_AGENTS_WEB_PORT=18080 make up
COMPOSE_PROJECT_NAME=marketing-agents-demo MARKETING_AGENTS_WEB_PORT=18080 make logs
COMPOSE_PROJECT_NAME=marketing-agents-demo MARKETING_AGENTS_WEB_PORT=18080 make down
```

Project names must start with `marketing-agents-`; web ports must be 1024–65535.
The default project is `marketing-agents-local`. `make logs` reads the latest 100
lines per service; `make down` stops only the selected project and retains its
named volumes. It does not reset data. Do not use broad Docker pruning to repair
this installation.

For native development, activate Python 3.12, Node 24.20.0, pnpm 11.24.0, and
uv 0.10.7. Install PostgreSQL 14+ locally, with matching-major `initdb`, `postgres`,
`pg_ctl`, and `psql` on `PATH`, then run:

```sh
make bootstrap
make dev
```

Native mode serves [http://127.0.0.1:5173](http://127.0.0.1:5173), supervises the
owned PostgreSQL, API, and both workers, and stores its cluster/key beneath ignored
`data/native`. PostgreSQL listens only on a private temporary Unix socket; its
application role is not a superuser. Ctrl-C stops owned child processes and removes
only transient sockets, retaining the cluster and key. Missing PostgreSQL tools
fail explicitly, without installing tools or selecting another database.

Existing SQLite installations require `make dev LOCAL_DATABASE=sqlite` with the
correct `LOCAL_STATE`; otherwise choose a fresh state directory for PostgreSQL.
No automatic conversion or key rebinding occurs. The separate `make migrate`,
`make seed`, and `make seed-check` defaults use a PostgreSQL URL at `localhost:5432`
and `data/digest.key`, not the supervisor's private socket/key. Do not run them
against native state without explicitly setting both values. See the detailed
[native workflow](local-operations.md#native-workflow)
for path and port overrides.

Native startup, including version checks, uses a sanitized environment and
disables Corepack network acquisition and pnpm updates. Public tool-cache
locations such as `COREPACK_HOME` and `XDG_CACHE_HOME` are preserved; credential,
proxy, and loader variables are not. Missing installed/cached dependencies must
be acquired separately with explicit `make bootstrap`, never by enabling runtime
network access. This does not turn the native processes into an OS-level sandbox.

After bootstrap, `make verify-native-offline` checks the real native launcher in
explicit SQLite compatibility mode with fresh storage, an empty home, synthetic
credential variables, all five mock demos,
and restart/replay. It requires pinned Node/Corepack and a prewarmed pnpm cache;
it fails rather than skipping or installing absent prerequisites. It is a
separate native-only gate, not part of the backend verification image tests or a
replacement for committed-export Compose verification. That verification image
includes only the pinned standalone Node binary needed by browser-inventory
controls, not Corepack, Vite or the native development toolchain. The backend
application runtime image has no Node development toolchain. The separate
`make verify-del-10-native PYTHON=.venv/bin/python` exercises the PostgreSQL default,
all five mock demos, restricted application role, private socket, and restart
persistence. These are commands and coverage descriptions, not a fresh green
release claim. Detailed historical OBJ-04 instrumentation and coverage limits
are recorded in [OBJ-04](verification/requirements/OBJ-04.md).

## Readiness and safe operation

Initialization is ordered: owned PostgreSQL readiness, local key
creation/verification, migration and seed,
then API/workers/web. Liveness only means a process responds; readiness checks
the initialized database/catalog and mock composition. A successful web page load
or safe-mode banner is not evidence that a queued run completed.

The default `postgres-data` and `postgres-secrets` volumes form a pair.
The `postgres-socket` and `api-socket` volumes are disposable IPC, not backups.
Legacy SQLite volumes remain untouched; new PostgreSQL volumes do not convert
them. Native and Compose paths are separate installations. Changing a project
name creates new storage rather than
migrating an old installation.

## Current workflow scope

Claim: **Deterministic mock behavior** — the worker's
[executable registry](../apps/api/src/marketing_agents/infrastructure/executable_workflows.py)
supports five named demo workflows and explicit manual dry-run paths for all
43 catalog instances: 37 read-role/local-output paths and six inert write-proposal
previews. Explicit mock execution of the write-only roles is separate and requires
exact-action approval. A preview grants no dispatch authority, and a mock receipt is not
external delivery. The scoped [OBJ-03 record](verification/requirements/OBJ-03.md)
describes the implemented paths and their API/worker evidence. Supply valid
catalog-schema input and, for write-role previews or execution, the matching typed
operator command; arbitrary prose and generated actions are not commands.

Use **Runs & audit** to inspect run state and timeline. Use **Approvals** for each
immutable proposed action. Catalog visibility and successful intake do not promise
arbitrary workflow execution, completion or a real integration. This documentation
review is not a fresh health check of the running installation.

## Backup and recovery

The scheduler process also performs bounded, leased retention maintenance, using
the six [configured detail TTLs](data-handling.md#recorded-expiry-and-bounded-retention-maintenance).
It runs at most one 50-detail batch per UTC minute and never treats TTL as a
reason to erase active or recovery-needed work. Restart the same paired
installation to resume; do not delete maintenance claims manually. A stopped
scheduler does not purge, and a backlog may outlive the TTL. Logical erasure
does not remove data from backups or securely overwrite filesystem pages.

Expired historical runs retain their state and audit timeline with an expiry
notice. Detail endpoints can return `410 detail_expired`; authenticated
`/api/v1/retained-details/{record_kind}/{record_id}` reads expose safe retained
identities, never erased content. An expired retry key remains reserved and
must not be repurposed for new work.

[Local observability](observability.md) documents bounded JSON diagnostics and
process-local metrics. Metrics reset on process restart and are not audit facts;
there is no analytics exporter or public metrics endpoint. Never add raw request,
exception or provider objects to diagnostics to troubleshoot a failure.

Follow the existing [paired backup and restore procedure](local-operations.md#paired-backup-and-restore)
for complete commands and prerequisites. PostgreSQL backup uses a custom-format
logical dump with a consistent exported snapshot; SQLite's online backup API is
available only through the explicit legacy backend option. Both stage a
checksummed database/key pair privately and restore only into new scoped storage.
PostgreSQL restore temporarily starts its owned database for import/verification,
then stops it; it does not start API/workers/web. Matching PostgreSQL server/client
majors are required, and restore is not a database-engine or major-version upgrade.
Stop the native supervisor before `make backup-local LOCAL_MODE=native
LOCAL_STATE=/absolute/private/native DESTINATION=/absolute/private/new-backup`;
the wrapper refuses an active installation and temporarily supervises only its
owned PostgreSQL database during backup.
Backups are secret-bearing:
keep them out of Git, screenshots, ordinary logs, and CI artifacts.

Do not copy a live PostgreSQL cluster or `.db` alone, delete WAL files, regenerate
a lost key beside a
database, or overwrite the source installation. If the key or database is lost,
recover a known-good matching pair into new storage. The checks fail closed for
missing halves, mismatches, unsupported versions, unsafe permissions, or modified
bundles. There is no claimed production RPO/RTO or application-level database
encryption. See [data handling](data-handling.md).

## Troubleshooting without bypassing controls

| Symptom | Safe next step |
| --- | --- |
| Port already occupied | Select an unused loopback port with `MARKETING_AGENTS_WEB_PORT`; use the native supervisor's explicit port options for native mode. Do not kill unrelated listeners. |
| Native toolchain rejected | Activate the exact versions above, rerun bootstrap from frozen locks, and retry. Do not remove the version check or replace lockfiles to hide drift. |
| Native PostgreSQL prerequisite or state rejected | Install matching PostgreSQL14+ tools separately; preserve existing state. Select explicit SQLite compatibility for SQLite data or a fresh PostgreSQL state directory. Never remove ownership/version checks or replace a paired key. |
| Registry acquisition fails | Check local Docker/package access during the build/bootstrap phase. Runtime provider credentials and network opt-ins are not a remedy. |
| Data volume unwritable or key permissions invalid | Preserve the current pair and inspect the scoped paths/ownership using the detailed runbook. Do not recursively chmod unrelated directories. Recover a verified pair if necessary. |
| Migration/catalog readiness failure | Read bounded service diagnostics, compare the selected revision and catalog, and preserve the existing pair. Unknown revisions, downgrade, and fingerprint mismatch are not fixed by reseeding blindly. |
| Startup timeout or failed clean verification | Record the phase and safe diagnostic; check resources and current verification notes. A retry does not explain the first failure or make it disappear. Do not label an unexplained failure successful. |
| Accepted run does not complete | Inspect its authoritative state, workflow, errors, and worker diagnostics. A receipt is admission only; unsupported workflows and terminal failures must not be represented as completed. |
| Worker or scheduler was terminated | Restart the same installation. Durable leases have bounded expiry and fenced ownership. Do not delete claims or replay actions manually; inspect the recovered run/occurrence before taking further action. |
| Instance configuration revision conflict | Refresh the authoritative configuration, review the new revision, and reapply the intended deployment-level change. Reseeding preserves configuration; it is not a conflict bypass. |
| Approval expired, payload changed, or request already used | Refresh the authoritative request and review its disabled/conflict reason. Do not reuse a decision or edit stored hashes. A changed action needs a newly proposed action and valid approval; no automatic UI replacement workflow is promised. |
| Email has one approval but no progress | Expected mock boundary: both exact approvals must remain valid before any connector or welcome-draft model call. Inspect the second request and the run, not just the first decision receipt. |
| Demo submission says “Stop waiting” | This stops waiting for intake, not the server run. Retry without editing to recover the same idempotent receipt while that form state remains available; inspect Runs & audit before submitting new work. |
| Real-adapter or network-enabled settings rejected | Restore the committed mock/offline/local settings. The entrypoints intentionally reject live settings; there is no approved live deployment in this guide. |

## Verification and disclosure

[Testing](testing.md) lists the current commands and what each includes.
`make verify-clean REF=HEAD` checks an isolated export of a committed revision,
not uncommitted documentation or the existing developer database. The detailed
[verification runbook](local-operations.md#verification-and-troubleshooting)
explains its owned resources, acquisition/runtime network separation, and
cleanup. A full browser suite and the startup browser smoke are different checks.

**Residual risk:** the web proxy retains an OS-level outbound route on its scoped
ingress bridge; the committed nginx configuration uses only static assets and
the private API socket. Backend containers have no IP network. Docker and host
filesystem administrators, plus the self-approving local principal, are trusted.
Do not expose the UI publicly. [Security](security.md) and
[identity and authorization](identity-and-authorization.md) describe the boundary.

**Deferred real-adapter work:** hosted operation, real authentication, live
delivery, production key management, and production recovery qualification need
separate implementation and approval; mock or historical local success proves
none of them.
