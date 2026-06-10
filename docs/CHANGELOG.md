# Devserver Changelog

Top-level changelog. Sub-projects keep their own detailed changelogs; link them
here.

## Data pipelines
Garmin / banking / Playnite / AoE4-replay / X-bookmarks pipelines on Dagster
(+ DBOS / Restate experiments). Full detail:
[`pipelines/docs/CHANGELOG.md`](../pipelines/docs/CHANGELOG.md).

## agent-sandbox: SSH workspace for the remote agent (2026-06-10)
- Added the `agent-sandbox` compose service: a second `ankit/devbox:1.4` container, driven over
  SSH from another machine instead of by Hermes. Purpose: give the remote agent a real
  filesystem so large outputs land in files on the devserver instead of in model context.
- Mounts: `/projects` read-only, `/projects/agent_out` read-write (new host dir), fresh
  `agent_sandbox_home` volume. On `mybridge`, so it reaches mcpproxy (`172.19.0.1:3130`),
  `agent-browser`, `speaches`, etc.
- SSH published on port `2222` (key-only, `AllowUsers ankit`, `PermitRootLogin no` — hardening
  baked into the devbox image). No GPU, no `seccomp:unconfined`, no OP service-account token —
  least privilege relative to `agent-devbox`.
- Removed the image-baked `id_ed25519` private key from the seeded home: this box only needs to
  be SSH'd *into*, and the shared dev key would let a tenant pivot to other devboxes.
- Added `just agent-sandbox-add-key` (authorize the remote agent's own pubkey) and
  `just agent-sandbox-smoke` (SSH + write-mount check).

## OpenClaw auth doctor helpers (2026-06-09)
- Added `just openclaw-doctor` and `just openclaw-doctor-fix` recipes, and made
  `just upgrade-openclaw` surface pending OpenClaw migrations after rebuilding.
- Pinned OpenClaw's preferred OpenAI auth profile through the rendered config patch, composing the
  `openai:` profile prefix with a 1Password-backed username reference so the tracked template
  contains no private email address.

## MCPProxy code-mode enabled (2026-06-09)
- Set `enable_code_execution: true` and `code_execution_timeout_ms: 600000` (10 min) in both
  `config/mcpproxy.seed.json` and the live `/data/mcp_config.json` (volume `mcpproxy_data`;
  prior config backed up at `/data/mcp_config.json.bak`).
- Set the default `routing_mode` to `retrieve_tools` and capped retrieval results with
  `tools_limit: 5`; `/mcp/all` remains available for direct all-tools routing.
- Restarted mcpproxy and verified the sandbox executes via `mcpproxy code exec --config /data/mcp_config.json`.
- Caddy `/mcp/code` route added in the homeserver repo (`volumes/caddy/dev.Caddyfile`).
- Added the live `websets` upstream to `config/mcpproxy.seed.json` so fresh volumes recreate it.
- Updated the homeserver Caddy route to proxy the full MCPProxy host, including `/ui/` and
  `/api/v1/*`, behind the existing private-network gate.

## Agent container toolbox mounts (2026-05-29)
- Mounted `/projects/toolbox` into `agent-devbox` at `/home/ankit/toolbox` and
  `/home/ankit/.agents` read-only, matching its read-only `/projects` workspace.
- Mounted `/projects/toolbox` into `openclaw` at `/home/ankit/toolbox` and
  `/home/ankit/.agents` read-write, matching its read-write `/projects` workspace.
- Kept other services unchanged because they do not consume the devbox-style agent home.

## OpenClaw (2026-05-27)
Re-added OpenClaw as two compose services (`agent-devbox` left untouched):
- `openclaw` — gateway, thin image `docker/openclaw/` (`FROM ankit/devbox:1.4` + `npm i -g openclaw`).
  State restored from `volumes/openclaw` (bind-mounted to `/home/ankit/.openclaw`). Gateway on
  `127.0.0.1:18789`.
- `agent-browser` — generic chromium + Xvfb + x11vnc + noVNC sidecar (`docker/agent-browser/`,
  vendored from upstream openclaw's `Dockerfile.browser`). CDP relay on `9222` (auth-gated, internal
  to `mybridge`); noVNC GUI on `127.0.0.1:6080`. Renamed from `openclaw-browser` because it's not
  openclaw-specific — any agent on `mybridge` can drive it via the auth-gated CDP endpoint.
OpenClaw drives the browser only over CDP (`browser.profiles.sidecar`, `attachOnly: true`,
`cdpUrl: …@agent-browser:9222`), so no GUI lives in the gateway container. Secrets via
`config/openclaw.env.tmpl` → `just rs`.
Recipes: `just oc-build` / `oc-up` / `oc-logs` / `ab-logs`. Caddy routes:
`openclaw.dev.ankitson.com` → gateway, `agentbrowser.dev.ankitson.com` → noVNC. See [`NOTES.md`](NOTES.md).

## MCPProxy gateway (2026-06-02)
- Added the Exa stdio MCP server from live Claude/Pi client configs to the
  MCPProxy upstream seed, using `EXA_API_KEY` from 1Password.
- Added a host-networked `mcpproxy` service backed by the shared `ankit/mcpproxy:0.35.0` image.
- Added a seed-once Fastmail upstream configuration with OAuth scopes, direct default routing,
  disabled code execution, disabled telemetry, and mandatory downstream MCP authentication.
- Added a dedicated 1Password-backed admin env template and operator recipes for targeted startup,
  logs, health, Fastmail OAuth, downstream token creation, and authenticated smoke tests.
- Persisted MCPProxy live config, OAuth state, and token hashes in the sensitive `mcpproxy_data`
  named volume.

### Fastmail OAuth helper (2026-06-03)
- Changed `just mcpproxy-auth-fastmail` to wait for the Fastmail upstream to become connected after
  printing the daemon's headless authorization URL, then approve Fastmail's discovered tools.
- Documented that the Fastmail authorization URL must be opened on the same host that runs
  `mcpproxy`, because the OAuth callback is bound to `127.0.0.1`.
- Moved OpenClaw's MCPProxy endpoint into `config/openclaw.env.tmpl` as `MCPPROXY_GATEWAY_URL` and
  made `config/openclaw.config.patch.json` reference that env var.

### OpenClaw config patch rendering (2026-06-04)
- Replaced the static OpenClaw MCPProxy patch with `config/openclaw.config.patch.json.tmpl`.
- Mounted the rendered `secrets/openclaw.config.patch.json` into the OpenClaw container so
  OpenClaw receives literal JSON instead of unsupported `${...}` placeholders.

### OpenClaw app runner (2026-06-04)
- Added `config/openclaw-app-runner/runner.ts`, a small static/process web app router for OpenClaw
  deployments.
- Added the `openclaw-app-runner` devserver Compose service, mounted to the Cybernetics deployment
  workspace.
- Added Just recipes for starting, logging, and smoke-testing OpenClaw web apps.
- Documented the deployment contract and follow-up workspace decisions in
  `docs/2026-06-04-openclaw-webapps/`.
