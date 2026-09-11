# DEL-07 local verification evidence

## Scope and mechanism

The [manifest](DEL-07.json) binds required domain, application, catalog, API,
adapter, persistence, frontend and browser tests to an ordered local verifier.
GitHub CI remains manually disabled by user direction, with its source preserved.
The separate unmerged CI repair branch is not included in this work.

The [testing guide](../../testing.md) documents the commands, measured safety
policy, automatic network fixtures, generated API artifacts and honest limits.
The [verification record](../../verification.md) distinguishes component results
from complete, exact-source acceptance results.

The runner begins with catalog gates, uses fresh coverage storage outside the
source tree, and stops on the first failed or timed-out gate. Its before/after
source inventory includes caller edits, executable modes and symlinks; acceptance
additionally compares actual bytes against the selected commit independently of
Git index hints. Nested commands inherit offline/frozen dependency settings.

The safety checker requires exact counters for all 17 selected modules. Tests
exercise valid state transitions and hashes alongside stale or corrupt persisted
snapshots and broken-port results. Defensive exceptions are retained. The only
source-pinned exclusion is the pre-existing two-line exhaustive-enum fallback;
it is explicitly not counted as exercised. One unreachable connector exception
tail is simplified without removing a failure path.

Host Vitest denies real transports globally; explicit in-memory mocks remain
usable. Every owned browser spec uses an automatic context guard. The browser
canary uses owned local tripwire listeners, never an external destination. It
checks actual browser denial separately from the complete journey aggregate.
The isolated API generator checks both artifacts in memory without overwriting,
and the frontend session response type consumes the generated contract.

## Connection witness

The dependency-free Node gate retains all four current canary tests and restores
only `scripts/node-network-guard.mjs` to base
`7f9ea62d5489bde73bae4ddcc87eee4dc34b164c`. Tests install stub transports before
the guard, so a broken implementation cannot create real traffic. Development
archive checks and the committed candidate preflight passed all four controls
with current code and failed all four assertions with the restored implementation,
with no skips or import failures. The final feature witness must independently
reproduce this result.

## Verification status

The frozen-worktree full local aggregate passed all 20 gates on 2026-09-11:
2,679 backend passes, 32 optional PostgreSQL skips, 427 frontend passes and 32
browser passes across 15 runners. Fresh coverage passed all 17 safety modules
with 2,741 statements and 1,056 branches covered and the disclosed two-line
exclusion. All 1,143 source files were unchanged by the 1,972.837-second run.
The exact command and sanitized report hash are in the reader-facing record.

The additional native composed UI smoke passed desktop/mobile health checks and
both overview endpoints returned 200. This is not a live-delivery or full
browser-to-worker mutation claim: the existing demo browser journeys use route
mocks, while actual mutation/restart behavior is separately process-tested.
The current-source paired Compose backup gate preserved all 47 tables, all 43
instance configurations and key identity, refused overwrites, and removed its
owned resources and private bundles. Deadlines were not changed.

The exact committed candidate `12e935ae913fec29bc482a9f449de7759ccfac6d`
passed all eight clean-state phases with unchanged 1,620/120-second deadlines,
preserved caller state and successful owned-resource cleanup. All five deployed
mock demos, the two-approval Email barrier, zero-write reseed, replay/restart,
offline backend/frontend suites and production browser smoke passed. The ingress
web container retained its disclosed outbound route. The verification record
pins the report hash and distinguishes the diagnostic's subtest events and
optional-driver skip from unique host test counts.

The matrix records the locally verified DEL-07 behavior. The final amended
feature still must pass every manifest gate and the connection witness before
merge; the candidate and preflight are not substitutes for that attestation.
Historical successes, development coverage combinations and a passing browser
run before network-policy review are not substitutes either. Generated reports
stay outside Git and exclude raw payloads and credentials. No deadline, cleanup
check, coverage threshold or history rule is waived by disabling CI.
