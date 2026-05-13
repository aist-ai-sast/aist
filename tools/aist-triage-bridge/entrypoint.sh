#!/usr/bin/env bash
set -euo pipefail

SOCKET_PATH="${AIST_LOCAL_TRIAGE_BRIDGE_SOCKET:-/run/claude-bridge/bridge.sock}"

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

# Ensure the bridge can append to per-pipeline logs after dropping privileges.
# Keep this scoped to AIST_LOG_DIR rather than broadening permissions on /app/media.
if [ -n "${AIST_LOG_DIR:-}" ]; then
    mkdir -p "$AIST_LOG_DIR"
    chown claude:claude "$AIST_LOG_DIR"
fi

# ── Drop to claude user ──────────────────────────────────────────────────
# Claude OAuth tokens are no longer global env. The bridge accepts a
# per-request `subprocess_env` field on /analyze[-sync] (Task 4 of
# docs/plans/2026-05-12-claude-as-org-integration.md). The platform
# resolves the right token from OrgIntegration(type=CLAUDE_CODE) for
# the project being analysed and passes it in the request body. If the
# token is missing for a given request, the spawn of `claude -p` will
# fail with claude's own auth-error and the bridge surfaces that as
# an error CallbackPayload.
echo "Starting aist-triage-bridge on UDS: $SOCKET_PATH"
echo "  AIST_WORKING_DIR=${AIST_WORKING_DIR:-/app/aist}"
echo "  AIST_LOCAL_TRIAGE_TIMEOUT=${AIST_LOCAL_TRIAGE_TIMEOUT:-1800}"

exec gosu claude uvicorn main:app \
    --app-dir /app/bridge \
    --uds "$SOCKET_PATH" \
    --log-level info
