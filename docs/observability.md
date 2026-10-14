# Local operational telemetry

Claim: **Implemented but not live-tested** — operational telemetry is local to
each API/worker process. It is diagnostic information, not the durable audit
ledger, an analytics service, or evidence of production reliability. CI remains
disabled. No exporter, remote collector, public metrics endpoint, or background
telemetry transmission is installed.

## What operators can inspect

The API and workers write allowlisted JSON lines to their existing local process
streams. `make logs` reads bounded tails for the selected Compose project; native
supervision writes to its private per-run log directory described in
[local operations](local-operations.md#native-workflow). A line contains a fixed
event/outcome code, mock provider mode, and count. Run state and request duration
are included when applicable. Server-generated random correlation/run/step IDs
may appear in logs; they never become metric labels. Unsupported ID shapes are
omitted. Logs do not contain paths/query strings, HTTP headers or bodies,
approval reasons, model prompts, connector payloads, destinations, payload
digests, or raw exception messages/representations.
The production Uvicorn error handler uses a fixed `api_server_diagnostic` event
and allowlisted severity, without formatting messages, arguments, exception
tracebacks, cached exception text, or stack context. Access logging remains off.
Thus the server error logger cannot bypass the application logger's content
minimization; detailed exception messages are deliberately unavailable there.

The local composition object exposes `runtime.telemetry.metrics.snapshot()` for
in-process diagnostics and tests. This is not a CLI for an already running
process and does not query or modify the database. Each immutable sample has a
name, allowlisted labels, observation count, bounded sum, and maximum. There are
no individual observation arrays. Counts reset on process restart and are not
aggregated across API, run-worker, and scheduler-worker processes.

| Metric | Meaning and bounded dimensions |
| --- | --- |
| `requests`, `request_seconds` | HTTP response class and duration; labels are only `1xx` through `5xx`, never method/path or caller identity. |
| `run_transitions` | Committed received/transitioned state; labels are a domain state and workflow/template from the bounded trusted startup catalog, or `unknown`. |
| `approval_decisions`, `approval_queue_seconds` | Committed approved/rejected/expired decisions and elapsed time from the stored request timestamp to that decision. This is observed queue dwell at decision time, not an instantaneous gauge of every pending approval. |
| `connector_results` | Committed controlled tool-attempt/external-action results collapsed to succeeded, failed, unknown, or retry. A mock success is not real delivery. |
| `connector_retries` | Committed external-action retry release or subsequent controlled tool reservation. It does not imply a retry already reached a provider. |
| `idempotency` | Committed manual/webhook duplicate or conflict observations; no idempotency key or digest labels. |
| `scheduler_claims`, `scheduler_lag_seconds` | Successful persisted claim and nonnegative difference between observation time and its scheduled due time. Idle polls do not increment claims. |
| `scheduler_misfires` | Committed skip/run-once misfire decisions. |
| `audit_write_failures` | Failed append attempts, with a fixed failure code and no raw database error. The original exception/rollback semantics remain unchanged. |
| `redactions` | Actual schema/field/secret/pointer mask operations while a request or worker execution is in scope; never the removed value or field name. |
| `diagnostic_failures`, `dropped_series` | Broken local diagnostic sinks/projection and fixed-capacity overflow. These do not authorize changing product state. |

The metric store holds at most 1,024 series, including its preallocated overflow
counter. Workflow/template allowlists have at most 128 entries each; other label
values are fixed in code. Observation counts/sums saturate at 10^12 and observed
durations at 365 days. Metrics reject arbitrary labels, nonfinite values, negative
values and booleans. Updates and snapshots are protected by a local lock. Actor
IDs, emails, run/schedule IDs, hashes, arbitrary destinations and errors cannot
create series.

## Transaction and privacy boundaries

Audit-derived observations are projected to safe, finite fields and buffered
until the outer database transaction commits. The buffer holds at most 1,024
observations. Outer and savepoint rollback discard their affected observations;
a nested savepoint commit does not publish early. Operational logging failure
after a successful commit does not turn that success into a retryable business
failure. Audit writes themselves remain fail-closed and transactional.

Redaction counting uses a context-local scope around each API request or worker
advancement. It counts mask operations, not unique people or documents. A
repeated projection can increment it again. No removed value, exception, or
untrusted context is passed to the counter.

Claim: **Residual risk** — these are best-effort, process-local diagnostics.
Process termination can lose buffered/unflushed observations, different processes
have separate counters, and fixed-capacity overflow is intentionally lossy.
Local filesystem/process administrators remain trusted. Durable state, sequence,
approval and receipt facts must be read from the authenticated audit/resource
APIs. Do not use diagnostic counts as billing, approval authority, or a substitute
for the audit ledger. There is no production telemetry or availability claim.

## Verification

Claim: **Implemented and verified** (focused local unit/integration evidence) — finite label
sets/capacity, saturation, thread safety, exact-code logging, privacy canaries,
context-local masking, and broken-sink handling are exercised by
[`test_exec_06_telemetry.py`](../tests/unit/observability/test_exec_06_telemetry.py).

The real-runtime integration cases exercise Email admission/replay/approval/dispatch,
transaction and savepoint rollback, audit fault injection, request canaries,
and scheduler claim/lag/misfire:

```sh
.venv/bin/python -B -m pytest tests/unit/observability \
  tests/integration/security/test_exec_06_observability.py
```

These tests use temporary local installations and deterministic mock adapters.
They do not contact live providers or qualify production behavior.
Claim: **Acceptance target not yet verified** — the complete EXEC-06 verification
must pass on the final committed candidate; focused telemetry results do not
replace that gate.
