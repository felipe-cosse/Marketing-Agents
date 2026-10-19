# DEL-11 — Public project README

## Scope and authority

The user requested continuation of README commit
`5b3b2283e945786bdfe43c250f87f1b522540384`, keeping CI disabled and every
checkpoint intact, with full verification before asking to merge. DEL-11 starts
at updated main `3ce52a8a2a10a16be019090a5c32ff3e3be7e12f` on
`req/del-11-public-readme`. The original README commit remains unchanged on
`codex/public-readme-checkpoint-5b3b228`.

This is a new bounded documentation requirement, not another DEL-06 primary
commit or a direct merge of the README checkpoint. The matrix adds only this
requested scope. Application, Compose, database, collector, dependency locks,
CI and protected evidence policy/schema/executor files remain unchanged.
Machine authority: [DEL-11.json](DEL-11.json).

## Documentation and visual assets

The public README continues the checkpoint's product overview, local quick
start, guided product tour, architecture diagram, five demo summaries,
development guidance and documentation navigation. Existing workflow counts,
approval boundaries, mock/provider distinctions, operator trust limits and
verification disclaimers remain present. Two narrow native-setup corrections
list Corepack on PATH and restore the explicit existing-SQLite database/state
selection with a link to the native operations workflow.

The three PNGs and SVG are retained byte-for-byte from the reviewed checkpoint.
The PNGs were captured from source
`691a03d4b1e816007bc0d12e63d29ed7c2e6cce2` in a native local stack with synthetic
presets and deterministic mocks. Their capture source is not relabeled as this
new documentation candidate. The capture notes distinguish native UI pictures
from the Compose architecture diagram and explain reproduction and limitations.

The diagram documents the private API and PostgreSQL Unix sockets, backend and
database containers without IP networking, and the web proxy's remaining
outbound route. Neither the diagram nor a screenshot is an assertion of
universal egress isolation, real provider execution or production readiness.
Human review compares these claims with the unchanged current implementation.

## Regression boundaries

The dedicated DEL-11 test module checks the public tour and native guidance,
local visual inventory, reviewed bytes and dimensions, capture provenance,
local link/anchor resolution and passive accessible SVG structure. Small
negative controls exercise hidden or missing references and provenance, changed
assets, escaped/non-regular paths and unsafe SVG content. Visibility checks reuse
the existing bounded comment/example filter, not a full Markdown renderer.
It does not fetch external pages, execute documented
examples, launch providers or modify source assets.

The original DEL-06 checker and fixed 13-guide inventory stay intact. Complete
DEL-06 documentation, EXEC-06 demo documentation, AC-17 claim taxonomy and
EXEC-08 handoff controls run alongside the new tests; no existing assertion is
removed to accommodate the redesigned README. The new test module is also
discovered by the complete backend suites.

## Frozen-source verification contract

Claim: **Implemented but not live-tested** until the following completes for
the exact frozen candidate. A focused pass, historical screenshot or previous
AC-18 execution cannot qualify the changed DEL-11 tree.

1. Run all documentation and visual-asset controls, scoped formatting/lint,
   repository text hygiene, tracked-secret scanning and diff checks. Independently
   review the README's actual setup commands, source facts, images and limits.
2. Freeze one nonempty `[DEL-11]` primary commit directly on the recorded main
   base, with verification commands in its body. Preserve any failed candidate
   before replacement; never amend a qualified commit just to add results.
3. Run `python -B -m scripts.ac_16_quality run` for the unchanged complete
   20-gate quality inventory, 17 safety-coverage modules and 16 browser runners.
   Keep all coverage, network, inventory, source-drift and deadline checks.
4. Separately acquire the exact-source offline image with
   `python -B scripts/ac_15_offline.py acquire`. Acquisition may contact public
   pinned dependency/image registries; runtime verification does not acquire.
5. Run `python -B -m scripts.verify_requirement_evidence branch --id DEL-11
   --base main --run --witness`. All 11 manifest gates must pass: complete
   documentation controls, scoped format/lint, four complete offline suites,
   exact-source quality audit, committed clean-state production delivery and
   both SQLite and PostgreSQL Compose recovery paths.
6. The connection witness restores only README to main while retaining the new
   tests and assets. It must fail on the missing public-tour/image contract, not
   an import error, missing dependency or deadline. Inspect the ordinary failure
   separately; a nonzero code alone is not enough to establish causality.
7. Inspect source/tree/image identity, actual results, bounded cleanup, retained
   desktop/mobile runtime screenshots, disabled CI and every checkpoint tip.
   Keep sanitized execution records outside Git; never retain private database
   or key bundles in documentation. Ask for explicit publication and
   non-fast-forward merge approval only after full verification succeeds.

Each full offline suite and recovery/delivery gate retains its existing
1,800-second outer deadline. Clean-state retains its 1,620-second execution and
120-second cleanup bounds. The optional PostgreSQL skips in ordinary backend
suites do not qualify that database: the clean-state journey separately requires
live PostgreSQL tests, and PostgreSQL recovery is mandatory. No additional
host-native PostgreSQL or standalone VMA diagnostic result is claimed here.

Mechanical checks establish the declared documentation contracts, not arbitrary
semantic correctness, universal file safety or independently authenticated
execution. CI remains disabled, not green. Verification is not publication or
merge approval, and this README is not a production qualification claim.
