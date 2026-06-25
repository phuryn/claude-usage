#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
IMAGE="claude-usage"
CONTAINER="claude-usage"
NETWORK="claude-usage-net"
PORT=9898

echo "▶  Checking for running container..."
if docker ps -q --filter "name=^${CONTAINER}$" | grep -q .; then
  echo "⏹  Stopping ${CONTAINER}..."
  docker stop "$CONTAINER"
fi

echo "🔗  Ensuring isolated network..."
if ! docker network inspect "$NETWORK" &>/dev/null; then
  docker network create \
    --opt com.docker.network.bridge.enable_ip_masquerade=false \
    "$NETWORK"
fi

echo "⬇  Pulling latest..."
cd "$REPO_DIR"
git pull

echo "🔨  Building image..."
docker build -t "$IMAGE" .

echo "🚀  Starting container..."
# SECURITY [C-2]: HOST=127.0.0.1 binds the server inside the container to
# localhost only.  The host-facing port is already published via -p $PORT:8080,
# so this does not affect reachability from the host machine.  It does prevent
# other containers on the same Docker network from connecting directly to the
# dashboard port — reducing the blast radius if another container is compromised.
# Override with -e HOST=0.0.0.0 only if you need the server reachable from a
# reverse proxy running in the same Docker network.
docker run --rm -d \
  --name "$CONTAINER" \
  --network "$NETWORK" \
  -p "$PORT:8080" \
  -v "$HOME/.claude:/root/.claude:ro" \
  -v "${CONTAINER}-data:/data" \
  -e HOST=127.0.0.1 \
  "$IMAGE"

echo "✅  Running at http://localhost:${PORT}"
