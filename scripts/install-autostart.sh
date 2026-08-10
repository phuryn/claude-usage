#!/usr/bin/env bash
# Installs a login/boot service that runs run-docker.sh, so the claude-usage
# container comes up automatically and picks a free port if 9898 is taken.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RUN_SCRIPT="$REPO_DIR/scripts/run-docker.sh"
LABEL="com.claude-usage.dashboard"

case "$(uname -s)" in
  Darwin)
    PLIST_DIR="$HOME/Library/LaunchAgents"
    PLIST="$PLIST_DIR/${LABEL}.plist"
    LOG_DIR="$HOME/Library/Logs/claude-usage"
    mkdir -p "$PLIST_DIR" "$LOG_DIR"

    cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>${LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>${RUN_SCRIPT}</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>${LOG_DIR}/stdout.log</string>
  <key>StandardErrorPath</key><string>${LOG_DIR}/stderr.log</string>
</dict>
</plist>
EOF

    launchctl unload "$PLIST" 2>/dev/null || true
    launchctl load "$PLIST"
    echo "✅  Installed LaunchAgent: $PLIST"
    echo "    Logs: $LOG_DIR"
    ;;

  Linux)
    UNIT_DIR="$HOME/.config/systemd/user"
    UNIT="$UNIT_DIR/${LABEL}.service"
    mkdir -p "$UNIT_DIR"

    cat > "$UNIT" <<EOF
[Unit]
Description=claude-usage dashboard container
After=docker.service

[Service]
Type=oneshot
ExecStart=/bin/bash ${RUN_SCRIPT}
RemainAfterExit=yes

[Install]
WantedBy=default.target
EOF

    systemctl --user daemon-reload
    systemctl --user enable --now "${LABEL}.service"
    echo "✅  Installed systemd user unit: $UNIT"
    echo "    Enable lingering to start at boot without login: sudo loginctl enable-linger $USER"
    ;;

  *)
    echo "Unsupported OS: $(uname -s)" >&2
    exit 1
    ;;
esac
