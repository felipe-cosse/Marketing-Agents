# 15 — Local operations, documentation, and release

Status: planned

Depends on: all product workstreams

Unblocks: clean-checkout acceptance and final handoff

## Objective

Make the complete local platform start with one documented command and no cloud/provider credentials, then prove it from isolated tracked inputs and fresh storage. Package operational and product documentation so readers can distinguish architecture, mock behavior, implemented controls, acceptance results, deferred real integrations, and residual risk.

## Canonical Compose topology

```text
web (published on 127.0.0.1 only)
  -> reverse proxy /api to internal api

api (internal)
run-worker (internal, same backend image)
scheduler-worker (internal, same backend image)
migrate-seed (one-shot owner, same backend image)
local-secret-init (one-shot owner of scoped digest-key volume)

shared named SQLite data volume
shared named local-secret volume, writable only by init and read-only to runtime services
internal application network with no external egress by default
```

Services:

### `local-secret-init`

- Mounts the SQLite data volume read-only for presence checks and atomically creates the per-install digest key only when no key and no existing database file are present.
- Runs as the backend service UID, writes only the scoped secret volume, and enforces `0700` directory/`0600` key permissions.
- Never overwrites a key. A missing key beside an existing database stops before migration; a later database fingerprint/version mismatch stops runtime readiness with recovery guidance.
- Completes before migration. The migration/seed owner records or verifies the non-secret key fingerprint/version, and API/run worker/scheduler mount the key read-only.

### `migrate-seed`

- Owns `alembic upgrade head` and catalog seed during startup.
- Runs to completion before API/workers.
- Uses a lock/one-shot Compose dependency to avoid migration races.
- Fails the stack on migration/catalog validation/count drift.

### `api`

- Internal port only.
- Liveness and readiness health checks.
- Mock/offline/local-identity settings by default.
- Does not run migrations implicitly on every process.

### `run-worker`

- Same immutable image and settings as API.
- Runs bounded database claim loop.
- Uses a distinct worker ID and graceful shutdown/cancellation checks.

### `scheduler-worker`

- Same image and database.
- Runs scheduler claim loop only.
- Creates work through application intake, not direct run execution.

### `web`

- Builds static SPA in a pinned Node builder stage.
- Serves through a small non-root web server/reverse proxy.
- Publishes only `127.0.0.1:<documented-port>`.
- No remote assets, analytics, or external network dependencies.

Validate whether the chosen Docker internal-network configuration still permits the intended host-published web port on all supported Docker versions. If not, use a narrowly scoped ingress network plus a no-egress internal service network and document the tradeoff.

## Docker files

```text
compose.yaml
docker/api.Dockerfile
docker/web.Dockerfile
docker/web.conf
scripts/entrypoint-api.sh
scripts/entrypoint-run-worker.sh
scripts/entrypoint-scheduler.sh
scripts/migrate-and-seed.sh
```

Container requirements:

- Pin base images by supported version and preferably digest at release.
- Multi-stage builds with frozen dependency installs.
- Non-root runtime users.
- Read-only root filesystem where practical; writable database/temp paths explicit.
- No secret copied into image layers.
- Health checks use local endpoints/process checks.
- Graceful stop interval exceeds bounded worker cleanup time.
- Image metadata includes source revision/version without PII.

## One-command startup

Canonical user command:

```text
make up
```

It should wrap:

```text
docker compose up --build
```

Expected behavior:

1. Build from frozen lockfiles.
2. Create the scoped network plus separate data and local-secret volumes.
3. Run the shared digest-key initializer once.
4. Migrate and validate/seed catalog once, recording/verifying the key fingerprint.
5. Start API/workers/web after initialization succeeds.
6. Wait for/readiness through documented output.
7. Present one local URL and safe-mode indicators.

No `.env`, cloud credential, model key, connector key, database server, Redis, or manual seed step is required.

## Native development path

Document as secondary:

1. Install pinned Python/Node/package-manager versions.
2. `make bootstrap` from frozen locks.
3. `make init-local-secret` initializes or verifies the ignored task-specific local key path; `make bootstrap` may call this target, and migration must depend on its verification.
4. `make migrate seed` or a dedicated combined target records/verifies the key fingerprint before accepting data.
5. `make dev` starts API, workers, scheduler, and Vite with supervised process cleanup.

Native mode uses a task-specific local data directory ignored by Git and binds services to loopback. It must preserve the same mock/offline settings.

## Shared local-secret initializer

Compose `local-secret-init`, `make init-local-secret`, native bootstrap/migrate, and tests invoke the same application CLI, for example `python -m marketing_agents.workers.local_secret_init`; they must not duplicate key-generation logic in shell or Compose YAML. The CLI accepts only explicit task-scoped data/key paths, uses atomic create/no-overwrite semantics, distinguishes an empty installation from a lost key, verifies ownership/mode/version/fingerprint, and returns stable diagnostic codes. Native restart tests must prove the same local key is reused and readiness fails if the paired database/key invariant is broken.

## Root task runner

Required targets:

- `help`
- `bootstrap`
- `init-local-secret`
- `dev`
- `up`
- `down`
- `logs`
- `migrate`
- `seed`
- `catalog-validate`
- `format`
- `format-check`
- `lint`
- `typecheck`
- `test` as the aggregate deterministic backend/frontend/contract/integration target
- `test-backend`
- `test-frontend`
- `test-contract`
- `test-e2e`
- `acceptance`
- `backup-local`
- `restore-local`
- `verify`
- `verify-clean`

Targets should use explicit Compose project/resource names and never remove unrelated containers, volumes, networks, databases, or files.

## Clean-state verification script

Planned path: `scripts/verify_clean_state.sh`.

Algorithm:

1. Verify Git exists and required tracked files/lockfiles are present at the selected commit.
2. Create a task-scoped temporary directory and materialize that commit with `git archive` (or an equivalently isolated detached source export) so untracked, ignored, or worktree-only files cannot influence the build.
3. Refuse unsafe broad target variables; create a unique scoped Compose project name rooted in that exported source.
4. Acquire pinned base images and frozen dependencies, then build from the exported tracked files. This acquisition phase may require package/image registry access unless caches are prewarmed; record what was fetched and do not confuse registry access with application/provider egress.
5. Create fresh named data and local-secret test volumes; do not reuse developer data or key material.
6. Start with no `.env` and no provider/cloud credentials.
7. Wait with a bounded deadline for readiness.
8. Verify safe session modes and exact catalog counts.
9. Run catalog seed again and prove zero-write/idempotent behavior.
10. Exercise all five demos.
11. Prove Email pre-approval zero calls and post-approval one call per action.
12. Restart API/run-worker/scheduler and prove the digest key/fingerprint remain stable with no duplicate webhook/action/occurrence.
13. Exercise webhook replay and schedule concurrency fixtures.
14. After acquisition, run backend/frontend/static verification in prebuilt containers with external network disabled; run browser verification on a scoped local-only bridge and reject non-loopback destinations.
15. Record sanitized command/result evidence.
16. Stop only the scoped Compose project and remove only its two test volumes, network, and temporary export.
17. Confirm the selected tracked source/generated state remains reproducible and report unrelated caller-worktree dirtiness separately.

Use traps for cleanup, bounded waits, clear failure diagnostics, and explicit resource names. Do not use broad destructive Docker cleanup commands.

The current directory has an initialized Git repository but no commits. Implementation must create an authorized baseline commit before a literal `git archive`/clean-checkout assertion can be verified. This planning task does not create that commit.

## CI design

Planned workflow: `.github/workflows/ci.yml`.

Jobs:

1. Catalog + backend static gates.
2. Backend unit/integration/contract tests with network blocked.
3. Frontend formatting/lint/type/unit/build.
4. Browser acceptance with loopback-only request policy.
5. Clean-state Compose smoke.
6. Optional PostgreSQL compatibility.

CI rules:

- Pin Python 3.12 and Node 24 LTS patch/digest at implementation time.
- Frozen `uv` and `pnpm` installs.
- Minimal default GitHub token permissions.
- SHA-pin third-party actions.
- Cache by lockfile hash only.
- No provider/cloud credentials in required jobs.
- Test images run with no external network where possible.
- Dependency and base-image acquisition is a distinct pinned/frozen phase that may use registries; required test and application-runtime phases have no provider/service egress.
- Upload only sanitized test/coverage/browser reports.
- Fail on catalog/generated API/Alembic drift or dirty tracked output.

## Operational behavior

### Startup/readiness

- Liveness: process responds.
- Readiness: database available, migration current, catalog exact/seeded, mocks registered.
- Workers log a safe readiness line and use health/process checks appropriate to long-running loops.

### Graceful shutdown

- API stops accepting new work.
- Workers stop claiming and complete/cancel only within bounded grace.
- Running external action result is recorded honestly.
- Leases expire for recovery if a worker terminates.

### Backup/recovery documentation

For the local demo, document `make backup-local` as a paired, protected backup operation:

1. Quiesce writers or use the SQLite online-backup API and checkpoint/WAL rules correctly.
2. Stage the database snapshot, digest key, key version/fingerprint, schema/catalog revisions, and a checksummed manifest in a task-scoped temporary directory.
3. Verify the database metadata matches the copied key before publishing the bundle with an atomic same-filesystem rename.
4. Apply directory mode `0700` and key file mode `0600`; label the bundle secret-bearing, exclude it from Git/ordinary reports, and never upload it as CI evidence.

`make restore-local BACKUP=<explicit-path>` validates checksums, permissions, schema/key versions, and fingerprint before restoring into explicitly new empty scoped data and secret volumes. It stages both halves and starts no service until the pair validates; it never overwrites an active installation. Missing database/key/manifest, mismatched fingerprints, or permissive key permissions fail closed with recovery guidance.

Test Compose and native restart, paired backup/restore into new storage, interrupted staging, missing-either-half, mismatch, and permissions cases. Do not describe this local procedure as a production backup strategy or claim measured RPO/RTO.

### Troubleshooting

Document:

- Port conflict.
- Unwritable data volume.
- Migration/catalog readiness failure.
- Stale/unsupported host toolchain in native mode.
- Worker/scheduler lease recovery.
- Config revision conflict.
- Approval expiry/hash/reuse conflict.
- Real adapter mistakenly selected while network disabled.

## Required product documentation

### `README.md`

- What the platform is and is not.
- One-command setup and local URL.
- Exact catalog counts.
- Architecture summary.
- Five demo walkthroughs.
- Verification commands.
- Mock/offline/local-identity warning.
- Links to detailed docs.

### `docs/architecture.md`

- Component diagram and boundaries.
- Catalog authority versus database projection.
- API/run-worker/scheduler flow.
- Explicit DAG and typed artifact flow.
- Approval/external-action sequence.
- SQLite/PostgreSQL boundary.

### `docs/assumptions.md`

- Source evidence precedence.
- Community duplicate semantics.
- Classification/capability/trigger assumptions.
- Email and Community demo choices.
- Lifecycle extension decision.
- Local identity/self-approval.
- Misfire/DST behavior.
- Real-adapter and retention assumptions.

### `docs/security.md`

- Threat model/trust boundaries.
- Approval/idempotency guarantees and limitations.
- Prompt injection/content policies.
- Redaction/data handling/retention.
- Secrets/network/default modes.
- Browser safety.
- Residual risks.

### Additional operator/developer docs

```text
docs/identity-and-authorization.md
docs/data-handling.md
docs/adapter-contracts.md
docs/catalog-authoring.md
docs/testing.md
docs/operations.md
docs/demos.md
docs/verification.md
docs/adr/*.md
```

## Documentation claim taxonomy

Every significant behavior is labeled as one of:

- Implemented and verified.
- Implemented but not live-tested.
- Deterministic mock behavior.
- Acceptance target not yet verified.
- Deferred real-adapter work.
- Assumption.
- Residual risk.

Never convert a static code trace or mock result into a production claim.

## Verification record

`docs/verification.md` should record:

- Source revision/commit.
- Date and environment/toolchain versions.
- Exact commands.
- Pass/fail/skip results.
- Catalog counts/hash.
- Demo results/call counts.
- Clean-state/restart behavior.
- Any unverified control or remaining assumption.

Generated evidence must not contain secrets, PII, raw payloads, or full prompts.

## Ordered implementation tasks

1. Finalize pinned toolchains, frozen lockfiles, safe environment defaults, and scoped root Make targets.
2. Build non-root multi-stage backend/web images and one-shot migration/seed initialization.
3. Wire API, run worker, scheduler worker, web proxy, SQLite volume, health checks, and loopback-only publication in Compose.
4. Prove `make up` starts from no `.env`/credentials and exposes exact safe session/readiness state.
5. Wire the shared digest-key initializer into Compose and native bootstrap/migrate, then prove native/Compose restart invariants.
6. Implement native bootstrap/dev commands with the same mock/offline behavior.
7. Implement paired protected local backup/restore and failure-path tests.
8. Implement bounded/scoped `verify_clean_state.sh`, including fresh storage, demos, replay/race/restart checks, and cleanup traps.
9. Add CI jobs with frozen installs, minimal permissions, pinned actions, network restrictions, and sanitized artifacts.
10. Write README, architecture, assumptions, security, identity, data, adapter, catalog, demo, testing, operations, and verification documents.
11. Run the full release checklist from a clean tracked baseline and record commands/results/limitations.
12. Review every documentation claim against traceability status before final handoff.

## Release checklist

- [ ] Git repository and clean tracked state exist.
- [ ] Source frames and prompt remain present.
- [ ] Lockfiles and pinned toolchains are committed.
- [ ] Fresh migrations and repeat seed pass.
- [ ] Exact catalog/API/UI counts pass.
- [ ] All five demos and provenance checks pass.
- [ ] Email approval/tamper/reuse/crash/call-count tests pass.
- [ ] Webhook replay and schedule race/restart tests pass.
- [ ] Backend/frontend formatting, lint, typing, tests, and builds pass.
- [ ] No-network and secret/PII canary tests pass.
- [ ] Accessibility and narrow-screen tests pass.
- [ ] Required docs and ADRs match implementation.
- [ ] `make up` works without `.env` or credentials.
- [ ] Native and Compose restarts reuse the same verified digest key; paired backup/restore succeeds and broken pairs fail closed.
- [ ] `make verify-clean` passes and cleans only scoped resources.
- [ ] Verification record distinguishes mock/target/deferred behavior.

## Exit criteria

- One documented command starts the complete local platform from fresh storage.
- Default Compose exposes only the loopback web entry point and requires no credentials.
- Migration/seed ownership prevents startup races.
- Native and Docker workflows use pinned/frozen dependencies.
- Native/Compose initialization and paired local backup/restore preserve the database/digest-key invariant.
- CI and clean-state scripts are safe, scoped, bounded, and green.
- README and required architecture/assumptions/security documents are complete.
- Verification evidence is exact, sanitized, and honest about mock/deferred behavior.
