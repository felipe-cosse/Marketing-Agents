# DEL-10 — PostgreSQL primary database

## Scope and authority

The user requested PostgreSQL as the primary database, initially asked to migrate
existing SQLite records, then explicitly authorized a fresh seeded PostgreSQL
installation when no existing SQLite data was present. Bounded inspection found
no Marketing Agents native database or Compose database volume. Unrelated Docker
volumes are outside scope. No data conversion or destructive reset is claimed.

This requirement starts at main `1cc328ca5494ecf543595430d1e2437c610846ac` on
`req/del-10-postgresql-primary`. Keep CI disabled and all checkpoint branches
unchanged. The original ARCH-03 and DEL-05 records retain their historical source
scope; DEL-10 explicitly supersedes their default database choice. The matrix
adds one user-requested requirement without altering protected evidence policy,
schema or executor. Machine authority: [DEL-10.json](DEL-10.json).

## Implementation and boundaries

- Settings, Compose and managed native startup default to PostgreSQL. asyncpg is
  required; the compatibility extra remains valid. Explicit SQLite URLs and the
  preserved `compose.sqlite.yaml` topology remain supported. Existing SQLite
  state blocks an implicit switch; no fallback silently hides old records.
- Compose uses pinned PostgreSQL 17 tools, a separate database UID and private
  peer-authenticated Unix socket. The application role cannot administer roles,
  databases, replication or bypass row security. Database and backend containers
  have no IP network; web retains the previously disclosed ingress/outbound
  boundary. No database port is published and no Docker daemon setting changes.
- Native development supervises an owned PostgreSQL process with no TCP
  listener. Private metadata, storage and socket checks prevent adoption of
  unrelated clusters. Cleanup targets only the owned process group and paths.
- Migrations, installation-key pairing and repeatable catalog seeding remain
  mandatory before startup. Database and key are a recovery pair. PostgreSQL
  backup uses a consistent exported snapshot and custom-format logical dump,
  bounded checksummed metadata and a private key copy; restore refuses existing
  destinations. Native restore requires the same major version. SQLite's online
  backup remains a separate explicit compatibility path.
- Runtime evidence reads complete schema/table state consistently without
  mutation and includes key identity. Restart/replay retains existing run,
  occurrence and receipt checks rather than replacing them with row counts.

Snapshot evidence fails closed on unsupported executable schema objects rather
than silently omitting views, triggers or user routines. PostgreSQL may rewrite
equivalent literal-array casts in CHECK expressions during restore. Their
read-only server-normalized projections are compared without ANALYZE; constraint
names/options and other definitions remain part of the digest. Changed literals,
operators or unsupported constraint options must still fail or change evidence.
Only owned local socket forms are accepted by the PostgreSQL snapshot reader.

Sequence state is not MVCC-transactional in PostgreSQL. Evidence includes exact
owned sequence definitions/state and rejects movement during sampling, but this
is not a universal atomic online sequence snapshot. The recovery round-trip uses
quiescent owned source storage; native backup requires stopped application state.
Live Compose dumps retain PostgreSQL's ordinary exported-snapshot semantics, not
a stronger production recovery guarantee. Unmanaged sequences are unsupported.

## Concurrent projection correction

A repeated real native PostgreSQL journey exposed an HTTP 503 while a valid run
was completing. Default READ COMMITTED allows later SELECTs to observe a newer
worker commit; existing multi-query corruption checks then correctly rejected
the mixed projection. SQLite's existing read transaction had provided a stable
snapshot. This was not a startup, network or polling deadline failure.

The bounded correction adds a dedicated read-only UoW for resource projections.
Only those PostgreSQL transactions use REPEATABLE READ and READ ONLY. Approval
creation/renewal and all normal write/claim transactions retain their existing
factory and isolation. Corruption comparisons remain intact. A deterministic
two-transaction barrier regression must show a coherent old projection while a
worker completes, continued rejection of genuine corrupt records, and unchanged
writable isolation. Adding retries or ignoring rejected reads is not a fix.

## Verification contract

Claim: **Implemented but not live-tested** for the final candidate until the
following frozen-source qualification completes. Earlier passing diagnostics
and failed attempts are not the final candidate's results.

1. Run focused settings, Compose, native ownership, snapshot, recovery and
   projection regressions plus formatting, lint, types and documentation checks.
2. Freeze one nonempty `[DEL-10]` primary commit with verification commands in
   its body. Preserve any failed candidate before replacing it. Never rewrite
   a checkpoint or main, and do not amend qualified source merely to add results.
3. Run `python -B -m scripts.ac_16_quality run` for the original complete 20-gate
   inventory, all safety coverage and all browser runners on the exact commit.
4. Run `python -B -m scripts.verify_requirement_evidence branch --id DEL-10
   --base main --run --witness`. All nine gates and the expected-failing connection
   witness are required. The witness restores only settings to the base; retained
   default-selection assertions must fail ordinarily, not through import errors.
5. Verify the native PostgreSQL and explicit SQLite fallback journeys, mandatory
   PostgreSQL tests, complete committed-export backend/frontend/browser journey,
   native paired recovery and both Compose recovery paths, source/tree identity,
   bounded cleanup, disabled CI
   and all checkpoint refs. Inspect retained desktop/mobile screenshots.
6. Report observed counts, limits and exact candidate, then ask for explicit
   publication and non-fast-forward merge approval. Verification is not approval.

The clean journey additionally executes PostgreSQL tests under network-none;
its original 1,620-second execution and 120-second cleanup budgets remain.
The acceptance aggregate retains the original quality/clean/SQLite recovery
gates and adds PostgreSQL recovery. The manifest runs complete constituent
checks, not a literal `make acceptance` invocation. No optional skip establishes
PostgreSQL qualification and no deadline, network check or coverage threshold
is reduced. Native tests use installed supported binaries; Compose uses pinned
PostgreSQL 17. Evidence remains outside Git and excludes all backup/key bundles.

## Limits

Claim: **Deterministic mock behavior** — all five workflows and the two-approval
Email barrier still operate on mock providers. The production browser journey
covers the Social draft, not every demo through UI controls. Claim: **Residual
risk** — local administrators remain trusted, database pages/backups are not
application-encrypted, and web retains its disclosed outbound route. Hosted
deployment, live-provider delivery, cross-major restore, production identity and
RPO/RTO remain unqualified. CI is disabled, not green; preserved checkpoints and
historical evidence do not become new PostgreSQL pass records.
