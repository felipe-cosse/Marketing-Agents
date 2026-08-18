# ADR-0002: Catalog as versioned authority

- Status: Accepted
- Date: 2026-08-18

## Context

The organization must preserve exact source-backed names and counts without mixing reusable role definitions with deployment configuration.

## Decision

Store a versioned, ordered YAML/JSON catalog under `catalog/v1`, validate it with local Draft 2020-12 JSON Schemas, and compile it into an immutable projection before database writes. Templates own names, purposes, prompts, schemas, capabilities, triggers, effect classification, approval policy, and execution bounds. Instances own only deployment state and refer to exactly one template. References must remain inside the catalog root; remote references and path traversal fail validation. A canonical semantic hash excludes paths, mtimes, and load timestamps.

## Consequences

The database is a projection, not the editing authority. Catalog changes are reviewable and repeatable. Compiler failure blocks startup readiness and seed operations.

## Verification

Structural, reference, semantic, exact-inventory, hash-stability, and reseed tests. Relates to ASM-002, ASM-003, and ASM-023.
