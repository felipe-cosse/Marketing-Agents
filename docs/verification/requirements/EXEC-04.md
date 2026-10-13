# EXEC-04 — State machine, approval and mock prerequisites

## Scope

Prepared on 2026-10-13 from main `be19a910879ad68c8c4b6da9374f81df6d53a675`
on `req/exec-04-foundation-qualification`.
Machine authority: [EXEC-04.json](EXEC-04.json).

The core state machine, persistence, approval boundary and deterministic adapters
already exist under their subject requirements. This change makes M3–M5 a live,
serial prerequisite command before proceeding to EXEC-05 and adds a
joined restart/artifact/audit journey. It establishes current qualification order;
it does not claim that previously merged API/UI work was historically developed
after this gate.

## Executable boundary

`make verify-exec-04` invokes [the fixed runner](../../../scripts/verify_exec_04.py):

1. M3: all pure-domain tests, persisted lifecycle tests, fresh migrations,
   repeatable catalog seeding, local configuration preservation, readiness,
   trigger projection, PostgreSQL fixture controls and native database commands.
2. M4: durable mock composition and LLM/connector contracts, controlled read
   execution and recovery, audited step state, real local process composition,
   and the new joined read-only restart journey.
3. M5: complete AC-08 approval-barrier, AC-09 tamper/reuse/expiry and AC-11
   durable mock crash-recovery test files.

Each milestone has a 240-second execution limit. The requirement-evidence command
has a 900-second outer limit. A failed milestone prevents later milestones, and a
failed requirement gate prevents qualification for the next requirement. This is
a dedicated command, not a nested execution inside the full local verifier.
No top-level gate, coverage policy, full-suite inventory, existing deadline,
network check or Docker setting is removed or relaxed. CI stays disabled.

The runner accepts no command or test selectors, removes inherited Make/pytest
filter options through the existing environment builder, rejects unsafe provider
and test-loader settings, uses socket-denial flags and offline/frozen dependency
settings, and checks source contents before and after each successful milestone.
Any failed, interrupted, unavailable or timed-out command stops later milestones.
Caller edits are preserved, not reset. Process-group termination reuses the
existing local verifier's bounded behavior.

[Control regressions](../../../tests/tooling/test_exec_04_foundation_gate.py)
pin the inventory and actual Makefile command. The connection witness restores
the control files to the base and must fail. The
[new journey](../../../tests/acceptance/test_exec_04_foundation_restart.py) joins
actual API admission, runtime reconstruction, real worker completion, validated
artifact provenance and paginated public timeline evidence for the same run.

## Verification record

Observed pre-commit verification on 2026-10-13:

- New restart journey with explicit socket-denial flags: 1 passed in 9.88 seconds.
- `python -B -m pytest -q tests/tooling/test_exec_04_foundation_gate.py`:
  64 passed in 12.28 seconds; Ruff lint and formatting passed.
- Existing DEL-07 local-verifier, AC-16 quality and AC-17 claim-taxonomy modules:
  161 passed and 214 subtests passed in 53.13 seconds.
- `make -j1 verify-repository` with pinned project tools passed documentation,
  architecture, source evidence, repository text, formatting, secret scanning and
  retained-branch history checks (118 completed of 124 on the baseline main).

The first `make -j1 verify-exec-04` attempt used the checkout's Intel Python
3.12.12 under translation. M3 reached the unchanged 240-second deadline after
240.035 seconds, returned `-15`, and stopped the aggregate; M4/M5 did not run.
That attempt is not qualifying evidence. No source drift was reported.

A separate ignored environment at `.cache/exec04-native-2026-10-13` uses the
already-installed native ARM Python 3.12.12 and the same frozen lock. Some native
wheels were not cached, so a separate `uv sync --frozen` acquisition prepared the
environment before tests. It installed 56 locked packages and changed no lockfile,
test inventory, network control or deadline. The original `.venv` is preserved.

The complete native retry used:

```sh
UV_PROJECT_ENVIRONMENT=.cache/exec04-native-2026-10-13 make -j1 verify-exec-04
```

All three milestones passed in order, with no source drift:

| Milestone | Pytest result | Child duration | Bounded gate duration |
|---|---|---|---|
| M3 domain and persistence | 495 passed | 202.83 seconds | 204.046 seconds |
| M4 runtime and mocks | 122 passed | 48.46 seconds | 49.543 seconds |
| M5 approval safety | 8 passed | 31.21 seconds | 31.974 seconds |

These are 625 live prerequisite tests, not the full repository suite. Every
milestone retained its 240-second limit; the runner returned `valid: true` and
`error: null`. Independent scope review passed the existing evidence parser and
policy, with no protected-file edits. The committed-feature gate and restored-base
connection witness must also pass before merge; their tree-bound attestation is
written by the existing requirement verifier outside the source tree.

## Limitations

- Qualification targets local SQLite. PostgreSQL fixture controls are not live
  PostgreSQL installation evidence.
- Restart means rebuilding the runtime against the same database and digest key.
  AC-11 uses controlled in-process cancellation after a durable receipt; neither
  result establishes every OS-kill or hardware-failure interleaving.
- Provider and connector effects are deterministic mocks, not real delivery.
- These focused milestones do not replace AC-15's four kernel-isolated suites,
  AC-16's complete source-bound quality aggregate, or EXEC-07 clean-state checks.
- No historical implementation-order claim is made, and CI remains paused.
