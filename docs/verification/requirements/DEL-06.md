# DEL-06 product documentation evidence

## Scope

DEL-06 supplies the complete product documentation set required by plan 15.
The implementation is the reader-facing README and guides, not just this record.
It preserves the existing local-operations runbook and stable ASM-001–ASM-024
IDs, closes stale lifecycle wording using accepted ADR-0004, and documents the
actual DST and physical-retention limitations.

The [verification record](../../verification.md) gives exact source, environment,
commands and limits. Independent cross-review compares operational/demo labels,
catalog/adapter mechanisms, and security/data claims with implementation.
No application, catalog, schema, migration, adapter, or CI behavior changes.

## Executable checklist

The [manifest](DEL-06.json) binds all allowed paths and four scoped claims to
the offline documentation consumer and its negative controls.

`make test-del-06-docs` checks the 13 product guides and their navigable links,
headings, shell Make targets, pinned versions, catalog counts/hash, assumption
IDs, and taxonomy. Tests copy only documents/contract inputs/linked evidence to
owned temporary storage and independently omit or corrupt these inputs.
No examples are executed, external links fetched, or developer state reused.
The existing assumption-register suite is a separate gate.

The connection witness retains the new checker and tests while restoring the
13 product guides to the base. Expected failure is missing/obsolete product
documentation, not a missing dependency or import; the checker uses the standard
library and accepts an explicit copied/archive root.

## Results and limits

Baseline governance passed on `d28b40a317207cd24446657b01ed83fb41b2eeeb`:
14 source tests, 57 tooling tests, architecture checks, and valid retained history
with 94 requirement merges and 30 missing. Catalog hash/count validation passed.
The focused security/identity/redaction/retention refresh passed 162 tests; its
exact command and environment are in the reader-facing verification record.

The documentation gate passed all 22 tests, including missing-guide, bad-link,
anchor, navigation, compound-command, source-pin and taxonomy negative controls.
Independent cross-review found and corrected catalog-path hash wording, the
expiry-only approval renewal boundary, and the API-versus-worker Unix-socket
description. Checker review also led to explicit coverage of every compound Make
command, not only the first command in a shell line.

The final committed gate/witness results are checked before handoff; the generated
attestation binds the feature/tree hash. Historical product runtime results are
not relabeled as new DEL-06 tests.

CI remains paused by user direction. The separate CI repair branch is preserved
and unmerged. This documentation requirement does not waive failed clean-state
checks or claim the remaining 124-ID acceptance audit has completed.
