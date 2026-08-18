# ADR-0006: Loopback-only local identity

- Status: Accepted
- Date: 2026-08-18

## Context

The credential-free demo needs an approval actor, but trusting caller-supplied actor headers would permit trivial impersonation.

## Decision

In local mode the server supplies one fixed principal and roles from configuration; it never accepts actor identity from request headers or bodies. Local identity is permitted only when every listener is loopback-bound, trusted-proxy forwarding is disabled, and the environment is not production. Mutating HTTP requests require same-origin CSRF protection. Self-approval is an explicit local-v1 policy and the UI displays that limitation. A future identity provider implements the same principal/authorization port.

## Consequences

The demo remains credential-free without pretending to be production authentication. Remote exposure or production plus local auth fails closed at startup.

## Verification

Settings-combination, spoofed-header, CSRF, role/scope, self-approval, host, and forwarded-header tests. Closes ASM-011 for local v1 and relates to ASM-012.
