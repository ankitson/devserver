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
  echo
  echo "── openclaw doctor (pending migrations — NOT auto-applied) ──────────"
  # This rebuild path only swaps the binary + re-applies the config patch; it does
  # NOT run version migrations. A bump can move stores (e.g. cron jobs.json → SQLite)
  # and silently strand data. Surface them here so you apply them deliberately.
  sleep 5   # let the gateway finish starting before doctor inspects state
  docker exec openclaw openclaw doctor 2>&1 | grep -v "Agent mode detected. Run" || true
  echo
  echo "⚠️  REVIEW the doctor output above. If it lists changes, apply them with:"
  echo "        just openclaw-doctor-fix"
  echo "    (rewrites config/auth/session state — read 'just openclaw-doctor' first)."

# Surface pending OpenClaw migrations / config issues (read-only, safe).
openclaw-doctor:
  docker exec openclaw openclaw doctor

# Apply OpenClaw migrations (rewrites config/auth/session state; review openclaw-doctor first).
openclaw-doctor-fix:
  # cron jobs.json->SQLite import only triggers if a legacy ~/.openclaw/cron/jobs.json exists.
  docker exec openclaw openclaw doctor --fix

# ── OpenClaw web app deployment surface ────────────────────────────
openclaw-apps-up:
  {{COMPOSE}} up -d --no-deps openclaw-app-runner

openclaw-apps-logs:
  {{COMPOSE}} logs -f openclaw-app-runner

openclaw-apps-smoke slug="hello-openclaw":
  curl -fsS -H "Host: {{slug}}.dev.ankitson.com" http://127.0.0.1:18880/

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

# ── Bifrost LLM gateway ─────────────────────────────────────────────
BIFROST_URL := "http://127.0.0.1:8090"

# List the providers Bifrost loaded from config/bifrost.config.json.
bifrost-providers:
  curl -fsS {{BIFROST_URL}}/api/providers | python3 -c 'import sys,json; [print("-", p["name"], "("+p.get("provider_status","?")+")") for p in json.load(sys.stdin)["providers"]]'

# Smoke test: call a free OpenRouter model end-to-end through Bifrost.
# Usage: just bifrost-test [model]   (model is the bifrost id, default a free one)
bifrost-test model="openrouter/openai/gpt-oss-20b:free":
  curl -fsS --max-time 120 {{BIFROST_URL}}/openai/v1/chat/completions \
    -H 'Content-Type: application/json' \
    -d '{"model":"{{model}}","messages":[{"role":"user","content":"Reply with exactly: BIFROST_OK"}],"max_tokens":20}' \
    | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d["choices"][0]["message"]["content"].strip() if d.get("choices") else "ERR "+str(d.get("status_code"))+" "+json.dumps(d.get("error",{})))'

# Smoke test the extra_params passthrough: pin an OpenRouter model to a specific
# upstream provider through Bifrost (the `provider` object is merged upstream only
# when `x-bf-passthrough-extra-params: true` is set). Default pins deepseek-v4-flash
# to the ZDR `wafer` BYOK endpoint. Usage: just bifrost-test-pin [model] [provider]
bifrost-test-pin model="openrouter/deepseek/deepseek-v4-flash" provider="wafer":
  curl -fsS --max-time 120 {{BIFROST_URL}}/openai/v1/chat/completions \
    -H 'Content-Type: application/json' \
    -H 'x-bf-passthrough-extra-params: true' \
    -d '{"model":"{{model}}","messages":[{"role":"user","content":"Reply with exactly: PIN_OK"}],"max_tokens":16,"extra_params":{"provider":{"only":["{{provider}}"],"allow_fallbacks":false}}}' \
    | python3 -c 'import sys,json; d=json.load(sys.stdin); print((d["choices"][0]["message"]["content"].strip()+" | resolved="+str(d.get("model"))) if d.get("choices") else "ERR "+str(d.get("status_code"))+" "+json.dumps(d.get("error",{})))'

# Sync declarative OpenRouter presets (config/openrouter-presets.json) to the
# OpenRouter account. Presets bake provider routing server-side, so they survive
# Bifrost (which strips the request-body `provider` field) — this is how we pin
# deepseek-v4-flash onto the ZDR `wafer` BYOK endpoint through the gateway.
# Reference a preset as model `openrouter/@preset/<slug>`. No inference cost.
openrouter-presets-sync:
  python3 tools/openrouter_presets.py

openrouter-presets-check:
  python3 tools/openrouter_presets.py --check

# Register OpenRouter BYOK provider credentials (config/openrouter-byok.json) so
# models route to a provider's first-party endpoint on YOUR key/credits. Needs an
# OpenRouter management key (OPENROUTER_MGMT_KEY or management_key_op in the json),
# not a normal sk-or- key. Provider keys read from 1Password at sync time.
openrouter-byok-sync:
  python3 tools/openrouter_byok.py

openrouter-byok-list:
  python3 tools/openrouter_byok.py --list

# Copy a SillyTavern Chat Completion preset JSON into the local user volume.
sillytavern-preset-copy file:
  mkdir -p "volumes/sillytavern/data/default-user/OpenAI Settings"
  cp "{{file}}" "volumes/sillytavern/data/default-user/OpenAI Settings/"

# List the MCP tools Bifrost discovered from mcpproxy (exa search + websets).
bifrost-mcp-tools:
  curl -fsS {{BIFROST_URL}}/api/mcp/clients | python3 -c 'import sys,json; [ (print("client",c["config"]["name"]+":"), [print("  -",t["name"]) for t in c.get("tools",[])]) for c in json.load(sys.stdin)["clients"]]'

# Smoke test: drive a model through Bifrost that web-searches via mcpproxy->exa
# (agent mode auto-executes the read-only exa search tools). Usage: just bifrost-test-search [model] [query...]
bifrost-test-search model="nvidia/meta/llama-3.1-8b-instruct" *query="What is the maximhq Bifrost LLM gateway? Answer in one sentence.":
  curl -fsS --max-time 120 {{BIFROST_URL}}/openai/v1/chat/completions \
    -H 'Content-Type: application/json' \
    -H 'x-bf-mcp-include-clients: mcpproxy' \
    -d '{"model":"{{model}}","messages":[{"role":"user","content":"Use the web_search tool, then: {{query}}"}],"max_tokens":400}' \
    | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d["choices"][0]["message"].get("content") if d.get("choices") else "ERR "+json.dumps(d.get("error",{})))'

# Wipe Bifrost runtime state (config.db + logs.db). Forces a clean re-seed from
# config/bifrost.config.json on next start. Does NOT touch the config file itself.
bifrost-reset:
  {{COMPOSE}} stop bifrost
  rm -rf ./volumes/bifrost/*.db ./volumes/bifrost/*.db-*
  {{COMPOSE}} up -d bifrost
  @echo "bifrost reset — re-seeded from config/bifrost.config.json"

# ── MCPProxy shared MCP gateway ─────────────────────────────────────
# Keep the Compose project name stable when running from an isolated worktree.
mcpproxy-up:
  {{COMPOSE}} -p devserver up -d --build --no-deps mcpproxy

mcpproxy-logs:
  {{COMPOSE}} -p devserver logs -f mcpproxy

mcpproxy-health:
  curl -fsS http://172.19.0.1:3130/healthz
  @echo

# ── DNS probe: host-networked Go resolver diagnostics ───────────────
dns-probe-up:
  {{COMPOSE}} -p devserver up -d --build --no-deps dns-probe

dns-probe-stop:
  {{COMPOSE}} -p devserver stop dns-probe

dns-probe-logs:
  {{COMPOSE}} -p devserver logs -f dns-probe

dns-probe-tail:
  tail -f logs/dns-probe.jsonl

dns-probe-host-tail:
  tail -f logs/dns-probe-host.jsonl

dns-probe-host-logs since="now":
  journalctl --follow --output=json --since "{{since}}" --no-pager \
    -u systemd-resolved -u tailscaled -u docker -u NetworkManager \
    | tee -a logs/dns-probe-host.jsonl

dns-probe-clean:
  rm -f logs/dns-probe.jsonl logs/dns-probe-host.jsonl

# Enable this before a sleep/wake repro. It is intentionally separate because
# resolved debug logging is noisy and should be turned back down after capture.
dns-debug-on:
  sudo resolvectl log-level debug

dns-debug-off:
  sudo resolvectl log-level info

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
    auth login --server=fastmail --timeout=5m >"$output" 2>&1
  for _ in $(seq 1 30); do
    auth_url="$(
      docker logs --since "$started" mcpproxy 2>&1 \
        | python3 -c 'import json, re, sys; matches = re.findall(r"\"auth_url\": \"([^\"]+)\"", sys.stdin.read()); print(json.loads(f"\"{matches[-1]}\"") if matches else "")'
    )"
    if [[ -n "$auth_url" ]]; then
      break
    fi
    sleep 1
  done
  if [[ -z "${auth_url:-}" ]]; then
    echo "Timed out waiting for MCPProxy to emit a Fastmail authorization URL." >&2
    cat "$output" >&2
    exit 1
  fi
  printf 'Open this URL in a browser running on this host (%s):\n\n%s\n\n' "$(hostname)" "$auth_url"
  echo "Waiting for Fastmail to complete the loopback callback and connect..."
  for _ in $(seq 1 60); do
    if docker exec mcpproxy mcpproxy \
      --config /data/mcp_config.json \
      --data-dir /data \
      upstream list --output json 2>/dev/null \
      | python3 -c 'import json, sys; server = next((s for s in json.load(sys.stdin) if s.get("id") == "fastmail"), {}); sys.exit(0 if server.get("connected") and server.get("status") == "ready" else 1)'; then
      docker exec mcpproxy mcpproxy \
        --config /data/mcp_config.json \
        --data-dir /data \
        upstream approve fastmail >/dev/null
      echo "Fastmail OAuth is complete, the upstream is connected, and tools are approved."
      exit 0
    fi
    sleep 5
  done
  echo "Timed out waiting for Fastmail OAuth to complete." >&2
  echo "Open the URL above on the same host before the callback listener expires, then rerun mcpproxy-auth-status." >&2
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

# ── agent-sandbox: SSH workspace for the remote agent ───────────────────
# Authorize an additional public key for SSH into agent-sandbox.
# Usage: just agent-sandbox-add-key "ssh-ed25519 AAAA... agent@host"
agent-sandbox-add-key key:
  docker exec agent-sandbox sh -c 'echo "{{key}}" >> /home/ankit/.ssh/authorized_keys && ssh-keygen -lf /home/ankit/.ssh/authorized_keys'

# SSH reachability + write-mount smoke test (uses your local dev key).
agent-sandbox-smoke:
  ssh -o BatchMode=yes -p 2222 ankit@127.0.0.1 \
    'echo ok: $(whoami)@$(hostname) && touch /projects/agent_out/.smoke && rm /projects/agent_out/.smoke && echo "agent_out writable"'

# ── gilfoyle: homeserver ops agent (cron-driven loops) ──────────────────
# Workspace: /cybernetics/agents/gilfoyle/. Surfaces to Discord #homeserver-ops.

# (Re)register gilfoyle's cron jobs in the running gateway. Idempotent by name.
gil-cron-setup:
  bin/setup-gilfoyle-cron.sh

# List gilfoyle's scheduled jobs.
gil-cron-list:
  docker exec openclaw openclaw cron list --agent gilfoyle

# Force-run a loop now by job name and wait for terminal status.
# Usage: just gil-loop gilfoyle-health-watch
gil-loop name:
  #!/usr/bin/env bash
  set -euo pipefail
  id="$(docker exec openclaw openclaw cron list --agent gilfoyle --json \
    | python3 -c 'import json,sys; d=json.load(sys.stdin); jobs=d.get("jobs",d) if isinstance(d,dict) else d; print(next((j["id"] for j in jobs if j.get("name")=="{{name}}"), ""))')"
  if [ -z "$id" ]; then echo "No gilfoyle job named {{name}} (run: just gil-cron-setup)" >&2; exit 1; fi
  docker exec openclaw openclaw cron run "$id" --wait --wait-timeout 10m --poll-interval 2s

# Follow gilfoyle's gateway logs.
gil-logs:
  {{COMPOSE}} logs -f openclaw
