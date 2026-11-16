#!/usr/bin/env bash
set -euo pipefail

SOCKET_PATH="${AIST_LOCAL_TRIAGE_BRIDGE_SOCKET:-/run/claude-bridge/bridge.sock}"

# Import mounted public certificates before dropping privileges. Public roots stay installed.
update-ca-certificates

# ── Run as root: fix permissions ──────────────────────────────────────────
# Add claude to the group that has gid $1, creating it as "$2-<gid>" when the image has none.
join_group_by_gid() {
    local gid="$1" group
    if group=$(getent group "$gid"); then
        group="${group%%:*}"
    else
        group="$2-$gid"
        groupadd -g "$gid" "$group"
    fi
    usermod -aG "$group" claude
}

# Give claude user access to docker socket (needed for `docker compose exec`)
if [ -S /var/run/docker.sock ]; then
    join_group_by_gid "$(stat -c '%g' /var/run/docker.sock)" docker-host
fi

# The checkout is bind-mounted from the host and owned by the deploying user, whose uid is not
# claude's. claude reads it through the owning group: the checkout directory and its .env must
# be group-readable, and the group must not be root.
WORKING_DIR="${AIST_WORKING_DIR:-/app/aist}"
if [ -d "$WORKING_DIR" ]; then
    CHECKOUT_GID=$(stat -c '%g' "$WORKING_DIR")
    if [ "$CHECKOUT_GID" != "0" ]; then
        join_group_by_gid "$CHECKOUT_GID" aist-checkout
    fi
    if ! gosu claude test -r "$WORKING_DIR" -a -x "$WORKING_DIR" \
        || { [ -e "$WORKING_DIR/.env" ] && ! gosu claude test -r "$WORKING_DIR/.env"; }; then
        echo "claude (uid $(id -u claude)) cannot read $WORKING_DIR or its .env (owner gid $CHECKOUT_GID);" \
             "make the checkout group-readable by a non-root group" >&2
        exit 1
    fi
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
