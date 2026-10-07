# RUN-09 verification note

Action-integrity checks reject semantically changed proposals against an existing exact approval. Leaf replacement is rejected with `full_set_epoch_required`: changed actions require a wholly new authorization-set epoch, and this helper does not create or persist that replacement. Only unchanged-action expiry renewal may issue generation plus one, preserving the exact action/set/revision and linking the old expired request. Canonical JSON key order and Unicode normalization do not cause false invalidation.

The RUN-09 gate is unit-level integrity evidence. Durable/public API rejection, renewal, and recovery are covered by later requirement suites, including AC-09; they must not be inferred from this unit gate. This current-document correction does not alter the retained RUN-09 feature commit, merge, matrix statement/hash, or historical exact-tree evidence.

Machine authority: `RUN-09.json`. Runtime evidence is generated outside Git.
