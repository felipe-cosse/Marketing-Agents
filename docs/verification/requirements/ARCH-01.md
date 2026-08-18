# ARCH-01 verification

Status: verified locally

The backend is pinned to Python 3.12 and a lockfile resolves FastAPI, Pydantic, SQLAlchemy, Alembic, and the supporting local stack. The package establishes application, domain, infrastructure, adapters, API, and workers boundaries. Settings are frozen, typed, secret-masking, mock-first, and loopback-only for local identity. Unsafe host, production-local-auth, real-provider, and unknown-setting combinations fail validation.

The FastAPI application is created through a side-effect-free factory and exposes typed liveness with trusted-host rejection. Ruff format/lint, strict mypy, and all Python tests pass under the pinned environment. Database readiness and PostgreSQL repository compatibility remain separately owned by API-01 and ARCH-03.
