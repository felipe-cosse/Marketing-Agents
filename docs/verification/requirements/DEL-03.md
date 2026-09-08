# DEL-03 verification

The deterministic model, five exact demo renderer/schema pairs, and eight typed
connector families were implemented by earlier requirements. DEL-03 completes
their deliverable with a reusable, configuration-checked durable connector
factory and combined offline contract/demo gates.

`build_durable_connector_bundle` requires the caller's unit-of-work factory and
clock. Its immutable default profile is mock-only with both network and real
connector opt-ins disabled. It delegates mode and catalog checks to the existing
connector builder and supplies one database-backed receipt ledger to all write
families. Construction performs no I/O or schema creation. The Email demo uses
this shared factory when constructing its runtime; its dispatcher still owns
approval proofs, exact action authorization, binding checks, and recovery.

The low-level `build_connector_bundle` preserves process-local construction for
isolated tests. Such a bundle remains ineligible for dispatcher writes. Runtime
callers use the durable factory with the same persistent database after restart.

The new file-backed SQLite witness dispatches one approved mock write, disposes
the engine, and reconstructs the engine and bundle without schema creation. Both
adapter receipt replay and terminal dispatcher replay preserve the original
receipt and produce no second effect. Registry corruption, non-mock selections,
and network opt-ins fail before opening a transaction.

DEL-03 also corrects the existing RUN-03 audit-rollback recovery test clock. A
connector deadline does not end a worker's longer live lease. The test now first
proves recovery is a no-op at the deadline, then advances to the later lease/call
boundary and verifies receipt reconciliation with no additional connector call.
Production recovery semantics are unchanged.

Local aggregate verification commands:

```sh
make test-del-03-contracts
make test-del-03-demos
make verify-architecture
```

The contract gate covers model schema/bounds checks, all connector families,
exact authorization, fail-closed configuration, receipt uniqueness, concurrent
claims, lost responses, and recovery. Five demo acceptance paths run with sockets
denied; Email retains the all-approvals-before-any-call barrier. Trusted injected
renderers and gateway/UoW wrappers provide failure cases, not a production fault
setting.

The manifest cites the DEL-03 Make targets for reused ARCH-06/ARCH-07, RUN-05,
and demo tests. Those targets enumerate the original test files without
relabeling or copying their earlier requirement evidence.

The causal witness keeps the new factory and tests but restores the configured
builder's ledger-injection seam to the base revision. The runtime-composition
gate must fail when the configured builder can no longer receive durable storage.

No model SDK, real connector, migration, seed, worker process bootstrap, or
PostgreSQL execution is claimed. Durable mock receipts demonstrate local replay
safety, not real-world delivery or universal distributed exactly-once behavior.

Machine authority: [`DEL-03.json`](DEL-03.json).
