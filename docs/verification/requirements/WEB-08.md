# WEB-08 verification

WEB-08 adds an executable accessibility boundary around the production control surface. Its browser journey uses keyboard input and automated Axe scans on the concrete organization-chart, detail, approval, run, and artifact states that it opens. Labels, landmarks, status announcements, modal semantics, visible focus, contrast, reduced motion, and reflow are evaluated on those rendered states rather than inferred from component names.

The production focus treatment applies to links, buttons, form fields, and explicit tab stops. It uses an outline and offset plus the shared focus-ring token, so focus is not communicated only by a status color. The reduced-motion policy changes scroll behavior and collapses animation and transition duration; the organization-tree disclosure also removes its transition under the same media preference.

The application shell retains named header, primary-navigation, safe-mode, and route-main landmarks. Catalog result counts and the exercised approval/run/artifact state changes use polite live regions or status roles. The approval-decision and unsaved-change surfaces retain a modal role, programmatic title and description, initial focus, Tab containment, Escape handling when dismissal is available, and focus restoration to a viable prior control or documented fallback.

The focused unit gate selects every Vitest case carrying the `WEB-08` marker. The dependency-free witness reads the production CSS and React sources under pinned Node 24.20.0 and fails if the focus, reduced-motion, landmark, live-region, or modal source connections disappear. This source witness is intentionally narrower than behavioral proof: the Playwright gate owns computed styles, actual keyboard focus movement, rendered accessible names, Axe results, the declared reflow viewport, and the loopback-only network boundary.

The browser gate uses Playwright-managed Chromium and Axe only on the states and viewports it explicitly exercises, and only serious or critical Axe violations block acceptance. Lower-impact findings remain outside this gate's acceptance threshold. Axe is an automated rules engine, not complete accessibility conformance or a substitute for screen-reader and manual usability evaluation. The evidence therefore does not claim other browser engines, physical-device input, operating-system assistive-technology parity, or deployed reverse-proxy behavior. WEB-09 retains final visual-fidelity and neutral-branding sign-off.

Machine authority: [`WEB-08.json`](WEB-08.json).
