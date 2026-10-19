# README visual assets

These files support the [project README](../../README.md). Product images are actual
browser captures of the local application; they are not generated UI concepts.

## Capture provenance

- Captured: 2026-10-19, America/Los_Angeles.
- Application source: `691a03d4b1e816007bc0d12e63d29ed7c2e6cce2`.
- Environment: native supervised stack, owned PostgreSQL, fresh isolated state,
  deterministic mock model, mock connectors, local operator identity.
- Browser: Chrome through Playwright, 1600 × 1050 CSS-pixel viewport, light theme.
- Data: versioned seeded catalog and the built-in Social content draft safe preset.
  No customer data, provider credentials, or real delivery was used.

| Asset | Content |
| --- | --- |
| [org-chart.png](org-chart.png) | Full seeded organization in graph view, before any demo run |
| [demos.png](demos.png) | Social demo switchboard and safe input preset |
| [artifact-output.png](artifact-output.png) | Actual authorized output panel from the completed Social demo |
| [system-architecture.svg](system-architecture.svg) | Editable diagram of the default Compose topology |

## Reproduce the screenshots

1. Use a fresh, separately named local installation; follow [operations](../operations.md).
   Keep the application's mock provider settings.
2. Open the UI in a 1600 × 1050 browser viewport. Capture **Org chart** with
   **Graph** selected and the complete hierarchy fitted.
3. Open **Demos**, keep **Social content draft**, and capture the initial safe preset.
4. Select **Create draft**, then **Open accepted run**. Wait for **Completed**.
5. Open the produced artifact under **Artifacts & provenance**. Capture the
   **Authorized payload (bounded view)** panel in **Structured view**.
6. Review every image for accidental private data before replacing it here. PNGs are
   direct captures; only the artifact panel uses an element-level crop.

Timestamps, run IDs, and installation-keyed digests naturally differ between captures.
These screenshots illustrate deterministic mock behavior; they do not establish
full-suite verification or external delivery. The architecture diagram represents
[compose.yaml](../../compose.yaml), while the screenshots use the native development UI.

The SVG has an embedded accessible title/description and no external image or font
references. Update it alongside composition changes and check it against
[the architecture guide](../architecture.md).
