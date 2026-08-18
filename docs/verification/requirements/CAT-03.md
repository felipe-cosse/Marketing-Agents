# CAT-03 verification note

The compiler now enforces all 43 stable IDs, namespace derivation, ordinal consistency, null v1 variant labels, and deployment-only field ownership. Mutation tests reject duplicate IDs, wrong namespaces, ordinal drift, copied role definitions, and invented variant purposes.

Machine authority: `CAT-03.json`. Runtime evidence is generated outside Git.
