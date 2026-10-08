# AC-15 acquisition only: execution uses this prebuilt image with --network none.
# Build context is the verified export wrapper produced by scripts/ac_15_offline.py.
FROM node:24.20.0-bookworm-slim@sha256:ba849c60be29959425b8734d57b8b4b7d56f98edd9504c9af091d5281095a71e AS node
FROM ghcr.io/astral-sh/uv:0.10.7@sha256:edd1fd89f3e5b005814cc8f777610445d7b7e3ed05361f9ddfae67bebfe8456a AS uv
FROM python:3.12.14-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
COPY --from=uv /uv /usr/local/bin/uv
RUN apt-get update && apt-get install --yes --no-install-recommends make git libstdc++6 strace \
    && apt-get clean \
    && ln -s ../lib/node_modules/corepack/dist/corepack.js /usr/local/bin/corepack \
    && corepack enable \
    && groupadd --gid 10001 ac15 \
    && useradd --uid 10001 --gid 10001 --create-home ac15
WORKDIR /app
ENV COREPACK_HOME=/opt/corepack COREPACK_ENABLE_DOWNLOAD_PROMPT=0 \
    UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy PYTHONDONTWRITEBYTECODE=1
RUN corepack prepare pnpm@11.24.0 --activate
COPY source/pyproject.toml source/uv.lock ./
RUN uv sync --frozen --no-install-project --python /usr/local/bin/python3
COPY source/package.json source/pnpm-lock.yaml source/pnpm-workspace.yaml source/.nvmrc ./
COPY source/apps/web/package.json /app/apps/web/package.json
COPY source/tools/api-contract/package.json /app/tools/api-contract/package.json
RUN pnpm install --frozen-lockfile \
    && PLAYWRIGHT_BROWSERS_PATH=0 pnpm --dir apps/web exec playwright install --with-deps chromium
COPY source/ /app/
COPY source-manifest.json /opt/ac15/source-manifest.json
RUN uv sync --frozen --python /usr/local/bin/python3
RUN chown -R 10001:10001 /app /opt/corepack
ARG SOURCE_COMMIT
ARG SOURCE_TREE
ARG SOURCE_SHA256
LABEL io.marketing-agents.ac15.schema="1" \
    io.marketing-agents.ac15.source-commit=$SOURCE_COMMIT \
    io.marketing-agents.ac15.source-tree=$SOURCE_TREE \
    io.marketing-agents.ac15.source-sha256=$SOURCE_SHA256
ENV PATH="/app/.venv/bin:$PATH" PYTHONPATH=/app/apps/api/src:/app \
    PYTHONUNBUFFERED=1 UV_OFFLINE=1 UV_NO_SYNC=1 UV_FROZEN=1 \
    COREPACK_ENABLE_NETWORK=0 COREPACK_ENABLE_AUTO_PIN=0 \
    PNPM_CONFIG_OFFLINE=true PNPM_CONFIG_UPDATE_NOTIFIER=false NPM_CONFIG_OFFLINE=true \
    PLAYWRIGHT_BROWSERS_PATH=0 CI=true RAYON_NUM_THREADS=1 \
    APP_ENV=local AUTH_MODE=local LLM_PROVIDER=mock CONNECTOR_MODE=mock \
    ALLOW_EXTERNAL_NETWORK=false REAL_LLM_OPT_IN=false REAL_CONNECTOR_OPT_IN=false
USER 10001:10001
ENTRYPOINT ["python", "scripts/ac_15_offline.py", "inside"]
