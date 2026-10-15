# DEL-05: immutable acquisition, then a non-root, mock-only local runtime.
FROM node:24.20.0-bookworm-slim@sha256:ba849c60be29959425b8734d57b8b4b7d56f98edd9504c9af091d5281095a71e AS verification-node
FROM ghcr.io/astral-sh/uv:0.10.7@sha256:edd1fd89f3e5b005814cc8f777610445d7b7e3ed05361f9ddfae67bebfe8456a AS uv
FROM postgres:17.11-bookworm@sha256:3645570cccdfa447589da9f57dd740faa29b30938e861289a5574b6ca6b03826 AS postgres-tools
FROM python:3.12.14-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254 AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY --from=postgres-tools /usr/lib/postgresql/17 /usr/lib/postgresql/17
COPY --from=postgres-tools /usr/share/postgresql/17 /usr/share/postgresql/17
COPY --from=postgres-tools /usr/share/postgresql/postgresql.conf.sample /usr/share/postgresql/postgresql.conf.sample
COPY --from=postgres-tools /usr/lib/*-linux-gnu/libpq.so.5* /usr/local/lib/
RUN apt-get update && apt-get install --yes --no-install-recommends \
      libzstd1 liblz4-1 libxml2 libpam0g libssl3 libgssapi-krb5-2 zlib1g \
      libldap-2.5-0 libicu72 libsystemd0 libreadline8 \
    && apt-get clean && ldconfig \
    && for binary in /usr/lib/postgresql/17/bin/*; do ln -s "$binary" /usr/local/bin/; done
RUN groupadd --gid 10001 marketing && useradd --uid 10001 --gid 10001 --no-create-home marketing \
    && useradd --uid 999 --gid 10001 --no-create-home postgres \
    && mkdir -p /app /var/lib/marketing-agents/data /var/lib/marketing-agents/secrets /var/run/marketing-agents \
    && chown -R 10001:10001 /app /var/lib/marketing-agents \
    && chown 10001:10001 /var/run/marketing-agents \
    && chmod 0750 /var/run/marketing-agents \
    && chmod 0700 /var/lib/marketing-agents/data /var/lib/marketing-agents/secrets \
    && mkdir -p /var/lib/postgresql/data /var/run/marketing-agents-postgres \
    && chown 999:10001 /var/lib/postgresql/data /var/run/marketing-agents-postgres \
    && chmod 0700 /var/lib/postgresql/data \
    && chmod 0750 /var/run/marketing-agents-postgres
WORKDIR /app

FROM base AS postgres
ARG SOURCE_REVISION=uncommitted
LABEL org.opencontainers.image.title="Marketing Agents private PostgreSQL" \
      org.opencontainers.image.revision=$SOURCE_REVISION
COPY docker/postgres /app/postgres
USER 999:10001
CMD ["/bin/sh", "/app/postgres/start.sh"]

FROM base AS dependencies
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./
# Only locked dependency wheels are installed; application source is copied explicitly.
RUN uv sync --frozen --no-dev --no-install-project --python /usr/local/bin/python3

FROM dependencies AS verification
# EXEC-07: backend inventory contracts execute the real dependency-free Node module.
COPY --from=verification-node /usr/local/bin/node /usr/local/bin/node
RUN apt-get update && apt-get install --yes --no-install-recommends make git && apt-get clean
RUN uv sync --frozen --no-install-project --python /usr/local/bin/python3
COPY --chown=10001:10001 . /app
RUN node --input-type=module -e 'import { readFileSync } from "node:fs"; import { readBrowserInventory } from "./apps/web/scripts/browser-evidence-inventory.mjs"; if (process.versions.node !== readFileSync(".nvmrc", "utf8").trim()) throw new Error("verification_node_version_mismatch"); readBrowserInventory("./apps/web");'
ENV PATH="/app/.venv/bin:$PATH" PYTHONPATH=/app/apps/api/src:/app \
    UV_OFFLINE=1 UV_NO_SYNC=1
USER 10001:10001
CMD ["make", "verify-del-05-offline-backend"]

FROM base AS runtime
ARG SOURCE_REVISION=uncommitted
LABEL org.opencontainers.image.title="Marketing Agents local backend" \
      org.opencontainers.image.version="0.1.0" \
      org.opencontainers.image.revision=$SOURCE_REVISION
COPY --from=dependencies /app/.venv /app/.venv
COPY --chown=10001:10001 apps/api/src /app/apps/api/src
COPY --chown=10001:10001 catalog /app/catalog
COPY --chown=10001:10001 scripts /app/scripts
ENV PATH="/app/.venv/bin:$PATH" PYTHONPATH=/app/apps/api/src:/app \
    APP_ENV=local AUTH_MODE=local LLM_PROVIDER=mock CONNECTOR_MODE=mock \
    ALLOW_EXTERNAL_NETWORK=false REAL_LLM_OPT_IN=false REAL_CONNECTOR_OPT_IN=false \
    DATABASE_URL="postgresql+asyncpg://marketing_agents@localhost:5432/marketing_agents?host=/var/run/marketing-agents-postgres" \
    MARKETING_AGENTS_DIGEST_KEY_PATH=/var/lib/marketing-agents/secrets/digest.key \
    CATALOG_ROOT=/app/catalog/v1
USER 10001:10001
CMD ["python", "-m", "marketing_agents.workers.serve_api"]
