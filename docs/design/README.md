# Marketing Agents product design specification

Status: accepted implementation reference

## Source hierarchy

The product hierarchy remains governed by `IMPLEMENTATION_PROMPT.md` and the six
captured source frames in `references/`. The generated concepts below are visual
implementation references only; they must never override catalog names, counts,
ordering, effect classification, or safety policy.

Source-backed facts:

- One visible `Marketing Agents` root.
- Five departments in order: Social media, Blog & SEO, Email, Community, and
  Partnerships.
- Twelve function groups, 36 templates, and 43 deployed instances.
- Community contains seven shared templates with two deployments each, displayed
  as `.01` and `.02` without an invented business distinction.
- The Marketing Orchestrator is control-plane status, not instance 44.

## Accepted concepts

- Desktop: [`concepts/marketing-agents-desktop.png`](concepts/marketing-agents-desktop.png)
- Mobile: [`concepts/marketing-agents-mobile.png`](concepts/marketing-agents-mobile.png)

The concepts were generated from the local source frames for implementation
clarity. They define visual structure, density, responsive behavior, and component
anatomy. All UI text and controls remain code-native.

## Required corrections to illustrative concept content

The concept generator supplied a few illustrative values. Implementation must
correct them as follows:

- Use only the authoritative catalog inventory and exact `5/12/36/43` counts.
- Use stable IDs from plan 02; do not use illustrative IDs visible in the images.
- Use vendor-neutral purposes and capability labels. In particular, Newsletter
  Subscriber targets the configured newsletter system rather than naming Loops.
- Use neutral internal icons; do not copy vendor logos, badges, or the watermark.
- Label mock effects as mock receipts and never imply real external delivery.
- Use current injected timestamps and deterministic fixtures, not dates shown in
  the concept.
- Partnership recommendations remain advisory, and Community reminders remain
  unsent drafts with recommended UTC times.

## Desktop composition

- A restrained top navigation row: `Org chart`, `Approvals`, `Runs & audit`, and
  `Demos`.
- An always-visible safe-mode row containing `Local environment`,
  `Deterministic mock model`, `Mock connectors`, `External network off`, and
  `Local identity — not production authentication`.
- Search and explicit department/function/deployment-status/recent-run-state/
  capability filters.
- A wide, pannable hierarchy canvas with orthogonal connectors and deterministic
  source order.
- Zoom out, zoom level, zoom in, fit, and pan controls with keyboard equivalents.
- A selected-agent inspector containing template identity, deployment ID, purpose,
  capabilities, triggers, approval policy, recent runs, configuration, and the
  `Run with mocks` action.
- Pending approval count is operational state, never an invented business metric.

## Mobile composition

- Preserve the same navigation labels and local-identity warning.
- Replace the graph with one semantic `tree`/`treeitem` hierarchy; do not shrink
  the desktop graph into an unreadable viewport.
- Use previous/next visible-item navigation, parent/collapse and child/expand
  navigation, Home/End, type-ahead, visible focus, and correct ARIA metadata.
- Use at least 44 by 44 CSS-pixel touch controls.
- Present agent details in a bottom sheet or dedicated full-width route without
  horizontal page overflow.

## Design tokens

| Token | Value | Use |
|---|---|---|
| `--color-canvas` | `#f2f7fb` | True pale blue-gray hierarchy background |
| `--color-surface` | `#ffffff` | Navigation, cards, panels, sheets |
| `--color-text` | `#172033` | Primary text |
| `--color-muted` | `#667085` | Descriptions and metadata |
| `--color-border` | `#cfd8e3` | Group boundaries and controls |
| `--color-accent` | `#2463eb` | Active navigation, selection, primary action |
| `--color-awaiting` | `#c65f00` | Awaiting approval only |
| `--color-safe` | `#11854b` | Validated/succeeded safe state |
| `--radius-control` | `8px` | Inputs and compact controls |
| `--radius-card` | `10px` | Agent cards and hierarchy labels |
| `--shadow-panel` | `0 12px 36px rgb(30 50 80 / 12%)` | Inspector and mobile sheet only |

Use a system UI font stack with explicit control typography. Do not load remote
fonts. Default app chrome is 13–14px, body content is 14–16px, and hierarchy names
must remain legible at the default canvas zoom.

## Component families

- `AppShell`, `PrimaryNav`, `SafeModeBar`, and `SessionWarning`.
- `CatalogToolbar`, `SearchInput`, `FilterMenu`, and `CanvasControls`.
- `OrgChartCanvas`, `HierarchyGroup`, `AgentCard`, and `OrthogonalEdge`.
- `OrgTree`, `TreeItem`, and `MobileAgentSheet`.
- `AgentInspector`, `MetadataList`, `CapabilityList`, `RunSummary`, and
  `MockRunButton`.
- `ApprovalQueue`, `ApprovalDecisionDialog`, `RunTimeline`, `ArtifactViewer`, and
  schema-driven configuration/dry-run forms.

## Motion and accessibility

- Motion clarifies viewport changes, panel entry, and state transitions only.
- Respect `prefers-reduced-motion` and provide non-animated equivalents.
- Never encode state with color alone.
- Maintain 4.5:1 text contrast and 3:1 non-text control contrast.
- Preserve focus across canvas/tree mode changes and inspector/sheet closure.
- All generated/model content renders as text or sanitized structured components;
  no raw HTML sink is permitted.

## Fidelity acceptance

Final visual verification compares native-size browser screenshots against both
concept images with `view_image`. The fidelity ledger must cover at least:

1. App chrome and allowed visible copy.
2. Hierarchy order, grouping, density, and default viewport.
3. Typography, colors, borders, radii, and icon treatment.
4. Inspector and mobile sheet anatomy.
5. Desktop canvas and mobile semantic-tree interaction.
6. Responsive overflow, focus, reduced motion, and touch targets.
