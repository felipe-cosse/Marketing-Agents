# EXEC-06 — Demos, local tooling and documentation

## Scope and authority

Continues the published `96a631e` checkpoint on
`req/exec-06-demos-tooling-docs` from main `4bd6e8c`.
The checkpoint is preserved on `codex/exec-06-checkpoint-96a631e`; all earlier
checkpoint branches remain intact. User authorization permits replacing only
the EXEC-06 published feature tip using force-with-lease after complete
verification. It does not authorize rewriting or merging main. CI stays disabled.
Machine authority: [EXEC-06.json](EXEC-06.json).

This increment closes the M8–M9 demo/operator tooling gaps, configured retention
propagation and bounded local maintenance, local operational telemetry and the
implemented-flow threat review. It reuses existing runners and preserves network
canaries, Docker settings, coverage requirements, inventories and deadlines.
It does not claim final clean-state acceptance (EXEC-07), release attestation
(EXEC-08), real integrations or production identity.

## System changes

- [Demo CLI and recipes](../../demos.md) admit exactly one fixed synthetic preset
  with an explicit retry key. Local/mock/offline settings are checked first;
  arbitrary payloads, briefs and automatic approval are not CLI options.
- [Retention](../../data-handling.md) records independent TTLs for new detail,
  preserves historical expiry and protects active/recovery-needed material.
  Leased minute occurrences have immutable scope and a bounded total batch.
  Erasure, sealed non-executable identities, count-only audit and completion
  are atomic. Safe history survives while detail is explicitly expired.
- [Local observability](../../observability.md) records only allowlisted JSON
  diagnostics and bounded process-local metrics. Committed audit observations
  wait for outer commit; rollback/savepoints discard them. No exporter or
  telemetry-based execution authority is introduced.
- [Security review](../../security.md#exec-06-implemented-flow-threat-review)
  covers the actual new data flows, leakage paths and residual host/backups risk.
- Existing clean-export verification stops only its owned application processes
  before isolated suites, then resumes and rechecks readiness before browser
  smoke. Bounded failure diagnostics preserve exact deadlines and network checks.
  The unrelated historical CI repair branch and its policy edits are not merged.

## Complete qualification and evidence boundary

```sh
make -j1 verify-exec-06
```

This target directly invokes the existing source-bound AC-16 runner: all 20 gates,
full backend/frontend inventories, all 17 safety-critical coverage modules and
all 16 registered browser runners. It accepts no selector or replacement command.
The source must be committed and clean throughout; a matching external tree-keyed
successful record and independently observed successful execution are required
before merge. The short manifest audit proves local record consistency, not
authenticated execution. A synthetic record or passing control tests is not a
substitute. Reports stay outside Git so recording a result does not change the
qualified tree.

The Makefile connection witness keeps new tests while restoring only the target
implementation to main. Its ordinary live-hook assertion must fail, without an
import/dependency failure. Other gate controls verify failure forwarding, exact
inventory/deadlines and unchanged coverage policy.

## Verification status and review corrections

Focused development checks are not full qualification. The complete committed
candidate must be run after all implementation and review corrections are frozen.
Observed focused checks include real Email/scheduler restart across changed TTLs,
telemetry transaction/redaction/server-error canaries, retained API authorization
and identity-only projection, demo controls and the complete-run entrypoint controls.

Review and regression testing exposed historical audit reconstruction using new
settings, an unconfigured terminal-cleanup audit factory, migration backfill that
violated frozen-DDL rules, and expired child detail hiding the historical run
timeline. These are corrected in the implementation and regressions; failed
development runs are not qualification evidence. Existing frozen migration,
schema parity, network, coverage and deadline controls remain required.

Final review also found that an immutable historical retry could incorrectly
block retention after a later successful attempt. Eligibility now distinguishes
superseded attempts from outstanding recovery, with a real retry-to-success
regression. Failed-attempt audit events retain the safe error code required by
their unchanged semantic validator; an invalid purge rolls back atomically.
Desktop and mobile browser checks preserve the historical timeline
and explicitly show expired detail without inferring that no action ever ran.

## Limits

- Logical field erasure is not secure media deletion; WAL/pages, host snapshots
  and paired backups have separate lifetimes. Bounded backlog and recovery
  exclusions prevent a deletion SLA. Operational identities and replay digests
  can remain pseudonymous data.
- SQLite/local mock operation is the supported qualification target. Optional
  PostgreSQL skips are not passing runtime compatibility tests.
- Retired work cannot be executed or reconstructed; safe history can outlive
  detail. Historical exact-key retries do not issue a new run.
- Local logs/metrics are diagnostic, bounded and best effort, not durable audit
  substitutes or hosted monitoring. Schema redaction is not arbitrary-text DLP.
- New quality-run evidence does not establish a fresh kernel-isolated AC-15
  four-suite pass, clean-export acceptance or production delivery. Any such run
  must be separately observed and identified; historical reports are not reused
  as fresh results.
