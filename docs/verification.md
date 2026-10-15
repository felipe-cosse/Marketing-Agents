# Verification record and claim taxonomy

## How to read a claim

Every significant product claim uses one of these categories. A source link
explains a mechanism; only a matching executed gate establishes tested behavior.

| Category | Meaning |
| --- | --- |
| Implemented and verified | A named test or recorded gate exercised the stated behavior under its stated environment and scope. Not automatically live-provider or production proof. |
| Implemented but not live-tested | Source implements a path, but the relevant deployed/live environment has not been exercised in this record. |
| Deterministic mock behavior | A local fixture or mock result; no real model call, publishing, email, subscription, or CRM delivery is implied. |
| Acceptance target not yet verified | Required work or final acceptance remains outstanding. Existing code or a green unrelated test is insufficient. |
| Deferred real-adapter work | Real-provider integration, credentials, delivery reconciliation, and provider-specific qualification are outside the shipped mock runtime. |
| Assumption | An explicit choice in the [register](assumptions.md), with a revisit trigger rather than an observed external fact. |
| Residual risk | A known trust boundary, limitation, or incomplete guarantee that remains after the scoped checks. |

## EXEC-08 current verification handoff

### Source and qualification boundary

Record date: **2026-10-15 UTC** (2026-10-14 on the local Pacific calendar).
Inherited EXEC-07 baseline: main
`a8e197da4ca9f4a7abbc842af737b1c8fbecb2ef`, feature
`63df4bb4f39f22d31e9c3638b5e7e6e4049a4650`, identical tree
`b8eccaaf6cd91a1cb7ac64bbb850187e13e8e93f`.
The observed results below are **Implemented and verified** only for that
baseline and its stated environments, not fresh EXEC-08 verification. EXEC-08
changes reporting contracts and documentation, plus the explicitly approved safe
YAML loader optimization described below. No workflow or delivery semantics,
validation requirements, verification deadlines or Docker settings are changed.

The baseline full quality run used macOS, native Python **3.12.12**, uv
**0.10.7**, Ruff **0.16.3**, mypy **1.20.2**, Node **24.20.0**, pnpm
**11.24.0** and Corepack **0.35.0**. The clean-state journey used Docker Desktop
**4.95.0**, Engine **29.8.2**, Compose **5.6.0**, native arm64 images and
the repository-pinned dependency locks. The host's separate Intel `.venv` was
preserved; qualification used the native cached environment. Platform-specific
or live-provider behavior is not inferred from these local results.

### Files changed

- `Makefile`: expose the documentation-only handoff check.
- `docs/verification.md`: separate this handoff from dated historical records.
- `docs/assumptions.md`: describe shipped bounded logical retention accurately.
- `scripts/verify_exec_08_handoff.py`: bounded, read-only curated claim checks.
- `tests/tooling/test_exec_08_handoff.py`: lightweight independent negative controls.
- `apps/api/src/marketing_agents/infrastructure/catalog/loader.py`: prefer the C-backed safe YAML parser with the pure-Python safe fallback and unchanged loading boundaries.
- `tests/catalog/test_exec_08_yaml_loader.py`: parser compatibility, fallback and security regressions.
- `docs/implementation-plan/16-requirements-traceability-matrix.md`: record EXEC-08 implementation without declaring unrelated work verified.
- `docs/verification/requirements/EXEC-08.json`: source scope, six gates and restored-base connection witness.
- `docs/verification/requirements/EXEC-08.md`: qualification procedure and reporting limits.

### Commands and observed baseline results

All rows in this table are inherited observations, not candidate results.
Failures, errors, skips and subtests are reported separately;
subtests are not additional test cases.

| Command / observation | Baseline result and boundary |
| --- | --- |
| `python -B -m scripts.ac_16_quality run` | All 20 gates passed on the EXEC-07 feature, 2026-10-14 22:31:42–23:09:24 UTC, 2,261.191 seconds total. Backend: 81 catalog tests passed and 4,932 other backend tests passed, 5,013 unique passes total; 33 optional PostgreSQL tests skipped, zero failures/errors. The backend gate used 1,795.860 of its unchanged 1,800 seconds; this is narrow deadline headroom, not permission to extend it. |
| Safety, frontend and browser components of that aggregate | 17 safety modules reached the required complete statement/branch coverage, with two approved excluded lines; 472 frontend tests in 55 files passed. All 16 browser runners completed unfiltered: 39 product browser cases plus six OBJ-06 browser network-canary controls, with zero skips, including OBJ-06's real-runtime desktop/mobile path. Node controls are counted separately from these Playwright browser cases. The existing large Vite chunk warning was non-failing. |
| `python -B -m scripts.ac_16_quality audit` | The persisted record matched the source and lock hashes. The approved two-parent merge retained the feature's identical tree; the audit supports this specific feature/merge relationship, not arbitrary same-tree revisions or authenticated execution. All 1,366 captured source files had zero drift. |
| `make verify-clean REF=HEAD`, via `make -j1 verify-exec-07` | The postmerge clean-state gate passed in 918.924 seconds. Its captured backend log reports 5,013 backend tests passed and 416 subtests passed, with 33 optional PostgreSQL tests skipped and zero failures/errors. Frontend: 472 passes in 55 files. Diagnostic pass-event totals include subtests and must not be substituted for unique test counts. |
| `make test-del-05-compose-backup`, invoked with `-j1` by the manifest | Paired local backup/restore gate passed in 41.749643 seconds. Successful clean/backup JSON stdout was not retained separately; output hashes alone do not establish new detailed demo or backup counters. No production recovery objective or secure deletion claim follows. |
| `python -B -m scripts.verify_requirement_evidence branch --id EXEC-07 --base main --run --witness` before merge; `python -B -m scripts.verify_requirement_evidence history --ref main --allow-incomplete --check-branches --run-latest --witness` after merge | All five EXEC-07 gates returned zero and the restored-base connection witness returned one. The latest postmerge attestation was recorded at 2026-10-14 23:56:36 UTC. Owned verification containers, volumes and networks were removed; dependency caches and images were retained. |

The quality record is stored outside Git under
`/tmp/marketing-agents-ac16/b8eccaaf6cd91a1cb7ac64bbb850187e13e8e93f.json`;
the baseline requirement attestation is
`/tmp/marketing-agents-requirement-evidence/EXEC-07-b8eccaaf6cd91a1c.json`.
These local paths may expire and are not portable release artifacts. Historical
failed attempts remain failed: checkpoint `900b050` did not qualify, and a later
metadata-only amendment from `495ab23` caused a source-identity audit failure.
The final `63df4bb` run above was a complete rerun, not a waiver of either
failure. Both checkpoint branches remain intact.

### Candidate verification and handoff

Candidate verification is pending **at source-authoring time**. This immutable
document cannot contain its own final commit identity or a result produced
after that commit. The final handoff must report the observed outcome and exact
revision from the external source-bound attestation; until then, this document
does not establish a candidate pass. Documentation checks do not authenticate
execution or prove arbitrary prose true.

The first EXEC-08 candidate, `7ce6a8d`, passed all 20 quality gates but
misclassified six browser canary controls as Node controls in this guide.
Independent review caught the reporting error. Its subsequent evidence run was
deliberately interrupted during image acquisition, before the clean-state
journey completed; it is not a six-gate pass. The candidate is preserved on
`codex/exec-08-checkpoint-7ce6a8d`. Correcting the wording changes the qualified
source, so the corrected candidate requires new complete qualification below.

The next candidate, `7199ace`, corrected that classification but its complete
quality rerun timed out in the backend gate at 1,800.020 seconds, after 11 gates
passed. It reached about 89% of collected backend items with no assertion
failure reported before timeout; this is not a backend or aggregate pass. It is
preserved on `codex/exec-08-checkpoint-7199ace`. A separate diagnostic of the
interrupted architecture module plus handoff tests passed 31 tests and 59
subtests in 3.78 seconds; the last displayed module is not an established cause.

The user then approved a bounded safe YAML loader optimization: prefer
`CSafeLoader` when installed and retain `SafeLoader` as the explicit fallback.
Inputs containing tabs, percent signs, question marks or backslashes stay on the
original parser to preserve known scanner/directive/escape/tag differences;
inputs with more than 128 potential collection openings or a line beginning
with 128 spaces also retain the original parser's recursion behavior.
These conservative dispatch guards are not a new
input rejection or depth limit. The checked-in catalog uses the fast path.
Both paths retain the same duplicate-key constructor and local path, symlink, byte-size,
UTF-8 and mapping-root checks. Compatibility/security regressions exercise both
paths, including catalog equivalence and rejected inputs. A pre-change,
process-only catalog diagnostic measured median 0.2092 versus 0.1552 seconds
over five runs per parser, with identical catalog hashes. This is not a promised
full-suite speedup or proof of the timeout's cause. No schema-validation cache,
dependency, check, deadline, network or Docker setting change is authorized.

Freeze one primary feature commit, including these verification commands in its
body, before qualification. With native Python and pinned Node tools on `PATH`,
use `PYTHONDONTWRITEBYTECODE=1 UV_OFFLINE=1 UV_FROZEN=1 COREPACK_ENABLE_NETWORK=0`.
Do not inject runner overrides, alter deadlines, lower coverage, filter suites
or change Docker/network settings. Run `python -B -m scripts.ac_16_quality run`
for all 20 quality gates, then
`python -B -m scripts.verify_requirement_evidence branch --id EXEC-08 --base main --run --witness`.
The manifest runs the handoff check, its regressions plus YAML compatibility,
existing documentation and assumption controls, AC-17 claims, the source-quality
audit, the complete clean-state journey and paired backup/restore. Its connection witness restores
only the changed verification guide and must fail the documentation gate.

The [requirement record](verification/requirements/EXEC-08.md) and
[manifest](verification/requirements/EXEC-08.json) define those gates. Report
every command's actual status, pass/fail/error/skip counts where captured,
warnings, scope, source identity and cleanup outcome. Do not fill missing
counters from expected output. If source or commit metadata changes, reassess
and repeat the required source-bound qualification. Only after the complete
successful run should the feature be offered for explicit non-fast-forward
merge approval; no such merge is authorized by this handoff.

### Assumptions and remaining limits

CI remains disabled, not green. Preserve the original 16 checkpoint branches,
both EXEC-08 checkpoints and all retained requirement branches. The baseline
inventory has 122 of 124 requirements merged: DEL-01 remains pending and EXEC-08 is not yet merged.
This reporting change neither completes DEL-01 nor retroactively verifies
unrelated matrix rows.

Historical AC-15 evidence is not a fresh four-suite run. Neither the literal
`make acceptance` aggregate nor a fresh AC-15 kernel-isolation four-suite run
is claimed here. The full quality and clean-state commands have their own
declared network boundaries; dependency acquisition can use registry networking,
and web route-mocked checks are distinct from real-runtime browser checks.

Deterministic mock behavior is not live-provider delivery. PostgreSQL skips
are not passes. Production authentication, provider credentials, remote
exactly-once delivery, production recovery objectives and deletion SLAs remain
unqualified. The [24-entry assumption register](assumptions.md) retains its
revisit triggers, including provisional local self-approval (ASM-011). ASM-024 now
reflects the shipped leased logical retention batches; this is not secure-media
erasure, backup deletion or a production guarantee.

Credentials, raw payloads, databases and secret-bearing backups are not
verification artifacts. Keep only sanitized status/count/hash evidence outside
Git; exclude full prompts and personal data. Local hashes and source consistency
checks do not authenticate who executed a report. Existing historical sections
below retain their dates, failures, limitations and then-current vocabulary.

## AC-17 current documentation review

Historical snapshot: this section records the AC-17 review at the dated baseline
below. Its retained heading and words such as "current", "unverified" and "not
merged" describe that review, not the latest main or EXEC-08 candidate. Later
requirement records have their own scope; they do not retroactively turn this
source review into product reruns.

Review date: **2026-10-12**. Product-source baseline:
`09a732609f8d871fc5a6495e84f5347e3c939be5` (main after AC-14).
This review updates documentation and adds bounded claim-contract tests; it does
not change product behavior. The final AC-17 feature identity and executed gates
belong to its [manifest](verification/requirements/AC-17.json) and
[requirement record](verification/requirements/AC-17.md), with committed-tree
attestation stored outside Git. No document can embed its own final commit hash.

Claim: **Implemented but not live-tested** in this documentation review — the
current source provides five demos and 43 explicit role dry-run paths, aggregate
verification commands, and 16 browser runners. These are source-reviewed facts,
not freshly executed application results. Previous requirement records retain
their own dates, revisions, commands, pass/fail/skip counts and limitations.
The significant distinctions reviewed across the 13 required guides are:

| Subject and current guide | Category and bounded claim | Source/evidence reviewed |
| --- | --- | --- |
| [Workflow scope](../README.md#current-workflow-scope), [architecture](architecture.md#current-workflow-scope), [catalog authoring](catalog-authoring.md#current-executable-catalog-boundary), [operations](operations.md#current-workflow-scope) | Deterministic mock behavior: 37 read/local-output paths and six inert previews; explicit approved mock writes are separate. No live delivery or arbitrary workflow execution. | [OBJ-03 record](verification/requirements/OBJ-03.md), [workflow registry](../apps/api/src/marketing_agents/infrastructure/executable_workflows.py). |
| [Current commands](../README.md#current-verification-commands) and [browser scope](testing.md#current-browser-verification-scope) | Implemented but not live-tested here: command availability is not a passing aggregate. OBJ-06's real-runtime/native transport is distinct from route-mocked browser tests. | [Makefile](../Makefile), [local verifier](../scripts/verify_local.py), [OBJ-06 record](verification/requirements/OBJ-06.md). |
| [Demo walkthroughs](demos.md) and [adapters](adapter-contracts.md) | Deterministic mock behavior: displayed expectations, accepted intake, approval decisions, and completed mock receipts are different facts. | [Scenario registry](../apps/api/src/marketing_agents/demos/registry.py), [DEL-03 record](verification/requirements/DEL-03.md). |
| [Security](security.md), [identity](identity-and-authorization.md), and [data handling](data-handling.md) | Implemented and verified only in their linked scoped historical tests; local identity is not production authentication, and projection expiry is not physical erasure. | [RUN-10 record](verification/requirements/RUN-10.md), [SAFE-07 record](verification/requirements/SAFE-07.md), [ADR-0010](adr/0010-data-retention.md). |
| [Assumptions](assumptions.md) and [local operations](local-operations.md) | Assumption and residual risk: local self-approval, trusted host access, web outbound capability, no physical retention sweeper, optional PostgreSQL, and no remote exactly-once guarantee remain explicit. | [Assumption tests](../tests/source/test_src_03_assumption_register.py), [ADR-0007](adr/0007-external-action-delivery.md), [Compose](../compose.yaml). |
| [Real adapters](adapter-contracts.md#adding-a-real-integration) | Deferred real-adapter work: local mocks require no provider credentials. Separate dependency acquisition may use registry networking; it is not runtime delivery or live-provider qualification. | [Runtime composition](../apps/api/src/marketing_agents/workers/runtime/composition.py), [testing prerequisites](testing.md#claim-status-and-prerequisites). |

The [AC-17 checker](../scripts/verify_ac_17_claims.py) combines the existing
guide/link/pin checks with a curated set of significant section contracts.
Copied-root negative controls remove classifications, scope limits or evidence,
restore stale statements, and promote mocks/targets to unsupported success.
These checks detect those particular regressions; they do not prove arbitrary
prose is true or execute linked behavior tests. Its linked behavior evidence is
source review only, not product reruns. Independent source review remains
necessary. [Regression tests](../tests/tooling/test_ac_17_claim_taxonomy.py)
exercise these distinctions. A restored-base witness must fail for the corrected documentation
claims while retaining the checker, tests and dependencies.

Claim: **Acceptance target not yet verified** — AC-15, AC-16 and final-source
acceptance remain unverified. The separate AC-15 diagnostic checkpoint is not
merged into this baseline and its browser failure is not waived. AC-17 does not
rerun the four complete suites, qualify a live provider, or certify PostgreSQL.
Historical counts below are not current results. The
[requirements matrix](implementation-plan/16-requirements-traceability-matrix.md)
remains the requirement inventory. GitHub CI remains paused by
user direction; this review does not poll, re-enable, or retry it.

The AC-17 requirement record reports the actual documentation-gate commands,
host/toolchain, results and witness outcome. Networked dependency acquisition is
recorded separately from the offline checks. Credentials, raw payloads, database
files and secret-bearing backup bundles are not verification artifacts.

Claim: **Implemented and verified** for documentation checks only — the combined
AC-17, DEL-06 and assumption-register regression run passed 62 tests in 12.727
seconds on macOS/Python 3.12.12. The new AC-17 module separately passed 37 tests
and 107 subtests; its lint and formatting checks passed. The actual committed-tree
gates and restored-base witness remain required before merging. None of these
documentation results is a fresh product or full-suite acceptance result.

## Source and environment

Historical DEL-06 snapshot: all dates, environments and results in this section and
the following **Local DEL-06 checks** section belong to that requirement. They
are preserved, not refreshed by AC-17 or EXEC-08. References to the current host
mean the host at that recorded execution, not the latest verification environment.
The later DEL-07 and OBJ-03 sections are
likewise dated historical snapshots, including their then-pending attestations.

Record date: **2026-09-11**. Product-code baseline:
`d28b40a317207cd24446657b01ed83fb41b2eeeb` (main before DEL-06).
DEL-06 is a documentation/checker-only requirement based directly on that commit.
Its final feature/tree identity and executed gate results are bound by the
[requirement manifest](verification/requirements/DEL-06.json) and the external
tree-bound attestation produced after the feature commit; this document cannot
self-reference its own final commit hash.

Current host checks used macOS and Python **3.12.12**, uv **0.10.7**. The active
shell has Node **24.3.0**, not the project's required **24.20.0**. No frontend or
browser validation is claimed for this documentation run. The required pnpm
version is **11.24.0**; no package acquisition was performed by DEL-06.

Catalog content version: **1.0.0**. Exact inventory: **5 departments, 12 functions,
36 role templates, 43 instances**. The separate Marketing Orchestrator is not
instance 44. The compiled release hash, checked against the
[lock](../catalog/v1/release.lock.json), is:

```text
catalog-sha256-v1:3970f3f23341d3e43a83ff73985e0485addd6c0df7519595f535420c09a9ced1
```

## Local DEL-06 checks

Historical snapshot: the commands and outcomes below belong to DEL-06 at its
stated source and environment. They are not fresh EXEC-08 execution evidence.

Claim: **Implemented and verified** only for the results explicitly marked
passed below. Documentation validation does not execute all behaviors it describes.

| Command / source | Result and scope |
| --- | --- |
| `UV_OFFLINE=1 PYTHONDONTWRITEBYTECODE=1 make verify-governance PYTHON=.venv/bin/python` on the clean baseline | Passed: formatting, 14 source tests, 57 tooling tests, architecture boundaries, and retained-branch history. 124 requirements, 94 merged, 30 missing; one approved maintenance merge. Counts are a baseline snapshot, not the post-DEL-06 inventory. |
| `UV_OFFLINE=1 PYTHONDONTWRITEBYTECODE=1 make verify-catalog-release` | Passed: compiled catalog hash and the exact inventory above. |
| Focused security/identity command below on the same product code | Passed: 162 tests in 15.14 seconds; no live providers, Docker stack, or PostgreSQL server. |
| `make test-del-06-docs PYTHON=.venv/bin/python` | Passed: 13 guides, local links/targets and source-pin checks, plus 22 documentation tests including negative controls. The final committed feature is rechecked with both manifest gates and the connection witness. |

Exact focused command (13 existing modules):

```sh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/security/test_safe_01_default_mock_mode.py \
  tests/security/test_safe_02_external_write_authorization.py \
  tests/security/test_safe_03_compliant_source_access.py \
  tests/security/test_safe_04_untrusted_content.py \
  tests/security/test_safe_07_redaction_retention.py \
  tests/security/test_safe_10_secret_hygiene.py \
  tests/security/test_run_06_input_redaction.py \
  tests/security/test_run_06_timeline_redaction.py \
  tests/security/test_orch_09_audit_redaction.py \
  tests/integration/api/test_run_10_identity_dependency.py \
  tests/integration/api/test_api_09_transport_security.py \
  tests/unit/application/test_run_10_authorized_approval_actor.py \
  tests/unit/application/test_api_07_audit_resources.py
```

The documentation gate checks 13 required guides, repository-contained links and
heading anchors, actual Make targets in shell examples, release counts/hash,
toolchain pins, stable assumption IDs, and claim-category definitions. Negative
controls remove guides or corrupt these inputs. A connection witness retains
the checker/tests but restores the product guides to the base, which must fail.
This catches specific omissions/drift; it cannot prove arbitrary prose semantics
or that a source link's test has recently run. Independent source review remains
part of DEL-06.

## DEL-07 local verification

Historical snapshot: retain these dates, counts, candidate identities, failures
and then-pending gates exactly within DEL-07's scope. Statements about the
"current-source" backup and "final amended feature" refer to that requirement,
not the latest main or a fresh EXEC-08 result.

Claim: **Implemented and verified** — on 2026-09-11, the frozen DEL-07 worktree
based on `7f9ea62d5489bde73bae4ddcc87eee4dc34b164c` passed all **20 local gates**
in 1,972.837 seconds. Python was 3.12.12, with explicitly selected Node 24.20.0
and installed pnpm 11.24.0 dependencies. The runner compared 1,143 source files
before/after and found **zero changes**. This evidence record was updated after
that run; it is not a final committed-tree or complete acceptance attestation.

```sh
# Select the Node version pinned in .nvmrc first.
UV_OFFLINE=1 PYTHONDONTWRITEBYTECODE=1 make verify PYTHON=.venv/bin/python
```

- Catalog: 63 tests passed first. The remaining backend suite passed 2,616 tests
  in 1,455.70 seconds, with 32 optional PostgreSQL cases skipped: **2,679 total
  backend passes**, not PostgreSQL qualification.
- Fresh safety coverage: all 17 required modules passed with **2,741 covered
  statements and 1,056 covered branches**, no missing measured statements or
  branches, and only the disclosed source-pinned two-line enum fallback excluded.
- Backend format/lint passed across 403 selected source/test files; mypy passed
  for 264 application files. The new tooling's separate static checks also passed.
- Frontend: all 427 tests in 53 Vitest files passed, zero skipped; formatting,
  lint, TypeScript and production build passed. The existing large-chunk build
  warning remains a warning, not a performance qualification.
- Browser: all 15 unfiltered runners passed **32 cases with zero skips** under
  the corrected uniform network fixture, including hierarchy, accessibility,
  responsive layouts and all five demo interfaces.
- The actual Chromium network canary passed: eight expected denied cases had
  zero unapproved-server hits, one in-memory positive passed, and context reuse
  was rejected. The Node network/policy/inventory controls passed all 20 cases.
- Both API artifacts regenerated in memory without drift. Three API snapshot
  tests and six generator controls passed. The 96 safety-checker, 90 runner and
  29 repository-text negative/control cases also passed inside the full suite.
- Repository text checks passed for 395 files without fetching external URLs;
  documentation, source provenance, architecture, retained history, tracked
  secret-pattern scan and whitespace checks passed.

The sanitized report is `verification.json` in the external
`marketing-agents-del07-zof87ina` artifact directory; SHA-256:
`80c2502560566c7b4902993557c5ceecb85038efe53717fcd3c46dcf1c81a8d3`.
Its accompanying coverage report was freshly produced by this run, not combined
with earlier development reports.

Claim: **Deterministic mock behavior** — an additional native smoke used
`workers.serve_api` with a new private migrated/seeded SQLite/key pair and the
production frontend preview. The approvals and instance-status endpoints both
returned 200. Desktop 1440x1000 and mobile 426x923 checks passed for identity,
43 instances, visible root/control plane, zoom/fit/search, tree interaction,
no blank/error overlay, no horizontal overflow, and zero page/console errors or
warnings. Screenshots were inspected. Owned processes stopped, ports 8000/4173
were free afterward, and the temporary database/key were removed.

An earlier smoke against bare `api:create_app` failed with two safe 503 responses
because that factory omitted runtime services. The later composed-runtime check
passed; the earlier failed setup is not relabeled as a success. Browser demo
mutations remain route-mocked; these checks do not by themselves prove the full
UI-to-worker-to-approval-to-artifact chain. Actual process integration tests are
separate evidence, and neither suite proves live-provider delivery.

Claim: **Implemented and verified** for the current-source paired backup gate:
`make test-del-05-compose-backup PYTHON=.venv/bin/python` passed. All 47 tables,
43 instances/configurations and key identity were preserved; overwrite attempts
were refused without changes. No application services started or IPC storage
was copied. Both the verifier and independent Docker queries confirmed owned
container/volume/alias/network cleanup, and private bundles were removed. The
original 420/150-second execution/cleanup deadlines were unchanged.

Claim: **Deterministic mock behavior** — exact committed clean-state verification
passed all eight phases for local candidate
`12e935ae913fec29bc482a9f449de7759ccfac6d`, tree
`470bc558f72402888ad42d057171c0d934530565`:

```sh
make verify-clean REF=12e935ae913fec29bc482a9f449de7759ccfac6d PYTHON=.venv/bin/python
```

The sanitized `report.json` in external directory
`marketing-agents-del07-clean.pMZhxO` has SHA-256
`31a24b503a4eaa3bdaf12bce5689f0be2670a58afb3ffdd62308ffa8f9f49f12`.
The tracked export hash was
`5d808fab0275b1c39e67f1550bf9f1b17b74420ab6cfb2dbbb009b1c178a590a`.
Pinned container Python was 3.12.14, distinct from the host's 3.12.12.
All five deployed demos completed; Email made zero calls before both approvals,
then exactly two successful mock actions with one delivery attempt each.
Restart preserved action/receipt/run identity and webhook/scheduler admission
replay identity. Reseeding made zero writes and preserved 43 configurations.

The offline backend diagnostic reported zero failures, 2,733 passed report
events and 33 skipped events. These are not unique-test counts: its event hook
also counts 55 successful unittest subtests. Source/dependency reconciliation
is 2,679 host passes minus one optional-driver case plus those 55 events.
The default Docker install omits the PostgreSQL extra, explaining the additional
skip; the sanitized report does not retain individual skipped-node names.
Offline frontend verification and the production browser smoke also passed.

Backend and offline-suite containers used network isolation; registry acquisition
was permitted separately. The ingress web container retained an outbound route,
so this is not an all-container egress-denial claim. The clean caller worktree
was preserved, owned runtime resources/export were cleaned, and execution and
cleanup deadlines remained 1,620/120 seconds. Reusable image caches may remain.

The four dependency-free Node witness controls also passed in the candidate
archive and failed all four assertions when only the guard implementation was
restored to the base. No import failures or real outbound traffic were involved.
This preflight is separate from the protected final branch attestation.

Claim: **Acceptance target not yet verified** — the final amended feature must
pass all manifest gates and its connection witness before merging. The local
results above verify DEL-07 behavior; they do not complete unrelated acceptance
rows or the whole-system acceptance target. The [testing guide](testing.md)
describes the commands and limits.

## OBJ-03 operational workflows

Historical snapshot: these preflight, failed aggregate and repair results belong
to OBJ-03 at its stated source. Its then-pending final gates are retained as
recorded; this section does not report current EXEC-08 qualification or CI state.

Claim: **Implemented and verified** within local mock boundaries — on 2026-09-14,
the ten application-source [OBJ-03 gate commands](verification/requirements/OBJ-03.json)
passed preflight before the final replay-fixture repair on the implementation based on
`19e96d3da4ecdbd3d87a69b76086b5438b8a990c`. Python was 3.12.12 and Node 24.20.0;
no dependencies were acquired. The [detailed record](verification/requirements/OBJ-03.md)
identifies commands, individual durations, component counts and limitations.
The matrix records these scoped tests, not whole-system acceptance. Every gate and
the actual restored-base witness must still independently pass on the final feature
commit before the non-fast-forward merge; the tree-bound attestation stays outside Git.

- Actual API admission and restarted workers exercised all 43 manual dry-run
  instances: 37 role-specific reads and six inert write-proposal previews.
- Explicit mock execution for all six write-only instances waits for independent
  approval of the exact action. Durable local receipts, replay and cancellation
  checks do not claim real delivery. Preview rendering grants no action authority.
- Saved schedule input is schema-validated, keyed to its workflow, admin-only and
  dry-run-only. Configuration/claim fences prevent stale admission; legacy schedules
  without explicit input remain readable but do not execute.
- Separate local-output executors preserve no-call accounting and atomic step,
  artifact and audit persistence. Populated SQLite upgrades and injected copy/drop,
  audit and final foreign-key failures preserve prior state on rollback.
- Frontend units, static checks, generated contracts and the declared WEB-03 browser
  runner passed. Additional real-API desktop/mobile QA verified saved-input
  save/reopen and privacy, not browser-to-worker completion or an approved write.

The current-source archive witness passed; restoring only the production attempt
classifier caused the expected ordinary `TOOL != NO_CALL` assertion failure, with
archive-local imports and no dependency/collection error. This is preflight evidence,
not a substitute for the final committed-tree witness. Optional PostgreSQL runtime
and live adapters remain unverified. The earlier interrupted broad diagnostic is
explicitly not a full-suite pass; separate completed output is required for any
additional broad-backend claim.

The subsequent permission-corrected broad backend run completed with 2,919 passes,
one stale replay-fixture failure, 32 optional PostgreSQL skips and 55 passing unittest
subtests in 1,398.36 seconds. The verification-only fixture must provide explicit
signed schedule input and the registered workflow, matching normal scheduler rules.
Its repair and regression are included in OBJ-03; all 82 targeted clean-state,
fixture and real-HTTP cases then passed in 40.71 seconds. No scheduler safety check
is relaxed. The expanded final manifest gate includes the fixture regression.
That broad result remains a failed aggregate, not a green full-suite claim. The detailed
record also discloses the earlier sandbox-only local-socket failures and successful
permission-corrected transport rerun.

GitHub reported the workflow `disabled_manually` on this resumption. CI remains off
by user direction; its source, the separate repair branch and remote refs are unchanged.

## Historical runtime and CI evidence

Claim: **Deterministic mock behavior**. The historical
[DEL-05 record](verification/requirements/DEL-05.md) reports its exact committed
clean run on 2026-09-08: all five demos, zero Email writes before both approvals,
two mock actions/receipts after approval, zero-write reseed preserving 43
configurations, stable database/key and replay identity across restart, isolated
backend/frontend suites, and production browser smoke. That evidence belongs
to its stated revision/environment; it is not a fresh DEL-06 full-stack test or
proof of real delivery. Its optional PostgreSQL skips remain skips.

CI was disabled by user direction on **2026-09-10**, and GitHub still reported
`disabled_manually` at the 2026-09-11 resumption. No workflow source or local
test command was removed. The latest observed main
[CI run](https://github.com/felipe-cosse/Marketing-Agents/actions/runs/34397649419)
failed the clean verifier's aggregate deadline. Five other jobs passed. The
unmerged CI repair branch's
[retry](https://github.com/felipe-cosse/Marketing-Agents/actions/runs/34497199010)
also hit that deadline after its backend suite passed; its prior attempt failed
during fresh startup. The repair branch is preserved, **not merged into this
product baseline**, and no claim is made that those intermittent failures are fixed.
Do not re-enable or retry CI merely to produce a green badge while it is paused.

## Remaining acceptance and risks

Historical snapshot: this closing summary preserves the earlier DEL-06-era
acceptance position and limitations; it is not the current requirement inventory
or an assertion that later merged work remains absent. In particular, the
[EXEC-06 retention implementation](verification/requirements/EXEC-06.md) now
provides bounded leased logical erasure. Current [assumptions](assumptions.md)
and [data handling](data-handling.md#recorded-expiry-and-bounded-retention-maintenance)
still exclude secure-media/backup erasure and a deletion SLA. Historical pending
statements below neither establish nor invalidate a later matching-source run.

Claim: **Acceptance target not yet verified** — final completion requires every
matrix ID and its actual gates, not just retained branch counts. DEL-06 does not
complete DEL-01, DEL-07, acceptance, execution, or remaining objective rows.
The [testing guide](testing.md) discloses the aggregate, coverage policy and
remaining validation gaps. Final clean-state/browser/backup verification still needs a matching
final source revision. CI being disabled is a current operator choice, not
evidence that a release gate passed.

Claim: **Residual risk** — local administrative self-approval, trusted Docker/
filesystem access, no application database encryption, web-container outbound
capability, no automatic physical retention sweeper, optional PostgreSQL
qualification, and non-qualified production backup/delivery remain explicit in
[security](security.md), [data handling](data-handling.md), and
[operations](operations.md).

Evidence must remain sanitized: no credentials, database/key bundles, raw user
payloads, full prompts, or personal data. Store generated reports outside Git;
record command outcomes honestly, including interruption, failure, skip, and
cleanup failure. Keep secret-bearing backups separate from verification artifacts.
