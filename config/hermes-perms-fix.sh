#!/bin/sh
# Hermes permissions fix — runs as s6 cont-init.d script (before user services).
# Ensures auth.json and other critical files are owned by hermes:hermes
# so the gateway can read them when it starts as the hermes user.
#
# This runs after the image's own stage2-hook.sh which chowns config.yaml
# unconditionally but NOT auth.json (it only chowns auth.json on first boot
# when the file is created from HERMES_AUTH_JSON_BOOTSTRAP).
#
# Mounted into the container at /etc/cont-init.d/03-hermes-perms-fix
# via the docker-compose volumes section.

set -eu

HERMES_HOME="${HERMES_HOME:-/opt/data}"

# Fix auth.json ownership unconditionally (like config.yaml gets fixed)
if [ -f "$HERMES_HOME/auth.json" ]; then
    chown hermes:hermes "$HERMES_HOME/auth.json" 2>/dev/null || true
    chmod 600 "$HERMES_HOME/auth.json" 2>/dev/null || true
fi

# Fix shared auth files
if [ -f "$HERMES_HOME/shared/nous_auth.json" ]; then
    chown hermes:hermes "$HERMES_HOME/shared/nous_auth.json" 2>/dev/null || true
fi
if [ -f "$HERMES_HOME/shared/nous_auth.lock" ]; then
    chown hermes:hermes "$HERMES_HOME/shared/nous_auth.lock" 2>/dev/null || true
fi

# Fix auth lock
if [ -f "$HERMES_HOME/auth.lock" ]; then
    chown hermes:hermes "$HERMES_HOME/auth.lock" 2>/dev/null || true
fi

echo "[perms-fix] auth.json ownership verified"
