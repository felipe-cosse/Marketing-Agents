# DEMO-02 verification

DEMO-02 implements the Blog and SEO metadata-to-review journey as a bounded,
read-only product demo. Its exact scenario and workflow ID is
`demo.blog-seo.content-review.v1`; it selects
`tpl.blog-seo.new-content.blog-post-updater` through
`inst.blog-seo.new-content.blog-post-updater.01`. The trusted registry owns the
version, schemas, committed preset, selected agent, one-step read-only DAG,
expected durable state path, exact call counts, and the safe submit label
`Create review`.

The four logical workflow phases use existing production boundaries. Registry
resolution validates and canonicalizes the bounded article title, provenance-only
HTTPS URL, supplied excerpt, last-updated and assessment timestamps, target
keywords, and current feature/integration metadata. Durable manual admission
creates the WorkItem and received Run with only the scenario-declared model
capability. The scenario DAG selects one `model.generate-structured` READ step
through `cap.model.generate-structured`. The controlled-read executor validates
the review output and persists one `content_review` ArtifactEnvelope before the
lifecycle reaches completed.

`assessment_at` is explicit fixture/input metadata so staleness never depends on
the worker clock. Both timestamps are normalized to UTC and age is the floor of
elapsed seconds divided by 86,400. The frozen levels are `current` for 0–89
days, `review_due` for 90–179 days, and `stale` from day 180. A last-updated
timestamp after assessment is rejected before any provider call.

Keyword coverage is a deterministic case-folded whole-phrase check over only the
supplied title and excerpt. It is not ranking, traffic, search-engine, or
production SEO evidence. Feature and integration gaps compare only supplied
metadata names with that same bounded content. The exact deterministic provider
returns three severity-bearing findings plus ordered gaps and recommendations;
the trusted transform adds the scenario identity, `content_review` artifact
type, `advisory_only`, `not_updated`, and an empty proposed-action list.

## API and control surface

`GET /api/v1/demo-scenarios` remains authenticated, private, and read-only. It
returns the exact Blog preset alongside Social, including selected agent,
schema, expected lifecycle and call counts, deterministic-mock mode, and safe
submit verb. The Demos page defaults explicitly to Social regardless of registry
sort order and exposes Blog only after its exact read-only contract and preset
compile successfully.

Selecting Blog shows the supplied-metadata boundary, canonical URL no-fetch
notice, exact agent IDs, deterministic model count, zero connector/action/write
counts, and the durable state path. Scenario switching resets form/receipt state
and is unavailable while an intake request is pending so its idempotent recovery
key is not discarded. Contract drift removes the affected Blog submission
control; unknown future scenarios are never rendered through this allowlist.

`POST /api/v1/demo-scenarios/{scenario_id}/runs` requires a human operator, the
local-session CSRF token, strict JSON, and exactly one bounded idempotency key.
It resolves caller overrides through the trusted registry and calls only the
existing durable manual-admission service. A newly created receipt is `202` with
a Run at `received` and bound instance, run, timeline, and artifact URLs;
execution is not drained in the request and the UI reports acceptance rather
than completion.

After obtaining the local session's CSRF token from `/api/v1/session`, the
committed Blog preset can be admitted with:

```sh
curl --fail --request POST \
  --header 'Accept: application/json' \
  --header 'Content-Type: application/json' \
  --header 'Idempotency-Key: demo-blog-review-0001' \
  --header 'X-CSRF-Token: <local-session-csrf-token>' \
  --data '{"overrides":{}}' \
  http://127.0.0.1:8000/api/v1/demo-scenarios/demo.blog-seo.content-review.v1/runs
```

## Executable evidence

The backend gate denies network sockets and exercises the committed fixture,
UTC age boundaries, exact scenario DAG and least-authority admission, durable
SQLite repositories, lifecycle, sealed deterministic provider, schema
validation, artifact provenance, replay, no-egress persistence, and Blog API
discovery/intake:

```sh
make test-demo-02-backend
```

The focused frontend gate type-checks the production app and runs the strict
transport and Blog page tests:

```sh
make web-test-demo-02-unit
```

The browser gate builds the production application, starts loopback-only API
and Vite servers, switches from Social to Blog, submits the schema-driven
supplied-metadata preset, checks the exact request and accepted-resource links,
rejects crawl/CMS/publish controls and non-loopback requests, verifies
wide/mobile reflow, and proves that a drifted Blog contract cannot submit:

```sh
make web-test-demo-02-e2e
```

## Evidence boundary

The completed journey uses a deterministic mock and temporary local SQLite
through a bounded worker/test drain. The default API composition publishes the
scenario registry but requires injection of the existing manual-admission
service for POST; without it, intake fails closed with `503`. Deployed worker
process composition remains owned by later delivery requirements. The browser
uses deterministic same-origin scenario and receipt fixtures plus the real local
session endpoint, so it does not claim live browser-observed worker completion.

The URL is retained as validated HTTPS provenance and never fetched. All review
evidence comes from the supplied title, excerpt, timestamps, keywords, and
product metadata. No crawler, search engine, CMS connector, upload, update,
publication, approval, proposed action, or external write occurs. The result is
advisory and makes no claim about search ranking or marketing outcomes.

Machine authority: [`DEMO-02.json`](DEMO-02.json).
