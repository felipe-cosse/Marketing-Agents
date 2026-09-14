# OBJ-02 source-modeled interactive chart

Requirement: display an interactive source-modeled org chart.

Status: implemented and locally verified within the read-only browser boundaries
below. The committed-feature gate and restored-base witness are required before merge.

## System change

The browser hierarchy boundary now rejects identity drift even when a response
preserves every arithmetic count. It requires the five department and twelve
function IDs in authoritative display order, function-owned template namespaces,
canonical template-derived instance IDs and ordinal one outside Community.
Community retains seven ordered `.01`/`.02` pairs. Templates cannot move between
Events and Education merely because both functions contain six cards.

These are transport validation checks, not a second editable catalog. Exact role
names and purposes still come from the compiled API catalog. Display-order sorting,
frozen projections, presentation-only updates and independently selectable
Community deployments remain supported.

## Objective coverage

| Behavior | Executed evidence |
|---|---|
| Exact source hierarchy, order, multiplicity and real role identities | Full frontend unit suite; API-02 hierarchy and OBJ-01 projection suites; WEB-01 browser |
| Invalid identity rejection and explicit retry recovery | Nine OBJ-02 normalization controls; dependency-free witness; OBJ-02 browser journey |
| Pan, zoom, fit and independent selection | WEB-01 component/viewport tests and complete browser specification |
| Search, filters, retained ancestors and bounded status updates | WEB-02 complete browser specification and frontend units |
| Mobile tree, keyboard navigation and focus | WEB-07 complete browser specification and frontend units |
| Accessibility, reduced motion and reflow | WEB-08 complete browser specification and frontend units |

The OBJ-02 browser journey changes only the first real hierarchy response to an
invalid ordinal. It verifies no graph, tree or selectable cards render, then uses
the actual retry button and real API response. It checks all 43 card identities,
zoom/fit, both Community detail responses and selection after a switch to a
390-by-844 semantic-tree layout. Screenshots and traces are retained outside the
repository by the scoped browser runner. The inherited default network guard is
unchanged; this journey performs no mutation.

The default browser-test API intentionally has no operational approval-count or
run-status services. Their exact real 503 responses must remain visibly unavailable,
not be represented as zero or successful service data. Other browser errors remain
failures. This does not qualify operational worker execution.

## Verification procedure

Run with the pinned Node 24 toolchain and repository virtual environment on PATH:

```text
node apps/web/scripts/run-web-01-unit.mjs
node apps/web/scripts/run-obj-02-witness.mjs
node apps/web/scripts/run-obj-02-browser.mjs
make verify-requirement REQUIREMENT=OBJ-02 BASE=4289affcf82344e1952563c3f1f8b42a1614a0a8 HEAD=<feature-commit> PYTHON=python3
make verify-history PYTHON=python3
```

The manifest additionally runs the entire frontend unit/lint suites and both
targeted Python suites. The browser runner typechecks and builds once, then runs
the complete WEB-01/02/07/08 specs without test-name filters. Local server binds
and Chromium need the host's loopback execution permission. No dependency install,
CI workflow, provider request or push is part of this verification.

Initial negative controls reproduced five missing rejection assertions on the
unchanged implementation (64 other tests passed). After the product change, all
73 scoped normalization/chart/client tests and all 444 frontend unit tests passed.
The initial browser pass completed 13 existing cases and all new interactions, but
correctly failed its overbroad console assertion on the two unavailable operational
services. The narrowed test must verify those exact negative states explicitly.

The corrected browser preflight passed **14/14** cases (22.5 seconds), including
the exact two unavailable-service responses and UI states. Sanitized preflight
also passed all **444 frontend unit tests**, frontend lint, **17 targeted API and
organization tests**, and the dependency-free identity witness. Browser typecheck
and production build passed; the pre-existing bundle-size warning remains visible.

Desktop and mobile screenshots were inspected: the complete ordered graph was
contained, and the selected second Community instance retained its distinct ID in
the narrow full-screen inspector without horizontal overflow.

Final committed-feature gates and the restored-base connection witness are required
before merge. The witness imports the production normalizer in an isolated archive
without frontend dependencies; restoring implementation must produce an ordinary
missing-rejection assertion. Preflight results are not a commit-bound attestation.

## Limits

- Browser plugin not available; the frontend-testing and Playwright workflow uses
  the repository's pinned Playwright-managed Chromium and existing local API/proxy.
- Viewport emulation and automated accessibility checks do not establish physical
  device, other-engine or screen-reader parity.
- WEB-02 status and WEB-08 operational fixtures retain their own documented limits.
  Neither they nor this chart objective prove an end-to-end worker run.
- Vite reports the existing bundle-size warning; the gate does not hide it or claim
  a performance-budget pass.
- CI stays disabled and no remote refs are changed. Whole-system acceptance remains
  separate from this bounded objective.
