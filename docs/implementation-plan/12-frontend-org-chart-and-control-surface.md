# 12 — Frontend org chart and control surface

Status: planned

Depends on: [09 — API contract](09-api-contracts-local-identity-and-errors.md), [11 — Demo scenarios](11-deterministic-department-demos.md)

Unblocks: browser acceptance, accessibility verification, and product demo

## Objective

Build an accessible React control surface in which the exact source hierarchy is both a visualization and an operational entry point for instance details, deployment configuration, schema-driven dry runs, approvals, run timelines, artifacts, and audit history. Preserve the source's clean hierarchy and density without copying logos, watermark, or vendor branding.

## Frontend architecture

```text
apps/web/src/
├── main.tsx
├── app/
│   ├── App.tsx
│   ├── router.tsx
│   ├── providers.tsx
│   └── ErrorBoundary.tsx
├── api/
│   ├── client.ts
│   ├── problems.ts
│   └── generated/schema.ts
├── features/
│   ├── session/
│   ├── catalog/
│   ├── org-chart/
│   ├── instance-detail/
│   ├── instance-config/
│   ├── dry-run/
│   ├── approvals/
│   ├── runs/
│   ├── artifacts/
│   ├── audit/
│   └── demos/
├── components/
├── hooks/
├── styles/
│   ├── tokens.css
│   ├── global.css
│   └── utilities.css
└── test/
    ├── setup.ts
    └── fixtures.ts
```

Use:

- React Router for page state/URLs.
- A query/cache library for server state, bounded polling, invalidation, and mutation conflicts.
- Generated OpenAPI types and one centralized API client.
- A maintained canvas library such as `@xyflow/react` for pan/zoom/viewport primitives, isolated behind `OrgChartCanvas`.
- A custom deterministic hierarchy layout rather than a force-directed graph.
- Vitest, Testing Library, MSW, automated accessibility checks, and Playwright.

Pin exact package versions during implementation and record the graph-library choice in ADR-0009. The semantic tree fallback is an application feature, not delegated to the graph library.

## Session and mutation security

The centralized API client owns the local CSRF flow:

1. Fetch `GET /api/v1/session` during application bootstrap with cache disabled.
2. Keep `csrfToken` only in memory; never use local storage, session storage, a URL, analytics, or error reporting.
3. Add `X-CSRF-Token` and JSON content type to every control-plane mutation while preserving any operation idempotency key.
4. If the API returns the specific pre-handler `csrf_token_invalid` problem after an API restart, refetch the no-store session once and retry the still-idempotent mutation once. Do not retry other `401`/`403` responses automatically.
5. Clear the in-memory token when the session/auth mode changes or the application shell unmounts.

Client tests must prove the header is absent from reads, present on every mutation, never persisted, refreshed after a simulated API restart, and not sent to any non-same-origin URL.

## Application shell

Primary navigation:

- `Org chart`
- `Approvals` with pending count
- `Runs & audit`
- `Demos`

Persistent safe-mode header:

- `Local environment`
- `Deterministic mock model`
- `Mock connectors`
- `External network off`
- `Local identity — not production authentication`

Never use success wording that implies a mock action reached a real service. A mock write is labeled `Mock connector receipt`.

## Hierarchy contract

Default unfiltered rendering contains:

- One Marketing Agents root.
- Five departments in authoritative source order.
- Twelve functions in authoritative source order.
- Forty-three selectable agent-instance cards.
- Thirty-six shared template identities in details/count summaries.

The Marketing Orchestrator may appear as a small `Control plane` status badge/panel attached visually to the root. It must be explicitly excluded from instance counts and must not look like a source role card.

## Deterministic visual layout

### Node levels

1. Root.
2. Departments.
3. Function headers/containers.
4. Agent instance cards.

### Geometry

- Use explicit widths/heights/gaps from design tokens.
- Lay departments left-to-right by `displayOrder`.
- Lay functions within departments by `displayOrder`.
- Stack agents vertically inside a rounded function container by `displayOrder`.
- Route thin orthogonal, non-interactive edges.
- Calculate department/function extents from child card counts.
- Fit the complete hierarchy on first load while preserving a readable maximum zoom path.
- Recompute only when hierarchy/filter/viewport mode changes.
- Do not use random layout seeds or alphabetical sorting.

The graph is expected to be wider than a typical viewport. Pan/zoom is intentional and matches the evidence that department frames are cropped views of one canvas.

## Visual system

Use internal tokens:

- Pale blue-gray canvas.
- White cards and hierarchy pills.
- Neutral gray borders/connectors.
- Dark high-contrast text.
- Limited department accents that meet contrast requirements.
- Neutral capability icons from a bundled open icon set or custom SVGs.
- Clear focus ring independent of status color.
- Status chips with text and icon, not color alone.

Agent card content:

- Neutral role/capability icon.
- Exact display name.
- Concise source-backed purpose.
- Enabled/disabled state.
- Generic capability badges.
- Recent run state when available, clearly separate from deployment state.
- Community source-ordinal chip when the template has two deployments.

Do not embed remote fonts/assets or copy product logos/watermarks.

The stable hierarchy query and separately polled instance-status summary are merged by stable instance ID. A run-state update must not force the full hierarchy/layout to refetch or recompute.

## Community duplicate treatment

For every Community template:

- Render both `.01` and `.02` cards in source order.
- Show `Instance 1 of 2` and `Instance 2 of 2` as a neutral deployment chip.
- Include the ordinal in the accessible name.
- Show distinct instance IDs and the shared template ID in details.
- Show `14 deployed instances · 7 reusable templates` on the Community department summary.
- Do not imply geography, schedule, audience, language, A/B test, or business specialization.

## Search and filters

Toolbar controls:

- Search display name, purpose, stable ID, and capability label.
- Department filter.
- Function filter constrained by selected department.
- Deployment state: enabled/disabled.
- Recent run state as a separate filter.
- Connector capability filter.
- Clear all.
- Zoom in/out.
- Fit/reset view.
- Graph/tree view toggle.

Rules:

- Matching instances retain their root/department/function ancestors.
- Default view always shows the full hierarchy.
- Search result count is announced in a polite live region.
- Empty results preserve a usable toolbar and clear action.
- If a focused node disappears, move focus to its nearest visible ancestor or search control.
- Filter state is reflected in URL query parameters where practical for shareable local views.

## Keyboard and semantic navigation

Avoid forcing users to tab through all hierarchy nodes. The semantic tree follows the standard ARIA tree keyboard pattern with `tree`/`treeitem`, `aria-expanded`, `aria-level`, `aria-posinset`, and `aria-setsize` as applicable:

- Up/down: previous/next visible tree item, regardless of sibling boundary.
- Left: collapse an expanded item; otherwise move to its parent.
- Right: expand a collapsed item; otherwise move to its first child.
- Home/End: first/last visible tree item.
- Enter/Space: select and open details.
- `/`: focus search unless typing in a form.
- Escape: close sheet/dialog and restore focus.
- Printable character type-ahead: move to the next visible item whose accessible name begins with the typed buffer.

Graph nodes expose meaningful labels/levels and pan the selected node into view. If the canvas library cannot provide robust tree semantics, the explicit `Tree view` is the accessibility authority. Only one graph/tree representation is interactive and exposed to assistive technology at a time.

## Responsive behavior

| Width | Default behavior |
|---|---|
| Wide desktop | Pannable graph with docked side detail panel |
| Medium/tablet | Graph with overlay detail sheet |
| Narrow/mobile | Expandable semantic department/function tree/list by default |

On narrow screens:

- Search remains immediately available.
- Filters move into a labeled sheet.
- Detail, approval, and artifact views become full-screen sheets/pages.
- Cards retain readable type and 44px touch targets.
- The app never shrinks the whole graph into illegible mini-cards.
- Users may still switch to graph mode if desired.

## Agent detail panel

Sections/tabs:

1. Overview: display name, purpose, department/function, effect, enabled state.
2. Deployment: instance ID, source ordinal, bindings, triggers, schedule, config revision.
3. Template: shared template ID, deployment count, source confidence, implementation notes.
4. Schemas: input/output structured views and raw JSON toggle.
5. Capabilities/policies: tools, effect, approval, retry, timeout, budget, rate limit.
6. Dry run: schema-driven form and demo presets.
7. Recent runs: state, timestamps, selected workflow, artifact/approval links.

Opening/closing preserves node focus. The panel title includes the instance ordinal when duplicated.

## Instance configuration

Editable fields are generated from an explicit deployment-config schema, not from the full instance response.

- Enabled state.
- Allowed trigger bindings/parameters.
- Registered mock connector bindings.
- Schedule configuration/misfire policy.
- Source ordinal is read-only.

Behavior:

- Explicit edit/save/cancel.
- Dirty-state warning before close/navigation.
- Send expected revision/`If-Match`.
- On `409`, show current server version and offer reload; never overwrite silently.
- Template-owned fields are never editable.
- Save success is announced and invalidates hierarchy/detail queries.

## Schema-driven dry-run form

Planned components:

```text
features/dry-run/
├── DryRunPanel.tsx
├── SchemaForm.tsx
├── SchemaField.tsx
├── ObjectField.tsx
├── ArrayField.tsx
├── FieldError.tsx
├── schemaDefaults.ts
├── schemaValidation.ts
└── mapProblemDetails.ts
```

Supported JSON Schema subset:

- Objects/nested objects with `additionalProperties: false`.
- Required fields.
- String, integer, number, boolean.
- Enum.
- Bounded arrays of primitives or bounded objects.
- Title, description, default, examples.
- String/number/array bounds.
- Date, date-time, URI, and email formats.
- Canonical `x-sensitive` annotation for data classification and browser persistence/logging behavior.
- Local `x-ui` annotations for presentation only, such as textarea/order/help. `x-ui` must not override sensitivity or policy.

Reject or render a safe explicit unsupported message for complex constructs not implemented/tested, including arbitrary objects and deep conditionals.

Form requirements:

- Client validation for feedback; server remains authoritative.
- Labels, required state, help, and errors programmatically connected.
- Error summary focuses the first invalid field.
- Map server JSON-pointer errors to controls.
- Sensitive values use appropriate controls and are not retained in browser logs/storage.
- URI fields warn that validation does not imply a fetch.
- Submit labels are `Create dry run` or `Run with mocks`.
- Mutating templates explain per-action approval before submit.
- Idempotency key is generated/persisted for safe UI retry.
- Successful submit navigates to the run timeline.

## Approval queue

Features:

- Pending first, with status/department/action-type filters.
- Show exact action type, destination summary, redacted payload, payload hash, requesting instance, run/step, expiry, and one-time state.
- Approve/reject dialog repeats the immutable action, not only the run name.
- Client sends expected hash; server remains authoritative.
- Disabled decision controls for expired/decided/consumed/superseded requests.
- No optimistic success claim; display server result/conflict.
- Clear distinction between approval recorded and connector action completed.
- Link to resulting timeline/external-action receipt.

For Email, show both approvals separately and an explicit `0 mock connector calls until both are approved` status from safe run summary data.

## Run timeline

Display stable sequence order with:

- Run state transitions.
- Selected agents/config/policy snapshots.
- Step readiness/attempt/result.
- Model and connector attempt summaries.
- Approval requested/decided/expired/consumed.
- External action proposed/dispatched/result and idempotency key.
- Artifact creation/provenance.
- Cancellation/failure/rejection.

Use text and icons, not color alone. Bounded polling stops or slows when terminal and pauses on hidden tabs where appropriate.

## Artifact viewer

- Render plain text as text.
- Render structured objects/arrays through safe components.
- Offer escaped syntax-highlighted JSON.
- Render Markdown only through a sanitizer configured to disallow raw HTML, scripts, remote embeds, and dangerous URLs.
- Never use unsanitized `dangerouslySetInnerHTML`.
- Show schema ID, hash, sensitivity, producer, parents/sources, and mock/provider version.
- Advisory artifacts display a prominent `Advisory — human decision required` banner.
- Mock receipts display `No real external delivery occurred`.

## Accessibility requirements

- Text contrast at least 4.5:1; non-text controls/boundaries at least 3:1 where required.
- Visible focus independent of color.
- Status expressed in text/icon plus color.
- 44×44 CSS-pixel touch controls on touch layouts.
- Zoom/pan available through buttons/keyboard, not mouse gesture only.
- Respect `prefers-reduced-motion`; no animated edges or forced smooth panning.
- Correct headings, landmarks, dialog/sheet focus trap, and focus restoration.
- Live regions for search counts, save status, approval result, and run-state change.
- Automated axe checks plus keyboard-only manual/browser scripts.

## Performance and resilience

- Forty-three cards do not require virtualization; prioritize deterministic accessibility.
- Memoize layout from catalog hash/filter state.
- Abort stale detail/list requests.
- Bound polling and exponential backoff.
- Show offline/API-not-ready states without losing unsaved form values.
- Use an application error boundary and route-level recovery.
- Do not persist sensitive inputs in local storage, analytics, or error reports.

## Ordered implementation tasks

1. Freeze the hierarchy/detail/config/run/approval/artifact OpenAPI projections and generate TypeScript types.
2. Build the application shell, route structure, in-memory session/CSRF-aware API client, safe-mode/session banner, design tokens, and error boundary.
3. Normalize the ordered hierarchy and implement/test the deterministic root/department/function/card layout independent of the canvas library.
4. Add canvas pan/zoom/fit controls, custom nodes/edges, selection, and complete source-count assertions.
5. Add the semantic tree mode, narrow-screen default, roving keyboard focus, focus restoration, and reduced-motion behavior.
6. Add search, separate deployment/run-state filters, capability filters, URL state, and accessible result announcements.
7. Build the instance detail and deployment-only configuration panel with optimistic revision conflict handling.
8. Build/test the documented JSON Schema form subset and server JSON-pointer error mapping; render every one of the 36 real catalog input schemas in a cross-layer contract test.
9. Build the approval queue and immutable exact-action decision dialogs.
10. Build run timeline, artifact/provenance viewer, advisory/mock labels, and sanitized Markdown/JSON rendering.
11. Integrate all five demo presets and the Email zero-call/two-approval surface.
12. Run component, accessibility, desktop/mobile, keyboard-only, XSS, and no-external-request browser tests before visual polish sign-off.

## Tests

Component/unit:

```text
apps/web/src/api/client.test.ts
apps/web/src/features/org-chart/*.test.tsx
apps/web/src/features/dry-run/*.test.tsx
apps/web/src/features/approvals/*.test.tsx
apps/web/src/features/runs/*.test.tsx
apps/web/src/features/artifacts/*.test.tsx
```

Required cases:

- Session/CSRF token is no-store/in-memory only, added to every mutation, refreshed once on the specific pre-handler stale-token response, and never sent cross-origin.
- Normalize/render exact `5/12/43` hierarchy and source order.
- Community has 14 cards and seven shared template IDs.
- Both duplicate instances are independently selectable and accessibly labeled.
- Search/filter keeps ancestors and restores focus safely.
- Roving keyboard model.
- Graph/tree view exposes only one interactive hierarchy.
- Detail completeness and template/instance separation.
- Config revision conflict.
- Schema form primitives, nesting, arrays, defaults, bounds, formats, and server error mapping.
- Every real catalog input schema renders without an unsupported/unsafe field and honors canonical `x-sensitive` metadata.
- Approval disabled/conflict/hash behavior.
- Timeline sequence ordering.
- Artifact provenance and HTML/XSS safety.
- Reduced motion and responsive tree behavior.

Playwright acceptance:

1. Load desktop chart and verify exact counts.
2. Use zoom buttons/fit view and search for a role.
3. Select both Community duplicate instances and compare shared template/distinct IDs.
4. Use keyboard only from search to node, detail, form, run, and artifact.
5. Verify mobile defaults to semantic tree.
6. Run four read-only demos and inspect artifacts.
7. Run Email to awaiting approval and verify zero-call status.
8. Exercise invalid and valid approval paths.
9. Verify every timeline transition and mock receipt label.
10. Run accessibility scans on chart/tree, detail, form, approvals, run, and artifact pages.
11. Exercise each required filter independently and in representative combinations.
12. Verify narrow mobile, tablet overlay, wide desktop, and browser 200% zoom/reflow behavior.

## Exit criteria

- Complete hierarchy and exact counts render from API data.
- Community duplication is visibly and semantically correct.
- Org chart is pannable/zoomable and a semantic narrow-screen tree is complete.
- Search, filters, details, configuration, forms, approvals, timeline, and artifacts work.
- Keyboard, focus, contrast, touch, and reduced-motion requirements pass.
- No copied logos/watermark, remote assets, or unsafe artifact HTML exist.
- Every mock/external-action label is honest.
- Frontend unit, accessibility, build, and Playwright suites pass.
