# RUN-01 verification note

The primary Run lifecycle now follows ADR-0004 through typed commands rather than an arbitrary state setter. Every state and command pair is tested, terminal states are immutable, failure phases are explicit, read-only plans release directly, write-bearing plans pause, and exact approvals expire at the boundary (`expires_at <= occurred_at`). Accepted transitions use contiguous sequence and version values; rejected attempts return safe evidence for ORCH-09 to persist later.

The portable `runs` table keeps one primary Run per admitted WorkItem and `run_state_transitions` stores its ordered append-only history. Receipt requires the exact derived sequence-one transition; two independent sessions with distinct candidate IDs converge on one Run and one initial row. A version-and-state conditional update and transition insert share one explicit transaction. File-backed SQLite tests prove restart continuity through completion, exact-one-winner concurrent CAS behavior, atomic WorkItem and initial-Run rollback, and restoration of the original Run when an injected transition-insert fault occurs.

Machine authority: `RUN-01.json`. Runtime evidence is generated outside Git. ORCH-09 retains ownership of general audit-event persistence and merged timeline projections; DEL-04 retains migration ownership.
