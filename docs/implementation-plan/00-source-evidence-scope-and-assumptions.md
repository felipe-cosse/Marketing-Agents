# 00 — Source evidence, scope, and assumptions

Status: planned

Depends on: none

Unblocks: every other plan

## Objective

Establish a durable evidence hierarchy before implementation so the team can distinguish source-backed catalog facts from implementation choices. This prevents logo-driven integrations, invented explanations for duplicate Community cards, and accidental expansion into unofficial scraping or live automation.

## Evidence inventory

| Evidence | What it establishes | What it does not establish |
|---|---|---|
| `IMPLEMENTATION_PROMPT.md` | Exact catalog, architecture constraints, runtime lifecycle, safety rules, deliverables, and acceptance criteria | No implemented code or runtime evidence |
| `references/linkedin-ai-agents-org-chart-overview.png` | Centered Marketing Agents root; five-department order; 12 function groups; broad single-canvas layout | Exact connector contracts, schedules, credentials, workflow edges |
| `references/linkedin-ai-agents-org-chart-social-media.png` | Social media function grouping, card ordering, titles, and concise role intent | Permission to scrape LinkedIn, YouTube, X, or Bluesky |
| `references/linkedin-ai-agents-org-chart-blog-seo.png` | Blog & SEO hierarchy and card order | A live CMS, search provider, or crawling implementation |
| `references/linkedin-ai-agents-org-chart-email.png` | Email hierarchy and source-visible role intent | Credentials, CRM/newsletter vendor contract, or permission to send |
| `references/linkedin-ai-agents-org-chart-community.png` | Seven role templates shown twice, producing 14 deployed cards | Why the duplicates exist or how their deployments differ |
| `references/linkedin-ai-agents-org-chart-partnerships.png` | Partnership hierarchy and advisory role intent | Automated partner decisions, fulfillment, or marketplace scraping |
| LinkedIn source-post URL in the prompt | Provenance pointer for the captured frames | A license to scrape or infer details not visible in local evidence |

## Visual observations to preserve

- One visible `Marketing Agents` root.
- Departments ordered left-to-right: Social media, Blog & SEO, Email, Community, Partnerships.
- Functions grouped under their department and agents grouped under their function.
- A wide, pannable hierarchy with thin orthogonal connectors.
- Pale blue-gray canvas, white rounded hierarchy labels, and dense but readable role cards.
- Card information hierarchy: neutral icon area, display name, concise purpose, capability/status metadata.
- Both occurrences of every Community role remain visibly present.

## Visual elements not to copy

- Third-party product logos.
- Tiny vendor badges as asserted connector bindings.
- The source-frame watermark.
- Brand colors or trade dress that imply an official integration.
- Pixel-perfect framing or clipping from the captured video.

Use neutral internal capability icons and text badges such as `social.read`, `crm.write`, or `calendar.read`. The source should influence hierarchy and information density, not branding.

## Evidence precedence

When evidence conflicts or is ambiguous, apply this order:

1. Explicit acceptance criteria in the implementation prompt.
2. Explicit technical and safety requirements in the prompt.
3. The authoritative catalog text in the prompt.
4. Clearly visible hierarchy, names, order, and role intent in the frames.
5. Documented implementation assumptions.

Never use a small logo, clipped neighboring card, or visual coincidence to override explicit prompt text.

## In scope

- A local-first backend, frontend, and persistent worker foundation.
- An exact catalog of 5 departments, 12 functions, 36 templates, and 43 instances.
- Interactive hierarchy plus configuration, simulation, approval, run, artifact, and audit surfaces.
- Deterministic model and connector mocks.
- Manual, webhook, and schedule trigger foundations.
- Explicit DAG planning, typed artifacts, bounded execution, retry, cancellation, and audit.
- Action-scoped approvals and idempotent mock external actions.
- Five required department demos.
- SQLite default with a configurable database URL and portable boundaries.
- One-command local startup, tests, CI, and required documentation.

## Out of scope for the first accepted release

- Production deployment, production SLO claims, or measured production results.
- Live publishing, messaging, enrollment, unsubscription, CRM/CMS/calendar mutation, or fulfillment.
- Unofficial scraping, browser automation, or reverse-engineered platform APIs.
- A general autonomous multi-agent chat system.
- Model-selected tools or open-ended agent-to-agent conversations.
- Multi-tenant identity, enterprise SSO, or production-grade authorization infrastructure.
- Distributed exactly-once guarantees for future connectors that lack provider idempotency.
- A reason for Community duplication.
- Vendor-specific UI branding.

## Decision and assumption register

Every item below must also appear in `docs/assumptions.md` or an ADR when implementation begins.

| ID | Decision or assumption | Rationale | Validation or follow-up |
|---|---|---|---|
| ASM-001 | The repository is greenfield and the prompt's default stack applies. | No established code or tooling exists. | Recheck before scaffolding in case files were added after this plan. |
| ASM-002 | Prompt text takes precedence over tiny or ambiguous frame details. | Frames are source evidence, not a full technical specification. | Record any discrepancy rather than silently choosing. |
| ASM-003 | Community `.01` and `.02` identify source occurrences only. | The source supplies no business differentiation. | Both instances share one template; no invented variant label. |
| ASM-004 | The Marketing Orchestrator is not a 44th instance. | The prompt explicitly calls it a control plane. | Exclude it from catalog count queries and UI instance counts. |
| ASM-005 | Department/function nodes are grouping boundaries, not LLM agents. | The prompt explicitly forbids automatic agent interpretation. | Planner may route through them but never invoke them. |
| ASM-006 | Drafting is read-only; publishing, sending, enrollment, subscription, unsubscription, external writes, and representational actions are mutating. | This is the conservative effect boundary required by the prompt. | Enforce through central capability metadata and catalog semantic checks. |
| ASM-007 | The Email demo uses two writes and one artifact. | It covers subscriber and onboarding intent without sending mail. | Propose `newsletter.subscribe`, propose `crm.upsert-contact`, generate a welcome draft. |
| ASM-008 | All Email approvals must exist before either write dispatches. | This gives a simple, provable zero-call approval boundary. | Test one-of-two approval still yields zero calls. |
| ASM-009 | The Community demo creates a reminder draft and recommended UTC time only. | The required artifact can be demonstrated without calendar or messaging writes. | Assert zero calendar/messaging connector calls. |
| ASM-010 | Partner recommendations remain advisory. | The prompt forbids consequential automatic decisions. | Schema and UI must label the result advisory and expose evidence/confidence. |
| ASM-011 | The local identity provider may allow self-approval. | Credential-free single-user demo; prompt does not require separation of duties. | Make the policy configurable and label it as a demo limitation. |
| ASM-012 | Default services bind to loopback and use mocks with external network disabled. | Reduces accidental exposure and live calls. | Startup test must reject unsafe production/local-auth combinations. |
| ASM-013 | SQLite is the required acceptance database. | Zero configuration is explicit. | Add PostgreSQL contract coverage only as an optional compatibility job. |
| ASM-014 | Five-field cron plus IANA timezone is the v1 schedule format. | It is sufficient for the required persistent scheduler. | Validate supported grammar and document DST behavior. |
| ASM-015 | `run_once` coalesces missed occurrences into one catch-up based on the persisted due time. | Prevents restart storms while preserving a stable occurrence ID. | Test missed-range audit metadata and next-future calculation. |
| ASM-016 | `skip` records the miss and advances to the first future wall-clock occurrence. | It makes the misfire choice explicit and auditable. | Test no run is created for the missed occurrence. |
| ASM-017 | The required lifecycle may be extended with failure transitions from active pre-execution states. | Validation/planning system errors need an auditable terminal outcome. | Decide in ADR-004; retain and test every required transition. |
| ASM-018 | Real write connectors do not ship in the accepted local release. | The objective is a safe foundation and deterministic demos. | A later connector must pass contract/security review before registration. |
| ASM-019 | One optional real LLM adapter may ship behind two explicit opt-ins. | Demonstrates the seam without weakening offline defaults. | No live provider call is part of acceptance; use fake transport tests. |
| ASM-020 | Runtime IDs and timestamps may vary; deterministic business fields may not. | Tests need stable semantic outputs without freezing all metadata. | Golden tests normalize volatile metadata. |
| ASM-021 | Browser polling is sufficient for v1. | Real-time sockets are not required for the control surface. | Use bounded polling and stop when a run becomes terminal. |
| ASM-022 | UI status filters separate deployment state from recent run state. | “Status” is otherwise ambiguous. | Label filters explicitly and test both. |
| ASM-023 | Catalog prompts and schemas use local references only. | Prevents remote resolution and keeps validation offline. | Reject remote `$ref` values and path traversal. |
| ASM-024 | The application stores only the minimum recoverable execution envelope. | Approval and restart recovery need persistence, but logs/audit must be redacted. | Define schema-aware sensitivity metadata and retention before freezing migrations. |

## ADRs to create before broad implementation

| ADR | Decision |
|---|---|
| `docs/adr/0001-stack-and-monorepo.md` | Default stack, package managers, and monorepo boundaries |
| `docs/adr/0002-catalog-as-versioned-authority.md` | YAML/JSON Schema layout and database projection |
| `docs/adr/0003-database-backed-workers.md` | API, run worker, scheduler worker, and no broker |
| `docs/adr/0004-run-lifecycle-failure-semantics.md` | Required transitions plus pre-execution failure handling |
| `docs/adr/0005-action-scoped-approval.md` | Canonical action hash, one-time approval, and all-approvals barrier |
| `docs/adr/0006-local-identity.md` | Loopback-only fixed principal, roles, and self-approval policy |
| `docs/adr/0007-external-action-delivery.md` | Idempotency support, crash window, and `outcome_unknown` |
| `docs/adr/0008-schedule-misfires-and-dst.md` | Cron grammar, timezone rules, `skip`, and `run_once` |
| `docs/adr/0009-frontend-hierarchy-layout.md` | Canvas library, deterministic layout, and semantic fallback |
| `docs/adr/0010-data-retention.md` | Sensitivity classes, TTLs, deletion/pseudonymization, and audit skeleton |

## Ordered implementation tasks

1. Copy this register into the product assumptions document and assign an owner/status field.
2. Write ADRs 0001–0008 before the affected modules are implemented.
3. Build a source-evidence table that maps each department/function/template to the prompt and relevant frame.
4. Preserve display order explicitly in catalog files; never derive source order alphabetically.
5. Add a catalog field that separates `source_confidence` from `implementation_notes`.
6. Add a UI visual note explaining that capability badges are implementation metadata, not copied vendor affiliations.
7. Add traceability IDs to tests and documentation where practical.
8. Review every new connector or workflow assumption against this register before merging.

## Exit criteria

- All source files have been inspected and recorded.
- Every catalog role has an evidence row.
- Community duplicates are described without a fabricated business explanation.
- In-scope and out-of-scope boundaries are accepted.
- Every material assumption has an ID and planned documentation destination.
- ADRs required for the next milestone are approved.
- No implementation plan treats a vendor logo as a connector requirement.
