# Devserver Notes

Top-level open threads / gotchas. Sub-projects keep their own detailed notes;
link them here.

## Data pipelines
Production status, Garmin API quirks, auth/rate-limit notes, landing-zone
contract, and pending work. Full detail:
[`pipelines/docs/NOTES.md`](../pipelines/docs/NOTES.md).

## 2026-06-09 - MCPProxy code-mode and full private route
- **Code execution**: `enable_code_execution` is on in both the live MCPProxy config and
  `config/mcpproxy.seed.json`; `code_execution_timeout_ms` is 600000 (10 minutes).
- **Reproducible upstreams**: the seed now includes `exa`, `fastmail`, and the live `websets`
  upstream so a fresh `mcpproxy_data` volume recreates the current server list.
- **Caddy exposure**: homeserver Caddy now proxies the full `mcp.dev.ankitson.com` host through
  `private_only`, including `/mcp/code`, `/ui/`, and `/api/v1/*`. MCP routes still use downstream
  bearer tokens; admin API calls still require `MCPPROXY_API_KEY`.
- **Routing detail**: MCPProxy remains host-networked for loopback OAuth callbacks. Homeserver Caddy
  maps `mcpproxy` to the `mybridge` host gateway with `extra_hosts` and uses
  `reverse_proxy mcpproxy:3130`.

## OpenClaw + agent-browser
- **Two services**:
  - `openclaw` — gateway, thin `docker/openclaw/` image (`FROM ankit/devbox`).
  - `agent-browser` — generic CDP/noVNC chromium sidecar (`docker/agent-browser/`, vendored from
    upstream openclaw's `Dockerfile.browser`). **Not openclaw-specific** — any agent on `mybridge`
    can drive it.
- **Devbox-tag dependency**: `docker/openclaw/Dockerfile` builds `FROM ankit/devbox:${DEVBOX_TAG}`
  (default `1.4`, the same tag `agent-devbox` runs). If the devbox image is rebuilt/retagged, bump
  the `DEVBOX_TAG` build arg in `docker-compose.yml` (openclaw service) and rebuild (`just oc-build`).
- **State source**: live state is `volumes/openclaw` (newer/more complete than the
  `/projects/openclaw-back` Feb-6 cold backup). `openclaw.json` already holds channel/provider
  secrets; `volumes/` is gitignored.
- **CDP auth is mandatory**: the sidecar's CDP relay only listens off-loopback when
  `OPENCLAW_BROWSER_CDP_AUTH_TOKEN` is set. `openclaw.json` references it as
  `${OPENCLAW_BROWSER_CDP_AUTH_TOKEN}` in `browser.profiles.sidecar.cdpUrl` (env-substituted at
  config read), so the token stays in 1Password / `secrets/openclaw.env`, not in the JSON.
  Env-var names kept as `OPENCLAW_BROWSER_*` because that's the upstream relay's wire contract,
  even though the service is now `agent-browser`.
- **Host-header rewrite in the relay**: Chrome's DevTools endpoint rejects any Host header that
  isn't `localhost`/IP. The vendored relay in `docker/agent-browser/entrypoint.sh` rewrites the
  Host to `localhost:<UPSTREAM>` before forwarding upstream; `--remote-allow-origins=*` is added
  for WS-upgrade safety. OpenClaw normalizes the discovered WS URL back to the relay host, so this
  is transparent.
- **1Password**: gateway/CDP/noVNC secrets all reuse the existing `clankers/local-service/password`
  item (no dedicated openclaw item). Swap to per-purpose items later if you want them rotated separately.
- **Browser GUI / sessions**: noVNC at `agentbrowser.dev.ankitson.com` (or `127.0.0.1:6080`).
  Chromium profile persists in the `agent_browser_home` volume; log into sites there once to
  persist cookies. Restoring the old `volumes/openclaw/browser/openclaw/user-data` is best-effort
  (OS-bound encrypted creds won't carry).
- **Remote browsers**: add a profile like
  `laptop: { cdpUrl: "http://<host-or-tailscale>:9222", attachOnly: true }` pointing at Chrome run
  with `--remote-debugging-port=9222` on another device.
- **Caddy routes** (in `~/hroot/homeserver/volumes/caddy/dev.Caddyfile`):
  `openclaw.dev.ankitson.com` → `openclaw:18789`; `agentbrowser.dev.ankitson.com` → `agent-browser:6080`.
- **Stale `@crab` route** in the main Caddyfile points to a long-gone `openclaw-gateway` container —
  safe to delete or repoint at `openclaw:18789` next time you touch that file.

## Host-shared agent directories
- **Toolbox contract**: `/projects/toolbox` is the canonical host-shared toolbox clone. Agent
  containers that need personal instructions/skills should bind-mount it at both
  `/home/ankit/toolbox` and `/home/ankit/.agents`; do not rely on baked image symlinks or entrypoint
  mutation for these paths.
- **Current consumers**: `agent-devbox` mounts toolbox read-only because its `/projects` workspace is
  read-only; `openclaw` mounts toolbox read-write because its `/projects` workspace is read-write.
- **Non-consumers**: `hermes`, `agentsview`, `agent-browser`, and `speaches` do not run the
  devbox-style agent home and do not need these mounts right now.

## 2026-06-02 - Dockerized MCPProxy gateway
- **Goal**: provide one MCP gateway for standards-compliant upstream MCP servers and MCP-capable
  clients without forwarding downstream credentials to upstream providers.
- **Decision**: run MCPProxy personal edition `v0.35.0` with host networking because its OAuth
  callback listener binds to `127.0.0.1:<ephemeral-port>`. Keep its HTTP API bound to the
  `mybridge` gateway at `172.19.0.1:3130`, which Caddy can reach without a LAN listener.
- **Bootstrap**: `config/mcpproxy.seed.json` adds Fastmail and is copied only for an empty data
  volume. MCPProxy owns live config, OAuth refresh tokens, DCR credentials, and hashed agent tokens
  under the sensitive `mcpproxy_data` volume after first boot.
- **Client contract**: default direct routing is `/mcp`; explicit direct routing is `/mcp/all`;
  retrieval routing for large catalogs is `/mcp/call`. All MCP routes require a scoped downstream
  bearer token separate from `MCPPROXY_API_KEY`.
- **Upstreams imported from clients**: `fastmail` came from Pi/OpenCode dotfiles. `exa` came from
  live Claude and Pi MCP configs and runs as `npx -y exa-mcp-server@3.2.1` with `EXA_API_KEY`
  injected from 1Password into the MCPProxy container.
- **Rollout**: complete Fastmail authorization in a browser running on the devserver host, generate
  the shared downstream agent token, store it in 1Password, then smoke-test before wiring clients.
  See [`docs/2026-06-02-mcpproxy-gateway/README.md`](2026-06-02-mcpproxy-gateway/README.md).

## 2026-06-03 - Fastmail MCPProxy OAuth investigation
- **Finding**: MCPProxy and Exa are healthy, but Fastmail is not connected because no completed
  OAuth callback has persisted a refresh token in `mcpproxy_data`.
- **Operational cause**: Fastmail OAuth uses a daemon-owned loopback callback at
  `127.0.0.1:<ephemeral>/oauth/callback`. The authorization URL must be opened on the same host
  running `mcpproxy`; opening it elsewhere sends the callback to the wrong machine.
- **Helper fix**: `just mcpproxy-auth-fastmail` now treats daemon-mode `auth login` as an initiation
  step, prints the URL, waits until `upstream list` reports Fastmail as connected and ready, then
  approves Fastmail's discovered tools so downstream clients can see them.
- **OpenClaw config**: the OpenClaw patch reads `${MCPPROXY_GATEWAY_URL}` from `openclaw.env`
  instead of hardcoding the gateway endpoint in `openclaw.config.patch.json`.

## 2026-06-04 - OpenClaw config patch startup failure
- **Symptom**: `openclaw` was in a Docker restart loop with exit code `1`; logs only repeated
  `TypeError: Invalid URL`.
- **Root cause**: the entrypoint failed before `openclaw gateway`, during
  `openclaw config patch --file /run/openclaw/openclaw.config.patch.json`. OpenClaw 2026.5.28 no
  longer expands literal `${...}` placeholders in config patches, so the raw
  `${MCPPROXY_GATEWAY_URL}` value was parsed as a URL and crashed patch application.
- **Fix**: moved the MCPProxy patch to `config/openclaw.config.patch.json.tmpl` and render it with
  the existing `render-secrets` / `just rs` flow into `secrets/openclaw.config.patch.json`, which
  contains a literal URL and rendered bearer header at container startup.

## 2026-06-04 - OpenClaw web app runner
- **Goal**: let OpenClaw publish static pages and small backend web apps without Docker socket access
  or write access to homeserver infrastructure.
- **Decision**: added `openclaw-app-runner` to devserver. It reads app code and `apps.json` from
  `/home/ankit/hroot/cybernetics/agents/openclaw-webapps`, serves static apps, starts small process
  apps, and exposes one router on `mybridge`.
- **Routing**: Caddy's `*.dev.ankitson.com` explicit routes keep precedence; the final fallback now
  proxies unknown dev subdomains to `openclaw-app-runner`, which serves configured app slugs or
  returns its own 404.
- **Follow-ups**: see `docs/2026-06-04-openclaw-webapps/ADR.md` for workspace/AGENTS consolidation
  decisions to evaluate after the runner is live.
