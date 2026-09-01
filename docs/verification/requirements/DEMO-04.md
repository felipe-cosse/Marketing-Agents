# DEMO-04 verification

DEMO-04 implements the read-only Community event-signup reminder journey as
`demo.community.reminder-draft.v1`. The trusted scenario owns a committed
synthetic preset, validates the session's local date/time and IANA timezone,
calculates the recommended reminder time in UTC, and performs one deterministic
structured model call. The completed Run persists one `scheduled_reminder_draft`
ArtifactEnvelope with the original timezone/time, recommended UTC time, channel
label, signup provenance, and explicit `not_sent` and
`not_externally_scheduled` status.

The workflow is deliberately inert. Event and signup data are untrusted input;
no URL or provider is queried. It does not enroll an attendee, mutate a
calendar, create a provider schedule, propose an external action, request an
approval, invoke a messaging connector, or send a reminder. The persistent
scheduler remains a separate subsystem and does not turn this draft into a
scheduled delivery.

Authenticated discovery exposes the closed preset and exact expected behavior.
Operator-only POST retains the established CSRF, strict JSON, schema,
idempotency, scenario, instance, and dry-run boundaries and returns only a
private asynchronous `202` receipt. The Demos page reports acceptance without
claiming model execution or completion and removes Community submission if the
discovered safety contract drifts.

Executable evidence:

```sh
make test-demo-04-backend
make web-test-demo-04-unit
make web-test-demo-04-e2e
```

The backend gate denies network sockets. The browser gate permits loopback only
and uses deterministic same-origin discovery and receipt fixtures, so it proves
the production control surface rather than live worker completion. The model,
event, and persistence evidence is deterministic and local; it is not proof of
a provider schedule, delivered reminder, real attendee enrollment, or
production marketing outcome.

Machine authority: [`DEMO-04.json`](DEMO-04.json).
