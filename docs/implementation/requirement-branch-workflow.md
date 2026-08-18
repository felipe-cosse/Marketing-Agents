# Requirement branch workflow

Status: active

## Contract

Every requirement ID in
`docs/implementation-plan/16-requirements-traceability-matrix.md` receives one
dedicated branch, one primary requirement commit, and one non-fast-forward merge
to `main` before dependent requirements proceed.

Naming:

- Branch: `req/<lowercase-requirement-id>-<short-slug>`.
- Commit: `[REQUIREMENT-ID] <imperative summary>`.
- Merge: `merge: REQUIREMENT-ID <summary>`.

Branch procedure:

1. Require a clean `main` and a green preceding hard gate.
2. Create the requirement branch from current `main`.
3. Implement only the requirement and directly necessary tests/evidence.
4. Run targeted tests plus any affected catalog, migration, OpenAPI, security, or
   generated-artifact checks.
5. Review the staged diff and run `git diff --cached --check`.
6. Create one primary requirement commit with verification commands in its body.
7. Switch to `main`, merge with `--no-ff`, and rerun the targeted gate.
8. Keep the requirement branch until final history verification completes.
9. Record implementation/verification status in the traceability matrix only from
   actual evidence, never from code inspection alone.

Documentation/evidence requirements such as acceptance and execution rows still
receive real branches. Their commits add durable test evidence, verification
records, or traceability status; empty commits are prohibited.

## Inventory

The authoritative matrix currently contains 123 IDs:

```text
SRC-01 SRC-02 SRC-03
OBJ-01 OBJ-02 OBJ-03 OBJ-04 OBJ-05 OBJ-06
CAT-01 CAT-02 CAT-03 CAT-04 CAT-05 CAT-06 CAT-07 CAT-08
ARCH-01 ARCH-02 ARCH-03 ARCH-04 ARCH-05 ARCH-06 ARCH-07 ARCH-08
ORCH-01 ORCH-02 ORCH-03 ORCH-04 ORCH-05 ORCH-06 ORCH-07 ORCH-08 ORCH-09
DOM-01 DOM-02 DOM-03 DOM-04 DOM-05
RUN-01 RUN-02 RUN-03 RUN-04 RUN-05 RUN-06 RUN-07 RUN-08 RUN-09 RUN-10
SCHED-01 SCHED-02 SCHED-03 SCHED-04 SCHED-05 SCHED-06
API-01 API-02 API-03 API-04 API-05 API-06 API-07 API-08 API-09
WEB-01 WEB-02 WEB-03 WEB-04 WEB-05 WEB-06 WEB-07 WEB-08 WEB-09
DEMO-01 DEMO-02 DEMO-03 DEMO-04 DEMO-05 DEMO-06
SAFE-01 SAFE-02 SAFE-03 SAFE-04 SAFE-05 SAFE-06 SAFE-07 SAFE-08 SAFE-09 SAFE-10 SAFE-11
DEL-01 DEL-02 DEL-03 DEL-04 DEL-05 DEL-06 DEL-07 DEL-08
AC-01 AC-02 AC-03 AC-04 AC-05 AC-06 AC-07 AC-08 AC-09 AC-10 AC-11 AC-12 AC-13 AC-14 AC-15 AC-16 AC-17
EXEC-01 EXEC-02 EXEC-03 EXEC-04 EXEC-05 EXEC-06 EXEC-07 EXEC-08
```

The history verifier in `scripts/verify_requirement_history.py` derives this list
from the matrix rather than trusting the duplicated human-readable inventory.
