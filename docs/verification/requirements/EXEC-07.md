# EXEC-07 — Complete clean-state verification

## Scope and authority

Continues from updated main `ae2ce2e` on
`req/exec-07-clean-state-verification`. CI remains disabled and every existing
checkpoint branch is preserved. The user requested full verification before a
merge request; this work does not authorize a merge into main.
Machine authority: [EXEC-07.json](EXEC-07.json).

## Controls and journey

`make verify-exec-07` serially invokes the existing complete clean-state command
with `REF=HEAD`, overriding an older caller-supplied ref. The command does not
change acquisition, suite inventories, coverage, network controls, Docker
settings, or deadlines. Focused controls exercise the actual recursive Make
path, failure forwarding and ordinary assertion failures for disconnected or
bypassed hooks. The connection witness restores only Makefile to main while
retaining the tests.

The runtime smoke now checks the exact five unique scenario IDs and their
reviewed write counts before admitting any demo. Previously, checking only five
items and trusting each item's write classification could report Email
preapproval success without actually running Email. The corrected evidence
requires observations of both blocked actions before approval, both still
blocked after only one approval, and one successful receipt per action after
both approvals. Negative tests exercise substituted, duplicate and malformed
inventory, altered write classifications and missing approval observations.
Replay retains the original verified preapproval observation and checks the
same run/action/receipt identities; it does not claim to observe preapproval
again after restart.

The unchanged clean-state journey runs all nine phases:

1. Export the selected committed tree and reconcile blob bytes and modes.
2. Acquire pinned images and build from frozen dependencies in that export.
3. Start through public `make up`, with no cloud/provider credentials, three
   fresh owned volumes, bounded readiness and exact safe modes/catalog counts.
4. Activate only the explicit synthetic replay fixture after public readiness.
5. Reseed and prove zero writes against a logical database snapshot.
6. Exercise all five deployed demos, including both Email approvals.
7. Exercise webhook/scheduler admissions and restart/replay without duplicate
   runs, actions, receipts or occurrences, preserving the paired key fingerprint.
8. Quiesce only owned runtime processes; run the complete prebuilt backend and
   frontend suites with Docker network-none, preserving failure diagnostics.
9. Resume and recheck the runtime, run production browser smoke, retain scoped
   desktop/mobile evidence, and verify source/caller state did not drift.

Cleanup verifies and removes only the owned containers, three test volumes,
network and temporary export. The existing total execution budget is 1,620
seconds; cleanup has 120 seconds. Paired backup verification separately retains
its 420-second execution and 150-second cleanup budgets. No timeout extension or
retry of a selected subset counts as a successful aggregate.

## Required final verification

Freeze one clean primary feature commit before qualification. Run the unchanged
complete AC-16 20-gate aggregate on that tree, with all 17 safety-coverage modules
and 16 registered browser runners. Then execute the EXEC-07 manifest and
connection witness. Its actual gates include the clean-state command, the
paired Compose backup/restore round-trip, focused controls and a same-tree
quality-record audit. Keep all reports outside Git so recording results does
not change the qualified source.

Focused tests and the implementation status in the matrix are not a full pass.
Each actual final run must succeed before requesting merge. A failed attempt
remains failed evidence; preserve its checkpoint before revising the candidate.
The manifest's output hashes and local quality record are consistency evidence,
not authenticated execution. Independently observed execution is also required.

## First candidate and isolated-environment corrections

Candidate `900b050` is preserved on `codex/exec-07-checkpoint-900b050`.
Its complete host quality run passed all 20 gates: 81 catalog tests, 4,912 backend
tests with 33 optional PostgreSQL skips, all 17 safety modules, 472 frontend
tests and all 16 browser runners. The backend gate exited in 1,768.326 seconds,
inside its unchanged 1,800-second deadline. This evidence belongs only to that
candidate's tree, not a subsequently corrected candidate.

The actual clean-state attempt passed deployed startup, reseed, five demos and
restart/replay, but its network-none backend suite reported four failures,
4,990 passes, 33 skips and 401 successful subtests in 770.17 seconds. Automatic
cleanup removed the owned project resources and temporary export. The frontend,
production browser and paired-backup gates were not reached; this is not a
successful clean-state qualification.

Two failures came from synthetic AC-16 fixture artifact timestamps falling
outside the sampled report interval. The correction assigns explicit timestamps
only to owned synthetic files and adds exact interval-boundary tests. Production
freshness validation and real report timestamps are unchanged. Two other
failures were `FileNotFoundError: node` from existing Python tests that execute
the actual browser-inventory module. The verification-only image now copies the
already-pinned standalone Node binary used by the frontend build and checks its
version/module import at build time. It adds no frontend package installation,
does not change the application runtime stage, and leaves both inventory tests
intact. No test is skipped, no deadline is extended, and no network control is
relaxed. All complete qualification must run again against the corrected commit.

## Limits

- Image/dependency acquisition may reach registries. Backend and prebuilt test
  containers have no external IP network, but static web ingress retains a disclosed
  operating-system outbound route. Browser requests are scoped to the exact
  local origin. This is not an all-container firewall claim.
- SQLite, local identity and deterministic mock adapters are the qualification
  target. Optional PostgreSQL skips are not PostgreSQL qualification; no real
  delivery, production authentication, cloud deployment or production RPO/RTO
  is established. Backup fixtures contain synthetic data and owned test keys.
- This does not establish a fresh AC-15 four-suite kernel-isolation run, a
  literal invocation of `make acceptance`, DEL-01 completion or EXEC-08 final
  attestation. CI is disabled, not green. Checkpoints remain intact.
