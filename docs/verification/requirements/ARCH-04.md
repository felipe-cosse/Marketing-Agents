# ARCH-04 implementation evidence

Status: implemented; authoritative catalog verification pending

The repository now contains eight explicit Draft 2020-12 structural schemas and a read-only catalog compiler. It rejects duplicate YAML keys, absolute/escaped/symlinked paths, remote schema references, invalid or duplicate IDs, broken hierarchy references, unsafe write-policy combinations, and count/distribution drift. It resolves prompts and typed schemas locally, returns frozen ordered records, and produces a domain-separated semantic hash independent of absolute paths and file metadata.

Six synthetic positive and negative compiler tests pass with strict mypy and Ruff. The authoritative 5/12/36/43 production dataset is intentionally not partial in this branch; CAT-01 through CAT-08 add it, and EXEC-03 makes that complete catalog a hard gate.
