#!/bin/sh
# Hermes config initialization — runs as s6 cont-init.d script (before user services).
# Syncs config.yaml from /config mount (read-only from host) into /opt/data,
# preserving Discord channel settings but updating model and other managed config.

set -eu

CONFIG_SOURCE="/config/hermes.yaml"
CONFIG_DEST="/opt/data/config.yaml"
CONFIG_BACKUP="/opt/data/config.yaml.bak"

# If source config exists, sync critical sections while preserving discord channels
if [ -f "$CONFIG_SOURCE" ]; then
  # Backup existing config if it exists
  if [ -f "$CONFIG_DEST" ]; then
    cp "$CONFIG_DEST" "$CONFIG_BACKUP"
  fi

  # Copy full config from source (this will update model, providers, etc)
  cp "$CONFIG_SOURCE" "$CONFIG_DEST"

  # Restore discord channel settings from backup if they existed
  if [ -f "$CONFIG_BACKUP" ] && grep -q "free_response_channels:" "$CONFIG_BACKUP"; then
    # Extract the discord section from the backup
    discord_start=$(grep -n "^discord:" "$CONFIG_BACKUP" | cut -d: -f1)
    if [ -n "$discord_start" ]; then
      # Find the next top-level key after discord
      discord_end=$(tail -n +$((discord_start + 1)) "$CONFIG_BACKUP" | grep -n "^[a-z]" | head -1 | cut -d: -f1)
      if [ -z "$discord_end" ]; then
        discord_end=$(wc -l < "$CONFIG_BACKUP")
      else
        discord_end=$((discord_start + discord_end - 1))
      fi

      # Extract discord section from backup
      sed -n "${discord_start},$((discord_end - 1))p" "$CONFIG_BACKUP" > /tmp/discord.yaml

      # Replace discord section in new config
      if grep -q "^discord:" "$CONFIG_DEST"; then
        new_discord_start=$(grep -n "^discord:" "$CONFIG_DEST" | cut -d: -f1)
        new_discord_end=$(tail -n +$((new_discord_start + 1)) "$CONFIG_DEST" | grep -n "^[a-z]" | head -1 | cut -d: -f1)
        if [ -z "$new_discord_end" ]; then
          new_discord_end=$(($(wc -l < "$CONFIG_DEST") + 1))
        else
          new_discord_end=$((new_discord_start + new_discord_end - 1))
        fi

        # Replace the discord section
        (
          head -n $((new_discord_start - 1)) "$CONFIG_DEST"
          cat /tmp/discord.yaml
          tail -n +$((new_discord_end)) "$CONFIG_DEST"
        ) > "$CONFIG_DEST.tmp" && mv "$CONFIG_DEST.tmp" "$CONFIG_DEST"
      fi
    fi
  fi

  chown hermes:hermes "$CONFIG_DEST" 2>/dev/null || true
  echo "[hermes-config-init] Synced config.yaml from $CONFIG_SOURCE"
fi
