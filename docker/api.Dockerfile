# DEL-05: immutable acquisition, then a non-root, mock-only local runtime.
FROM ghcr.io/astral-sh/uv:0.10.7@sha256:edd1fd89f3e5b005814cc8f777610445d7b7e3ed05361f9ddfae67bebfe8456a AS uv
FROM python:3.12.14-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254 AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN groupadd --gid 10001 marketing && useradd --uid 10001 --gid 10001 --no-create-home marketing \
    && mkdir -p /app /var/lib/marketing-agents/data /var/lib/marketing-agents/secrets /var/run/marketing-agents \
    && chown -R 10001:10001 /app /var/lib/marketing-agents \
    && chown 10001:10001 /var/run/marketing-agents \
    && chmod 0750 /var/run/marketing-agents \
    && chmod 0700 /var/lib/marketing-agents/data /var/lib/marketing-agents/secrets
WORKDIR /app

FROM base AS dependencies
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./
# Only locked dependency wheels are installed; application source is copied explicitly.
RUN uv sync --frozen --no-dev --no-install-project --python /usr/local/bin/python3

FROM dependencies AS verification
RUN apt-get update && apt-get install --yes --no-install-recommends make git && apt-get clean
RUN uv sync --frozen --no-install-project --python /usr/local/bin/python3
COPY --chown=10001:10001 . /app
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
    DATABASE_URL=sqlite+aiosqlite:////var/lib/marketing-agents/data/marketing_agents.db \
    MARKETING_AGENTS_DIGEST_KEY_PATH=/var/lib/marketing-agents/secrets/digest.key \
    CATALOG_ROOT=/app/catalog/v1
USER 10001:10001
CMD ["python", "-m", "marketing_agents.workers.serve_api"]
