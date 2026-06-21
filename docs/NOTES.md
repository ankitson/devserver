# Devserver Notes

Top-level open threads / gotchas. Sub-projects keep their own detailed notes;
link them here.

## Data pipelines
Production status, Garmin API quirks, auth/rate-limit notes, landing-zone
contract, and pending work. Full detail:
[`pipelines/docs/NOTES.md`](../pipelines/docs/NOTES.md).

## 2026-06-20 - Bifrost LLM gateway
- **What**: `bifrost` compose service (`maximhq/bifrost`), a second LLM gateway next to LiteLLM.
  OpenAI-compatible at `http://bifrost:8080` on `mybridge`; local UI/logs `http://127.0.0.1:8090`.
  Declarative config in `config/bifrost.config.json` (re-applied every boot); keys in
  `secrets/bifrost.env`. Model ids are `<provider>/<model>`, e.g.
  `openrouter/openai/gpt-oss-20b:free`, `anthropic/claude-haiku-4-5`.
- **Passing OpenRouter routing (ZDR, provider pinning) through Bifrost — TWO working methods.**
  By default Bifrost **strips unknown body fields** like OpenRouter's `provider` routing object
  (verified: `provider.only:["nonexistent"]` 404s direct to OpenRouter, but a bare body through
  Bifrost completes). OpenRouter also has **no ZDR header** — routing is a body field. Two ways to
  get it through the gateway:
  1. **`extra_params` + header (per-request, dynamic — preferred for ad-hoc ZDR).** On the OpenAI
     route, nest provider-native fields under `extra_params` and send
     `x-bf-passthrough-extra-params: true`; Bifrost merges them as top-level fields into the upstream
     request. **Verified**: with the header, `extra_params.provider.only:["nonexistent"]` now 404s
     ("No allowed providers"), and `extra_params.provider.only:["wafer"]` on deepseek-v4-flash routes
     to the wafer endpoint. Example for ZDR:
     ```
     curl http://127.0.0.1:8090/openai/v1/chat/completions \
       -H 'x-bf-passthrough-extra-params: true' -H 'Content-Type: application/json' \
       -d '{"model":"openrouter/deepseek/deepseek-v4-flash","messages":[...],
            "extra_params":{"provider":{"only":["wafer"],"allow_fallbacks":false}}}'
     ```
     Note: this is body+header, so it suits raw API/script clients. Clients that only let you set the
     model string (e.g. opencode) can't inject `extra_params` → use method 2.
  2. **OpenRouter Presets (server-side, model-string only — preferred for opencode).** A preset stores
     the routing *on OpenRouter* and is referenced purely via the model string (`@preset/<slug>`),
     which Bifrost passes through untouched. So:
  - `config/openrouter-presets.json` defines the `zdr-deepseek-wafer` preset
    (`model: deepseek/deepseek-v4-flash`, `provider: {only:["wafer"], allow_fallbacks:false}`).
  - `just openrouter-presets-sync` (tools/openrouter_presets.py) POSTs it to the account — idempotent,
    no inference cost. `just openrouter-presets-check` prints the live config.
  - Call it through Bifrost as model `openrouter/@preset/zdr-deepseek-wafer`. **Verified end-to-end**
    (direct + through Bifrost + through opencode): served by Wafer, `is_byok:true`, OpenRouter
    `cost:0` (billed to the wafer side). `allow_fallbacks:false` + `only:["wafer"]` means a success
    can *only* be wafer.
  - Conceptually: pinning to a single ZDR provider via `only` is **sufficient** for ZDR — you do NOT
    additionally need `zdr:true`/`data_collection:"deny"`; `only` already removes all fallback. Those
    flags matter only when you let OpenRouter *choose* among providers. (For a softer setup, a preset
    with `provider:{order:["wafer"], data_collection:"deny"}` = "prefer wafer, never fall back to a
    non-ZDR provider".)
  - **BYOK** (wafer's own key + credits) is an account-level Integrations setting, not part of the
    preset; the preset just forces routing to wafer, and OpenRouter uses the stored wafer key (5%
    BYOK fee on OR credits; `cost:0` on tiny calls).
  - **Global fallback (method 3):** for a blanket "never retain" default across *all* requests/clients
    regardless of gateway, set it once in the dashboard at <https://openrouter.ai/settings/privacy>
    (disable prompt training/logging; require non-retaining providers). No per-request work, but it
    can't target one specific provider the way a preset/`extra_params` can.
- **OpenRouter key limit**: the `op://clankers/openrouter` key has a *total credit limit* (was $10,
  spent; raised to $15 → ~$5 free). Free models don't draw it, but paid/BYOK calls need headroom for
  the BYOK fee — if paid calls 403 with "Key limit exceeded (total limit)", raise it at
  <https://openrouter.ai/settings/keys>.
- **Claude/Codex subs DON'T work via Bifrost**: it proxies **API keys only**. Claude Code and Codex
  CLI both prefer their own subscription OAuth and have to be logged out to point at a gateway —
  there's no way to ride a Pro/Max/ChatGPT subscription through Bifrost. The `anthropic`/`openai`
  providers here are API-key billed (and the `anthropic-key1` key currently has $0 balance, so
  Claude-via-Bifrost 400s with "credit balance too low" until funded).
- **Free-model flakiness**: OpenRouter free models are heavily shared/rate-limited — expect 429s on
  hot ones (llama-3.3-70b, qwen3-coder) and occasional slow ones (bumped the openrouter
  `default_request_timeout_in_seconds` to 120). `openai/gpt-oss-20b:free` was reliable in testing.
- **Caddy**: not yet exposed via Caddy (no `bifrost.*.ankitson.com` route) — local/`mybridge` only.
  Add a homeserver Caddy `reverse_proxy bifrost:8080` if LAN/TLS access is wanted, mirroring litellm.

### Phase 1 additions (2026-06-20): NVIDIA NIM, BYOK, STT, search
- **NVIDIA NIM** = custom provider `nvidia` (`integrate.api.nvidia.com`, key `op://clankers/nvidia-build`).
  Gotchas learned the hard way:
  - **`base_url` must omit `/v1`** — Bifrost appends `/v1/chat/completions` (so use
    `https://integrate.api.nvidia.com`, not `.../v1`, else 404). Same as the openrouter `/api` base.
  - **Custom providers don't accept `models:["*"]`** — wildcard "cannot be used with other values" and
    `*`-alone doesn't resolve custom models. You must **list explicit model ids** in the key's `models`.
  - **Env-var changes need `just up bifrost` (recreate), NOT `just restart`** — `docker compose restart`
    does not reload `env_file`, so a new key reads empty and the provider shows "no valid keys found".
    (config.json *content* changes DO apply on a plain restart, since it's a bind-mount read at boot.)
  - Verified: `nvidia/meta/llama-3.1-8b-instruct` → `NIM_OK`.
- **DeepSeek + Mistral via OpenRouter BYOK** — NOT separate Bifrost providers; they ride OpenRouter and
  bill your own provider key/credits once registered. Path verified through Bifrost today (mistral via
  `mistralai/ministral-3b-2512`; deepseek unpinned via `deepseek/deepseek-chat-v3.1`) on OpenRouter
  credits. **BLOCKED on registering the BYOK keys**: `POST /api/v1/byok` needs an OpenRouter
  **management/provisioning key** (the normal `sk-or-` key returns 401 "Invalid management key"), and
  op has only the one `openrouter` item. Two ways to finish:
  1. Add a provisioning key to op (e.g. `op://clankers/openrouter-mgmt`) and run `just openrouter-byok-sync`
     (config `config/openrouter-byok.json`, script `tools/openrouter_byok.py` — reads the deepseek/
     mistral keys from op at sync time).
  2. Or add them by hand at <https://openrouter.ai/settings/integrations>.
  Once registered, OpenRouter **auto-prioritizes** BYOK for that provider (no pinning needed; pin with
  `provider.only:["deepseek"]` to force it). Note: DeepSeek's first-party endpoint only *appears* on
  OpenRouter once its BYOK key exists — until then `only:["deepseek"]` 404s.
- **Speech-to-text** = custom provider `speaches` pointed at the existing local Whisper service
  (`http://speaches:8000`, free, no GPU cost beyond what speaches already uses). Gotchas:
  - Needs **`network_config.allow_private_network: true`** (Bifrost blocks private-IP hops by default;
    speaches is on `mybridge`).
  - speaches needs no auth but Bifrost still requires a key entry → `SPEACHES_API_KEY=none`.
  - Verified: `POST /openai/v1/audio/transcriptions` with model
    `speaches/deepdml/faster-whisper-large-v3-turbo-ct2` transcribed the JFK clip correctly.
- **Web search**: Bifrost has **no native web search** — search is only available as an **MCP tool**.
  And the `maximhq/bifrost` image has **no node/npx/python**, so it **cannot run stdio MCP servers** —
  only HTTP MCP. Solved in Phase 2 by pointing Bifrost at **mcpproxy** over HTTP, which already
  federates **Exa web search** — so search now works through Bifrost (see Phase 2). Brave was therefore
  not needed; the key is staged at `op://clankers/brave` if you want it as an additional engine (add it
  to mcpproxy, not Bifrost). ("codex search" in the original ask is ambiguous — Codex's own web_search
  is an OpenAI Responses-API hosted tool, separate from this; Bifrost gets search via the Exa MCP tool.)

### Phase 2 (2026-06-20): MCP port — Bifrost ↔ mcpproxy
- **Approach**: rather than re-declaring every upstream, Bifrost connects to **mcpproxy** as a single
  **HTTP MCP client** (`mcp.client_configs[]`, `connection_string: http://172.19.0.1:3130/mcp/all`,
  the host-gateway address of the host-networked mcpproxy). mcpproxy already federates exa/fastmail/
  websets, so Bifrost inherits all of them. Verified: Bifrost discovered **26 tools** (exa ×3 +
  websets ×23; fastmail's OAuth tools aren't in the downstream agent-token's federated set).
- **Token can't be an env ref in MCP headers** — Bifrost only env-substitutes `connection_string`, NOT
  header values (tested: `Bearer env.X` → 401 from mcpproxy). So `config.json` now carries the
  mcpproxy bearer token and is **rendered from `config/bifrost.config.json.tmpl` → `secrets/bifrost.config.json`**
  by `just rs` (op inject), exactly like `openclaw.config.patch.json.tmpl`. The compose mount changed
  from `./config/bifrost.config.json` to `./secrets/bifrost.config.json`. **A fresh setup must run
  `just rs` (or `op inject` the two bifrost templates) before `just up bifrost`.**
- **Tools are deny-by-default**: a request opts in per-call with header `x-bf-mcp-include-clients: mcpproxy`
  (or `x-bf-mcp-include-tools: <client>-<tool>`). Nothing is exposed to a model unless it asks.
- **Agent mode**: `tools_to_auto_execute` is set to the **read-only Exa search tools only**
  (`exa__web_search_exa`, `_advanced_exa`, `web_fetch_exa`) so Bifrost auto-runs them in a loop and
  returns a grounded answer in one call. The 23 websets tools remain available (`tools_to_execute:["*"]`)
  but require client approval via `/v1/mcp/tool/execute` — destructive ops (create/delete webset) never
  auto-run. **Verified end-to-end**: `nvidia/meta/llama-3.1-8b-instruct` + `x-bf-mcp-include-clients: mcpproxy`
  searched the web and answered correctly.
- **Security note**: Bifrost has no downstream auth yet (local/`mybridge` only), so any caller that sends
  the include header can reach these tools. Fine for now; add Bifrost virtual keys / a Caddy auth layer
  before exposing it off-box.

### SillyTavern (2026-06-20)
- `sillytavern` compose service (`ghcr.io/sillytavern/sillytavern:latest`, port 8000) on mybridge,
  state under `./volumes/sillytavern/{config,data,plugins,extensions}` (gitignored). Local debug at
  `127.0.0.1:8001`; LAN/TLS at **https://sillytavern.dev.ankitson.com** (private_only Caddy route in
  homeserver `dev.Caddyfile`).
- **Reverse-proxy posture** set declaratively via compose env (`SILLYTAVERN_LISTEN=true`,
  `WHITELISTMODE=false`, `SECURITYOVERRIDE=true`) so it works on a fresh volume — `config.yaml` itself
  is in the gitignored volume. ST has no auth of its own; Caddy private_only is the gate.
- **Pointed at Bifrost**: pre-seeded `data/default-user/settings.json` →
  `oai_settings.chat_completion_source=custom`, `custom_url=http://bifrost:8080/openai/v1`,
  `custom_model=nvidia/meta/llama-3.1-8b-instruct` (+ placeholder `api_key_custom` in secrets.json;
  Bifrost is keyless). ST proxies the API call server-side, so the internal `bifrost:8080` hostname
  resolves. **Verified end-to-end**: ST → Bifrost `/openai/v1/chat/completions` returns completions.
- To change models in the UI: API Connections → Chat Completion → Custom (OpenAI-compatible); the model
  dropdown is populated live from Bifrost's `/openai/v1/models` (any provider/model id works, e.g.
  `openrouter/...`, `nvidia/...`, `deepseek/deepseek-chat`).

### Follow-ups (2026-06-20): Caddy, DeepSeek-direct, fastmail
- **Caddy**: Web UI + API now at **https://bifrost.dev.ankitson.com** (private_only / LAN+Tailscale).
  Route added to `homeserver:volumes/caddy/dev.Caddyfile` (`reverse_proxy bifrost:8080`; Caddy is on
  mybridge so it resolves the container by name) and reloaded live. **That edit is in the homeserver
  repo and is currently uncommitted there.**
- **DeepSeek can't be BYOK'd through OpenRouter** — OpenRouter has **no DeepSeek-direct endpoint** for
  any deepseek slug (`only:["deepseek"]` 404s with `available_providers: [streamlake, deepinfra, novita]`;
  all the deepseek models on OR are served by third parties). Mistral BYOK *does* work (verified direct:
  `provider: Mistral, is_byok: true`). So DeepSeek was added as a **direct Bifrost provider** instead
  (`deepseek` custom openai-compatible, `https://api.deepseek.com`, key `op://clankers/deepseek`, models
  `deepseek-chat`/`deepseek-reasoner`). Wired & authenticating — currently returns **402 Insufficient
  Balance** (that DeepSeek account needs funding), not a config problem.
- **`is_byok` is invisible through Bifrost** — Bifrost normalizes the usage object and drops OpenRouter's
  `is_byok`/`cost` fields. To confirm BYOK, call OpenRouter directly. Routing still works through Bifrost
  (e.g. mistral `extra_params.provider.only:["mistral"]` hits the BYOK Mistral endpoint).
- **fastmail is configured in mcpproxy but does NOT reach Bifrost.** mcpproxy shows fastmail connected,
  OAuth healthy, 18 tools — but its `/mcp/all` downstream route (what Bifrost connects to) federates only
  the **stdio, non-OAuth** servers (exa + websets = 26 tools); the OAuth fastmail upstream is not exposed
  to downstream bearer tokens. So fastmail tools are absent from Bifrost's 26.
- **Do you have to use mcpproxy? Depends on transport:**
  - **stdio MCPs** (exa, websets, brave): the bifrost image has no node/npx/python, so it **can't run
    them** — they need mcpproxy (or a node-equipped sidecar) and reach Bifrost over HTTP. mcpproxy is the
    convenient single aggregation point for these.
  - **HTTP MCPs** (fastmail): Bifrost can host these **directly** (`connection_type:"http"` with
    `auth_type` `oauth`/`per_user_oauth`/`headers`) — no mcpproxy needed. Getting fastmail into Bifrost
    means adding it as a direct HTTP+OAuth client, which requires completing Fastmail's interactive OAuth
    flow against Bifrost (not done yet — mcpproxy currently owns that OAuth session).

## 2026-06-10 - agent-sandbox for the remote agent
- **Why**: mcpproxy's code-mode sandbox (goja VM) has no filesystem by design, so a remote
  agent can't persist large outputs through `/mcp/code`. An SSH-able devbox with a real FS is
  the cleaner pattern: run commands remotely, redirect big output to files, read back slices.
- **Division of labor**: mcpproxy stays the gateway for OAuth'd tool access (Fastmail/Exa/
  websets); `agent-sandbox` is the compute + artifact workspace. Artifacts land on the host at
  `/projects/agent_out/`.
- **Auth**: the seeded `authorized_keys` currently holds only the shared dev key. Prefer giving
  the remote agent its own keypair: `just agent-sandbox-add-key "ssh-ed25519 AAAA... agent@host"`.
  The baked-in private key was deleted from this container's home (inbound-only SSH).
- **Audit caveat**: work done over SSH bypasses mcpproxy's UI/audit trail — blast radius is the
  container plus `/projects/agent_out`, gated by the mounts.
- **Gotcha**: `compose up` warns about orphan containers (pipeline-dagster, agentmemory, etc.)
  in the `devserver` project — pre-existing, from services managed outside this compose file;
  don't `--remove-orphans` blindly.

## 2026-06-09 - MCPProxy code-mode and full private route
- **Code execution**: `enable_code_execution` is on in both the live MCPProxy config and
  `config/mcpproxy.seed.json`; `code_execution_timeout_ms` is 600000 (10 minutes).
- **Default routing**: `/mcp` uses `routing_mode: retrieve_tools` with `tools_limit: 5`; `/mcp/all`
  remains the explicit direct all-tools route.
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

## 2026-06-19 - DNS probe for MCPProxy post-wake failures
- **Goal**: reproduce and timestamp the DNS failure pattern that caused Fastmail OAuth refreshes to
  fail after sleep, without changing MCPProxy DNS behavior.
- **Probe design**: `dns-probe` is a static Go binary in an Alpine container with `network_mode:
  host`, matching MCPProxy's static Go + Alpine + host-network setup. It logs Go
  `net.DefaultResolver` lookups, direct `net.Resolver` lookups to configured DNS servers, and an
  HTTP GET to Fastmail's OAuth metadata endpoint.
- **Log files**: container probe output goes to `logs/dns-probe.jsonl`; host journal correlation
  goes to `logs/dns-probe-host.jsonl` when `just dns-probe-host-logs` is running.
- **Repro flow**: run `just dns-debug-on`, `just dns-probe-clean`, `just dns-probe-up`, and in a
  second terminal `just dns-probe-host-logs`. After wake, inspect the two JSONL logs and turn debug
  back down with `just dns-debug-off`.
