# 02 — Catalog schema, seed, and validation

Status: planned

Depends on: [00 — Evidence and assumptions](00-source-evidence-scope-and-assumptions.md), [01 — Repository scaffold](01-repository-scaffold-and-toolchain.md)

Unblocks: domain selection, persistence seed, demos, catalog API, and org chart

## Objective

Create a version-controlled catalog that is the authoritative definition of the organization and its maximum runtime authority. The compiler must reject count drift, broken references, unsafe tool/policy combinations, schema errors, copied template data in instances, remote references, and invented Community variants before any database write occurs.

## Exact count contract

| Department | Functions | Templates | Instances |
|---|---:|---:|---:|
| Social media | 3 | 12 | 12 |
| Blog & SEO | 2 | 6 | 6 |
| Email | 2 | 5 | 5 |
| Community | 3 | 7 | 14 |
| Partnerships | 2 | 6 | 6 |
| **Total** | **12** | **36** | **43** |

Function-level assertions are also fixed:

| Function ID | Templates | Instances |
|---|---:|---:|
| `func.social-media.new-content` | 6 | 6 |
| `func.social-media.research` | 2 | 2 |
| `func.social-media.tracking-analysis` | 4 | 4 |
| `func.blog-seo.new-content` | 3 | 3 |
| `func.blog-seo.tracking-analysis` | 3 | 3 |
| `func.email.newsletter` | 2 | 2 |
| `func.email.lifecycle-marketing` | 3 | 3 |
| `func.community.events` | 3 | 6 |
| `func.community.education` | 3 | 6 |
| `func.community.discussion` | 1 | 2 |
| `func.partnerships.implementation-partners` | 5 | 5 |
| `func.partnerships.integration-partners` | 1 | 1 |

## Stable ID convention

Use globally unique, human-auditable IDs with a type prefix:

```text
Department  dept.social-media
Function    func.social-media.new-content
Template    tpl.social-media.new-content.linkedin-post-drafter
Instance    inst.social-media.new-content.linkedin-post-drafter.01
Capability  cap.social.read-posts
Policy      policy.human-approval.external-write.v1
Action type crm.upsert-contact
```

Rules:

- IDs are lowercase ASCII and use dots between namespaces and hyphens within slugs.
- Capability IDs use `cap.<family>.<operation-slug>`; action types use the same canonical family/operation grammar without the `cap.` prefix. For example, `cap.crm.upsert-contact` authorizes an action of type `crm.upsert-contact`.
- IDs never encode mutable vendor binding, geography, schedule, audience, or environment.
- All non-Community templates have one `.01` instance.
- All seven Community templates have `.01` and `.02` instances.
- The `.01`/`.02` suffix is an identity ordinal only; `variant_label` remains null unless source-backed configuration is later added.
- Display names remain exactly as provided in the prompt even if slugs use normalized wording.

## Complete template and instance inventory

The classification below is maximum v1 authority, not a claim about the source logos. Every role still supports manual dry-run.

| Template ID | Display name | Initial effect | Additional trigger intent | Instance IDs |
|---|---|---|---|---|
| `tpl.social-media.new-content.linkedin-post-drafter` | LinkedIn Post Drafter | read-only | manual | `.01` |
| `tpl.social-media.new-content.linkedin-comment-replier` | LinkedIn Comment Replier | read-only draft | manual, webhook | `.01` |
| `tpl.social-media.new-content.youtube-description-generator` | YouTube Description Generator | read-only | manual, webhook | `.01` |
| `tpl.social-media.new-content.youtube-script-generator` | YouTube Script Generator | read-only | manual | `.01` |
| `tpl.social-media.new-content.linkedin-post-writer-new-youtube-videos` | LinkedIn Post Writer for New YouTube Videos | read-only draft | manual, webhook | `.01` |
| `tpl.social-media.new-content.tweet-writer-new-youtube-videos` | Tweet Writer for New YouTube Videos | read-only draft | manual, webhook | `.01` |
| `tpl.social-media.research.linkedin-lead-enricher` | LinkedIn Lead Enricher | read-only/advisory | manual, webhook | `.01` |
| `tpl.social-media.research.linkedin-influencer-post-researcher` | LinkedIn Influencer Post Researcher | read-only | manual, schedule | `.01` |
| `tpl.social-media.tracking-analysis.linkedin-post-tracker` | LinkedIn Post Tracker | read-only | manual, schedule | `.01` |
| `tpl.social-media.tracking-analysis.linkedin-comment-helper` | LinkedIn Comment Helper | read-only/advisory | manual, webhook, schedule | `.01` |
| `tpl.social-media.tracking-analysis.tweet-tracker` | Tweet Tracker | read-only | manual, schedule | `.01` |
| `tpl.social-media.tracking-analysis.bluesky-monitor` | Bluesky Monitor | read-only | manual, schedule | `.01` |
| `tpl.blog-seo.new-content.blog-post-writer` | Blog Post Writer | read-only preparation | manual | `.01` |
| `tpl.blog-seo.new-content.blog-post-updater` | Blog Post Updater | read-only/advisory | manual, schedule | `.01` |
| `tpl.blog-seo.new-content.linkedin-post-writer-new-blog-posts` | LinkedIn Post Writer for New Blog Posts | read-only draft | manual, webhook | `.01` |
| `tpl.blog-seo.tracking-analysis.seo-ranking-tracker` | SEO Ranking Tracker | read-only | manual, schedule | `.01` |
| `tpl.blog-seo.tracking-analysis.feature-launch-tracker` | Feature Launch Tracker | read-only | manual, schedule | `.01` |
| `tpl.blog-seo.tracking-analysis.integration-tracker` | Integration Tracker | read-only | manual, schedule | `.01` |
| `tpl.email.newsletter.newsletter-subscriber` | Newsletter Subscriber | mutating | manual, webhook | `.01` |
| `tpl.email.newsletter.unsubscribe-assistant` | Unsubscribe Assistant | mutating | manual, webhook | `.01` |
| `tpl.email.lifecycle-marketing.customer-onboarder` | Customer Onboarder | mutating | manual, webhook | `.01` |
| `tpl.email.lifecycle-marketing.new-customer-tracker` | New Customer Tracker | read-only/advisory | manual, webhook, schedule | `.01` |
| `tpl.email.lifecycle-marketing.churned-user-monitor` | Churned User Monitor | read-only/advisory draft | manual, webhook, schedule | `.01` |
| `tpl.community.events.attendee-scheduler` | Attendee Scheduler | mutating | manual, webhook | `.01`, `.02` |
| `tpl.community.events.live-session-reminder` | Live Session Reminder | read-only draft in v1 | manual, schedule | `.01`, `.02` |
| `tpl.community.events.event-stats-tracker` | Event Stats Tracker | read-only | manual, schedule | `.01`, `.02` |
| `tpl.community.education.course-cohort-onboarder` | Course Cohort Onboarder | mutating | manual, webhook | `.01`, `.02` |
| `tpl.community.education.material-builder` | Material Builder | mutating when sharing | manual | `.01`, `.02` |
| `tpl.community.education.course-progress-reminders` | Course Progress Reminders | mutating when messaging | manual, schedule | `.01`, `.02` |
| `tpl.community.discussion.new-member-onboarder` | New Member Onboarder | mutating | manual, webhook | `.01`, `.02` |
| `tpl.partnerships.implementation-partners.partner-application-reviewer` | Partner Application Reviewer | read-only/advisory | manual, webhook | `.01` |
| `tpl.partnerships.implementation-partners.partner-tracker` | Partner Tracker | read-only | manual, schedule | `.01` |
| `tpl.partnerships.implementation-partners.partner-finder` | Partner Finder | read-only/advisory | manual | `.01` |
| `tpl.partnerships.implementation-partners.swag-tracker` | Swag Tracker | read-only tracking in v1 | manual, webhook | `.01` |
| `tpl.partnerships.implementation-partners.community-challenge-tracker` | Community Challenge Tracker | read-only calculation/tracking in v1 | manual, webhook, schedule | `.01` |
| `tpl.partnerships.integration-partners.integration-partner-tracker` | Integration Partner Tracker | read-only | manual, schedule | `.01` |

The instance ID is obtained by replacing the `tpl.` prefix with `inst.` and appending the listed ordinal. Tests should enumerate the full resolved IDs rather than relying only on arithmetic counts.

## Catalog file layout

```text
catalog/
├── schema/
│   ├── manifest.schema.json
│   ├── department.schema.json
│   ├── function.schema.json
│   ├── template.schema.json
│   ├── instance.schema.json
│   ├── tool-capability.schema.json
│   ├── approval-policy.schema.json
│   └── trigger.schema.json
└── v1/
    ├── manifest.yaml
    ├── departments.yaml
    ├── functions.yaml
    ├── tool-capabilities.yaml
    ├── approval-policies.yaml
    ├── templates/
    │   ├── social-media.yaml
    │   ├── blog-seo.yaml
    │   ├── email.yaml
    │   ├── community.yaml
    │   └── partnerships.yaml
    ├── instances/
    │   ├── social-media.yaml
    │   ├── blog-seo.yaml
    │   ├── email.yaml
    │   ├── community.yaml
    │   └── partnerships.yaml
    ├── prompts/
    │   └── <template-id>.md
    └── schemas/
        └── <template-id>/
            ├── input.schema.json
            └── output.schema.json
```

`manifest.yaml` fixes document order, catalog format version, content version, JSON Schema dialect, source-evidence paths, and a local-only reference policy.

## Template schema contract

Every template must define or resolve:

```yaml
id: tpl.email.newsletter.newsletter-subscriber
display_name: Newsletter Subscriber
department_id: dept.email
function_id: func.email.newsletter
display_order: 10
purpose: Add new website signups to the configured newsletter system.
system_prompt_ref: prompts/tpl.email.newsletter.newsletter-subscriber.md
input_schema_ref: schemas/tpl.email.newsletter.newsletter-subscriber/input.schema.json
output_schema_ref: schemas/tpl.email.newsletter.newsletter-subscriber/output.schema.json
allowed_tool_capability_ids:
  - cap.newsletter.subscribe
supported_trigger_types:
  - manual
  - webhook
operation_classification: mutating
approval_policy_id: policy.human-approval.external-write.v1
retry_policy: { max_attempts: 2, backoff: bounded_exponential }
timeout_policy: { step_seconds: 30, run_seconds: 120 }
budget_policy: { max_steps: 5, max_model_calls: 1, max_tool_calls: 1 }
rate_limit_policy: { scope: template, max_calls: 20, window_seconds: 60 }
source_confidence: high
source_references: [IMPLEMENTATION_PROMPT.md#authoritative-catalog]
implementation_notes: Connector binding is an implementation choice; source names Loops only as chart evidence.
```

Policy numbers above are illustrative until the catalog ADR fixes them; every value must be finite, positive, and inside global safety ceilings.

Template requirements:

- `purpose` preserves source intent; system instructions add safety without changing the role.
- Input/output schemas use JSON Schema Draft 2020-12 and set `additionalProperties: false` on objects unless explicitly justified.
- Schemas include stable `$id`, `title`, descriptions, size bounds, and sensitivity metadata such as `x-sensitive`.
- Tool IDs resolve through the central capability registry.
- `operation_classification` is `read_only` or `mutating` and represents maximum allowed effect.
- Every write capability requires a human-approval policy.
- Retry, timeout, budget, and rate limit values may be stricter per template but never exceed global ceilings.
- Source confidence and implementation notes are separate fields.

## Instance schema contract

Instances contain deployment data only:

```yaml
id: inst.community.events.attendee-scheduler.01
template_id: tpl.community.events.attendee-scheduler
display_order: 10
enabled: true
variant: { source_ordinal: 1 }
trigger_bindings: []
connector_bindings: {}
schedule: null
configuration_revision: 1
```

Forbidden instance fields include display name, purpose, system prompt, input/output schema, tool allowlist, classification, retry/timeout/budget policy, and approval policy. Those values always resolve through the referenced template.

## Capability and approval-policy catalogs

The capability registry must be vendor-neutral and operation-specific. Initial categories include:

- Model/artifact: structured generation and deterministic transformation.
- Social: read posts/comments/metrics; no publish capability assigned in v1.
- Newsletter/email: subscribe, unsubscribe, and optional send command.
- CRM: read customer and upsert contact.
- CMS: read content; upload remains unassigned unless a later approved workflow needs it.
- Calendar/events: read sessions and enroll attendee.
- Community/messaging: read membership and send message.
- Spreadsheets: read range and update rows/points.
- Fulfillment: read status. A create-fulfillment command may exist as a reserved future capability but is assigned to no v1 template.

Each capability declares:

- Stable ID and description.
- `read` or `write` effect.
- Typed request/result schema references.
- Connector family.
- Whether provider idempotency is required, supported, or unavailable.
- Default timeout and data-classification metadata.

Initial approval policies:

- `policy.no-approval.read-only.v1` for read-only/artifact work.
- `policy.human-approval.external-write.v1` for every write.
- Optional stricter policy fields for required roles/scopes, expiry, and self-approval behavior.

The compiler rejects a write capability under a no-approval policy regardless of template classification text.

## Prompt and schema authoring sequence

For each template:

1. Copy the source-backed purpose without adding hidden integrations.
2. Define a narrow input object from data explicitly supplied by manual/webhook/schedule workflows.
3. Define a structured output artifact; advisory roles must include advisory status, evidence, uncertainty, and follow-up fields.
4. Add content-size, array-length, string-length, enum, URL, date-time, and required-field bounds.
5. Mark sensitive fields explicitly.
6. Write system instructions that separate trusted policy from untrusted content and prohibit tool selection.
7. Assign only necessary capabilities.
8. Assign triggers as implementation metadata and explain non-obvious choices.
9. Assign effect and approval policy.
10. Add positive, negative, boundary, and injection-like fixtures.

## Catalog compiler

Planned modules:

```text
apps/api/src/marketing_agents/infrastructure/catalog/models.py
apps/api/src/marketing_agents/infrastructure/catalog/loader.py
apps/api/src/marketing_agents/infrastructure/catalog/reference_resolver.py
apps/api/src/marketing_agents/infrastructure/catalog/validator.py
apps/api/src/marketing_agents/infrastructure/catalog/compiler.py
apps/api/src/marketing_agents/infrastructure/catalog/hash.py
apps/api/src/marketing_agents/infrastructure/catalog/seeder.py
apps/api/src/marketing_agents/workers/catalog_cli.py
```

Compilation pipeline:

1. Load the manifest from an explicit catalog root.
2. Reject files not listed in the manifest when strict mode is enabled.
3. Validate each raw YAML/JSON document against its structural schema.
4. Resolve only normalized paths inside the catalog root.
5. Reject remote `$ref`, URI schemes, absolute paths, symlink escapes, and traversal.
6. Parse prompts as inert text, never templates with executable code.
7. Compile every input/output and capability schema.
8. Resolve department, function, template, capability, policy, and trigger references.
9. Enforce semantic safety constraints and maximum global budgets.
10. Enforce exact counts, function distribution, department totals, stable order, and Community multiplicity.
11. Ensure every non-Community template has exactly one instance and every Community template exactly two.
12. Ensure instances contain no template-owned fields.
13. Canonicalize the fully resolved catalog and compute a versioned SHA-256 digest.
14. Return one immutable normalized catalog object or a complete path-aware error report.

## Semantic validation failures

The compiler must fail on:

- Duplicate IDs or display-order collisions within a sibling group.
- Missing or cross-department function references.
- Missing template/capability/policy/schema/prompt references.
- An instance with zero or multiple template references.
- Instance-owned copies of template fields.
- Unknown or unsupported trigger values.
- Write capability plus no-approval policy.
- Read-only template plus a write capability.
- Unbounded retry, timeout, array, content, or graph values.
- Remote or escaping file/schema references.
- Invalid JSON Schema or unsupported UI schema constructs.
- Missing sensitivity annotations on known PII fields.
- Count or Community multiplicity drift.
- Vendor binding asserted only from a frame logo.

## Seed behavior

- Validation and compilation occur completely in memory before opening a transaction.
- Catalog-controlled departments, functions, policies, capabilities, templates, and instance identities are upserted by stable ID and catalog hash.
- Local mutable instance configuration lives in separate rows and is inserted only when missing.
- Reseeding never overwrites local enabled state, connector binding, trigger parameters, or schedule configuration.
- Missing records are never silently deleted. Retirement requires an explicit later policy.
- `seed --check` performs a no-write drift comparison.
- A `catalog_releases` row records version, hash, imported timestamp, and source manifest.

## Ordered implementation tasks

1. Freeze catalog format/version, stable ID rules, display order, and local-reference policy in ADR-0002.
2. Author the structural schemas for manifest, department, function, capability, policy, template, instance, and trigger documents.
3. Seed departments, functions, capability definitions, and approval policies before role files.
4. Author the five departmental template files with all 36 exact display names/purposes and initial classifications; generate a golden authoritative inventory of ID, display name, department, function, display order, and source purpose.
5. Author one prompt plus input/output schema pair for every template, including bounds and sensitivity annotations.
6. Author the five instance files, using one instance for every non-Community template and `.01`/`.02` for each Community template.
7. Implement loader, confined reference resolver, schema compiler, semantic validator, canonicalizer, and catalog hash.
8. Add exact-count, distribution, uniqueness, multiplicity, completeness, schema-fixture, and safety-policy tests.
9. Implement the transactional database seeder and no-write drift check only after in-memory validation is green.
10. Prove repeat seeding preserves local deployment configuration and record the initial catalog hash/version.

## Tests

```text
tests/catalog/test_structural_schemas.py
tests/catalog/test_reference_resolution.py
tests/catalog/test_exact_counts.py
tests/catalog/test_function_distribution.py
tests/catalog/test_stable_ids.py
tests/catalog/test_community_multiplicity.py
tests/catalog/test_template_completeness.py
tests/catalog/test_instance_field_ownership.py
tests/catalog/test_schema_compilation.py
tests/catalog/test_schema_fixtures.py
tests/catalog/test_capability_policy_safety.py
tests/catalog/test_catalog_hash.py
tests/catalog/test_authoritative_inventory.py
tests/integration/catalog/test_seed_idempotency.py
tests/integration/catalog/test_local_config_preserved.py
```

High-value assertions:

- Exactly `5/12/36/43` globally and the exact per-function matrix above.
- Exactly seven Community template IDs, each referenced by `.01` and `.02`.
- No two instance IDs collide.
- Every template ID maps to the exact source display name, department, function, display order, and purpose in the golden inventory.
- Every template has all required fields and both schemas compile.
- Every positive/negative schema fixture behaves as expected.
- All write capabilities resolve to human approval.
- Compiling twice yields identical normalized output and hash.
- Seed twice changes no operational rows.
- A locally disabled instance remains disabled after reseed.

## Planned commands

```text
make catalog-validate
make catalog-fixtures
make seed
uv run marketing-agents catalog show-counts
uv run marketing-agents catalog seed --check
```

## Exit criteria

- The complete 36-template and 43-instance source inventory exists.
- Exact global, department, and function counts pass.
- Community duplication is represented only at the instance layer.
- All prompts, schemas, capabilities, triggers, classifications, policies, budgets, and notes validate.
- Catalog compilation is deterministic and offline.
- Remote/path-escaping references are rejected.
- Seed is transactional, repeatable, and preserves local deployment configuration.
- Catalog validation passes before runtime implementation proceeds.
