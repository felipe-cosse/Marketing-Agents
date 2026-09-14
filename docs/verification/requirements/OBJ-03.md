# OBJ-03 operational workflow evidence

Requirement: safely configure, simulate, approve, and audit work.

Status: implemented and locally verified within the mock-only boundaries below.
The application-source gates passed preflight; the final fixture repair passed its
82-case targeted regression. The expanded final committed-feature gates and
restored-base connection witness remain required before merge; preflight is not a
commit-bound attestation or whole-system acceptance.

## Implemented boundaries

The [manifest](OBJ-03.json) covers a shared immutable executable-workflow registry,
role-specific local execution, explicit approved mock writes, saved schedule input,
the administrative configuration editor, and the supporting audit/migration changes.
The original five named demo definitions retain their own schemas and hashes;
ordinary agent-card work is not relabeled as a demo.

Every one of the 43 instances has a manual dry-run path through actual API admission
and a restarted worker. The paths have different, explicit semantics:

- The 37 read-role instances produce their own catalog-schema output: 31 use a
  deterministic mock model adapter and six use an authorized local transform.
  Role handlers use supplied business content or explicitly supplied structured
  evidence. They preserve advisory-only restrictions, limits and schema provenance;
  they do not derive business metrics from fixture hashes or execute generated actions.
- The six write-only instances, across four roles, produce typed proposal documents
  during a dry run. The reserved planner-output step is a local `NO_CALL` operation,
  while retaining the identity of the real catalog WRITE capability being described.
  It grants no model or transform capability, connector binding, write action,
  approval request, provider attempt, rate-window consumption or execution receipt.
  Its `proposed_actions` output is document content, never authority to dispatch.
- Explicit mock execution of those write-only roles uses a versioned typed operator
  command, the normal approval boundary and controlled connector dispatcher. A
  successful action has a durable mock connector receipt and one tool call, not a
  fabricated catalog result artifact. Rejection, cancellation, invalid commands,
  stale configuration/admission snapshots, workflow drift and exhausted attempts
  remain denial paths. Receipts explicitly disclose that no external side effect
  occurred. Completed replay and interrupted-receipt recovery are separately tested.

No real provider execution or external business-state mutation is part of this work.
The independent human approval decision belongs to the submitted exact action;
rendering a preview never creates or consumes an approval.

Persisted previews retain their admitted configuration snapshot. Later instance
disablement or configuration edits do not revoke an already-planned inert preview;
explicit run cancellation is the stop boundary. Mock writes separately recheck
current configuration before dispatch. That check does not revoke an external call
already in flight.

### Explicit operator commands

For the selected role, put the matching JSON document below into the manual input's
`source_content` string and provide its schema-required `request_id`. These examples
are command documents, not complete API request bodies. The role selects the command
type and capability; the document cannot select an arbitrary tool or grant authority.

Newsletter subscriber (`tpl.email.newsletter.newsletter-subscriber`):

```json
{"version":1,"command":{"contact_ref":"contact.local","list_ref":"list.local"}}
```

Unsubscribe assistant (`tpl.email.newsletter.unsubscribe-assistant`):

```json
{"version":1,"command":{"contact_ref":"contact.local","list_ref":"list.local"}}
```

Attendee scheduler (`tpl.community.events.attendee-scheduler`):

```json
{"version":1,"command":{"attendee_ref":"attendee.local","session_ref":"session.local"}}
```

Course cohort onboarder (`tpl.community.education.course-cohort-onboarder`):

```json
{"version":1,"command":{"recipient_refs":["participant.local"],"body":"Welcome to the local course."}}
```

The parser requires exactly `version` (integer `1`) and `command`; extra or duplicate
keys, free-form prose and generated `proposed_actions` lists are not commands.
References must be explicit, normalized and bounded; recipients must be unique.
Manual `dry_run` creates only a proposal preview. Explicit manual `mock_execute`
also requires the selected instance's enabled registered mock connector binding and
an independent approval of the exact resulting action before controlled dispatch.
It does not enable real-provider delivery or scheduled writes.

## Configuration and scheduler

The administrator editor obtains the restricted current configuration and its exact
revision instead of relying on a stale public detail projection. It renders saved
input using the selected catalog schema, preserves nested field identity, identifies
that the input will be stored locally, and requires explicit valid input before a
schedule can be enabled. Viewers remain read-only. Conflict and server-validation
responses preserve the draft until the operator explicitly reloads or edits it.

The API persists explicit input with an installation-keyed binding over the instance,
workflow ID and definition hash, input schema ID and hash, digest-key version, mode
and input payload. The configuration revision is not part of this HMAC. Separate
configuration-revision and claim fences protect atomic configuration, schedule
synchronization and redacted audit changes. Relevant changes invalidate outstanding
claims without removing prior occurrences, runs or receipts. Stale claim consumption
must be a no-op. The scheduler validates the keyed input binding before admitting
the occurrence, and the worker uses that exact admitted input after restart.

Existing schedules without an explicit input remain readable and retained. They do
not acquire synthetic business content or become executable merely because a worker
runs. Schedules support only `dry_run`; `mock_execute` is an explicit manual action,
not a scheduled mode. Supported trigger/mode combinations remain registry-constrained.

## Local output and migration invariants

Authorized transforms and planner proposal outputs use separate audit events,
`artifact.transformed` and `artifact.previewed`. The local output, successful step
transitions and event commit atomically. Provenance binds the admitted input digest,
catalog, template, instance, configuration revision, workflow and output schema.
Preview events additionally require the exact local planner provider identity.

Actual SQLite tests verify restart replay, concurrent completion and cancellation
ordering without inventing execution attempts or incrementing model/tool counters.
Renderer, schema, byte-budget, exact-deadline, late-fence and audit failures preserve
every prior stored row. Separate domain and database negative matrices reject
connector/approval metadata on planner previews and provider-attempt links on their
audit events.

Frozen migrations `0007` and `0008` retain legacy configuration/schedule state and
extend output/audit constraints without importing mutable ORM definitions. Populated
predecessor tests create real completed model and independently approved mock-write
history, then compare all rows before and after upgrade. They retain exact links,
versions, audit ordering, actions and receipts. Injected faults after actual table
copy/drop and a final foreign-key violation after revision advancement must restore
the entire earlier data/schema/index/revision snapshot. Foreign-key enforcement is
checked on the same reused migration-owner connection, and a subsequent valid
upgrade must still succeed. PostgreSQL offline DDL compilation is not a claim of
PostgreSQL runtime verification.

## Gates and connection witness

The manifest declares ten bounded gates: registry/renderers; read and preview
execution; approved mock writes and existing demos; configuration/scheduler behavior;
local outputs and populated migrations; the specific NO_CALL witness; web units and
static checks; the existing WEB-03 browser runner; generated contracts and architecture;
and contract/architecture negative controls. Installed pinned Python and Node tooling
is required. No gate installs dependencies or changes CI state.

Python gates explicitly set pytest's source path with
`-o pythonpath=apps/api/src`. This ensures that a restored archive imports its own
source instead of the original checkout's editable installation. The connection
witness restores only
`apps/api/src/marketing_agents/domain/runtime_policy.py` to the requirement base
`19e96d3da4ecdbd3d87a69b76086b5438b8a990c` and runs:

```text
tests/unit/application/test_obj_03_preview_planning.py::test_planner_output_family_is_never_a_model_or_tool_call
```

That test calls the production family-to-attempt classifier. A temporary current-source
archive passed the exact gate in 2.59 seconds. Restoring only that implementation file
from the base produced the expected ordinary `TOOL != NO_CALL` assertion failure
(exit 1, one failed test, 2.14 seconds). An import probe confirmed archive-local source;
there was no missing test, dependency or import error. The final feature verifier must
independently execute and record this witness against the committed tree.

## Verification status and limits

Application-source preflight on 2026-09-14 used Python 3.12.12 and Node 24.20.0 with
installed pinned dependencies. Nine gates ran through the unmodified verifier's
`execute_gate`, including its temporary HOME and sanitized environment. The exact
positive witness command ran in the source-isolated temporary archive described
above. Every command exited zero; the restored-base witness separately exited one.

| Manifest gate | Preflight duration (seconds) |
| --- | ---: |
| Registry and renderers | 13.548 |
| Read and preview runtime | 83.535 |
| Approved mock writes and existing demos | 168.253 |
| Configuration and schedules | 145.478 |
| Local outputs and migrations | 135.440 |
| Production NO_CALL witness, current-source archive | 2.590 |
| Web units and static checks | 36.090 |
| WEB-03 browser runner | 15.154 |
| Generated contracts and architecture | 6.349 |
| Contract and architecture negative controls | 11.235 |

The gate durations are individual results, not additive wall time: some checks ran
concurrently. This table predates the replay-fixture addition to the mock-write/demo
gate; its separate final repair regression is recorded below. Catalog release,
13 documentation guides, source provenance and the
preceding retained-branch history also passed. History before OBJ-03 remained
96 of 124 requirements, with 28 missing and one approved maintenance merge. No
history policy, security guard, verification script or workflow source was changed.

Development verification on 2026-09-14 completed a focused 114-test run in 143.27
seconds covering preview domain/audit constraints, actual SQLite negative matrices,
populated migration preservation and rollback, DEL-04 successor regressions, local
artifacts and ORCH-06 planning budgets. A separate 76-test run covered the new preview
unit cases plus existing runtime-policy and audit contracts. These counts overlap and
must not be added together. Scoped mypy passed eight source modules; Ruff, formatting
and whitespace checks passed for the corresponding owned changes.

Additional component checks completed on the combined development source:

| Component | Result | Covered boundary |
| --- | --- | --- |
| Catalog mock writes | 31 passed in 92.62 seconds | Exact approvals and receipts, including disabled-after-receipt recovery without another call. |
| Scheduler liveness | 11 passed in 66.50 seconds | Four configuration-edit interleavings, five fail-closed error controls, expiry/reclaim, and an obsolete claim with real inner HMAC corruption. |
| Configuration binding and catalog runtime | 18 passed in 83.28 seconds | Full binding and catalog files, including the 37-read-instance loop, signed webhook and scheduled-input restart. |
| Proposal-preview runtime | 5 passed in 35.25 seconds | All six write-only dry-run paths, restart replay, mode rejection, actual SQLite audit rollback and cancellation before rendering. |
| Preview planner | 10 unit controls passed across a single-test run and a nine-test run | Production NO_CALL classifier and planner controls; not reported as one ten-test invocation. |
| Frontend unit suite | 435 passed across 53 files | Editor, transport, schema-field and existing frontend regressions. |
| Backend configuration/API/OpenAPI checks | 47 passed | Restricted configuration and generated API contract checks. |
| Prior WEB-03 browser suite | 3 passed | Existing inspector, revision, conflict and layout journey. |

These counts overlap with earlier checks and each other and must not be summed into
a single suite total. Scheduler Ruff, formatting and mypy checks also passed. The
broad backend diagnostic was intentionally interrupted with exit code 130 after
827 passes and 32 optional PostgreSQL skips in 676.77 seconds: it had started before
the final source changes. It is an incomplete diagnostic, not a full-suite pass or
verification attestation.

A later sandboxed full-backend attempt stopped after 1,111 passes, 32 optional
PostgreSQL skips and two Unix-socket transport failures (892.97 seconds). The sandbox
denied local socket binding. The same three transport tests passed in 9.78 seconds
with local-socket permission, without a code or security-guard change.

The permission-corrected full backend run then completed: 2,919 passed, one failed,
32 optional PostgreSQL skips and 55 passing unittest subtests in 1,398.36 seconds.
Its sole failure was the verification-only DEL-05 replay fixture: it still created
an unbound schedule with an obsolete workflow ID. That is incompatible with the new
explicit-input contract. The fixture now issues a genuine signed dry-run input and
binds the matching registered workflow/configuration revision;
the scheduler's rejection of legacy unbound rows remains unchanged. The expanded
mock-write/demo gate includes this fixture regression. This aggregate is recorded
as a failed run, not relabeled as a green full suite.

After this fixture-only repair, all 82 targeted clean-state, fixture and real-HTTP
regression cases passed in 40.71 seconds with local-socket permission, including all
five fixture cases. Assertions retain exact keyed input, mode, schema, workflow,
configuration revision, occurrence/work replay, zero-write reseed and next-day
recurrence. The preparation-time bound tolerates crossing a minute during setup.
No application runtime, scheduler rejection rule, verification deadline, protected
history policy or network guard changed in this repair. The earlier 77-path feature
passed its ten committed gates and witness, but that attestation is superseded by
the fixture amendment; all expanded final gates must independently pass again.

### Real-API browser development QA

Separate real-API Chromium checks passed at desktop 1536 by 1024 and mobile 390 by
844. They exercised saving and reopening explicit scheduled input in the supported
`dry_run` mode, verified public-projection privacy and no browser-storage use, and found no console
warnings, blocking overlay or horizontal overflow. Root review included the desktop
and mobile screenshots. The original Chromium `Sec-Fetch-Site: same-origin` header
reached the API; no API or security-policy relaxation was applied.

This was a development check with a specifically bounded temporary bridge, not an
unqualified pass of the protected browser runner. Its existing `route.fetch` guard
strips Fetch Metadata headers, so the check used temporary native forwarding only
for the exact local port 4173 origin, CDP no-redirect handling, off-origin request
aborts and blocked service workers. Off-origin and HTTP 307 redirect tripwires made
zero requests to the port 5199 destination. No protected files were edited, and the
temporary servers were cleaned up afterward.

The declared WEB-03 runner still uses real hierarchy/detail reads but route-mocked
configuration responses. The separate real-API check verifies editor save/reopen and
the scheduled mode, not a composed browser-to-worker scheduled run, a manual dry-run
execution, or an approved write.

The matrix records the locally tested behavior above. These preflight results are
not a frozen-feature attestation. All manifest gates and the actual restored-base
connection witness must independently pass on the final committed feature before merge.
Live-provider delivery and optional PostgreSQL runtime checks are not claimed.
GitHub CI stays manually disabled, and this work does not publish branches, merge the
separate CI repair branch, or waive the local requirement-history protocol.
