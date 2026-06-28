# Devserver Changelog

Top-level changelog. Sub-projects keep their own detailed changelogs; link them
here.

## Data pipelines
Garmin / banking / Playnite / AoE4-replay / X-bookmarks pipelines on Dagster
(+ DBOS / Restate experiments). Full detail:
[`pipelines/docs/CHANGELOG.md`](../pipelines/docs/CHANGELOG.md).

## 2026-06-27

### OpenClaw routed entirely through Bifrost
- Added a custom `bifrost` provider to `config/openclaw.config.patch.json.tmpl`
  (`baseUrl http://bifrost:8080/openai/v1`, `api: openai-completions`, `apiKey: none` — Bifrost has
  `enforce_auth_on_inference: false`). Model ids carry Bifrost's `provider/model` prefix, so OpenClaw
  refs are `bifrost/codex/gpt-5.4`, `bifrost/deepseek/deepseek-v4-flash`, etc. (Bifrost receives the
  canonical `codex/gpt-5.4`).
- Set `agents.defaults.models` to a `bifrost/*`-only allowlist, making Bifrost the **sole** provider
  OpenClaw can use. Primary model is `bifrost/codex/gpt-5.4` (ChatGPT subscription, no API credits).
- `fallbacks: []` — note the Codex sub is rate-limited (5h primary window); with no fallback, agents
  error when the limit is hit. Candidate fallbacks if wanted: `bifrost/openai/gpt-5.4-mini` or
  `bifrost/deepseek/deepseek-v4-flash`.
- Apply: `just rs` → `just restart openclaw` (entrypoint re-applies the patch on start; no env change).

### Codex/ChatGPT subscription as Bifrost `codex` provider
- Added `codex-oauth` Compose service (`ankit/codex-oauth-proxy:local`, built from
  `/projects/dockers/codex-oauth-proxy/Dockerfile`) wrapping EvanZhouDev/openai-oauth: turns a ChatGPT
  Plus/Pro subscription into an OpenAI-compatible `/v1` endpoint on `:10531`, auto-refreshing the
  Codex OAuth token and proxying to `chatgpt.com/backend-api/codex`. Internal (mybridge + loopback
  debug port); dedicated `auth.json` mounted from `secrets/codex-oauth/` (gitignored).
- Registered the custom provider `codex` in `config/bifrost.config.json.tmpl`
  (`base_url http://codex-oauth:10531`, `base_provider_type: openai`, `models: ["*"]`). Added
  placeholder `CODEX_PROXY_API_KEY=none` to `config/bifrost.env.tmpl` (shim needs no key; Bifrost
  requires a key entry).
- Used `["*"]` rather than an explicit model list: a throwaway-Bifrost test (same image) proved the
  wildcard now resolves for custom providers and routes any `codex/<id>` to the shim
  (`codex/gpt-5.3-codex-spark` returned a real completion). This contradicts the older NVIDIA-era note
  that custom providers can't wildcard — that limitation is fixed in this Bifrost version. The shim's
  `/v1/models` is account-aware, so it (not a static list) is the source of truth for what exists.
- Added Justfile recipes: `codex-oauth-login`, `codex-oauth-up`, `codex-oauth-logs`,
  `codex-oauth-models`, `codex-oauth-test`.
- Activation is manual (needs `op` + browser OAuth): `just rs` → `just codex-oauth-login` →
  `just up --build codex-oauth` → `just up bifrost`. See NOTES for the full runbook.
- Switched the `deepseek` custom provider from the explicit `deepseek-chat`/`deepseek-reasoner` list to
  `models: ["*"]` (same now-verified wildcard support). `anthropic`/`openai` were already `["*"]`;
  `nvidia` kept explicit (its NIM catalog is a fixed allowlist).

### Bifrost Unsloth stream timeout
- Set Unsloth's Bifrost `stream_idle_timeout_in_seconds` to 300 seconds in the config template,
  rendered local config, and live provider SQLite row.
- Set opencode's Bifrost provider `timeout` and `chunkTimeout` options to 300000 ms so its client-side
  request and chunk caps match the intended 5-minute window.

### Bifrost 1.6.0 dynamic image
- Updated `/projects/dockers/bifrost-dynamic` to build from upstream Bifrost transport tag
  `transports/v1.6.0`, keeping the local `model-policy-suffix` plugin image published as
  `ankit/bifrost-dynamic:local`.
- Verified Docker Hub publishes `maximhq/bifrost:v1.6.0`; `maximhq/bifrost:latest` currently points
  to the same amd64/arm64 image manifest.


## 2026-06-26

### Unsloth Studio provider
- Added a Bifrost custom OpenAI-compatible provider `unsloth` pointed at the local Studio host, with
  model `unsloth/default` for the active local Studio model.
- Added `UNSLOTH_STUDIO_API_KEY` to the Bifrost env template, sourced from
  `op://clankers/llm-windows/password`.
- Added `bifrost/unsloth/default` to OpenCode's local Bifrost model list.
- Updated OpenCode's `unsloth/default` metadata to advertise a 131072-token context window.
- Updated the Windows Unsloth service to `unsloth==2026.6.9` / `unsloth_zoo==2026.6.7` and
  llama.cpp `b9821`.
- Updated the Windows `win-models` Unsloth defaults to 131072 context and removed the obsolete
  `--simple-policy` llama installer flag so future llama.cpp updates work with current Studio.
- Added `--reasoning-format deepseek` to the Windows Unsloth serve path and a persistent Studio
  route shim that converts Gemma 4 `<think>...</think>` output back into OpenAI-style
  `reasoning_content` before Bifrost sees it.
- Verified `unsloth/default` through Bifrost with a streaming prompt above the previous 4096-token
  limit.
- Verified Gemma 4 thinking remains visible as metadata: direct Studio streams emit
  `delta.reasoning_content`, and Bifrost normalizes that to `delta.reasoning`/`reasoning_details`
  while keeping `delta.content` clean.

### Bifrost Model-Policy Suffix Plugin
- Switched the `bifrost` Compose service to build `/projects/dockers/bifrost-dynamic` as
  `ankit/bifrost-dynamic:local`.
- Added `model-policy-suffix` to `config/bifrost.config.json.tmpl` and the live rendered Bifrost
  config, loading `/app/plugins/model-policy-suffix.so`.
- Updated OpenCode's default model to
  `bifrost/openrouter/deepseek/deepseek-v4-pro[zdr,provider=digitalocean]`.
- Replaced visible OpenCode preset routes with suffix routes, including
  `bifrost/openrouter/deepseek/deepseek-v4-flash[zdr,provider=digitalocean]`.
- Removed the DS4 Flash DigitalOcean preset from the declarative preset config so new
  model/provider combinations are managed by Bifrost suffixes instead.
- Verified the suffix route without OpenRouter presets: impossible provider pins fail at
  OpenRouter, DigitalOcean pins report `provider_name: DigitalOcean`, and OpenCode succeeds through
  the suffix model.
- Expanded the Bifrost suffix plugin to accept arbitrary OpenRouter request params from the model
  string via raw JSON object, quoted JSON, `json64:...`, query-style, or dotted-key suffixes while
  preserving the shorthand `[zdr,provider=...]` syntax.
- Added a configured OpenCode `json64:` DS4 Flash DigitalOcean example, because OpenCode rejects
  arbitrary unlisted model strings even though direct Bifrost calls can use them.

### OpenCode DeepSeek v4 Pro ZDR Preset
- Added `zdr-deepseek-v4-pro` to `config/openrouter-presets.json`, routing
  `deepseek/deepseek-v4-pro` through OpenRouter with `provider.zdr:true` and
  `provider.data_collection:"deny"`. Updated it to pin `provider.only:["digitalocean"]` with
  `allow_fallbacks:false`.
- Updated `~/.config/opencode/opencode.jsonc` to expose
  `bifrost/openrouter/@preset/zdr-deepseek-v4-pro` and make it the default model, while keeping
  `bifrost/openrouter/deepseek/deepseek-v4-pro` available for non-ZDR opt-out.
- Added OpenRouter preset `zdr-deepseek-v4-flash-digitalocean` and exposed it in OpenCode as
  `bifrost/openrouter/@preset/zdr-deepseek-v4-flash-digitalocean`.
- Restricted OpenCode's visible provider set to `enabled_providers:["bifrost"]` without changing the
  auth store, and added `~/.config/opencode/plugins/bifrost-passthrough-headers.js` to attach
  `x-bf-passthrough-extra-params:true` on Bifrost chat requests.
- Removed the misleading DS4 Flash `model.options.provider` example from OpenCode config after
  OpenRouter logs showed that OpenCode did not transmit it as raw provider-routing JSON.
- Synced the preset to OpenRouter and verified it through Bifrost.
- Documented the Bifrost source/discussion finding: stock aliases/routing do not inject OpenRouter
  `provider` fields; central Bifrost-owned policy would require `extra_params` per request or a
  custom plugin on a dynamically linked Bifrost build.

## Bifrost LLM gateway (2026-06-20)
- Added the `bifrost` compose service (`maximhq/bifrost:latest`) — a Go OpenAI/Anthropic-compatible
  LLM gateway alongside the existing LiteLLM one. On `mybridge`; other services reach it at
  `http://bifrost:8080`. Web UI + request logs on `http://127.0.0.1:8090` (`BIFROST_PORT`).
- Config is declarative: `config/bifrost.config.json` (checked in, no secrets — keys referenced as
  `env.OPENROUTER_API_KEY` etc.) mounted read-only over the app-dir. Bifrost re-applies it on every
  boot. Runtime sqlite (`config.db`, `logs.db`) lives in the bind-mounted `./volumes/bifrost`
  (gitignored). Provider keys come from `secrets/bifrost.env` (template `config/bifrost.env.tmpl`,
  rendered by `just rs` — keys: openrouter, anthropic-key1, openai).
- Providers: `openrouter` (tested end-to-end with free models, e.g. `openrouter/openai/gpt-oss-20b:free`),
  `anthropic` and `openai` (API-key paths wired; the anthropic key currently has no credit balance).
- **Claude/Codex subscriptions are NOT usable through Bifrost** — it proxies API keys only. Claude
  Code/Codex CLI both prefer their own OAuth and must be logged out to use a gateway; there is no
  subscription pass-through. The anthropic/openai providers here are API-key billed.
- **Zero Data Retention / provider pinning through Bifrost** — three ways (OpenRouter has no ZDR
  *header*; routing is a request-body `provider` object, which Bifrost strips by default):
  1. **`extra_params` + `x-bf-passthrough-extra-params: true`** — nest `{"provider":{...}}` under
     `extra_params` and Bifrost merges it into the upstream request. Per-request, best for raw API.
     Verified: the header flips `provider.only:["nonexistent"]` from a completion to a 404.
  2. **OpenRouter Presets** — routing stored server-side, referenced by model string `@preset/<slug>`
     (passes through Bifrost untouched; best for model-string-only clients like opencode).
     `config/openrouter-presets.json` + `just openrouter-presets-sync` (tools/openrouter_presets.py)
     define `zdr-deepseek-wafer` (pins `deepseek/deepseek-v4-flash` → ZDR `wafer` BYOK endpoint,
     `allow_fallbacks:false`). Verified e2e through Bifrost + opencode: served by Wafer, `is_byok:true`.
  3. **Account-level** privacy default at <https://openrouter.ai/settings/privacy> (global, blanket).
  See `docs/NOTES.md` for the full writeup, conceptual ZDR notes, and curl examples.
- **Phase 1 providers (2026-06-20)**: added **NVIDIA NIM** as custom provider `nvidia`
  (`integrate.api.nvidia.com`, `op://clankers/nvidia-build`; verified `nvidia/meta/llama-3.1-8b-instruct`)
  and **speaches** as custom STT provider (local Whisper at `http://speaches:8000`,
  `allow_private_network:true`; verified `/v1/audio/transcriptions` on the JFK clip). Custom-provider
  gotchas captured in NOTES (base_url without `/v1`; explicit model lists — no `*`; env changes need
  `just up` not `restart`). **DeepSeek + Mistral** route via OpenRouter (BYOK) — path verified through
  Bifrost; registering the BYOK keys is blocked on an OpenRouter management key (`config/openrouter-byok.json`
  + `tools/openrouter_byok.py` + `just openrouter-byok-sync` are ready; or use the dashboard). **Web
  search**: Bifrost has none natively and can't run stdio MCPs (no node in image) → search comes via
  mcpproxy over HTTP (Phase 2); Brave key staged at `op://clankers/brave`.
- **Phase 2 — MCP port (2026-06-20)**: Bifrost connects to **mcpproxy** as one HTTP MCP client
  (`http://172.19.0.1:3130/mcp/all`) and inherits all federated tools — **26 discovered** (Exa web
  search ×3 + websets ×23). Because Bifrost doesn't env-substitute MCP header values, `config.json` now
  holds the mcpproxy bearer token and is rendered from `config/bifrost.config.json.tmpl` →
  `secrets/bifrost.config.json` (compose mount moved to `secrets/`; run `just rs` before first boot).
  Tools are deny-by-default (opt in per request with `x-bf-mcp-include-clients: mcpproxy`); only the
  read-only Exa search tools are in `tools_to_auto_execute` (agent mode), so web search returns a
  grounded answer in one call while destructive websets ops stay manual. **Verified end-to-end**:
  `nvidia/meta/llama-3.1-8b-instruct` web-searched and answered. This also satisfies the search ask
  (via Exa; Brave not needed). `just bifrost-mcp-tools` / `just bifrost-test-search`.
- **SillyTavern (2026-06-20)**: added the `sillytavern` service (`ghcr.io/sillytavern/sillytavern`),
  pointed at Bifrost (Custom OpenAI-compatible → `http://bifrost:8080/openai/v1`, pre-seeded model
  `nvidia/meta/llama-3.1-8b-instruct`). Reverse-proxy posture via compose env vars; Web UI at
  **https://sillytavern.dev.ankitson.com** (private_only). Verified ST → Bifrost end to end.
- **Follow-ups (2026-06-20)**: Web UI/API exposed at **https://bifrost.dev.ankitson.com** (private_only;
  route in homeserver `dev.Caddyfile`). **Mistral BYOK** confirmed working via OpenRouter; **DeepSeek
  can't BYOK on OpenRouter** (no DeepSeek-direct endpoint) so added a **direct `deepseek` provider**
  (`api.deepseek.com`, wired & authenticating — that account needs funding). **fastmail** is healthy in
  mcpproxy (18 tools) but its OAuth upstream isn't federated through `/mcp/all`, so it doesn't reach
  Bifrost; to use it, add fastmail directly to Bifrost as an HTTP+OAuth MCP client. See NOTES.
- opencode wired to Bifrost: added a `bifrost` provider in `~/.config/opencode/opencode.jsonc`
  (openai-compatible, `http://127.0.0.1:8090/openai/v1`) with free OpenRouter + Claude model ids;
  verified `opencode run --model bifrost/openrouter/openai/gpt-oss-20b:free` end-to-end.
- New recipes: `just bifrost-providers`, `just bifrost-test [model]`, `just bifrost-reset`.

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

## 2026-06-19

### DNS Probe
- Added `tools/dns_probe`, a static Go DNS/HTTP probe built on Alpine to mirror MCPProxy's resolver
  environment.
- Added a host-networked `dns-probe` Compose service that writes JSONL diagnostics to
  `logs/dns-probe.jsonl`.
- Added Just recipes for starting/stopping/tailing the probe, collecting host journal logs, and
  toggling `systemd-resolved` debug logging.

### MCPProxy image
- Updated the `mcpproxy` Compose build args and image tag from v0.35.0 to v0.43.0 using the
  upstream linux-amd64 release checksum.
- Rebuilt and recreated only the `mcpproxy` service, preserving the existing `mcpproxy_data` volume.
- Retested `retrieve_tools` for the Fastmail calendar update query; v0.43.0 still filters
  `fastmail:update_event` when `exclude_destructive=true`.

### OpenClaw agent thinking defaults
- Set `agents.defaults.thinkingDefault` to `high` in the OpenClaw startup patch and live state for
  newly created agents.
- Set `thinkingDefault: high` on the existing `main`, `gilfoyle`, and `austin` OpenClaw agent
  entries.

## 2026-06-20

### SillyTavern Chat Completion presets
- Added a `just sillytavern-preset-copy` recipe to copy local Chat Completion preset JSON files into
  SillyTavern's `OpenAI Settings` user-volume directory.
