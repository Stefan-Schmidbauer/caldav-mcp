#!/usr/bin/env bash
#
# Deploy the CalDAV MCP server from a dev machine to a server.
#
# The image is built by the *remote* Docker daemon over an SSH context, so no
# registry and no source checkout on the server are involved. The compose
# service definition and the secrets stay on the server, owned by the admin
# user — this script only replaces the image and recreates the container.
#
# Because no registry holds the image, that service definition has to carry
# `pull_policy: never` (it is in deploy/docker-compose.snippet.yml). Otherwise
# the admin's `docker compose pull` fails on this service and aborts the pull
# for the whole stack. The `up -d` below is unaffected either way.
#
# Configure it per machine in deploy.env (gitignored, see deploy/deploy.env.example)
# or by exporting the same variables. There are no built-in host defaults.
#
# Usage:  ./deploy.sh [--no-build]
#
set -euo pipefail

cd "$(dirname "$0")"

# Per-machine settings; environment wins over the file. Sourcing the file would
# overwrite what is already exported, so those values are set aside first and
# put back afterwards. (The ${a[@]+…} form keeps bash 3.2 on macOS quiet about
# an empty array under `set -u`.)
if [ -f deploy.env ]; then
    _from_env=()
    for _v in DEPLOY_HOST DOCKER_CTX REMOTE_COMPOSE HEALTH_URL; do
        [ -n "${!_v+set}" ] && _from_env+=("${_v}=${!_v}")
    done
    set -a; . ./deploy.env; set +a
    for _kv in ${_from_env[@]+"${_from_env[@]}"}; do export "$_kv"; done
fi

DEPLOY_HOST=${DEPLOY_HOST:-}
DOCKER_CTX=${DOCKER_CTX:-}
REMOTE_COMPOSE=${REMOTE_COMPOSE:-/opt/docker/docker-compose.yml}
SERVICE=caldav-mcp
IMAGE=caldav-mcp
HEALTH_URL=${HEALTH_URL:-http://127.0.0.1:9153/healthz}

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

[ -n "$DEPLOY_HOST" ] \
    || die "DEPLOY_HOST is not set. Copy deploy/deploy.env.example to deploy.env and fill it in, or export DEPLOY_HOST=user@host."

# One context per host, shared by every service deployed there: a context is a
# connection to a Docker daemon, not to a service. Named after the host by
# default, without user and port — admin@s01.example.net:2222 becomes
# s01.example.net. Set DOCKER_CTX for something shorter.
if [ -z "$DOCKER_CTX" ]; then
    DOCKER_CTX=${DEPLOY_HOST#*@}
    DOCKER_CTX=${DOCKER_CTX%%:*}
fi

# --- version tag -------------------------------------------------------------
if git rev-parse --verify -q HEAD >/dev/null 2>&1; then
    # Named after the last release tag: v1.2.0 on the tag itself, and
    # v1.2.0-3-gafe0bf9 three commits past it. That makes the rollback command
    # in the README refer to a version someone can place, rather than to a bare
    # commit SHA. --always falls back to the short SHA before the first tag
    # exists, so this works in a repo that has never been released. All three
    # forms are valid Docker tags.
    TAG=$(git describe --tags --always)
    if ! git diff --quiet HEAD -- .; then
        TAG="${TAG}-dirty"
        info "Working tree has uncommitted changes — tagging as ${TAG}"
    fi
else
    TAG=$(date +%Y%m%d-%H%M%S)
fi

# --- docker context ----------------------------------------------------------
if ! docker context inspect "$DOCKER_CTX" >/dev/null 2>&1; then
    info "Creating docker context '${DOCKER_CTX}' -> ssh://${DEPLOY_HOST}"
    docker context create "$DOCKER_CTX" --docker "host=ssh://${DEPLOY_HOST}"
fi

# The context is shared and may have been created by another repository — so
# make sure it leads where this deploy is meant to go, rather than building and
# restarting on whatever host the name happens to point at.
CTX_HOST=$(docker context inspect "$DOCKER_CTX" -f '{{.Endpoints.docker.Host}}')
[ "$CTX_HOST" = "ssh://${DEPLOY_HOST}" ] \
    || die "Docker context '${DOCKER_CTX}' points to ${CTX_HOST}, not ssh://${DEPLOY_HOST}. Set DOCKER_CTX to another name, or fix DEPLOY_HOST."

docker --context "$DOCKER_CTX" version >/dev/null 2>&1 \
    || die "Cannot reach the Docker daemon via context '${DOCKER_CTX}'. Check SSH access to ${DEPLOY_HOST}."

# --- build -------------------------------------------------------------------
if [ "${1:-}" != "--no-build" ]; then
    info "Building ${IMAGE}:${TAG} on ${DEPLOY_HOST}"
    # Context is this directory; it is streamed to the remote daemon over SSH.
    # Both tags point at the same image: :local is what compose references,
    # :$TAG stays behind so a previous build can be re-pinned for a rollback.
    docker --context "$DOCKER_CTX" build \
        -t "${IMAGE}:${TAG}" \
        -t "${IMAGE}:local" \
        --build-arg "VERSION=${TAG}" \
        .
else
    # Nothing was built, so there is no ${IMAGE}:${TAG} — name what really runs.
    TAG=local
fi

# --- recreate ----------------------------------------------------------------
# The strings below are assembled locally and run by a shell on the server, so
# every interpolated value is single-quoted. They come from deploy.env, not from
# anything untrusted — the quoting is here so that a path with a space stays one
# argument, and so this file is not a template for doing it unquoted elsewhere.
# --no-deps is not optional. Without it, `up -d <service>` brings the service's
# declared dependencies up too and recreates any whose configuration has drifted
# from what is running. The compose file on the server is the admin's copy and
# nothing keeps it in sync with deploy/docker-compose.snippet.yml, so this flag
# is what makes a deploy of this service touch only this service, whatever that
# file says.
info "Recreating ${SERVICE} via ${REMOTE_COMPOSE}"
ssh "$DEPLOY_HOST" "docker compose -f '${REMOTE_COMPOSE}' up -d --no-deps '${SERVICE}'"

# --- verify ------------------------------------------------------------------
info "Waiting for the server to answer"
for i in $(seq 1 15); do
    if ssh "$DEPLOY_HOST" "curl -sf -o /dev/null '${HEALTH_URL}'"; then
        info "Healthy — deployed ${IMAGE}:${TAG}"
        exit 0
    fi
    sleep 1
done

printf '\033[1;31mERROR:\033[0m server did not become healthy. Recent logs:\n' >&2
ssh "$DEPLOY_HOST" "docker logs --tail 30 '${SERVICE}'" >&2
exit 1
