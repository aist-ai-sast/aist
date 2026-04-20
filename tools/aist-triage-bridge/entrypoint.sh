#!/usr/bin/env bash
set -euo pipefail

SOCKET_PATH="${AIST_LOCAL_TRIAGE_BRIDGE_SOCKET:-/run/codex-bridge/bridge.sock}"

# ── Run as root: fix permissions ──────────────────────────────────────────
# Give claude user access to docker socket (needed for `docker compose exec`)
if [ -S /var/run/docker.sock ]; then
    DOCKER_GID=$(stat -c '%g' /var/run/docker.sock)
    groupadd -g "$DOCKER_GID" -o docker-host 2>/dev/null || true
    usermod -aG docker-host claude 2>/dev/null || true
fi

# Ensure socket directory is writable by claude
mkdir -p "$(dirname "$SOCKET_PATH")"
chown claude:claude "$(dirname "$SOCKET_PATH")"
rm -f "$SOCKET_PATH"

# ── Drop to claude user ──────────────────────────────────────────────────
# Verify Claude Code auth
if [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
    echo "ERROR: CLAUDE_CODE_OAUTH_TOKEN not set."
    exit 1
fi

echo "Claude Code OAuth token configured."
echo "Starting aist-triage-bridge on UDS: $SOCKET_PATH"
echo "  AIST_WORKING_DIR=${AIST_WORKING_DIR:-/app/aist}"
echo "  AIST_LOCAL_TRIAGE_TIMEOUT=${AIST_LOCAL_TRIAGE_TIMEOUT:-1800}"

exec gosu claude uvicorn main:app \
    --app-dir /app/bridge \
    --uds "$SOCKET_PATH" \
    --log-level info
