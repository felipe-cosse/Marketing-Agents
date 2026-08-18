# ADR-0001: Stack and monorepo boundaries

- Status: Accepted
- Date: 2026-08-18

## Context

The repository was greenfield. The prompt supplies a default stack and requires visibly separate domain, runtime, adapters, API, UI, and tests.

## Decision

Use Python 3.12 with FastAPI, Pydantic, SQLAlchemy, and Alembic under `apps/api`; React, TypeScript, and Vite under `apps/web`; and a root task runner. The Python package uses `apps/api/src/marketing_agents`. Catalog source remains under `catalog`, and shared tests remain under `tests`. Dependency versions and container toolchains are pinned. Imports flow inward: API and infrastructure may depend on application/domain ports, while domain code cannot import FastAPI, SQLAlchemy, provider SDKs, or UI code.

## Consequences

The local build has two language toolchains but one repository contract. Docker supplies Python 3.12 and the pinned Node runtime even when host versions differ. Package-boundary tests are required.

## Verification

Toolchain, import-boundary, backend build, frontend build, and clean-container tests. Relates to ASM-001 and ASM-012.
