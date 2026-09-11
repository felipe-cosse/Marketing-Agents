# Local operations

This is the operator entry point for the credential-free, single-user local
installation. **Implemented and verified (historical local evidence):** startup,
native supervision, paired backup/restore, and restart behavior are recorded in
[DEL-05](verification/requirements/DEL-05.md) and exercised by the
[runtime tests](../tests/integration/runtime/test_del_05_process_composition.py),
[native tooling tests](../tests/tooling/test_del_05_native.py), and
[backup tests](../tests/integration/db/test_del_05_backup.py).
Those records are not a fresh health check of an existing installation.

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
cloud credentials, a database server, or a manual seed step. Initial acquisition
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
uv 0.10.7, then run:

```sh
make bootstrap
make dev
```

Native mode serves [http://127.0.0.1:5173](http://127.0.0.1:5173), supervises the
API and both workers, and stores its database/key beneath ignored `data/native`.
Ctrl-C stops owned child processes. The separate `make migrate`, `make seed`, and
`make seed-check` defaults target a different installation (`data/marketing_agents.db`
and `data/digest.key`); do not run them against native state without explicitly
setting both paths. See the detailed [native workflow](local-operations.md#native-workflow)
for path and port overrides.

## Readiness and safe operation

Initialization is ordered: local key creation/verification, migration and seed,
then API/workers/web. Liveness only means a process responds; readiness checks
the initialized database/catalog and mock composition. A successful web page load
or safe-mode banner is not evidence that a queued run completed.

Use **Runs & audit** to inspect run state and timeline. Use **Approvals** for each
immutable proposed action. The current worker executes only registered demo
workflows; catalog visibility and successful intake do not promise arbitrary role
execution. Never interpret a mock receipt as real delivery.

The default persistent data and local-secret volumes form a pair. The API socket
volume is disposable IPC, not a backup of either. Native and Compose paths are
separate installations. Changing a project name creates new storage rather than
migrating an old installation.

## Backup and recovery

Follow the existing [paired backup and restore procedure](local-operations.md#paired-backup-and-restore)
for complete commands and prerequisites. It uses SQLite's online snapshot API,
stages a checksummed database/key pair privately, and restores only into new,
empty scoped storage. Restore does not start services. Backups are secret-bearing:
keep them out of Git, screenshots, ordinary logs, and CI artifacts.

Do not copy a live `.db` alone, delete WAL files, regenerate a lost key beside a
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
