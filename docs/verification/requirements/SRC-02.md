# SRC-02 verification

Status: verified locally

The source-authority boundary is represented in `catalog/source-evidence.json`, explained in `docs/source-authority.md`, and enforced by a standard-library test. The index covers all six local frames by path and SHA-256 hash. It explicitly excludes third-party integration details and duplicate-card explanations from source authority.

## Evidence

Command:

```text
python3 -m unittest tests/source/test_src_02_source_authority.py
```

Result on 2026-08-18: 2 tests passed. No network access or credentials were used.

Additional checks:

```text
python3 -m json.tool catalog/source-evidence.json
git diff --check
```

Both checks passed. This is repository-level verification, not a claim that the LinkedIn post or any external integration was accessed live.
