#!/bin/sh
# Hermes Discord configuration — runs as s6 cont-init.d script (before user services).
# Populates Discord credentials in ~/.hermes/.env from environment variables
# set via docker-compose env_file.

set -eu

HERMES_HOME="${HERMES_HOME:-/opt/data}"
ENV_FILE="$HERMES_HOME/.env"

# Only populate if env vars exist and .env doesn't already have them configured
if [ -f "$ENV_FILE" ]; then
  if [ -n "${DISCORD_BOT_TOKEN:-}" ] && ! grep -q "^DISCORD_BOT_TOKEN=[^ ]*" "$ENV_FILE" 2>/dev/null; then
    sed -i "s/^DISCORD_BOT_TOKEN=$/DISCORD_BOT_TOKEN=$DISCORD_BOT_TOKEN/" "$ENV_FILE"
    echo "[hermes-discord-init] DISCORD_BOT_TOKEN configured"
  fi

  if [ -n "${DISCORD_ALLOWED_USERS:-}" ] && ! grep -q "^DISCORD_ALLOWED_USERS=[^ ]*" "$ENV_FILE" 2>/dev/null; then
    sed -i "s/^DISCORD_ALLOWED_USERS=$/DISCORD_ALLOWED_USERS=$DISCORD_ALLOWED_USERS/" "$ENV_FILE"
    echo "[hermes-discord-init] DISCORD_ALLOWED_USERS configured"
  fi
fi
