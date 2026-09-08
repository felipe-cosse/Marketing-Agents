# DEL-05: frozen frontend build; no Node, compiler, or dev server in the runtime.
FROM node:24.20.0-bookworm-slim@sha256:ba849c60be29959425b8734d57b8b4b7d56f98edd9504c9af091d5281095a71e AS dependencies
WORKDIR /app
ENV COREPACK_HOME=/opt/corepack COREPACK_ENABLE_DOWNLOAD_PROMPT=0
RUN corepack enable && corepack prepare pnpm@11.24.0 --activate
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml .nvmrc ./
COPY apps/web/package.json /app/apps/web/package.json
RUN pnpm install --frozen-lockfile

FROM dependencies AS builder
COPY . /app
RUN pnpm --dir apps/web build

FROM builder AS verification
RUN apt-get update && apt-get install --yes --no-install-recommends make && apt-get clean \
    && PLAYWRIGHT_BROWSERS_PATH=/opt/playwright pnpm --dir apps/web exec playwright install --with-deps chromium \
    && chown -R node:node /app /opt/playwright /opt/corepack
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright CI=true COREPACK_ENABLE_NETWORK=0
USER node
CMD ["make", "verify-del-05-offline-web"]

FROM nginxinc/nginx-unprivileged:1.30.4-alpine@sha256:442753882674b49ae2c1de83ed67896131c0777f56df5005e356e62bc3f7e7ce AS runtime
ARG SOURCE_REVISION=uncommitted
LABEL org.opencontainers.image.title="Marketing Agents local web" \
      org.opencontainers.image.version="0.1.0" \
      org.opencontainers.image.revision=$SOURCE_REVISION
COPY --from=builder /app/apps/web/dist /usr/share/nginx/html
COPY docker/web.conf /etc/nginx/nginx.conf
USER 0
# Either API or web may initialize the shared IPC volume's copy-up metadata.
RUN mkdir -p /var/run/marketing-agents \
    && chown 10001:10001 /var/run/marketing-agents \
    && chmod 0750 /var/run/marketing-agents
USER 101:101
# Skip image template mutation: all configuration is committed and rootfs stays read-only.
ENTRYPOINT ["nginx"]
CMD ["-g", "daemon off;"]
