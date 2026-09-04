# ARCH-08 verification

ARCH-08 makes the repository's domain, application runtime, infrastructure adapters, API, web UI, and test ownership executable rather than conventional. The checked-in boundary policy declares required homes, inward Python dependency directions, exact per-layer third-party dependency ownership, narrow reviewed composition exceptions, pure contracts, API-to-ORM prohibitions, and frontend transport and test boundaries.

The duplicate top-level backend adapter package is removed. Its fail-closed local safe-profile registry now lives with the concrete provider and connector implementations under `infrastructure/adapters`; readiness and its SAFE-01 regression tests import that canonical home.

The hierarchy response contract now lives in the framework-neutral frontend `src/contracts` layer, while normalization and transport live under `src/api`. The org-chart feature keeps compatibility re-exports that point inward, so existing presentation consumers retain their public imports without allowing the API layer to depend outward on features.

The dependency-free checker parses Python imports through the standard-library AST and tokenizes TypeScript static and quoted-literal dynamic imports, main-entry dependency closure, direct UI `fetch` calls, and API path ownership. Focused Python and TypeScript negative fixtures deliberately introduce missing or escaping scan roots, legacy adapter ownership, outward and ORM imports, unapproved framework and SDK dependencies, inert import-like source text, test leakage, impure contracts, multiline API-to-feature imports, and UI-owned transport; every case must be rejected.

`verify-architecture` is part of the existing `verify-governance` CI path, and the focused frontend boundary suite is part of `verify-web`. This makes the contract a continuing guard for later implementation branches without claiming the final delivery aggregates owned by DEL-05 and DEL-07.

The causal witness retains the policy and checker while restoring the substantive backend and frontend implementation paths to the base tree. That restoration reintroduces both the legacy adapter home and API-to-feature edge, so the same dependency-free gate must fail for product reasons.

Static source analysis cannot prove runtime side effects, computed dynamic imports, reflection, dependency-injection behavior, or deployed topology. The reviewed composition exceptions remain explicit in `architecture-boundaries.json`, and generated OpenAPI client authority remains later delivery work.

Machine authority: [`ARCH-08.json`](ARCH-08.json).
