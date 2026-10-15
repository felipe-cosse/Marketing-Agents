# DEL-01 — Working backend/frontend delivery

## Scope and authority

Starts from main `1ce6612572e6627aa800aa552dd562700db2096a` after the approved
EXEC-08 merge, on `req/del-01-working-backend-frontend`. The user requested full
verification before a merge request, with CI disabled and every checkpoint
branch preserved. No main merge is authorized by this implementation request.
Machine authority: [DEL-01.json](DEL-01.json).

The requirement is the source prompt's working backend and frontend, not a new
domain feature or production deployment. Existing requirements implement the
API, worker, scheduler, persistence, UI and deterministic mocks. The inherited
EXEC-08 candidate `08587d54b2ba1ac1fb9b622867bea63dd3b83406` passed its complete
quality and six-gate qualification; its approved merge has the identical tree
`ed0aa8491586c13fdccd8a15ab6a55fa491749b5`. Those are historical observations,
not passing results for this new candidate.

## Preserved first candidate and approved replacement

The first DEL-01 candidate `8579bfaae227dd0dd33c6441217aadf5b9f68b67`, tree
`d2d5886129b7e0b735d6ebaf135c56e1cdb34141`, is preserved on
`codex/del-01-checkpoint-8579bfa`. Its complete AC-16 run passed all 20 gates;
the matching source-bound record remains outside Git. That successful old-tree
run does not qualify the relay replacement described below.

The subsequent clean-state attempt passed its isolated backend suite with
5,228 passes, 33 optional skips and 477 subtests in 750.60 seconds, and its
frontend suite with 472 passes across 55 files in 45.28 seconds. The production
browser then received HTTP 403 for the Social submission. The API trusted the
selected published-port origin, while the browser in the shared web namespace
used `http://127.0.0.1:8080`. This origin mismatch is distinct from the earlier
Fetch Metadata forwarding diagnostic. The failed manifest run did not reach
paired backup or its final connection witness. An independent witness
diagnostic produced the intended ordinary missing-hook assertion, but does not
complete that failed aggregate. Owned resources were removed.

The user approved a bounded loopback-relay correction with source/checkpoint
preservation and complete reruns. The replacement must pass fresh full AC-16
qualification and all six manifest gates plus the connection witness. Neither
the old candidate's passing suites nor a focused relay diagnostic is a passing
result for the replacement.

## Delivery gap and implementation

The existing production browser smoke proved hierarchy rendering, navigation,
desktop/mobile layout, console health and same-origin requests. Its browser
controls did not create a durable run. Separate HTTP smoke already exercised
all five real local demos and the Email approval barrier; several feature
browser tests intentionally use fulfilled API fixtures. Neither fact alone
proves a production UI submission reaches the worker and returns a persisted
artifact through the complete deployed path.

DEL-01 extends that existing production browser smoke with one real read-only
Social draft journey:

1. Open Demos and submit the safe Social preset through its visible control.
2. Observe the real accepted response and follow its rendered run link.
3. Wait for the authoritative completed run, correlate the receipt and identities,
   and require mock/read-only execution with no external actions.
4. Follow its rendered artifact link and verify the authorized schema, producer
   and provenance agree with that run.
5. Reload the same artifact and verify its persisted identity and provenance.

The journey does not fulfill responses, install a fixture API, manufacture a run
or write directly to the database. Existing hierarchy, zoom/search, mobile tree,
desktop/mobile screenshots, console and network assertions remain in place.
Stable diagnostic codes and successful check labels exclude response payloads,
session tokens and full page content. The existing outer browser, clean-state,
execution and cleanup deadlines are unchanged.

A pre-freeze live diagnostic exposed the existing generic browser guard's known
Fetch Metadata limitation: fetch-and-fulfill forwarding rejects genuine UI
mutations at the API's browser-security boundary. As already established by
OBJ-06, the production smoke now uses its native Chromium transport guard,
without fabricated headers or relaxed origin/CSRF validation. Exact-origin
requests, response-stage redirect denial, route locking and final guard audits
remain mandatory. A new optional deny-all-WebSocket mode preserves this smoke's
blanket WebSocket restriction; the existing guard's default and its original
canaries remain unchanged. An additional owned-loopback canary proves denial
before a same-origin WebSocket reaches its server while native POST metadata
remains intact. This is test transport wiring, not an application policy change.

The approved relay correction makes the browser's origin match the selected
published-port origin already trusted by the API. Clean-state verification
starts `del-01-loopback-relay.mjs --origin <selected-origin>` inside the existing
owned browser/web shared network namespace. Its loopback listener forwards raw
bytes from that selected port to the existing web listener on port 8080, then
runs the unchanged production browser journey against the selected origin.
If the selected port is already 8080, it uses the existing listener directly
without creating a self-forwarding relay.
It does not rewrite Host, Origin, Fetch Metadata, cookies or CSRF data, fabricate
an API response, or change the application's trusted-origin configuration. The
strict native browser guard, redirect denial, blanket WebSocket denial and
final network assertions still apply.

This proves origin parity inside the shared namespace, not that browser traffic
traversed the host-published port. Existing host readiness, actual Docker port
publication validation, five-demo HTTP, replay/restart and resumed-readiness
checks remain independently mandatory. The wrapper and relay own their
resources and must fail closed on invalid origins, transport or child failures;
their lifecycle remains within the unchanged browser and clean-state deadlines.
The wrapper owns relay sockets and the Node runner's process group; Playwright
handles graceful Chromium shutdown, while existing owned Docker-container
cleanup supplies hard containment because Chromium may use a separate process
group. This is not a generic native-process hard-kill or reaping guarantee.
No Docker daemon, Compose service or API security setting is changed.

The new `make verify-del-01` entrypoint serially invokes the existing complete
clean-state verifier with `REF=HEAD`, even if a caller supplies another `REF`.
It verifies committed source, not uncommitted edits. `make test-del-01` is only
the focused Make wiring, injected-browser and owned-loopback relay regression
suite; it cannot qualify the deployed backend/frontend. No product route, React
component, migration, dependency, Docker setting or CI behavior changes.

## Regression and connection evidence

The browser helper's lightweight controls exercise its actual orchestration and
reject invalid or mismatched receipt, run, artifact, provenance and reload
relationships. They remain explicitly simulated negative controls, not a
substitute for the real production journey.
The complete quality browser gate also executes the existing native-transport
canary suite, including the added strict-WebSocket control.

Relay regressions must exercise real owned-loopback forwarding, byte/header
preservation, invalid-origin and transport failures, and owned child/socket
cleanup. Clean-state regressions must require the selected origin and relay
entrypoint while preserving the existing browser command's network namespace,
ownership labels and timeout. The existing Node manifest gate runs both the
journey controls and relay controls under its unchanged 120-second bound. Its
network label is now `loopback_only` to describe those owned listeners honestly;
that metadata is not a new enforcement mechanism or a relaxation of the tests.

The Make regressions assert the committed-HEAD serial connection and failure
propagation, plus the unchanged full quality/acceptance gate inventories and
deadlines. The witness restores only `Makefile` to the base while retaining the
new tests and browser implementation. An ordinary missing-delivery-hook
assertion must fail; an import failure or a timeout is not the intended witness.
Protected evidence policy, schema and executor remain unchanged.

## Qualification procedure

Freeze one nonempty primary requirement commit with its verification commands
in the body before complete qualification. Keep the source and commit metadata
unchanged after the runs; a replacement candidate requires preservation and
fresh qualification. Keep reports, browser pixels and logs outside Git.

With the pinned native Python and Node toolchains on `PATH`, use
`PYTHONDONTWRITEBYTECODE=1 UV_OFFLINE=1 UV_FROZEN=1 COREPACK_ENABLE_NETWORK=0`:

1. Before freezing, run the focused new regressions, existing clean-state/browser
   evidence controls, relevant static checks and a real production journey
   diagnostic when needed. Diagnostics do not qualify the final frozen source.
2. Run `python -B -m scripts.ac_16_quality run` for all 20 unfiltered gates,
   including the existing safety coverage and complete browser inventory.
3. Run `python -B -m scripts.verify_requirement_evidence branch --id DEL-01
   --base main --run --witness`. All six gates must pass: Make controls, browser
   helper and relay controls, existing clean-state controls, exact-source quality
   audit, full committed-HEAD clean-state/production-browser journey and separate
   paired Compose backup/restore. The witness must fail for the expected reason.
4. Independently check source/tree/manifest identity, actual output counts,
   optional skips, safety coverage, all deadlines, owned-resource cleanup,
   unchanged checkpoint refs and disabled CI. Inspect the retained desktop and
   mobile pixels; a successful build is not rendered-app proof.
5. Report the exact qualified candidate and limitations, then ask for explicit
   publication and non-fast-forward merge approval. Do not update a qualified
   commit merely to embed results or its own final hash.

These are the complete constituent quality, clean-state and backup checks of
the acceptance aggregate, not a literal `make acceptance` invocation. A full
aggregate cannot be hidden inside one 1,800-second manifest gate. Gate-specific
limits, network controls and coverage are not relaxed to fit a wrapper.

## Preserved delivery limits

- The target remains single-operator local identity, SQLite and deterministic
  mocks. A working local application is not an autonomous marketing service.
- This new UI journey covers Social only. Existing five-demo HTTP and Email
  approval checks remain required; do not claim five browser-driven demos.
- Optional PostgreSQL skips do not qualify PostgreSQL. Live providers, external
  delivery, production identity, cloud deployment and production RPO/RTO remain
  unverified or deferred.
- Browser same-origin controls and network-none test containers do not eliminate
  the documented web ingress container's OS outbound route. Acquisition may use
  registries. This is not fresh AC-15 four-suite kernel-isolation evidence.
- The relay provides browser-origin parity only inside the owned shared network
  namespace. Host-publication, readiness and replay checks remain separate;
  do not describe the browser journey as traversing the host-published port.
- Local unsigned records check consistency, not authenticated execution.
  Counts from subtest/diagnostic events are not unique test counts.
- CI stays disabled, not green. Preserve all 19 inherited checkpoint branches,
  `codex/del-01-checkpoint-8579bfa` and every retained requirement branch. The
  approved replacement is limited to the unpublished DEL-01 primary commit;
  main and preserved checkpoints are not rewritten. No broad cleanup is part
  of DEL-01.
- Completing this feature's verification is not a main merge or production
  release authorization. Historical documents keep their original source scope;
  the final handoff supplies separately observed candidate results.
