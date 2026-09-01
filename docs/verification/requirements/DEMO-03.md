# DEMO-03 verification

DEMO-03 implements the Email signup-to-onboarding journey as a bounded,
approval-gated deterministic mock demo. Its exact scenario and workflow ID is
`demo.email.signup-onboarding.v1`. The trusted registry selects, in order,
`inst.email.newsletter.newsletter-subscriber.01` and
`inst.email.lifecycle-marketing.customer-onboarder.01`, owns the closed input
and output schemas, committed synthetic preset, three-step DAG, expected durable
state path and counts, and the safe submit label `Propose onboarding actions`.

The plan contains two independent root WRITE steps followed by one dependent
READ step. `subscribe-newsletter` proposes `cap.newsletter.subscribe` through
the configured `mock.newsletter.default` binding. `upsert-crm-contact` proposes
`cap.crm.upsert-contact` through `mock.crm.default`. Only after both immutable
actions have their own exact approval does `create-welcome-draft` become
eligible to run through `cap.model.generate-structured`. The email-send
capability is neither selected nor authorized.

The input uses a synthetic contact ID, a fixed registered newsletter-list
reference, explicit granted consent and source, canonical UTC consent/signup
timestamps, and bounded untrusted welcome context. Name and email are sensitive;
the connector action boundary redacts the newsletter contact reference and the
CRM contact fields. Consent capture after signup, non-canonical timestamps,
non-`.test` email destinations, unregistered list references, and caller-owned
capability, binding, action, approval, or hash authority are rejected before
planning or dispatch.

## Approval and execution boundary

Durable manual admission uses transport mode `mock_execute` and persists
`mock_execution` Work/Run authority. Planning atomically records the selected
instance revisions, topology, two immutable external actions, and two distinct
approval requests before the Run enters `awaiting_approval`. At this boundary
newsletter, CRM, and model call counts are all zero. Approving either action
alone leaves every call count at zero. The second unchanged approval releases
the complete authorization set and transitions the Run to `executing`; approval
handling itself never performs a connector or model call.

A later worker drain dispatches the two registered mock actions in plan order
with their stable action-derived idempotency keys. Durable mock receipts are
recorded before success is acknowledged, so retry and replay do not duplicate a
call. Once both WRITE steps succeed, the dependent deterministic model step
creates a reviewable welcome-message draft. It never sends email. One final
`email_onboarding_summary` ArtifactEnvelope contains the nested
`welcome_message_draft` and exactly two ordered authoritative mock receipt
references, each marked `mock_succeeded` and `external_side_effect: false`,
with complete scenario and execution provenance.

Cancellation before the complete approval set is released makes zero connector
or model calls. A cancellation racing after release is a forward execution
fence, not a rollback claim. The negative controls also prove unauthorized
decisions, action/hash drift, missing or changed selected-instance bindings, and
disabled email-send authority fail closed.

## API and control surface

`GET /api/v1/demo-scenarios` remains authenticated, private, and read-only. It
publishes Email only with its exact two-agent order, mutating effect, three-step
lifecycle, counts of one model call, two connector calls, two external actions,
two approvals, and two external writes, the registered-list preset, and the
safe submit verb. Social remains the explicit default and unknown or drifted
scenarios are not rendered through the frontend allowlist.

`POST /api/v1/demo-scenarios/{scenario_id}/runs` requires a human operator, the
local-session CSRF token, strict JSON, and exactly one bounded idempotency key.
The server resolves the complete preset through the trusted registry and admits
Email only in mock-execution mode. A private `202` receipt says only that intake
was accepted for asynchronous planning; it does not claim approval, execution,
or completion. It links the exact instance, Run, timeline, artifacts, and
filtered approval queue.

The Demos page uses the existing industrial control-room visual language, with
amber for the approval wait. It displays both selected agents, the two immutable
mock actions, the complete approval barrier, zero-call pre-approval promise,
draft-only welcome result, exact durable state path, and `Open approval queue`
link. Approval controls remain exclusively on `/approvals`; the Demos page
cannot approve or dispatch. Sensitive controls disable browser assistance,
remain in React memory, and never enter URLs, browser storage, cookies, or query
keys. The layout reflows without horizontal overflow at 426 pixels and honors
reduced motion.

## Executable evidence

The backend gate denies network sockets and exercises schema/semantic input
validation, exact multi-instance routing and configuration locks, immutable
action construction, redacted approval resources, the all-actions approval
barrier, zero-call partial approval, durable mock dispatch/idempotency, dependent
model execution, cancellation, replay, final artifact provenance, and private
Email API discovery/intake:

```sh
make test-demo-03-backend
```

The focused frontend gate type-checks the production app and runs strict
transport and Email control-surface tests:

```sh
make web-test-demo-03-unit
```

The browser gate builds the production application, starts loopback-only API
and Vite servers, selects Email, submits the schema-driven preset, checks the
exact mock-execution receipt and approval-queue link, rejects approval/send and
external-network controls, verifies wide/mobile reflow, and proves that a
drifted approval contract cannot submit:

```sh
make web-test-demo-03-e2e
```

## Evidence boundary

The completed journey uses registered deterministic mocks and temporary local
SQLite through a bounded worker/test drain. The HTTP request proves durable
asynchronous intake only; it never drains planning, approvals, or execution
inline. The browser gate uses deterministic same-origin discovery/receipt
fixtures plus the real local session boundary, so it does not claim live
browser-observed worker completion.

Mock receipts are durable control-plane evidence, not proof of a real newsletter
subscription, CRM update, delivered email, or production marketing outcome.
Fresh catalog seeds contain the two local mock bindings; existing operator-owned
instance configurations are preserved and must already expose those exact
bindings or the demo fails closed. Name and email are synthetic test data, and
the welcome artifact is a draft only.

Machine authority: [`DEMO-03.json`](DEMO-03.json).
