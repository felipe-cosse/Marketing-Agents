# DEMO-05 verification

DEMO-05 implements the read-only Partnerships application-review journey. A
trusted scenario owns a committed synthetic preset, validates bounded supplied
application data, performs one deterministic structured model call, and
persists one schema-valid advisory recommendation ArtifactEnvelope with durable
provenance.

The workflow is deliberately inert. Applicant and application values remain
untrusted input. The demo does not look up an applicant, accept or reject an
application, enroll a partner, mutate a CRM or tracker, request approval,
propose an external action, invoke a connector, notify anyone, or perform an
external write.

Authenticated discovery exposes the closed preset and exact expected behavior.
Human-operator POST retains the established CSRF, strict JSON, schema,
idempotency, scenario, instance, and dry-run boundaries and returns only a
private asynchronous `202` receipt. The Demos page reports acceptance without
claiming model execution, completion, or a partnership decision.

Executable evidence:

```sh
make test-demo-05-backend
make web-test-demo-05-unit
make web-test-demo-05-e2e
```

The backend gate denies network sockets. The browser gate permits loopback only
and uses deterministic same-origin discovery and receipt fixtures, so it proves
the production control surface rather than backend authorization controls or
live worker completion. The model and persistence evidence is deterministic and
local; it is not proof of real-model quality, production-database behavior, a
partnership outcome, or any external mutation.

Machine authority: [`DEMO-05.json`](DEMO-05.json).
