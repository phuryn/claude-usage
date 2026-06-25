#!/usr/bin/env bash
# start-tunnel.sh — run the dashboard locally and expose it via Cloudflare Tunnel.
#
# Prerequisites (one-time):
#   macOS:   brew install cloudflared
#   Linux:   https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/
#   Windows: winget install Cloudflare.cloudflared
#
# First run: copy .env.example → .env and fill in DASHBOARD_TOKEN (see below).
# Then just:  bash scripts/start-tunnel.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
ENV_FILE="$REPO_DIR/.env"

# ── Load .env if present ──────────────────────────────────────────────────────
if [[ -f "$ENV_FILE" ]]; then
  # Export key=value lines, skip comments and blanks
  set -a
  # shellcheck disable=SC1090
  source <(grep -v '^\s*#' "$ENV_FILE" | grep -v '^\s*$')
  set +a
fi

# ── Defaults ──────────────────────────────────────────────────────────────────
PORT="${PORT:-8080}"
HOST="127.0.0.1"   # dashboard binds loopback only; tunnel handles external access

# ── Guard: token must be set ──────────────────────────────────────────────────
if [[ -z "${DASHBOARD_TOKEN:-}" ]]; then
  echo ""
  echo "  ERROR: DASHBOARD_TOKEN is not set."
  echo ""
  echo "  Generate one and put it in $ENV_FILE:"
  echo "    python3 -c \"import secrets; print(secrets.token_urlsafe(32))\""
  echo "    echo 'DASHBOARD_TOKEN=<paste>' >> .env"
  echo ""
  exit 1
fi

# ── Launch dashboard in background ────────────────────────────────────────────
echo "[1/3] Starting dashboard on http://$HOST:$PORT ..."
cd "$REPO_DIR"
HOST="$HOST" PORT="$PORT" DASHBOARD_TOKEN="$DASHBOARD_TOKEN" \
  python3 cli.py scan          # incremental scan first
HOST="$HOST" PORT="$PORT" DASHBOARD_TOKEN="$DASHBOARD_TOKEN" \
  python3 -c "
import dashboard, webbrowser, threading, time
from pathlib import Path
import os, scanner

dashboard.DB_PATH = Path(os.path.expanduser('~/.claude/usage.db'))
server = dashboard.ThreadingHTTPServer(('$HOST', $PORT), dashboard.DashboardHandler)
print(f'Dashboard listening on http://$HOST:$PORT')
t = threading.Thread(target=server.serve_forever, daemon=True)
t.start()
# Keep alive — tunnel will be opened by the parent shell
while True:
    time.sleep(1)
" &
DASHBOARD_PID=$!
echo "  Dashboard PID: $DASHBOARD_PID"

# Wait for server to be ready
for i in $(seq 1 20); do
  if curl -sf "http://$HOST:$PORT/" -o /dev/null 2>/dev/null || \
     curl -sf "http://$HOST:$PORT/" -H "Authorization: Bearer $DASHBOARD_TOKEN" -o /dev/null 2>/dev/null; then
    break
  fi
  sleep 0.5
done

# ── Open Cloudflare Tunnel ────────────────────────────────────────────────────
echo "[2/3] Opening Cloudflare Tunnel (no account required for quick tunnels)..."
echo "      Your private URL will appear below. Bookmark it — it changes each run"
echo "      unless you set up a named tunnel (see scripts/tunnel-named.md)."
echo ""
echo "[3/3] Share only the URL+token with trusted parties. The token is:"
echo "      $DASHBOARD_TOKEN"
echo ""
echo "      Access the dashboard at:  <tunnel-url>/?token=<token>"
echo "      (or set Authorization: Bearer <token> header)"
echo ""

# Trap to clean up dashboard on exit
cleanup() {
  echo ""
  echo "Shutting down dashboard (PID $DASHBOARD_PID)..."
  kill "$DASHBOARD_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# This blocks until Ctrl+C — tunnel URL is printed by cloudflared itself
cloudflared tunnel --url "http://$HOST:$PORT"
