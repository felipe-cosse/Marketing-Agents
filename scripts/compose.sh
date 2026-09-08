#!/bin/sh
# DEL-05 scoped local operations. `down` always preserves database/key volumes.
set -eu
del05_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
del05_project=${COMPOSE_PROJECT_NAME:-marketing-agents-local}
del05_port=${MARKETING_AGENTS_WEB_PORT:-8080}
case "$del05_project" in
    marketing-agents-*) ;;
    *) echo 'Compose project must start with marketing-agents-.' >&2; exit 2 ;;
esac
case "$del05_project" in *[!a-z0-9-]*|*-|?????????????????????????????????????????????????????????????????*)
    echo 'Compose project name is invalid.' >&2; exit 2 ;;
esac
case "$del05_port" in ''|*[!0-9]*|??????*) echo 'Web port must be an integer.' >&2; exit 2 ;; esac
if [ "$del05_port" -lt 1024 ] || [ "$del05_port" -gt 65535 ]; then
    echo 'Web port must be between 1024 and 65535.' >&2; exit 2
fi
command -v docker >/dev/null 2>&1 || { echo 'Docker with Compose is required.' >&2; exit 2; }
docker compose version >/dev/null
if [ -n "${DOCKER_CONTEXT:-}" ]; then
    del05_endpoint=$(docker context inspect "$DOCKER_CONTEXT" --format '{{.Endpoints.docker.Host}}')
else
    del05_endpoint=${DOCKER_HOST:-$(docker context inspect --format '{{.Endpoints.docker.Host}}')}
fi
case "$del05_endpoint" in unix:///*) ;; *) echo 'Only a local Unix-socket Docker daemon is supported.' >&2; exit 2 ;; esac
export DOCKER_HOST="$del05_endpoint"
unset DOCKER_CONTEXT
# Keep source builds on that daemon even if the user selected a remote builder.
export BUILDX_BUILDER=default
del05_engine=$(docker version --format '{{.Server.Version}}')
del05_major=${del05_engine%%.*}
case "$del05_major" in ''|*[!0-9]*) echo 'Docker engine version is unavailable.' >&2; exit 2 ;; esac
[ "$del05_major" -ge 28 ] || { echo 'Docker Engine 28 or newer is required for loopback-only publishing.' >&2; exit 2; }
export COMPOSE_PROJECT_NAME="$del05_project" MARKETING_AGENTS_WEB_PORT="$del05_port"
unset COMPOSE_FILE COMPOSE_PROFILES COMPOSE_ENV_FILES
del05_compose() {
    docker compose --env-file /dev/null --project-directory "$del05_root" --project-name "$del05_project" --file "$del05_root/compose.yaml" "$@"
}
case "${1:-}" in
    up)
        del05_compose up --build --detach --wait --wait-timeout 180
        echo "Marketing Agents: http://127.0.0.1:$del05_port (local identity, deterministic mocks, API/workers network-disabled)"
        ;;
    down) del05_compose down --timeout 40 ;;
    logs) del05_compose logs --no-color --tail 100 ;;
    config) del05_compose config --quiet ;;
    *) echo 'Usage: scripts/compose.sh up|down|logs|config' >&2; exit 2 ;;
esac
