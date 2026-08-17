# 11 — Deterministic department demos

Status: planned

Depends on: catalog, orchestration, approval/action dispatcher, adapters, API

Unblocks: end-to-end acceptance, product documentation, and browser smoke tests

## Objective

Implement one explicit, schema-valid, provenance-linked scenario for each department. The demos prove control-plane behavior through deterministic mocks; they do not imply live publishing, delivery, CRM changes, web crawling, or production outcomes.

## Shared demo contract

Every scenario provides:

- Stable scenario/workflow ID and version.
- Selected template/instance IDs.
- Input JSON Schema and a committed example fixture.
- Explicit DAG and capability/effect list.
- Output artifact schema(s).
- Expected state path.
- Expected model/connector call counts.
- Deterministic business fields and normalized volatile fields.
- Complete provenance requirements.
- UI preset and documented API/CLI command.

Planned paths:

```text
apps/api/src/marketing_agents/demos/
├── registry.py
├── social_content_draft.py
├── blog_content_review.py
├── email_signup.py
├── community_reminder_draft.py
└── partnership_application_review.py

tests/fixtures/demos/
├── social-content-draft.json
├── blog-content-review.json
├── email-signup.json
├── community-reminder-draft.json
└── partnership-application-review.json
```

## Demo 1 — Social media content draft

### Mapping

- Scenario ID: `demo.social-media.content-draft.v1`
- Instance: `inst.social-media.new-content.linkedin-post-drafter.01`
- Effect: read-only/artifact generation
- Expected state: `received -> validated -> planned -> executing -> completed`
- Connector calls: zero
- Model calls: one deterministic mock call

### Input

- Content idea.
- Audience.
- Tone enum.
- Key points with bounded length/count.
- Optional call-to-action.
- Optional supplied source URLs as provenance only; no fetch occurs.

### DAG

1. Validate and normalize idea/constraints.
2. Generate structured draft with deterministic mock model.
3. Validate output schema.
4. Create `social_post_draft` artifact with provenance.

### Output artifact

- Platform label.
- Draft text.
- Hashtag list.
- Character count computed by deterministic transform.
- CTA summary.
- Source/reference summary.
- Safety/assumption notes.

Never call a social publish connector. UI buttons say `Create draft`, not `Publish`.

## Demo 2 — Blog & SEO content review

### Mapping

- Scenario ID: `demo.blog-seo.content-review.v1`
- Instance: `inst.blog-seo.new-content.blog-post-updater.01`
- Effect: read-only/advisory
- Expected state: direct read-only completion
- Connector calls: zero
- Model calls: one deterministic mock call

### Input

- Article title.
- Canonical URL for reference, validated but not fetched.
- Supplied excerpt/summary.
- Last-updated timestamp.
- Target keywords.
- Current feature/integration metadata supplied in the fixture.

### DAG

1. Validate article metadata and bounded supplied content.
2. Deterministically calculate age/staleness inputs.
3. Generate structured SEO/content review.
4. Validate and persist `content_review` artifact.

### Output artifact

- Staleness level and evidence.
- SEO findings with severity.
- Content gaps.
- Prioritized recommendations.
- Target keyword coverage.
- Source references and assumptions.

No crawling, search-engine query, CMS update, or upload occurs. Documentation must say the review uses supplied metadata.

## Demo 3 — Email signup approval boundary

### Mapping

- Scenario ID: `demo.email.signup-onboarding.v1`
- Instances:
  - `inst.email.newsletter.newsletter-subscriber.01`
  - `inst.email.lifecycle-marketing.customer-onboarder.01`
- Writes:
  - `cap.newsletter.subscribe`
  - `cap.crm.upsert-contact`
- Canonical action types:
  - `newsletter.subscribe`
  - `crm.upsert-contact`
- Read-only result: welcome-message draft artifact
- Approval policy: one exact approval per write, all approved before any call

### Input

- Synthetic demo contact ID.
- Name and email marked sensitive.
- Newsletter/list binding selected from registered mock configuration.
- Consent/source metadata.
- Signup timestamp.
- Bounded welcome context.

### Planning DAG

```text
validate signup
  ├── propose newsletter.subscribe action
  ├── propose crm.upsert-contact action
  └── define welcome-draft step

all exact approvals valid
  ├── dispatch newsletter.subscribe through mock
  ├── dispatch crm.upsert-contact through mock
  └── generate welcome draft

all three results -> onboarding summary artifact
```

The action payloads are constructed deterministically during planning from validated typed input; planning performs no provider or connector call.

### Required pre-approval proof

After workers drain planning:

- Run state is `awaiting_approval`.
- Two immutable external actions exist.
- Two approval requests exist with distinct action IDs/hashes.
- Newsletter mock call count is zero.
- CRM mock call count is zero.
- Model call count is zero if welcome generation is deliberately held behind the barrier.
- Timeline includes `received`, `validated`, `planned`, action proposals, approval requests, and `awaiting_approval`.

Approving only one action must leave both connector counts at zero.

### Required post-approval behavior

After one authorized decision for each unchanged action:

- Run transitions to `executing` asynchronously.
- Newsletter mock receives the approved payload and stable idempotency key once.
- CRM mock receives the approved payload and stable idempotency key once.
- Deterministic model creates a welcome-message draft; it does not send mail.
- Final `email_onboarding_summary` references both receipts and the welcome artifact.
- Run completes with every approval/action transition in the timeline.

### Negative demonstrations

- Unauthorized actor.
- Expected-hash mismatch.
- Payload/destination change.
- Expired approval.
- Rejected action.
- Reused/second decision.
- Crash after mock receipt but before local success.
- Cancellation before the atomic authorization barrier releases: no connector call occurs and the run ends in `cancelled`. If cancellation races after the barrier has consumed approvals and reserved the authorization set but before an individual connector dispatch, record the race explicitly and follow the reserved-action recovery rules in `06-approvals-external-actions-and-idempotency.md`; do not claim that the multi-action operation was rolled back.

Every case produces zero or one side effect exactly as its state permits; no failure path sends a welcome message.

## Demo 4 — Community reminder draft

### Mapping

- Scenario ID: `demo.community.reminder-draft.v1`
- Instance: `inst.community.events.live-session-reminder.01`
- Effect: read-only draft
- Expected state: direct completion
- Connector calls: zero calendar, event, and messaging calls
- Model calls: one deterministic mock call

### Input

- Event ID/name.
- Stable signup event ID, admitted source, and signup timestamp.
- Session local start date/time and IANA timezone.
- Reminder offset.
- Attendee-safe display name or synthetic identifier.
- Channel label as drafting context only.
- Bounded event details.

### DAG

1. Validate event/timezone/offset.
2. Calculate recommended reminder time in UTC.
3. Generate reminder draft.
4. Validate and persist `scheduled_reminder_draft` artifact.

### Output artifact

- Draft subject/title and message.
- Original session timezone/time.
- Recommended send time in UTC.
- Channel label.
- Source/input references.
- Signup event/source/admitted timestamp provenance.
- Explicit `not_sent` and `not_externally_scheduled` status.

This demo does not use Attendee Scheduler, add an attendee, mutate a calendar, create a provider schedule, or send. Instance `.02` remains visible but receives no invented specialty.

The persistent scheduler is demonstrated separately by scheduler acceptance tests; a later local schedule may trigger this read-only workflow without turning the artifact into a send action.

## Demo 5 — Partnership application review

### Mapping

- Scenario ID: `demo.partnerships.application-review.v1`
- Instance: `inst.partnerships.implementation-partners.partner-application-reviewer.01`
- Effect: read-only advisory recommendation
- Expected state: direct completion
- Connector calls: zero
- Model calls: one deterministic mock call

### Input

- Applicant ID and supplied organization metadata.
- Declared capabilities and regions.
- Supplied evidence records.
- Program criteria/constraints.
- Missing-information indicators.

### DAG

1. Validate and normalize the supplied application.
2. Apply deterministic evidence/risk extraction.
3. Generate structured advisory recommendation.
4. Validate and persist `partner_review_recommendation` artifact.

### Output artifact

- Recommendation: `accept`, `reject`, or `needs_information`.
- Advisory-only flag set to true.
- Evidence-linked rationale.
- Confidence and uncertainty.
- Risks/concerns.
- Missing information and follow-up questions.
- Explicit no-automatic-decision note.

No external applicant research, scraping, partner-record mutation, notification, or automated acceptance/rejection occurs.

## Provenance required for every demo artifact

- Scenario/workflow ID and version.
- Work item and admitted-input digest.
- Run and producing step.
- Template and instance IDs.
- Catalog version/hash and instance config revision.
- Input/parent artifact references.
- Mock provider/connector versions.
- Output schema ID/version and payload hash.
- Timestamp and sensitivity classification.

## Demo API and UI

`GET /api/v1/demo-scenarios` returns safe presets, expected behavior, selected instances, effect/approval summary, and JSON Schema.

`POST /api/v1/demo-scenarios/{id}/runs` accepts an idempotency key and optional fixture overrides that remain schema-valid.

The UI:

- Clearly labels deterministic mock mode.
- Shows selected agents and expected writes before submission.
- Uses safe verbs: draft, review, simulate, propose, approve.
- Links to the run timeline and artifacts.
- For Email, exposes the two separate approvals and zero-call status.
- Never displays a mock receipt as real delivery/CRM evidence.

## Determinism rules

- Canonical input + scenario version + mock version yields identical business fields.
- Runtime IDs, correlation IDs, and timestamps may differ.
- Golden assertions normalize volatile metadata.
- Array/order behavior is explicit.
- No result depends on system locale, wall clock, network, random seed, or database row order unless injected and fixed.

## Test matrix

| Demo | Schema valid | Provenance | Direct completion | Approval required | External mock calls |
|---|---|---|---|---|---|
| Social | Yes | Yes | Yes | No | 0 |
| Blog & SEO | Yes | Yes | Yes | No | 0 |
| Email | Yes | Yes | After approval | Two actions | Exactly 2 writes after both approvals |
| Community | Yes | Yes | Yes | No | 0 |
| Partnerships | Yes | Yes | Yes | No | 0 |

Planned tests:

```text
tests/acceptance/test_social_demo.py
tests/acceptance/test_blog_demo.py
tests/acceptance/test_email_demo.py
tests/acceptance/test_community_demo.py
tests/acceptance/test_partnerships_demo.py
tests/acceptance/test_demo_determinism.py
tests/acceptance/test_demo_provenance.py
```

## Ordered implementation tasks

1. Freeze scenario IDs, selected instances, schemas, fixtures, and expected call counts.
2. Register workflow definitions and validate DAGs against the catalog.
3. Implement deterministic fixture/model renderers.
4. Implement read-only Social, Blog, Community, and Partnerships flows first.
5. Implement Email action construction and two-approval barrier.
6. Add final artifact/provenance composition.
7. Expose demo discovery/run API.
8. Add UI presets and run/approval/timeline links.
9. Add golden, negative, crash, and no-network tests.
10. Document exact commands and what each demo does not do.

## Exit criteria

- All five scenario definitions and fixtures are version-controlled.
- Every output validates and has complete provenance.
- Four read-only demos complete without connector writes.
- Email first reaches awaiting approval with zero model/connector calls, remains at zero after one of two approvals, and calls each mock once after both.
- Email rejection, expiry, reuse, tamper, unauthorized actor, cancellation, and crash cases pass.
- Community status explicitly says not sent/not externally scheduled.
- Partnership output is visibly advisory.
- All demos are deterministic and perform no external network call.
