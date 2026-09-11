# Five deterministic local demos

These walkthroughs describe **deterministic mock behavior** implemented in the
[scenario registry](../apps/api/src/marketing_agents/demos/registry.py) and
[Demos page](../apps/web/src/features/demos/DemosPage.tsx). Start the complete local
stack using [operations](operations.md), then choose **Demos** in the navigation.
Do not supply real personal data or provider credentials. Live execution is
disabled; CI was paused at the operator's request on 2026-09-10. Current results
and limitations belong in the [verification record](verification.md), not in the
scenario's displayed expectations.

## Shared walkthrough and evidence boundary

1. Select the named scenario. Its API-declared schema and safe preset populate
   the form. The UI rejects unsupported or drifted scenario contracts instead of
   offering a guessed workflow.
2. Keep the preset for a repeatable walkthrough. **Reset safe preset** restores
   it; required fields, arrays, nested fields, bounds, and server errors are
   handled by the schema-driven form.
3. Select that scenario's submit button. An accepted receipt identifies the work
   and run; it proves intake, not execution, completion, delivery, or call counts.
4. Follow **Open accepted run**, **Open timeline**, or **Open artifacts**. Wait for
   the authoritative run's state; then open the resulting artifact to inspect
   its schema, digest, producer, source provenance, and mock-provider information.
5. For Email only, follow **Open approval queue** to review the two separately
   authorized actions as described below. The other four scenarios need no
   external-action approval because they only produce local drafts/recommendations.

**Stop waiting** aborts the browser's wait for an intake receipt; it does not
cancel the server run. The UI instructs you to retry without editing to recover
the idempotent receipt. Editing/resetting, reopening the page, or submitting a
new request is not a promise to reuse the previous run: inspect **Runs & audit**
before creating more work.

The following counts are contractual expectations, not live metrics read from
the current installation. Each read-only scenario selects one source instance;
Email selects the Newsletter Subscriber and Customer Onboarder.

| Completed scenario | Mock model calls | Mock connector calls | Proposed actions / required approvals |
| --- | --- | --- | --- |
| Social content draft | 1 | 0 | 0 / 0 |
| Blog & SEO content review | 1 | 0 | 0 / 0 |
| Email signup onboarding | 1 | 2, one per action | 2 / 2 |
| Community reminder draft | 1 | 0 | 0 / 0 |
| Partnership application review | 1 | 0 | 0 / 0 |

Read-only completion follows `received → validated → planned → executing → completed`.
Email adds `awaiting_approval` before execution. Invalid input, rejection,
cancellation, and terminal failure are not successful completion.

## Social content draft

Scenario ID: `demo.social-media.content-draft.v1`. Select **Social content draft**;
the page heading is **Social idea to draft artifact**.

- Input: `idea`, `audience`, `tone`, and `key_points`; optional `call_to_action`
  and `source_urls`. The preset discusses governed AI workflows for marketing
  and platform leaders, with professional tone and three key points.
- Action: keep the preset and select **Create draft**. Follow **Open artifacts**
  after the accepted run completes.
- Result: a schema-valid `social_post_draft` with reviewable text and source
  provenance. Reference URLs are input context, not instructions to fetch pages.
- Boundary: no social connector, external write, approval, or LinkedIn publication.
  Review the draft manually before considering any separate real publishing work.

**Implemented and verified (historical mock evidence):**
[DEMO-01 record](verification/requirements/DEMO-01.md),
[acceptance tests](../tests/acceptance/test_social_demo.py), and
[browser intake tests](../apps/web/e2e/demo-01-social-draft.spec.ts).

## Blog & SEO content review

Scenario ID: `demo.blog-seo.content-review.v1`. Select **Blog & SEO content review**;
the heading is **Blog metadata to SEO/content review**.

- Input: `article_title`, `canonical_url`, `supplied_excerpt`, `last_updated_at`,
  `assessment_at`, `target_keywords`, and `current_product_metadata` containing
  `features` and `integrations`. The preset article is “Governed AI workflows
  for marketing teams,” last updated 2025-12-01 and assessed at the supplied
  fixed time 2026-08-31. It does not silently use today's date.
- Action: keep the supplied metadata and select **Create review**. Open the run's
  artifacts after completion.
- Result: a `content_review` containing keyword coverage, content/metadata gaps,
  and recommendations based only on supplied evidence.
- Boundary: the canonical URL is provenance text and is never fetched. No
  crawling, search-ranking measurement, CMS update, upload, or automatic release.

**Implemented and verified (historical mock evidence):**
[DEMO-02 record](verification/requirements/DEMO-02.md),
[acceptance tests](../tests/acceptance/test_blog_seo_demo.py), and
[browser intake tests](../apps/web/e2e/demo-02-blog-content-review.spec.ts).

## Email signup onboarding

Scenario ID: `demo.email.signup-onboarding.v1`. Select **Email signup onboarding**;
the heading is **Email signup approval boundary**. This scenario uses
`mock_execute`, not live execution: representational writes still require exact
human approval even though both connectors are mocks.

The preset supplies synthetic `contact_id`, `name`, `email`, a fixed
`newsletter_list_ref`, `consent`, `signup_at`, and `welcome_context`. It uses
`demo-contact-0001`, Avery Demo, and `avery.demo@example.test`; the list is
`list.demo.email.signup-onboarding.v1`. Consent must be granted with source
`demo_signup_form` and a captured timestamp no later than signup. These are
synthetic fixtures, not a claim of consent from any real person.

1. Select **Propose onboarding actions**. The receipt says **Approval-gated run
   accepted**; do not read this as “subscribed,” “sent,” or “completed.”
2. Open the run and **Open approval queue**. Wait for planning to persist two
   action-scoped requests: `newsletter.subscribe` and `crm.upsert-contact`.
   Review each action's destination, redacted payload, hash, scope, expiry, and
   requesting run. Local identity explicitly permits the operator to self-approve.
3. Choose **Approve** on one request. In **Approve exact action?**, review the
   immutable action and confirm **Approve exact action**. With only one valid
   approval, both connector call counts and welcome-draft model calls remain zero.
4. Repeat the review and confirmation for the second request. Both approvals
   must still be valid; approval endpoints record decisions but do not themselves
   execute connectors. The worker subsequently advances the run.
5. Inspect the completed timeline and artifacts: one durable mock newsletter
   receipt, one durable mock CRM receipt, a `welcome_message_draft`, and an
   `email_onboarding_summary`. The summary has `email_send_status: not_sent`;
   the welcome draft has `send_status: not_sent`. Nothing was emailed.

The zero/one/both approval boundary is verified by
[the Email acceptance suite](../tests/acceptance/test_email_signup_demo.py):
before approval and after one approval there are zero connector/model calls;
successful completion has two connector calls and one model call. The suite also
exercises altered, expired, reused, cancelled, and crash/retry paths. This is
durable mock idempotency evidence, not universal exactly-once delivery for future
providers. The UI's expected-count panel or a decision receipt cannot establish
those runtime counts on its own.

Use **Reject** and **Reject exact action** only when that is the intended human
decision. A rejected, expired, consumed, or changed request cannot be treated as
fresh authorization. Refresh conflicts and inspect the authoritative run; do not
edit stored hashes or replay connector calls manually. Cancellation is best effort
and never reverses an already completed effect.

**Implemented and verified (historical mock evidence):**
[DEMO-03 record](verification/requirements/DEMO-03.md),
[DEMO-06 barrier record](verification/requirements/DEMO-06.md), and
[DEL-05 process-composition evidence](verification/requirements/DEL-05.md).
The [Email browser test](../apps/web/e2e/demo-03-email-signup.spec.ts) verifies the
UI/intake contract with a controlled response; it is not by itself proof of a
completed worker run or mock connector side effects.

## Community reminder draft

Scenario ID: `demo.community.reminder-draft.v1`. Select **Community reminder draft**;
the heading is **Event signup to reminder draft**.

- Input: `event_id`, `event_name`, `signup_event_id`, `admitted_source`, `signup_at`,
  `session_local_start`, `session_timezone`, `reminder_offset_minutes`,
  `attendee_display_name`, `channel_label`, and `event_details`. Use the complete
  supplied preset rather than inventing an enrollment or messaging integration.
- Action: select **Create reminder draft** and open the completed run's artifacts.
- Preset result: the session at `2026-09-17T09:00:00` in `America/Los_Angeles`
  resolves to `2026-09-17T16:00:00Z`; the 1,440-minute offset recommends
  `2026-09-16T16:00:00Z`. These times are derived from supplied event data, not a
  promise that a reminder will execute at that instant.
- Boundary: `scheduled_reminder_draft` is an artifact name, not a scheduled job.
  Its `delivery_status` is `not_sent` and `external_schedule_status` is
  `not_externally_scheduled`. No scheduler occurrence, calendar mutation,
  attendee enrollment, or message is created. The channel is drafting context.

Only one Live Session Reminder occurrence is selected; its duplicate remains a
separate source instance sharing the template, not an invented cohort or channel.

**Implemented and verified (historical mock evidence):**
[DEMO-04 record](verification/requirements/DEMO-04.md),
[acceptance tests](../tests/acceptance/test_community_reminder_demo.py), and
[browser intake tests](../apps/web/e2e/demo-04-community-reminder.spec.ts).

## Partnership application review

Scenario ID: `demo.partnerships.application-review.v1`. Select **Partnership
application review**; the heading is **Partner application to advisory review**.

- Input: `applicant_id`, `organization_metadata`, `declared_capabilities`,
  `declared_regions`, `evidence_records`, `program_criteria`, `program_constraints`,
  and `missing_information_indicators`. The preset organization is Northstar
  Systems Demo, with two synthetic evidence records and a missing security
  attestation against the supplied criteria.
- Action: select **Create advisory review** and inspect the completed artifact.
- Preset result: `partner_review_recommendation` with recommendation
  `needs_information`, criterion-linked rationale, risks/uncertainty, missing
  information, and follow-up questions. Changed valid input can yield `accept`
  or `reject` recommendation labels; neither is an authoritative decision.
- Boundary: supplied website references are not fetched; the scenario performs
  no external research, applicant notification, partner-record mutation, or
  automated acceptance/rejection. A human must decide any real partnership action.

**Implemented and verified (historical mock evidence):**
[DEMO-05 record](verification/requirements/DEMO-05.md),
[acceptance tests](../tests/acceptance/test_partnerships_demo.py), and
[browser intake tests](../apps/web/e2e/demo-05-partnership-review.spec.ts).

## Reproduce checks and understand limits

After the native dependencies are bootstrapped, the existing deterministic demo
and browser entry points are:

```sh
make test-del-03-demos
make web-test-demo-01-e2e
make web-test-demo-02-e2e
make web-test-demo-03-e2e
make web-test-demo-04-e2e
make web-test-demo-05-e2e
```

Browser commands require the pinned Node toolchain and browser acquisition via
`make web-bootstrap`; they start their own loopback test servers, not your existing
Compose stack. [Testing](testing.md) distinguishes controlled UI responses,
application acceptance, full browser checks, and committed clean-state runs.

**Assumption:** these presets demonstrate explicit choices from the source chart,
not hidden workflows or vendor integrations; see [assumptions](assumptions.md).
**Deferred real-adapter work:** actual publishing, sending, CRM/newsletter
integration, external research, enrollment, and fulfillment need separate
implementation, credentials, controls, and qualification. **Residual risk:** local
identity permits self-approval and the local host is trusted; do not expose this
demo as a multi-user service. **Acceptance target not yet verified:** full
repository release acceptance remains the individual matrix/evidence process,
not the successful completion of a single demo.
