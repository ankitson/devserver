# Devserver Changelog

Top-level changelog. Sub-projects keep their own detailed changelogs; link them
here.

## 2026-07-12

### Open WebUI: stream Chatterbox PCM directly to Web Audio

- Replaced the moving `ghcr.io/open-webui/open-webui:main` deployment with a
  locally built 0.10.2 source fork and a digest-pinned derived image.
- Added an authenticated, opt-in streaming PCM backend relay and a shared Web
  Audio queue used by Voice mode and Read Aloud.
- Scoped saved voices by TTS engine and model in reviewable frontend source.
- Routed only TTS directly to Chatterbox; left all Bifrost-backed services and
  routing unchanged.
- Removed the compiled-bundle voice patch and mounted audio-router patch from
  the running service wiring.
- Added focused backend/frontend tests and Just recipes for rebuilding and
  deploying the source image.


## Data pipelines
Garmin / banking / Playnite / AoE4-replay / X-bookmarks pipelines on Dagster
(+ DBOS / Restate experiments). Full detail:
[`pipelines/docs/CHANGELOG.md`](../pipelines/docs/CHANGELOG.md).

## 2026-07-09

### AgentsView: pull opencode sessions from Windows
- Added `opencode` to `REMOTE_SOURCES` for `desktop-win` at `C:\Users\ankit\.local\share\opencode`.
- Added `"opencode": "OPENCODE_DIR"` to `AGENT_ENV` mapping so the sync container knows which env var to set.
- Updated default `--agents` to `"codex,claude,opencode"` so the scheduler picks up opencode automatically.
- Pulled and synced 12 existing opencode sessions from desktop-win (4 from July 9, 5 from July 8).

### Open WebUI: add voice-chat UI wired to Bifrost and MCPProxy
- Added the `open-webui` Compose service (`ghcr.io/open-webui/open-webui:main`) on `mybridge`, reachable at `https://chat.home.ankitson.com`.
- Routed chat, STT (`speaches/deepdml/faster-whisper-large-v3-turbo-ct2`), TTS (`speaches/speaches-ai/Kokoro-82M-v1.0-ONNX`), embeddings (`ollama/nomic-embed-text`), and image generation through the single Bifrost gateway VK.
- Wired MCPProxy as an external tool server via `scripts/open-webui-entrypoint.sh`, which seeds `TOOL_SERVER_CONNECTIONS` before the Python app starts (mirrors the `unsloth-studio-entrypoint.sh` pattern).
- Enabled SearXNG web search, memories, folders, notes, channels, calendar, and automations; forced `ENABLE_PERSISTENT_CONFIG=false` so env vars always win over the WebUI database.
- Documented the full env var surface in `docs/open-webui.md`.

### Open WebUI: remediate chat-hang bug
- Root-caused hanging assistant messages (`chat_message.done=0`, `output=null`) to Open WebUI's `stream_body_handler` not breaking out of its `async for` loop after seeing `data: [DONE]` inside an `except` branch; fix lives in the custom image's `session_pool.py` in a separate repo.
- Added `scripts/fix-hanging-chats.py` to mark stuck `chat_message` rows as done directly in the SQLite DB, unsticking affected chats without a restart.

## 2026-07-08

### Audio: add audiocpp ggml engine and Nemotron ASR to Bifrost
- Added the `audiocpp` Compose service, a native ggml audio engine (TTS + STT) built from `/projects/external-repo/audio.cpp` via its own CUDA Dockerfile, exposing an OpenAI-compatible surface (`/v1/audio/speech`, `/v1/audio/transcriptions`, `/v1/models`, `/v1/audio/voices`).
- Added the `nemotron-asr` Compose service, a thin FastAPI shim around NVIDIA NeMo's `nvidia/nemotron-3.5-asr-streaming-0.6b` model, serving OpenAI-compatible `/v1/audio/transcriptions` (audio.cpp has no Nemotron support).
- Registered both as custom Bifrost providers (`base_provider_type: openai`) with `list_models: true`, alongside the existing `speaches` engine; both share the 2070 SUPER GPU and the host-path HF cache, so expect VRAM pressure under concurrent load.
- Added `config/audiocpp.json.tmpl`, rendered to `secrets/audiocpp.json` by `just rs`.

### Unsloth Studio: add patched web UI service
- Added the `unsloth-studio` Compose service, a UI/proxy instance backed by the patched source checkout at `/projects/code/unsloth`; Bifrost still routes the actual model calls to the Windows Unsloth backend.
- Pointed it at Bifrost (`UNSLOTH_BIFROST_BASE_URL`) and MCPProxy (`UNSLOTH_MCP_PROXY_URL`) via `scripts/unsloth-studio-entrypoint.sh` and `config/unsloth-bifrost.env.tmpl`.

### Bifrost: add opencode-zen provider, widen model wildcards
- Added an `opencode-zen` Bifrost provider keyed by `OPENCODE_ZEN_API_KEY`.
- Simplified the `nvidia-build-key` model list to a `["*"]` wildcard instead of an explicit per-model allowlist.
- Enabled `list_models: true` on the custom `openai`, `speaches`, `audiocpp`, and `nemotron-asr` providers so they all report models via `/v1/models`.

## 2026-07-06

### Bifrost: skip slow providers during model list
- Updated the custom Bifrost image to upstream `transports/v1.6.2`.
- Patched all-provider model listing to return partial results after a 10-second collection window instead of waiting on a down provider.
- Added provider status metadata to `/v1/models` and OpenAI-compatible `/openai/v1/models` without removing the normal model list data contract.
- Rebuilt and restarted `bifrost`; both model-list routes now return HTTP 200 in about 10 seconds with `unsloth` reported as timed out.

## 2026-07-05

### Bifrost: route MCPProxy through retrieval endpoint
- Changed the Bifrost `mcpproxy` MCP client from `http://172.19.0.1:3130/mcp/all` to `http://172.19.0.1:3130/mcp`.
- Disabled `allow_on_all_virtual_keys` and enabled `mcp_disable_auto_tool_inject` so MCP tools are not injected by default.
- Removed the temporary `mcp-inject` virtual key.
- Created `unsloth-win`, `openclaw`, `dev`, and `azimuth` Bifrost virtual keys, stored their values in `op://clankers/bifrost-vks/`, and bound each to the `mcpproxy` MCP client.
- Restarted `mcpproxy` and verified Bifrost now sees 10 retrieval-mode mcpproxy tools instead of the 54 direct tools.
- Documented the client contract: omit MCP include headers to opt out; use `x-bf-mcp-include-clients: mcpproxy` or `x-bf-mcp-include-tools: ...` to opt in per request.

### OpenClaw: use dedicated Bifrost virtual key
- Pointed the OpenClaw Bifrost provider at `op://clankers/bifrost-vks/openclaw` in the startup patch template and rendered secret patch.
- Preserved the existing `openai/gpt-5.4-mini` Bifrost model entry in the startup patch so OpenClaw's non-replacing config patch can apply cleanly.
- Restarted the existing `openclaw` container and verified the gateway is ready with the Bifrost provider key redacted and 10 configured Bifrost models.

### OpenClaw: disable bundled MCP for Emo
- Added `tools.deny: ["bundle-mcp"]` to the `emo` agent in the OpenClaw startup patch and live config.
- Restarted the `openclaw` container so Discord and the gateway loaded the updated Emo tool policy.

### OpenClaw: restrict Emo to messaging tools and selected skills
- Set the `emo` agent to `tools.profile: "messaging"` in the OpenClaw startup patch and live config.
- Replaced the `emo` skill allowlist with `["todoist", "webby"]`, removing `x-research` from Emo's visible skills.
- Restarted the `openclaw` container so Discord and the gateway loaded the new agent config.

### OpenClaw: set Bifrost provider timeout to 10 minutes
- Added `timeoutSeconds: 600` to the OpenClaw `bifrost` provider config in the startup patch template and rendered patch.
- Applied the same validated config patch to the mounted live OpenClaw config without restarting the gateway.

### OpenClaw: add Emo agent route
- Added the `emo` OpenClaw agent to the startup config patch with workspace `/cybernetics/agents/emo`, model `bifrost/unsloth/current`, and `thinkingDefault: "high"`.
- Added `emo` to agent-to-agent visibility, the Bifrost model allowlist, and the Discord binding for channel `1523459169867399248`.
- Scaffolded a blank Emo workspace template in the cybernetics vault.

### MinIO: add local S3-compatible download bucket
- Added a `minio` Compose service backed by `/mnt/store-ext4/minio`, with the S3 API on `127.0.0.1:39000` and console on `127.0.0.1:39001`.
- Added a `minio-init` one-shot service that creates the `files` bucket and grants anonymous download access.
- Added `config/minio.env.tmpl` so MinIO root credentials render from the 1Password `clankers/local-service` username and password fields.
- Configured advertised MinIO URLs for `https://minio.dev.ankitson.com` and `https://minio-console.dev.ankitson.com`.
- Added Just recipes for starting MinIO, printing client env vars, uploading a file, following logs, and smoke-testing public downloads.
- Documented endpoint, console, bucket name, and example AWS-compatible client usage in the README.

### OpenClaw: disable session-store cache for Discord `/new`
- Added `OPENCLAW_SESSION_CACHE_TTL_MS=0` to the OpenClaw Compose service so reply session initialization reads the live session store instead of a potentially divergent in-process cache.
- Cleared the stuck Discord `#general` channel session row after backing up `sessions.json`; preserved the old transcript file.
- Recreated the `openclaw` container and verified Discord is connected, config is valid, and there are no active `main` sessions before a fresh `/new` test.

### MCPProxy: add Todoist upstream
- Added `TODOIST_API_KEY` to the MCPProxy env template, rendered from `op://clankers/todoist-azimuth-agents/api-token`.
- Added a pinned `todoist` stdio upstream in `config/mcpproxy.seed.json` using `npx -y @doist/todoist-mcp@10.4.1`.
- Added and approved the live Todoist upstream in MCPProxy; it exposes 50 Todoist tools and now uses the Azimuth agents Todoist token.
- Verified the Azimuth agents token sees 5 projects and can create tasks in each visible project.
- Documented that native project/workspace scoping is not available through Todoist OAuth, the official Todoist MCP server, or MCPProxy token policy.

### AgentsView: ingest OpenClaw and remote-machine transcripts
- Added the OpenClaw agents session directory to the AgentsView container with `OPENCLAW_DIR=/agents/openclaw`.
- Added a read-only `/agent-sources` mount backed by `volumes/agentsview-sources` for mirrored remote-machine transcripts.
- Added `bin/agentsview-sync-sources.py`, which pulls Codex and Claude transcripts from configured remote machines, runs scoped AgentsView syncs, backs up `sessions.db`, and updates imported rows so the `machine` column reflects the source host.
- Added `just agentsview-sync` as the repeatable local entry point for manual or scheduled remote transcript syncs.

## 2026-07-01

### Bifrost: move Unsloth provider to the Windows Caddy hostname
- Changed the tracked Bifrost `unsloth` provider base URL from `http://desktop-win:8888` to `https://unsloth.win.ankitson.com`.
- Switched the Unsloth provider key from `models: ["default"]` to `models: ["*"]` so Bifrost exposes upstream-discovered Unsloth model IDs instead of a hardcoded alias.
- Re-rendered `secrets/bifrost.config.json`, force-recreated `bifrost`, and verified `unsloth/default` still completes successfully through the gateway.

### Bifrost: set Ollama provider pricing to zero
- Added a `governance.pricing_overrides` entry in the Bifrost config template for `provider_id: "ollama"` with wildcard model matching and zero input/output token cost.
- Covered the Ollama request types currently used in the stack: `chat_completion`, `text_completion`, `responses`, and `embedding`.
- Re-rendered `secrets/bifrost.config.json`, force-recreated the live `bifrost` container, and verified the override through `GET /api/governance/pricing-overrides`.

### OpenClaw: route memory embeddings through Bifrost with passthrough headers
- Added `agents.defaults.memorySearch` config for the `openai-compatible` provider targeting `http://bifrost:8080/openai/v1`.
- Set the embedding model to `ollama/nomic-embed-text:latest`, enabled `x-bf-passthrough-extra-params: true`, and configured `queryInputType` / `documentInputType` so Bifrost can map them into task prefixes.

### Bifrost: add embedding task-prefix shim for Ollama embeddings
- Enabled a second custom Bifrost plugin, `embedding-task-prefix`, from the local `ankit/bifrost-dynamic:local` image.
- Added a rule for `ollama/nomic-embed-text*` that rewrites embedding inputs from `input_type=query|document` into literal `search_query:` / `search_document:` text prefixes and strips `input_type` before forwarding upstream.

### Bifrost: repoint Ollama upstream to the Caddy hostname
- Changed `OLLAMA_URL` from the raw Docker service address to `https://ollama.dev.ankitson.com`.
- Kept the native Bifrost `ollama` provider config intact, so model discovery and embeddings now run through the Caddy/TLS endpoint.

### Bifrost: add native Ollama provider for local models
- Added `OLLAMA_URL=http://ollama:11434` to the Bifrost env template.
- Registered Bifrost's native `ollama` provider with wildcard model discovery and per-key
  `ollama_key_config.url`, so local Ollama models appear under `ollama/<model>` through
  `/openai/v1/models`.
- Added Just recipes to list discovered Ollama models and smoke-test embeddings with
  `ollama/nomic-embed-text:latest`.

### codex-oauth: replaced HTTP layer to fix reasoning-continuity bug
- Root-caused OpenClaw's intermittent `⚠️ Agent couldn't generate a response` failures (gilfoyle's
  cron loops especially) to a reasoning-continuity gap in `codex-oauth`'s upstream `openai-oauth`
  package: it re-encodes full message history from scratch on every Chat Completions call, with
  no way to preserve a reasoning model's state across a tool-call round trip. Confirmed the
  server-managed fix (`store:true`/`previous_response_id`) isn't available on this backend at all.
- Replaced `codex-oauth`'s entrypoint with a new `proxy-server.mjs` (plain Node, no framework)
  implementing stateless client-managed reasoning continuity: caches each tool-calling turn's raw
  reasoning + function_call items (via `include:["reasoning.encrypted_content"]`) keyed by that
  turn's tool_call ids, splices them back into the request on replay. Reuses only
  `openai-oauth`'s exported OAuth client.
- Added `tests/test_reasoning_continuity.py`, a real repro/acceptance suite (fresh calls, tool
  round trips, a gilfoyle-scale fixture built from its actual workspace files). Verified against
  gilfoyle's real cron payload — was 100% failing (76 consecutive errors), now clean through full
  multi-round loops.
- Removed an earlier `patch-responses-state.mjs` Dockerfile patch (enabled `openai-oauth`'s unused
  `CodexResponsesState` cache) after confirming it was inert for our traffic; superseded by the
  proxy rewrite.

## 2026-06-30

### Bifrost Privacy Suffix Aliases
- Extended the custom Bifrost `model-policy-suffix` plugin beyond OpenRouter so privacy directives can
  be parsed for custom providers too.
- Added `[tee]` / `[e2ee]` suffix support plus boolean and query-form variants.
- Mapped OpenRouter `[tee]` to Phala-only ZDR routing with fallbacks disabled.
- Mapped Venice `[e2ee]` friendly names to known `e2ee-*` model IDs while keeping real client-side
  encryption as an explicit caller responsibility.
- Rebuilt and restarted live Bifrost with the updated plugin.

### NanoGPT Bifrost Provider
- Added `NANOGPT_API_KEY` to the Bifrost env template using the 1Password `nanogpt` item.
- Added NanoGPT as a Bifrost custom OpenAI-compatible provider with wildcard model routing, model
  listing, chat completion, text completion, and embedding support.
- Rendered the Bifrost secrets, recreated the live Bifrost service, and verified NanoGPT active with
  603 models exposed through Bifrost, including 31 `nanogpt/TEE/...` models.

## 2026-06-29

### Phoenix OTLP Trace Viewer
- Added a loopback-only Phoenix Compose service for Azimuth workflow and agent traces.
- Persisted Phoenix state under `volumes/phoenix` and exposed the UI/HTTP OTLP endpoint on `127.0.0.1:6006`.
- Added Just recipes to start Phoenix, smoke-test the UI, and post the latest autosweep OTLP batch.
- Started Phoenix and ingested the latest 12 autosweep post-cap batch traces.

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

### OpenClaw default model and image upgrade
- Set the OpenClaw startup config patch to force `openai/gpt-5.4-mini` as the default model with no
  fallback chain.
- Removed the stale OpenCode provider override and OpenCode model entries that exposed
  `opencode/mimo-v2.5-free` in the default-path model set.
- Pinned the OpenClaw Compose build args to `OPENCLAW_VERSION=2026.6.10` and
  `OPENCLAW_CODEX_VERSION=2026.6.10`.
- Rebuilt and restarted the live `ankit/openclaw:local` image; the running container reports
  `openclaw@2026.6.10` and `@openclaw/codex@2026.6.10`.
- Seeded missing isolated Codex auth homes for the `main` and `austin` OpenClaw agents from the host
  Codex auth file.

### Job Search dedicated service
- Added a dedicated `job-search` Compose service built from `/projects/job-search` and tagged `ankit/job-search:local`.
- Mounted the live job-search mutable directories into the container and mounted `~/.codex` for extract/fit/answers.
- Pinned `agent-browser@0.27.1` in the image for hostile-page enrichment fallback support.
- Added a 90-second stop grace period and healthcheck for `/api/stats`.
- Built and started the service; a controlled restart exercised the app's SIGTERM shutdown path.

### Pipeline Dagster NAS degraded-mode mounts
- Temporarily replaced the `pipeline-dagster` `/mnt/synologydrive` bind sources for `/landing_zone` and `/aoe4-replays` with empty local placeholders under `./volumes/offline-synology/`.
- Left the original NAS bind lines commented in `docker-compose.pipelines.yml` so the change can be reverted when the NAS returns.
- Recreated `pipeline-dagster`; it is healthy with degraded empty landing directories.

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

### Unsloth Studio tools
- Updated the Windows `win-models` launcher so Unsloth Studio defaults to `--disable-tools` for the
  Bifrost/OpenCode model-server path.
- Kept explicit opt-in available by forwarding extra Just recipe args, e.g. `--enable-tools` for
  direct Studio UI sessions.
- Restarted the live Windows Studio service with server-side tools disabled.

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

### SillyTavern image generation
- Added an A1111-compatible image adapter that forwards
  SillyTavern image requests to either Bifrost's OpenAI-compatible image-generation endpoint or
  OpenRouter's image chat-completions endpoint.
- Added the `sillytavern-bifrost-image` Compose service and Just recipes for starting/logging it.
- Updated the live SillyTavern image-generation settings to use the adapter as the Stable Diffusion
  WebUI source.
- Switched the adapter backend through ignored runtime env and verified a real image generation.

## 2026-06-22

### SillyTavern ComfyUI image backend
- Added a ComfyUI backend to the image adapter, including checkpoint/sampler/scheduler discovery and
  a default txt2img workflow.
- Reconfigured `sillytavern-bifrost-image` to use an external ComfyUI API configured by ignored env.
- Added an adapter smoke test and the
  `just sillytavern-image-adapter-test` recipe.
- Updated the live SillyTavern image settings to use a ComfyUI model through the existing Stable
  Diffusion WebUI source.
- Disabled SillyTavern OpenAI media inlining in the live user settings so `/imagine me` prompt
  generation does not send prior generated image attachments to a text-only DeepSeek/OpenRouter
  model.
- Added A1111-compatible no-op responses for `sd-vae`, `sd-modules`, and `latent-upscale-modes` in
  the image adapter.
- Increased Bifrost's OpenRouter request timeout from 120 to 600 seconds in the config template,
  rendered config, and live provider sqlite row for long `/imagine scene` prompt-generation tests.
- Fixed the image adapter's A1111 model switching by persisting POSTed `/sdapi/v1/options`
  `sd_model_checkpoint` values and using the active checkpoint for `/txt2img` requests.
- Moved the adapter's concrete runtime model values out of Compose and into ignored env/config, and
  extended the smoke test to assert model switching sticks before generating an image.

### SillyTavern ComfyUI workflows
- Replaced the active SillyTavern ComfyUI workflow with the latest external copy.
- Added a fixed-parameter ComfyUI workflow and set it as the active SillyTavern workflow.
- Backed up the previous workflow and settings files under
  `volumes/sillytavern/data/default-user/backups/`.
- Verified the fixed workflow through the external ComfyUI API and saved a runtime smoke artifact.

### SillyTavern image adapter relocation
- Moved the adapter implementation out of devserver and into `/projects/dockers`.
- Updated the Compose build context and Just smoke-test recipe to use the external adapter path.
- Removed concrete image backend defaults from the adapter source and Dockerfile; runtime values now
  come from ignored env files or live app settings.
- Added a log ignore rule so generated smoke artifacts are not accidentally staged.
