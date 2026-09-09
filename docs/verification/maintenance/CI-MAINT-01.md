# CI-MAINT-01: retained history and failure diagnostics

User approval: 2026-09-09, explicitly authorizing the scoped maintenance
exception, browser-test and diagnostic repairs, and publication of 94 retained
requirement branches. No original requirement commit or merge is rewritten.

The approved base is `992acb0736ec66126497bc0ba83efc13f9bec052`.
The feature subject is `ci: repair retained refs and failure diagnostics`.
The merge subject is `merge: CI-MAINT-01 repair retained refs and CI diagnostics`.

## Defects and controls

- GitHub had only `main`, while governance requires retained local `req/*`
  branches. Published feature refs are fetched and atomically copied into the
  expected local namespace. Missing, duplicate, mismatched, or fabricated
  requirement refs still fail the original evidence checks.
- WEB-03's run-ID text selector matched both metadata and the recent-run link.
  The regression test now distinguishes their semantic roles and verifies the
  link destination, without changing the UI or weakening the assertion.
- WEB-05 still expected API destinations that WEB-06 intentionally replaced
  with in-app run timeline and action anchors. Its assertions now verify the
  current link names and destinations; approval safety checks are preserved.
- The clean-startup job discarded failed command output after hashing it.
  Offline stages now emit bounded, allowlisted diagnostics, with private raw
  output excluded. Failed GitHub jobs retain short-lived diagnostic artifacts.

## Verification scope

Maintenance topology and negative controls are covered by
`tests/tooling/test_ci_maintenance_history.py`; retained-ref provenance and
transactional safety by `tests/tooling/test_prepare_ci_history.py`; sanitized
offline diagnostics by `tests/tooling/test_del_05_offline_diagnostics.py`.
The existing full source, architecture, browser, backend, and clean-deployment
gates remain required. Local results and the authoritative GitHub run outcomes
must be reported separately; adding diagnostics alone does not prove the
previously opaque offline failure resolved.

Local pre-publication checks on 2026-09-09 passed: all 32 browser tests across
15 runners; 17 maintenance-history regressions; 18 retained-ref regressions;
22 existing evidence-tool regressions; 93 diagnostic/clean-verifier tests;
14 source tests; source, architecture, catalog-first, and format/lint checks.
History validation reports 124 requirements, 94 completed, and 30 missing.
All 94 retained GitHub branch tips were verified after an atomic create-only
publication. Their historical workflows are distinct from the maintenance
candidate's checks. Full backend and clean-startup outcomes are not claimed by
this pre-publication record.

This maintenance work does not complete any of the 30 remaining requirements.
