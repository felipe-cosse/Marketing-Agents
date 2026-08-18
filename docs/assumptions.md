# Assumptions and implementation decisions

This register separates source facts from choices needed to build the local v1 product. An `accepted` item is binding for this implementation. A `provisional` item is safe for the local release but must be revisited before its stated expansion. Changes require an ADR when they affect architecture, authorization, external effects, data handling, or lifecycle semantics.

Owner for every entry: Marketing Agents maintainers.

| ID | Status | Assumption or decision | Validation / revisit trigger |
|---|---|---|---|
| ASM-001 | accepted | The repository was greenfield at implementation start, so the prompt's Python 3.12/FastAPI and React/TypeScript/Vite defaults apply. | Revisit only if a pre-existing stack is discovered in earlier history. |
| ASM-002 | accepted | Prompt text overrides tiny or ambiguous frame details. | Record discrepancies in the source-evidence index. |
| ASM-003 | accepted | Community `.01` and `.02` are source occurrence ordinals only; they share one template and have no invented variant label. | Revisit only with explicit source evidence or operator configuration. |
| ASM-004 | accepted | Marketing Orchestrator is a control-plane service, never instance 44. | Catalog/API/UI count tests exclude it. |
| ASM-005 | accepted | Department and function nodes are routing/grouping boundaries, not invokable LLM agents. | Architecture and planner tests enforce this. |
| ASM-006 | accepted | Drafting and analysis are read-only; publishing, sending, enrollment, subscription, unsubscription, and external record changes are mutating. | Central capability metadata and semantic catalog validation enforce the boundary. |
| ASM-007 | accepted | The Email demo proposes two writes (`newsletter.subscribe`, `crm.upsert-contact`) and creates one welcome-draft artifact. | Demo contract test fixes this graph. |
| ASM-008 | accepted | The Email demo dispatches neither write until valid approvals exist for both immutable actions. | One-of-two approval tests must show zero connector calls. |
| ASM-009 | accepted | The Community demo creates a reminder draft and recommended UTC time without calendar or message writes. | Demo ledger must show zero write calls. |
| ASM-010 | accepted | Partner and churn outputs are advisory and cannot make consequential decisions or contact people automatically. | Schemas, capability assignment, and UI labels enforce this. |
| ASM-011 | provisional | The loopback-only, single-user development principal may self-approve because the demo has no external identity provider. | Production or multi-user use requires a real identity/authorization design and separation-of-duties decision. |
| ASM-012 | accepted | Services bind to loopback, use deterministic mocks, and disable external network behavior by default. | Startup/session and no-egress tests enforce this. |
| ASM-013 | accepted | SQLite is the required acceptance database; PostgreSQL compatibility is optional contract coverage. | A deployment target may promote PostgreSQL to a required gate. |
| ASM-014 | provisional | V1 schedules use five-field cron plus an IANA timezone. | Revisit before adding seconds, calendars, or provider-native schedules. |
| ASM-015 | accepted | `run_once` coalesces missed schedule occurrences into one catch-up identified by the persisted due time. | Restart/misfire tests validate stable occurrence identity. |
| ASM-016 | accepted | `skip` records a miss and advances to the first future wall-clock occurrence without creating work. | Scheduler audit tests validate this. |
| ASM-017 | provisional | Unrecoverable internal failures may add transitions from active pre-execution states to `failed`; schema-invalid ingress is rejected before admission. | ADR-0004 and exhaustive transition tests must close this before runtime freeze. |
| ASM-018 | accepted | No real write connector ships in the accepted local release. | Any real write adapter requires security review and connector-contract acceptance. |
| ASM-019 | provisional | One optional real LLM adapter may exist behind independent provider and network opt-ins, but acceptance never calls it. | Revisit against current official provider APIs when implemented. |
| ASM-020 | accepted | Runtime IDs and timestamps may vary; normalized business outputs, action hashes, and mock call ledgers are deterministic. | Golden tests normalize only documented volatile fields. |
| ASM-021 | provisional | Bounded browser polling is sufficient for v1 run updates. | Revisit if product requirements require server push or higher concurrency. |
| ASM-022 | provisional | UI filters distinguish deployment state from recent-run state instead of overloading one status. | Browser tests assert both labels and semantics. |
| ASM-023 | accepted | Catalog prompts and schemas resolve local references only; remote references and path traversal are invalid. | Catalog compiler tests enforce containment. |
| ASM-024 | provisional | Persistence stores only the minimum redacted envelope needed for recovery, approval integrity, audit, and configured retention. | ADR-0010 and migrations must close field classes and TTLs before persistence freeze. |

## Known local-v1 limitations

- Local identity is a documented convenience principal, not enterprise authentication.
- SQLite files are not encrypted by the application; host filesystem access remains a local-demo risk.
- Deterministic mock receipts prove local idempotency behavior, not universal exactly-once delivery for future providers.
- No production deployment, production SLO, or live-provider behavior is claimed.
- The source frames do not explain Community duplication or authorize vendor-specific integrations.

## Review rule

Before a change adds a real provider, external write, new persisted sensitive field, lifecycle transition, identity mode, or schedule grammar, it must either fit an accepted item above or update this register and the relevant ADR first.
