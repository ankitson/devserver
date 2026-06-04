# justfile — devserver-wide commands only.
# Pipeline-specific recipes live in pipelines/Justfile (run them as
# `just pipelines <recipe>`, e.g. `just pipelines up-dagster`).

set shell := ["bash", "-euo", "pipefail", "-c"]

COMPOSE := "docker compose"
TEMPLATE_DIR := "./config"
SECRETS_DIR := "./secrets"

# Pipeline-specific recipes (see pipelines/Justfile).
mod pipelines

# Render config/*.tmpl -> secrets/* using `op inject` (toolbox `render-secrets`).
# Supports both *.env.tmpl and *.json.tmpl files. Renders ALL templates
# (pipeline secrets included), so it lives here at the devserver level.
rs: render_secrets
render_secrets strict="false":
  render-secrets {{TEMPLATE_DIR}} {{SECRETS_DIR}} --pattern '*.env.tmpl' --pattern '*.json.tmpl' {{ if strict == "true" { "--strict" } else { "" } }}

clean-secrets:
  @rm -f {{SECRETS_DIR}}/*.env
  @echo "Removed generated secrets"

# ── Generic compose wrappers (work on any service[s]) ────────────────
# Examples:
#   just up openclaw agent-browser
#   just stop speaches
#   just logs openclaw
#   just build openclaw
#   just restart openclaw
#   just upgrade actualbudget        (pulls latest image + recreates)

up *args:
  {{COMPOSE}} up -d {{args}}

# `down` removes containers + networks; use `stop` if you want to keep them.
down *args:
  {{COMPOSE}} down {{args}}

stop *args:
  {{COMPOSE}} stop {{args}}

restart *args:
  {{COMPOSE}} restart {{args}}

logs *args:
  {{COMPOSE}} logs -f {{args}}

build *args:
  {{COMPOSE}} build {{args}}

# Pull latest image(s) and recreate. --no-deps keeps dependent services running.
# Use this for services with `image:` only. For locally-built services (see
# `upgrade-openclaw` below) the source dependency must be bumped first.
upgrade *services:
  {{COMPOSE}} pull {{services}}
  {{COMPOSE}} up -d --no-deps {{services}}

# Upgrade openclaw (locally-built; build context = ankitson/dockers git repo).
# Resolves the npm version to install (default: latest), passes it as a build
# arg, rebuilds, and recreates. The Dockerfile defaults to OPENCLAW_VERSION=
# latest, so no source pin to bump unless you pass a specific version here
# (useful for rollback: `just upgrade-openclaw 2026.5.25`).
upgrade-openclaw version="":
  #!/usr/bin/env bash
  set -euo pipefail
  V="{{version}}"
  [ -z "$V" ] && V=$(npm view openclaw version)
  echo "openclaw: building with openclaw@$V"
  {{COMPOSE}} build --build-arg OPENCLAW_VERSION="$V" openclaw
  {{COMPOSE}} up -d --no-deps openclaw
  echo
  docker exec openclaw bash -lc 'openclaw --version' 2>/dev/null | grep -v "Agent mode" | head -1 || true

# ── Speaches-specific (no generic compose equivalent) ────────────────
# Preload the default whisper model (downloads weights if not cached).
speaches-pull model="deepdml/faster-whisper-large-v3-turbo-ct2":
  curl -fsS -X POST "http://127.0.0.1:8800/v1/models/{{model}}" && echo
  curl -fsS "http://127.0.0.1:8800/v1/models/{{model}}" | python3 -m json.tool

# Smoke test: transcribe an audio file. Usage: just speaches-test path/to/audio.wav
speaches-test file:
  curl -fsS -X POST http://127.0.0.1:8800/v1/audio/transcriptions \
    -H "Content-Type: multipart/form-data" \
    -F "file=@{{file}}" \
    -F "model=deepdml/faster-whisper-large-v3-turbo-ct2" \
    -F "response_format=json" | python3 -m json.tool

# ── MCPProxy shared MCP gateway ─────────────────────────────────────
# Keep the Compose project name stable when running from an isolated worktree.
mcpproxy-up:
  {{COMPOSE}} -p devserver up -d --build --no-deps mcpproxy

mcpproxy-logs:
  {{COMPOSE}} -p devserver logs -f mcpproxy

mcpproxy-health:
  curl -fsS http://172.19.0.1:3130/healthz
  @echo

# Run this on the devserver host: Fastmail redirects the browser to a
# 127.0.0.1 callback listener owned by the host-networked container.
mcpproxy-auth-fastmail:
  #!/usr/bin/env bash
  set -euo pipefail
  started="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  output="$(mktemp /tmp/mcpproxy-auth-fastmail.XXXXXX)"
  trap 'rm -f "$output"' EXIT
  docker exec mcpproxy mcpproxy \
    --config /data/mcp_config.json \
    --data-dir /data \
    auth login --server=fastmail --timeout=5m >"$output" 2>&1 &
  login_pid=$!
  trap 'kill "$login_pid" 2>/dev/null || true; rm -f "$output"' EXIT
  for _ in $(seq 1 30); do
    auth_url="$(
      docker logs --since "$started" mcpproxy 2>&1 \
        | python3 -c 'import json, re, sys; matches = re.findall(r"\"auth_url\": \"([^\"]+)\"", sys.stdin.read()); print(json.loads(f"\"{matches[-1]}\"") if matches else "")'
    )"
    if [[ -n "$auth_url" ]]; then
      printf 'Open this URL in a browser running on the devserver host:\n\n%s\n\n' "$auth_url"
      wait "$login_pid"
      exit $?
    fi
    if ! kill -0 "$login_pid" 2>/dev/null; then
      wait "$login_pid" || true
      cat "$output" >&2
      exit 1
    fi
    sleep 1
  done
  echo "Timed out waiting for MCPProxy to emit a Fastmail authorization URL." >&2
  cat "$output" >&2
  exit 1

mcpproxy-auth-status:
  docker exec mcpproxy mcpproxy \
    --config /data/mcp_config.json \
    --data-dir /data \
    auth status --server=fastmail

# Prints the raw token exactly once. Store it in clankers/mcpproxy-agents.
mcpproxy-token-create:
  docker exec mcpproxy mcpproxy \
    --config /data/mcp_config.json \
    --data-dir /data \
    token create \
    --name shared-agents \
    --servers "*" \
    --permissions read,write,destructive \
    --expires 365d \
    --output json

# Usage:
# MCPPROXY_AGENT_TOKEN="$(op read 'op://clankers/mcpproxy-agents/password')" just mcpproxy-smoke
mcpproxy-smoke:
  #!/usr/bin/env bash
  set -euo pipefail
  : "${MCPPROXY_AGENT_TOKEN:?set MCPPROXY_AGENT_TOKEN from 1Password}"
  curl --fail-with-body --silent --show-error \
    http://172.19.0.1:3130/mcp/all \
    --header "Authorization: Bearer ${MCPPROXY_AGENT_TOKEN}" \
    --header "Accept: application/json, text/event-stream" \
    --header "Content-Type: application/json" \
    --data '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"devserver-smoke","version":"1.0.0"}}}'
  echo
