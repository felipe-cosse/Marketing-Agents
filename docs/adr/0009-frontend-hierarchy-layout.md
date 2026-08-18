# ADR-0009: Frontend hierarchy layout

- Status: Accepted
- Date: 2026-08-18

## Context

The UI must preserve a wide source-modeled org chart while remaining usable with keyboard, reduced motion, and narrow screens.

## Decision

Use a deterministic custom tree layout rendered with semantic React components and CSS/SVG connectors. The desktop canvas supports bounded pan/zoom while every node also participates in an accessible tree. At narrow widths the same ordered data renders as an expandable list/tree with a detail sheet; it does not shrink the full canvas. Search and filters operate on a single normalized hierarchy model. Neutral icons and internal capability badges replace source vendor branding. Motion is optional and disabled under reduced-motion preferences.

## Consequences

No graph-library runtime or branding dependency is required. Layout and interaction logic need focused tests and a desktop/mobile fidelity comparison against the accepted concept images.

## Verification

Component counts, keyboard traversal, focus, search/filter, responsive Playwright, Axe, XSS, reduced-motion, and visual review tests. Relates to ASM-021 and ASM-022.
