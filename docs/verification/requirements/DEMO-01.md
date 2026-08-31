# DEMO-01 verification

DEMO-01 implements the Social idea-to-draft journey as a bounded, inert product
demo. Its exact scenario and workflow ID is
`demo.social-media.content-draft.v1`; it selects
`tpl.social-media.new-content.linkedin-post-drafter` through
`inst.social-media.new-content.linkedin-post-drafter.01`. The trusted registry
owns the version, schemas, committed preset, selected agent, one-step read-only
DAG, expected durable state path, exact call counts, and the safe submit label
`Create draft`.

The four logical workflow phases are connected through existing production
services. Registry resolution validates and normalizes the bounded idea,
audience, tone, key points, optional call-to-action, and provenance-only HTTPS
references. Durable manual admission creates the WorkItem and received Run. The
scenario DAG selects one `model.generate-structured` READ step through
`cap.model.generate-structured`. The controlled-read executor validates the
model result, applies the deterministic character-count/envelope transform, and
persists one `social_post_draft` ArtifactEnvelope before the lifecycle reaches
completed.

The demo adapter is sealed to the exact credential-free deterministic provider
before execution and is renderer-selected by the exact template and
intermediate output schema. Real, local, arbitrary, or identity-drifted
providers are rejected before any call. Caller content enters the model request
as one untrusted user-input part and cannot select tools or authority.
The resulting artifact records platform, draft text, hashtags, transformed
character count, CTA summary, supplied reference summaries, safety notes,
`not_published`, and an empty proposed-action list. Its provenance binds the
work/input digest, run and step, workflow version and definition, template and
instance, catalog hash and configuration revision, work-input and observation
sources, mock provider version, output schema ID/version/hash, payload hash,
UTC creation time, and classification.

## API and control surface

`GET /api/v1/demo-scenarios` is authenticated, private, and read-only. It
returns the server-owned safe preset, schema, selected agent, expected lifecycle
and call counts, deterministic-mock mode, and safe submit verb. The Demos page
compiles that schema only after the exact Social safety contract matches; any
version, identity, effect, schema, count, state-path, or submit-label drift makes
the page unavailable and removes submission controls.

`POST /api/v1/demo-scenarios/{scenario_id}/runs` requires a human operator,
the local-session CSRF token, strict JSON, and exactly one bounded idempotency
key. It resolves caller overrides through the trusted registry and calls only
the existing manual durable-admission service. A newly created receipt is `202`
with a Run still at `received`, plus bound instance, run, timeline, and artifact
URLs; an idempotent replay may reflect the same Run's coherent current state.
Execution is not drained in the request. The page therefore says that a run was
accepted, never that its artifact has already completed.

An authenticated local discovery request is:

```sh
curl --fail --header 'Accept: application/json' \
  http://127.0.0.1:8000/api/v1/demo-scenarios
```

After obtaining the local session's CSRF token from `/api/v1/session`, durable
intake can be exercised with:

```sh
curl --fail --request POST \
  --header 'Accept: application/json' \
  --header 'Content-Type: application/json' \
  --header 'Idempotency-Key: demo-social-draft-0001' \
  --header 'X-CSRF-Token: <local-session-csrf-token>' \
  --data '{"overrides":{}}' \
  http://127.0.0.1:8000/api/v1/demo-scenarios/demo.social-media.content-draft.v1/runs
```

## Executable evidence

The backend gate denies network sockets and exercises the committed fixture,
scenario-owned DAG, durable SQLite repositories, lifecycle, deterministic model
adapter, schema validation, artifact provenance, idempotent replay, no-egress
rows, and the complete discovery/intake API boundary:

```sh
make test-demo-01-backend
```

The focused frontend gate type-checks the production app and runs the strict
transport and page tests:

```sh
make web-test-demo-01-unit
```

The browser gate builds the production application, starts loopback-only API
and Vite servers, submits the schema-driven preset, checks the exact request and
accepted-resource links, rejects publication language and non-loopback
requests, verifies wide/mobile reflow without horizontal overflow, and proves
that a drifted safety contract cannot be submitted:

```sh
make web-test-demo-01-e2e
```

## Evidence boundary

The completed journey uses the deterministic mock and temporary local SQLite
through a bounded worker/test drain. The default API composition publishes the
scenario registry but requires injection of the existing manual-admission
service for POST; without it, intake fails closed with `503`. A newly created
receipt must bind a received version-one Run. An idempotent replay may return
the same receipt after that Run has advanced, but the handler still performs no
execution itself. Deployed worker process composition remains owned by later
delivery requirements. The browser uses deterministic same-origin scenario and
receipt fixtures plus the real local session endpoint, so it does not claim
live browser-observed worker completion.

No source URL is fetched, no social connector is called, and no publish, send,
approval, delivery receipt, or external action is created. Determinism applies
to normalized business payloads; durable identities and timestamps remain
unique or clock-derived. This evidence covers the Social scenario only and does
not implement DEMO-02 through DEMO-06.

Machine authority: [`DEMO-01.json`](DEMO-01.json).
