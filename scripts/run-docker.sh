#!/usr/bin/env bash
set -euo pipefail
export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:$PATH"

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
IMAGE="claude-usage"
CONTAINER="claude-usage"
NETWORK="claude-usage-net"
PORT="${PORT:-9898}"

port_in_use() {
  # Port is "in use" only if held by something other than our own container.
  if command -v lsof &>/dev/null; then
    lsof -iTCP:"$1" -sTCP:LISTEN -n -P &>/dev/null
  else
    (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null && { exec 3>&-; return 0; } || return 1
  fi
}

echo "▶  Checking for existing container..."
if docker ps -aq --filter "name=^${CONTAINER}$" | grep -q .; then
  echo "⏹  Removing existing ${CONTAINER}..."
  docker rm -f "$CONTAINER"
fi

while port_in_use "$PORT"; do
  echo "⚠  Port ${PORT} in use, trying $((PORT + 1))..."
  PORT=$((PORT + 1))
done

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
docker run -d \
  --restart unless-stopped \
  --name "$CONTAINER" \
  --network "$NETWORK" \
  -p "$PORT:8080" \
  -v "$HOME/.claude:/root/.claude:ro" \
  -v "${CONTAINER}-data:/data" \
  -e HOST=0.0.0.0 \
  "$IMAGE"

echo "✅  Running at http://localhost:${PORT}"
