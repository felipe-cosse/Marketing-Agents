# API-08 verification

Status: verified locally

API-08 establishes a single fail-closed Draft 2020-12 validation policy for
runtime schemas and structured values. A schema is first reduced to strict
canonical JSON, checked as an object-shaped Draft 2020-12 document, and frozen
as an independent snapshot. An embedded `$id` is optional, but when a caller
declares an identity any embedded value must match it exactly. The Draft 2020-12
dialect is enforced in actual nested schemas as well as at the root. `$ref` and
`$dynamicRef` may resolve only to local JSON pointers or anchors within their
schema resource; remote or unresolved references and duplicate local anchors
fail during compilation. The legacy `$recursiveRef` and `$recursiveAnchor`
keywords are not implemented by the Draft 2020-12 validator and are therefore
rejected in actual subschemas. Reference-shaped keys inside annotation/default
data remain inert rather than being misclassified as schema behavior. Validation
applies format checks, an explicit inclusive nesting bound, and deterministic
error ordering. Failures expose only a stable code and a safe `/input` or
`/output` pointer; unsafe property names collapse to the root. Hostile Mapping
implementations whose `items()` or `values()` methods raise also fail closed,
without retaining rejected values, schema text, canaries, causes, or exception
context.

The existing JSON mutation surfaces now have strict raw-body seams before
FastAPI or an application executor receives a value. Instance configuration
PATCH, manual dry-run admission, and approval request/decision mutations match
the ASGI request path independently of `root_path`, so mounting the API under a
prefix cannot bypass validation. They accept one unencoded `application/json`
media type with at most one `charset=utf-8` parameter and reject content
encoding, another charset, repeated charset parameters, a missing/wrong media
type, or duplicate content-type fields. They bound both declared and streamed
bytes, cap JSON nesting, and reject a BOM, duplicate or Unicode-normalization-
colliding keys, invalid UTF-8, non-finite or overflowing numbers, and lone
surrogates. Their strict Pydantic DTOs then reject coercion and unexpected
authority fields. The application-level manual and webhook admission services
still validate the resolved template input schema and preserve only sanitized
pointers. The gate retains the API-03, API-04, API-05, and API-06 transport and
intake suites so these boundaries are exercised together rather than inferred
from the shared parser alone.

Webhook intake deliberately remains a separate signature-first seam. The route
bounds and preserves exact raw bytes, the API-05 verifier authenticates them,
and only then does its strict source mapper parse the envelope and the normal
incoming-work validator enforce the resolved input schema. API-08 does not put
the shared JSON parser before signature verification or alter that established
trust boundary; its signed schema-rejection and non-reflection tests are part of
the executable regression gate.

The controlled READ executor synchronously reconstructs the caller-owned
`ControlledReadCommand` into a private exact command before its first `await`.
A caller mutation scheduled for the next event-loop turn therefore cannot alter
the step, payload, attempt, adapter request, audit, or result. This includes a
denial raised after completion: its durable runtime-control audit remains bound
to the private command's real step and cannot retain a caller-forged step or
payload. A hostile command snapshot fails before any adapter call without
retaining its canary, cause, or exception context.

Each controlled READ adapter then declares a `RuntimeInputContract` and
`RuntimeOutputContract` before any attempt reservation. The executor detaches
the persisted operation into its private policy snapshot. It passes disposable
operation copies to the adapter's pure contract methods, recompiles the returned
input and output schemas into private contracts, and matches their identities,
classification, provider kind, and output schema hash to the sealed operation.
It validates the private command input before creating an attempt, consuming a
budget, or invoking `adapter.execute` or an external operation. The request and
its contract are rebuilt as another disposable snapshot before that call.

After return, the executor exact-matches the untrusted result to its private
durable request. The private operation and output-contract snapshots then
canonical-validate the payload and enforce byte, model-token, depth, and
hash-bound schema limits. Only after all those checks pass does the executor
reconstruct one detached exact `ReadAdapterResult` for artifact construction.
Adapter mutation of disposable operation, request, or contract objects cannot
change those private bounds; mutation scheduled after return cannot change the
detached successful result, classification, provenance, or artifact. A hostile,
malformed, oversized, over-budget, overly deep, or schema-invalid result becomes
a safe terminal denial, creates no artifact, and does not leak into the timeline.
A successful artifact records the exact output schema identity, version, and
hash in provenance.

The LLM boundary applies the same rule even when a provider claims structured
output. Requests must be exact `LLMRequest` instances and are reconstructed from
canonical JSON before use. Their output schema ID, body, and canonical hash are
bound in an immutable preflight snapshot before a renderer or real-provider
delegate is called. The deterministic renderer registry additionally binds the
same schema hash to its exact template/schema key. Responses must reconstruct as
an exact canonical `LLMResponse`, report a complete finish, preserve the selected
provider identity, remain within byte and token limits, and independently pass
the trusted schema. Tests mutate delegate-visible schema and budget objects and
prove those mutations cannot weaken the preflight snapshot.
Renderer configuration/output failures are normalized without retaining hostile
values, causes, or exception context.

The registered WRITE gateway reconstructs the authorized minimized payload as
the operation's strict request DTO before invoking a connector. After the call,
it requires an exact `ConnectorWriteResult`, canonicalizes it, and validates it
against that DTO's generated result schema. A malformed response or ordinary
exception while normalizing that post-call response may follow a real external
effect, so both are classified as `schema_invalid_response` with
`request_may_have_left_process=true`. The dispatcher therefore cannot record a
false success or persist a malformed action result; reconciliation remains tied
to the authoritative durable receipt.

The bounded executable gate runs the API-08 schema policy, LLM provider,
controlled READ, and WRITE-result tests together with the changed input DTO and
authorization suites. It also includes the configuration, manual, webhook,
approval, artifact-binding, runtime-policy, catalog-schema, and network-isolation
regressions. The shared Python test fixture denies sockets and DNS, so the
manifest records `network_requirement: deny`.

This evidence has deliberate ownership limits. API-09 still owns global
`application/problem+json`, correlation, status mapping, and terminal-error
presentation. DEL-05 owns the default runnable API/worker/LLM composition, and
DEL-07 owns the committed OpenAPI snapshot and generated client drift gate.
Tests inject providers, connectors, repositories, and services and make no live
network-provider claim.

The persisted READ operation does not yet seal the request-schema hash. Although
`RuntimeInputContract` computes that hash and validates before reservation, the
executor can independently match only request schema ID and classification, so
a same-ID changed request schema needs a later persisted hash to become
detectable without trusting the adapter declaration. Output schemas do have a
sealed hash. The executor validates output before persistence and places its
schema facts in artifact provenance, but the artifact repository has no schema
resolver and cannot independently revalidate the stored body during add or
hydration. Those limitations are recorded rather than converted into broader
claims.

Machine authority: [`API-08.json`](API-08.json).
