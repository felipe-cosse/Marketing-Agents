# ADR-0010: Data classification, redaction, and retention

- Status: Accepted
- Date: 2026-08-18

## Context

Restart recovery and approval integrity require durable data, while prompts, connector payloads, artifacts, and errors may contain personal or secret data.

## Decision

Classify fields as public, internal, personal, sensitive, or secret. A central field-aware redactor applies before persistence, logs, audit metadata, API projections, and connector/model diagnostics. Secrets are never persisted. Default local TTLs are configurable: execution/action detail 7 days, artifact and mock-receipt detail 30 days, and non-sensitive audit skeleton 90 days. Retention may delete or pseudonymize optional detail but preserves immutable event type, correlation IDs, transition/decision fact, and timestamp. Transition and approval audit events are written in the same transaction as their state changes. Backups pair the database with the local digest-key material and fingerprint; mismatched restore pairs fail closed.

## Consequences

The local database still relies on host filesystem protection and is not application-encrypted. Debugging sees redacted envelopes. Production encryption and key management remain deployment work.

## Verification

Nested canary, log/API redaction, TTL, preserved-skeleton, audit rollback, secret scan, and paired backup/restore tests. Closes ASM-024 and documents the local storage limitation.
