# ADR-0005: Immutable action-scoped approval

- Status: Accepted
- Date: 2026-08-18

## Context

A broad run approval could authorize substituted destinations or payloads and could be reused for multiple external effects.

## Decision

Each approval request binds exactly one canonical proposed action: action type, capability, destination, redacted payload, payload hash, run, step, scope, and expiry. Canonical JSON uses versioned domain separation before SHA-256. The server derives the actor from the active identity provider. Decision and one-time consumption are conditional, atomic state changes. Any relevant payload, destination, capability, or binding change supersedes the request. A multi-action workflow may dispatch only after every unchanged required action has a valid approval; it reserves the complete set atomically before calls.

## Consequences

Approvals are more granular and visible. The Email demo waits for two approvals and has zero connector calls with only one. Changing an action requires a new request.

## Verification

Canonicalization, tamper, expiry, reuse, concurrency, authorization-set, and all-approvals barrier tests. Relates to ASM-006 through ASM-008 and ASM-011.
