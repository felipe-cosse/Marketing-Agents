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

## Source and environment

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

Claim: **Acceptance target not yet verified** — final completion requires every
matrix ID and its actual gates, not just retained branch counts. DEL-06 does not
complete DEL-01, DEL-07, acceptance, execution, or remaining objective rows.
The [testing guide](testing.md) discloses missing aggregate aliases, coverage and
drift gaps. Final clean-state/browser/backup verification still needs a matching
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
