# CI-MAINT-02: isolated offline verification and timing diagnostics

User approval: 2026-09-10, explicitly authorizing this scoped CI repair and
history exception. The preceding CI-MAINT-01 exception and every original
requirement commit, merge, and retained branch remain unchanged.

The approved base is `d28b40a317207cd24446657b01ed83fb41b2eeeb`.
The feature subject is `ci: isolate offline verification and record timings`.
The merge subject is `merge: CI-MAINT-02 isolate offline verification and record timings`.

## Evidence and limits

The [main run](https://github.com/felipe-cosse/Marketing-Agents/actions/runs/34397649419)
passed catalog, governance, backend, frontend, and browser checks, but its
clean-startup verifier reached the 1,620-second aggregate execution deadline.
All deployed demo/reseed/restart phases passed, no pytest failure had been
reported, and owned-resource cleanup succeeded. Offline backend was interrupted
while the native HTTP demo/restart test was active. An active test identifier
does not establish that the test caused the whole overrun.

The identical source tree had passed the
[candidate clean job](https://github.com/felipe-cosse/Marketing-Agents/actions/runs/34391900567/job/102613191557)
in approximately 1,205 seconds. Main was about 92 seconds slower even before the
offline suites; its interrupted backend consumed about 1,365 seconds versus
962 seconds for the successful candidate. The focused native HTTP test passed
on the unmodified main tree in 23.16 seconds (Python 3.12.12, macOS ARM64).
These observations establish intermittent, multi-phase slowdown, not its root
cause. The candidate's previous startup failure also remains a distinct
observation; this change does not claim to repair that unproven failure cause.

## Scoped changes

The complete offline suites do not consume the deployed API or web service.
Previously those services kept running, and their readiness probes repeatedly
compiled the catalog and checked the database while offline tests ran. The
verifier now stops all four long-running services after the final persisted
snapshot and verifies none remain running. The two unchanged standalone suites
still run in separate containers with `--network none`.

Before browser verification, the verifier starts all four existing services,
waits for their production health checks, rechecks their network boundaries,
and verifies the same safe session and catalog counts as at initial startup.
No application readiness check, Compose network setting, demo assertion, test
command, test selection, or deadline is relaxed. Owned cleanup remains required
on every success, failure, timeout, and interruption path.

Timing diagnostics retain only bounded numeric durations/progress and static
source-allowlisted test names. Raw payloads, parameter identifiers, credentials,
exception messages, and tracebacks remain excluded. A report's last progress is
an observation, not a claim that an interrupted test completed.
Stage durations measure the wrapped commands; test timing starts at pytest
configuration and includes setup, call, and teardown. `completed` counts finished
test items separately from outcome reports, which can include unittest subtests.
An active test's session-relative start is not a live elapsed-time measurement.
Only the latest and eight slowest distinct source tests are retained; when
`truncated` is true, these summarize the retained stream tail, not the full run.

## Verification

Targeted tests cover stop/offline/resume/browser ordering, failure before
browser when shutdown or resumed readiness is invalid, timing/privacy negative
controls, and two distinct maintenance approvals without changing requirement
counts or weakening earlier history checks. Full committed clean-startup and
authoritative GitHub results must be reported separately after execution;
the harness change alone does not prove the intermittent timeout is resolved.

Local pre-publication checks on 2026-09-10 passed: the combined clean-coordinator,
diagnostic, maintenance-history, and existing evidence-verifier suite ran 170
tests plus 24 subtests; the final diagnostic-only rerun passed 41 tests.
Ruff formatting/lint and whitespace checks passed. Independent review found no
actionable issues and separately checked real pytest item/subtest counts. These
are targeted results, not a claim that full Docker or GitHub CI is already green.

This maintenance work does not complete any of the 30 remaining requirements.
