# WEB-03 verification

WEB-03 adds a selected-agent inspector to the verified organization chart. A card selection issues one strict, abortable, same-origin detail request keyed by catalog hash and instance ID. The response is cross-bound to the selected hierarchy identity and presents instance configuration, complete template metadata and policies, resolved capabilities, approval policy, input/output schemas, source evidence, and at most five newest-first recent runs. Reopening a cached selection sends its strong detail ETag and preserves exact object identity on a matching `304`.

Runtime presence is an explicit union. A static detail response says that recent-run data is unavailable; it is never presented as `never_run`. A dynamic response requires a watermark, coherent current status, and bounded run rows together. Community deployments include the real instance ID plus `Instance X of Y`, so deployments sharing a template remain distinguishable.

Configuration editing is deployment-only and schema-driven. The editor exposes `enabled`, `variantLabel`, `triggerBindings`, `connectorBindings`, and `schedule` only to a session containing `local_admin`. It loads the schema after explicit Edit, offers only supported trigger kinds and registered connector IDs, and persists an enabled schedule and its schedule trigger as identical values. A partial PATCH uses the instance configuration ETag rather than the whole-detail ETag, keeps CSRF material in memory, retries once only after `csrf_token_invalid`, and invalidates hierarchy plus selected detail after success. Conflicts and validation failures preserve the draft; `409` recovery requires an explicit reload and never automatically merges or resubmits.

Selection remains local UI state and does not widen WEB-02's URL contract. Closing with the button or Escape restores focus to the selected card. Changing selection, closing, or filtering a dirty editor invokes the chart-confined discard dialog. Status-summary changes invalidate only the selected detail and reuse the existing status poll instead of creating another timer.

## Accepted-concept fidelity ledger

The accepted 1536×1024 concept and the latest production-browser captures were inspected at original resolution.

| Fidelity point     | Verified implementation                                                                                                                                                                  |
| ------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Inspector geometry | A 331 px right rail docks flush with the chart at 1536×1024; measured bounds were chart `1157 px` plus inspector `331 px` inside the `1488 px` workspace.                                |
| Compact behavior   | At 1280×800 the inspector is a 360 px overlay; the chart retains its full `1232 px` width and document scroll width remains exactly `1280 px`.                                           |
| Surface language   | White surface, restrained border and shadow, blue identity glyph, green enabled pill, compact metadata, and the pale blue-gray chart preserve the accepted visual system.                |
| Header anatomy     | Identity icon, source-authoritative title, stable instance ID, state, and close control occupy the same compact header pattern as the concept.                                           |
| Content hierarchy  | Purpose, overview, deployment/configuration, capabilities/policies, schemas, and recent runs use scannable sections with a single independently scrolling inspector body.                |
| Chart relationship | The selected card keeps its blue emphasis while the complete hierarchy stays fitted; the dock does not overlap the wide chart and the compact overlay does not reflow it.                |
| Interaction states | Loading, stale refresh, error/retry, read-only session, edit, save, dirty warning, validation, conflict, reload, success, and static-runtime states are all explicit rather than silent. |

Above the fold, the concept's illustrative `Newsletter Subscriber`, `Awaiting approval`, and vendor-specific purpose are intentionally replaced by the selected API instance, its actual enabled state, and the vendor-neutral presented purpose. The concept's approval badge/action, kebab menu, fabricated May 2024 history, `View all`, and `Run with mocks` button are intentional deviations: they belong to WEB-04 through WEB-06 or have no authoritative runtime data and therefore are not rendered as inert UI. The existing product header, safe-local-mode strip, source counts, and catalog version remain visible because they are verified shell and safety requirements from WEB-01/WEB-02.

The dependency-free witness imports and executes the production detail normalizer, conditional fetch path, configuration serializer, and session normalizer under Node 24. Component tests cover completeness, loading/error behavior, runtime distinctions, edit authorization, schema options, partial changes, schedule coherence, conflict/validation preservation, and dirty cleanup. Playwright rebuilds the production Vite application, uses the real local hierarchy/detail/session APIs, overlays deterministic same-origin runtime and configuration boundaries, verifies conditional reuse and duplicate identity, exercises save/dirty/conflict/reload behavior, checks that no request leaves loopback, and captures the 1536×1024 and 1280×800 layouts.

Machine authority: [`WEB-03.json`](WEB-03.json).
