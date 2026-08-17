# 01 — Repository scaffold and toolchain

Status: planned

Depends on: [00 — Source evidence, scope, and assumptions](00-source-evidence-scope-and-assumptions.md)

Unblocks: catalog, domain, persistence, API, frontend, tests, and local operations

## Objective

Create a reproducible monorepo foundation that enforces the intended architecture before feature work begins. A clean checkout must be able to build through Docker without relying on the host's Python or Node versions, while native development remains convenient through pinned toolchains.

## Stack decision

| Area | Choice | Reason |
|---|---|---|
| Backend runtime | Python 3.12 | Required by the prompt |
| API | FastAPI + Pydantic | Required default stack and generated OpenAPI |
| Persistence | SQLAlchemy 2 + Alembic | Required default stack and portable migration path |
| Backend package manager | `uv` with a frozen lock | Fast reproducible installs; Docker and native parity |
| Frontend runtime | Node.js 24 LTS, pinned to a patch release during scaffolding | Supported LTS line as of planning; avoids the host's EOL Node 18 |
| Frontend package manager | `pnpm`, pinned through Corepack | Deterministic workspace install and compact dependency store |
| Frontend | React + TypeScript + Vite | Required default stack |
| Local database | SQLite file | Zero-configuration acceptance path |
| Canonical local startup | Docker Compose | Pins Python/Node and starts API, worker, scheduler, and web consistently |
| Root task interface | `Makefile` | One stable command vocabulary for humans and CI |

The current host check reports Python 3.13.3 and Node 18.12.1, so neither should silently define the target runtime. Node's official release table identifies the 24 line as LTS and Node 18 as end-of-life; pin an exact supported Node 24 patch in `.nvmrc`, CI, and the Docker builder when scaffolding begins: <https://nodejs.org/en/about/previous-releases>.

Before locking exact dependency versions, verify compatibility and support using primary project documentation. Commit every resolved lockfile; CI must use frozen-lock installs.

## Architecture boundaries

Use four backend layers:

1. `domain`: entities, value objects, enums, policies, state transition rules, hashes, and provenance. It imports neither FastAPI, SQLAlchemy, nor any provider/connector SDK.
2. `application`: commands, queries, orchestration, services, and ports. It depends on the domain and abstract interfaces.
3. `infrastructure`: database implementations, catalog loader, identity adapter, providers, connectors, and signature verification.
4. `api`: FastAPI routes, middleware, dependency wiring, and request/response schemas.

The web application consumes generated OpenAPI types rather than re-declaring backend DTOs manually.

## Initial directory layout

```text
.
├── .env.example
├── .gitignore
├── .python-version
├── .nvmrc
├── .github/workflows/
├── Makefile
├── README.md
├── compose.yaml
├── pyproject.toml
├── uv.lock
├── pnpm-workspace.yaml
├── pnpm-lock.yaml
├── apps/
│   ├── api/
│   │   ├── alembic.ini
│   │   ├── alembic/
│   │   └── src/marketing_agents/
│   │       ├── api/
│   │       ├── application/
│   │       ├── domain/
│   │       ├── infrastructure/
│   │       └── workers/
│   └── web/
│       ├── package.json
│       ├── vite.config.ts
│       └── src/
├── catalog/
│   ├── schema/
│   └── v1/
├── docs/
│   ├── adr/
│   └── implementation-plan/
├── scripts/
├── tests/
│   ├── acceptance/
│   ├── catalog/
│   ├── contract/
│   ├── integration/
│   └── unit/
└── references/
```

## Planned root configuration

### Python

`pyproject.toml` should contain:

- Python `>=3.12,<3.13` for the first release.
- Runtime dependencies grouped separately from development dependencies.
- An optional PostgreSQL extra and optional real-provider extra.
- Ruff formatting/lint rules.
- Mypy strictness settings with narrowly documented exceptions.
- Pytest, coverage, async-test, and network-blocking settings.
- Package discovery rooted at `apps/api/src`.

Avoid a second backend dependency manifest under `apps/api` unless workspace tooling requires it. One authoritative lock prevents Docker/CI/native drift.

### Frontend

`apps/web/package.json` should expose:

- `dev`, `build`, `preview`.
- `format`, `format:check`.
- `lint`.
- `typecheck`.
- `test`, `test:coverage`.
- `test:e2e`.
- `generate:api`, `check:api-generated`.

Pin the package manager version in `packageManager` and use Corepack in local/Docker/CI environments.

### Environment

`.env.example` must contain safe, non-secret defaults only:

```text
APP_ENV=local
DATABASE_URL=sqlite+aiosqlite:////data/marketing_agents.db
AUTH_MODE=local
LLM_PROVIDER=mock
CONNECTOR_MODE=mock
ALLOW_EXTERNAL_NETWORK=false
LOG_LEVEL=INFO
```

Do not include a real API key, webhook secret, production URL, or usable shared credential. Optional secrets should be blank and documented.

### Git hygiene

The workspace now has an initialized Git repository but no commits. The implementation phase must create an authorized baseline commit before a literal `git archive`/clean-checkout gate can run. Add ignore rules for:

- `.env` and environment-specific variants.
- Python virtual environments and caches.
- Node dependencies and build output.
- SQLite databases, WAL, and shared-memory files.
- coverage, Playwright, and generated temporary reports.
- local logs and temporary artifacts.

Do not ignore catalog files, migrations, OpenAPI snapshots, or required generated client types.

## Runtime topology

Use one backend image with different commands:

- `api`: accepts requests and returns `202` for work creation.
- `run-worker`: claims and executes persisted run steps.
- `scheduler-worker`: claims due schedules and creates work through the same intake service.
- `web`: serves the built SPA and reverse-proxies `/api` to the internal API service.

Do not add Redis, Celery, a cloud queue, or a separate workflow engine. A database-backed queue is sufficient for the prompt and keeps the foundation inspectable.

Tests should call `drain_once()` or equivalent worker methods rather than starting unbounded background loops or using sleeps.

## Import-boundary enforcement

Add `tests/unit/architecture/test_import_boundaries.py` or an import-linter configuration with these assertions:

- `domain` imports only the standard library and explicitly approved pure utilities.
- `application` may import `domain`, but not `api` or infrastructure implementations.
- `infrastructure` may implement application ports.
- `api` may depend on application DTOs/services and dependency wiring, not ORM models directly.
- `demos` register workflow definitions through application interfaces.
- Frontend feature modules call the central API client, not `fetch` ad hoc.

## Ordered implementation tasks

1. Reconfirm the directory remains greenfield and record any new files added since planning.
2. Verify the empty repository configuration and create the first baseline commit only when implementation is authorized.
3. Write ADR-0001 for stack, workspace, package managers, and layer boundaries.
4. Pin Python 3.12 and a supported Node 24 LTS patch in version files and Docker images.
5. Create the Python source package and minimal FastAPI app factory.
6. Create the Vite React TypeScript application without external fonts, logos, analytics, or network calls.
7. Add the root `Makefile` and make every target fail fast with clear prerequisites.
8. Add formatting, linting, typing, and empty test configurations.
9. Add settings validation with mock/offline defaults and fail-closed real-adapter flags.
10. Implement the project-scoped local digest-key file store/initializer and lifecycle tests; no key is committed or required from the developer.
11. Add a minimal `/health/live` endpoint and web shell to prove service wiring.
12. Add Dockerfiles and Compose service shells with non-root users and scoped writable paths.
13. Add import-boundary tests before domain/application code grows.
14. Generate and commit initial lockfiles using pinned toolchains.
15. Add a CI skeleton that installs from frozen locks and runs the baseline gates.

## Planned files

```text
.python-version
.nvmrc
.env.example
.gitignore
.dockerignore
pyproject.toml
uv.lock
pnpm-workspace.yaml
pnpm-lock.yaml
Makefile
compose.yaml
docker/api.Dockerfile
docker/web.Dockerfile
apps/api/src/marketing_agents/__init__.py
apps/api/src/marketing_agents/config.py
apps/api/src/marketing_agents/security/digests.py
apps/api/src/marketing_agents/infrastructure/secrets/local_digest_key.py
apps/api/src/marketing_agents/workers/local_secret_init.py
apps/api/src/marketing_agents/api/app.py
apps/api/src/marketing_agents/api/routes/health.py
apps/api/src/marketing_agents/workers/run_worker.py
apps/api/src/marketing_agents/workers/scheduler_worker.py
apps/web/package.json
apps/web/src/main.tsx
apps/web/src/app/App.tsx
tests/unit/architecture/test_import_boundaries.py
.github/workflows/ci.yml
```

## Baseline verification

| Check | Planned command | Expected result |
|---|---|---|
| Python format | `make format-check` | No changes required |
| Python/frontend lint | `make lint` | No violations |
| Static types | `make typecheck` | No errors |
| Unit-test harness | `make test` | Baseline tests pass with network blocked |
| Frontend build | `pnpm --dir apps/web build` | Static bundle builds |
| Compose configuration | `docker compose config --quiet` | Valid configuration |
| Mock-safe settings | targeted settings tests | Defaults are mock/offline; unsafe combinations fail |
| Boundary rules | architecture test | No forbidden imports |

## Failure cases to handle early

- Host Python is not 3.12: native command provides an actionable error; Docker remains canonical.
- Host Node is unsupported: native command points to `.nvmrc`; Docker remains canonical.
- Missing `.env`: local stack still starts with safe defaults.
- Real provider selected without network opt-in or credentials: startup fails clearly.
- `APP_ENV=production` with local identity: startup fails.
- SQLite directory is unwritable: readiness fails with a specific diagnostic.
- Alembic is not at head: readiness reports not ready rather than silently migrating in every service.
- Generated OpenAPI types drift: CI fails with the regeneration command.

## Exit criteria

- Pinned native and Docker toolchains are documented.
- Backend and frontend skeletons boot.
- Compose validates and uses mock/offline defaults.
- No service requires cloud credentials.
- Root commands for format, lint, type, test, build, and startup exist.
- Frozen Python and frontend lockfiles exist.
- Import boundaries are executable tests, not prose only.
- No external asset, analytics script, font, model, or connector call occurs at startup.
