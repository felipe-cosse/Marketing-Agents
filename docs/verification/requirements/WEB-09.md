# WEB-09 verification

WEB-09 closes the frontend visual boundary without treating the captured source
frames as a brand kit. The source frames remain authoritative for hierarchy,
source order, names, and broad density. The accepted desktop and mobile concepts
remain implementation references for composition and anatomy, subject to their
documented corrections. Vendor names that are part of authoritative role names
remain text; copied logos, vendor badges, the source watermark, and trade dress
do not become product assets.

The production visual system is code-native. It uses a system-UI-first font
stack, the accepted pale-canvas/surface/text/border/accent tokens, the dedicated
accepted panel shadow, and small custom SVG glyphs whose strokes inherit
`currentColor`. There are no product image or font files under the frontend
source or public asset roots. The detail inspector explicitly tells operators
that capability badges are implementation metadata rather than vendor
affiliations. Source-only vendor commentary is still removed from presented
purposes without erasing source-backed role names.

The browser gate builds the production application and exercises two native
review surfaces. Desktop uses a 1536 by 1024 CSS-pixel viewport at device scale
one. Mobile uses a 426 by 923 CSS-pixel viewport at device scale two, producing
an 852 by 1846 pixel capture that matches the accepted mobile concept's image
dimensions. The journeys retain the exact hierarchy and source order, open the
desktop inspector and mobile detail sheet, inspect computed visual tokens and
layout boundaries, reject horizontal document overflow, inventory rendered
media and loaded resources, and fail on every non-loopback request. Screenshots
are emitted as review artifacts for native-size `view_image` comparison; they
are not brittle cross-platform pixel snapshots.

The focused unit gate selects every Vitest case carrying the `WEB-09` marker.
The dependency-free Node 24 witness reads the production visual boundary,
checks the system font and exact accepted tokens, source-binds the custom icon
module and inspector disclosure, inventories local media/font files, verifies
the local Vite asset boundary, pins the accepted concept dimensions and SHA-256
digests, and requires every section of the fidelity ledger below. Restoring the
changed visual implementation to the base makes that witness fail.

## Native-size fidelity ledger

The reviewed authorities are
`docs/design/concepts/marketing-agents-desktop.png` at 1536 by 1024
(`b67f1007008f5680f31da22c247568e5265d67ed64f29b58ff278cd0262a795c`)
and `docs/design/concepts/marketing-agents-mobile.png` at 852 by 1846
(`f21b53c8817c9064a0eee2b870e22ed92dbf4b74024a1c8a2491d79fe76b2593`).
The six local source frames remain the hierarchy authority, not assets to copy
into the application.

### App chrome and visible copy

- The restrained primary navigation, persistent local-safe-mode row, local
  identity warning, search/filter chrome, and graph controls preserve the
  accepted composition.
- `Demos unavailable` is intentionally honest while the separately owned demo
  requirements remain unimplemented; it is not presented as a working link.
- Illustrative concept IDs, timestamps, counts, delivery language, and vendor
  copy do not override current authoritative API data or safe-mode wording.

### Hierarchy and default viewport

- One `Marketing Agents` root, five source-ordered departments, twelve
  source-ordered functions, and forty-three instance cards remain grouped on a
  pale blue-gray pannable canvas with thin orthogonal connectors.
- The desktop default keeps the complete hierarchy fitted and readable rather
  than copying the source video's crop. Both deployments of each Community
  template remain visible without an invented distinction.
- The control-plane root remains visually distinct and is not presented as
  instance forty-four.

### Typography and visual tokens

- The app uses a local system UI stack, dark high-contrast copy, white rounded
  surfaces, neutral borders, limited state accents, accepted control/card radii,
  and the accepted panel shadow.
- Generic bundled SVGs use `currentColor`; no scraped logo, source watermark,
  remote font, vendor badge, or brand-colored trade dress is loaded.
- Source-backed names such as LinkedIn or YouTube roles remain catalog text.
  Their presence is not represented as a connector affiliation or copied visual
  identity.

### Inspector and mobile sheet

- Desktop selection opens a bounded right inspector with the selected role,
  deployment/template metadata, purpose, capabilities, policies, configuration,
  dry-run controls, and recent-run state.
- The capability disclosure identifies badges as implementation metadata, not
  vendor affiliations. Mock and approval language remains explicit.
- Narrow selection opens the same operational detail as a full-width sheet with
  the accepted surface elevation rather than reproducing illustrative vendor
  iconography.

### Desktop and mobile interaction

- Desktop retains the graph, filters, search, graph/tree choice, zoom, pan, and
  fit controls around the source-modeled hierarchy.
- Mobile defaults to the semantic tree instead of shrinking the graph. It keeps
  source ordering and grouped expansion while exposing the detail sheet.
- The accepted mobile concept's operating-system status bar and home indicator
  are illustrative device chrome and are not copied into the web application.

### Responsive and accessibility continuity

- The declared desktop and mobile review viewports have no horizontal document
  overflow. Text, hierarchy boundaries, neutral icons, and the selected state
  remain visually coherent at both sizes.
- WEB-07 remains the authority for responsive tree behavior and WEB-08 for
  keyboard, focus, contrast, reduced motion, touch sizing, reflow, and automated
  accessibility acceptance. WEB-09 verifies their visual continuity without
  replacing those behavioral gates.
- Status remains expressed with text and iconography rather than color alone.

## Evidence boundary and limitations

Native-size comparison is a documented human visual review supported by browser
captures and executable structural/style assertions; aesthetic alignment is not
reduced to a pixel-diff score. Rendering evidence uses Playwright-managed
Chromium, a system font stack, the production Vite build, a real local hierarchy,
and deterministic same-origin runtime fixtures. It does not claim pixel identity
across operating systems, other browser engines, physical devices, a deployed
reverse proxy, or every route and data state.

The neutral-branding check is a product-source and rendered-asset review, not a
legal trademark opinion or an image-recognition proof. It distinguishes
source-backed vendor words from copied visual branding. Sanitized artifact text
may contain an explicit user-activated HTTP(S) review link, but Markdown images
are rendered as omitted text and the exercised application automatically loads
no remote font, logo, image, stylesheet, or script.

Machine authority: [`WEB-09.json`](WEB-09.json).
