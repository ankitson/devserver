# Devserver Notes

Top-level open threads / gotchas. Sub-projects keep their own detailed notes;
link them here.

## Data pipelines
Production status, Garmin API quirks, auth/rate-limit notes, landing-zone
contract, and pending work. Full detail:
[`pipelines/docs/NOTES.md`](../pipelines/docs/NOTES.md).

## 2026-07-01

### Bifrost Unsloth provider moved behind the Windows Caddy hostname
#### Goal
- Route Bifrost's Unsloth traffic through the stable HTTPS Windows hostname instead of the direct Docker/LAN host address.
#### Discovery
- `https://unsloth.win.ankitson.com/v1/models` is reachable and returns the Unsloth model catalog when called with the existing `UNSLOTH_STUDIO_API_KEY`.
- The tracked Bifrost config still pointed `unsloth` at `http://desktop-win:8888`.
#### Decision
- Changed the Bifrost `unsloth` provider `network_config.base_url` to `https://unsloth.win.ankitson.com`.
- Changed the Unsloth provider key from `models: ["default"]` to `models: ["*"]` so Bifrost can publish the upstream model catalog instead of a single hardcoded alias.
- Re-rendered `secrets/bifrost.config.json` from the template and force-recreated the live `bifrost` container so the mounted config and provider cache were refreshed together.
#### Verification
- `GET /api/providers` now reports `base_url: https://unsloth.win.ankitson.com` for provider `unsloth`.
- A direct authenticated call to `https://unsloth.win.ankitson.com/v1/chat/completions` returned a valid completion.
- A Bifrost-routed call to `http://127.0.0.1:8090/openai/v1/chat/completions` with model `unsloth/default` returned `UNSLOTH_CADDY_OK`.
- `GET /openai/v1/models` now exposes the discovered Unsloth model as `unsloth/unsloth/gemma-4-26B-A4B-it-qat-GGUF`.
- A Bifrost-routed completion to that discovered ID returned `UNSLOTH_DISCOVERY_OK`; the provider routing metadata shows the upstream model passed through as `unsloth/gemma-4-26B-A4B-it-qat-GGUF`.

### Bifrost Ollama provider zero-cost pricing override
#### Goal
- Make locally hosted Ollama usage show up as free inside Bifrost so spend tracking does not assign synthetic token costs to self-hosted models.
#### Discovery
- Bifrost's pricing override system is configured under `governance.pricing_overrides`, not inside the provider block.
- The live Bifrost model catalog exposes local Ollama models as `ollama/<model>`, and provider-scoped wildcard overrides accept `pattern: "*"` as a catch-all.
- `docker compose up -d bifrost` did not reload the mounted config in-place; the service needed a forced recreate before the override appeared in the governance API.
#### Decision
- Added a provider-scoped pricing override for `provider_id: "ollama"` with `pattern: "*"` and zero `input_cost_per_token` / `output_cost_per_token`.
- Scoped the override to `chat_completion`, `text_completion`, `responses`, and `embedding` so the common Ollama-backed request paths all resolve to zero cost.
#### Verification
- Re-rendered `secrets/bifrost.config.json`, force-recreated `bifrost`, and confirmed `GET /api/governance/pricing-overrides` returns the `ollama-provider-zero-cost` entry.

### OpenClaw memory embeddings routed through Bifrost with passthrough extra_params
#### Goal
- Make OpenClaw memory embeddings use `ollama/nomic-embed-text:latest` through Bifrost while preserving the query-vs-document role signal that the Bifrost prefix shim needs.
#### Discovery
- OpenClaw already lets memory-search remote configs set custom headers, but the OpenAI-compatible embedding adapter sends `input_type` as a top-level JSON field.
- Bifrost's OpenAI `/v1/embeddings` route only preserves provider-specific fields for plugins when callers use `extra_params` plus `x-bf-passthrough-extra-params: true`.
#### Decision
- Configured `agents.defaults.memorySearch` to use the `openai-compatible` provider against `http://bifrost:8080/openai/v1`, targeting `ollama/nomic-embed-text:latest`.
- Enabled `x-bf-passthrough-extra-params: true` and set `queryInputType: "query"` / `documentInputType: "document"` so the Bifrost embedding shim can rewrite text prefixes before forwarding to Ollama.
#### Verification
- Verification is via the live OpenClaw `/v1/embeddings` route: identical text should now diverge between query and document requests when routed through Bifrost.

### Bifrost embedding shim for prefix-sensitive embeddings models
#### Goal
- Make Bifrost compensate for embedding models that require literal task prefixes, so OpenClaw and other clients can keep using the OpenAI-compatible embeddings API.
#### Discovery
- `nomic-embed-text` behind Ollama does not appear to change embeddings when only `input_type` changes, but does change embeddings when the text is prefixed with `search_query:` or `search_document:`.
- OpenClaw already sends `input_type` on embedding requests, which gives Bifrost a stable signal for query-vs-document rewriting.
#### Decision
- Enabled a new `embedding-task-prefix` Bifrost plugin with a rule for `ollama/nomic-embed-text*` that maps `query -> search_query: ` and `document -> search_document: `.
- Configured the plugin to strip `input_type` after rewriting so Ollama only sees the normalized text payload.
#### Verification
- Verified through Bifrost that identical raw text now yields different embeddings for `input_type=query` versus `input_type=document`, which is the expected behavior for Nomic's task-prefixed format.

### Bifrost Ollama provider moved behind the Caddy hostname
#### Goal
- Route Bifrost's Ollama traffic through the same stable HTTPS dev hostname used by the rest of the local stack.
#### Discovery
- The homeserver Caddy instance already fronts `*.dev.ankitson.com`, and the Bifrost container can reach
  that hostname successfully over HTTPS from inside Docker.
#### Decision
- Changed `OLLAMA_URL` from `http://ollama:11434` to `https://ollama.dev.ankitson.com`.
- Left the native Bifrost `ollama` provider in place; only the upstream URL changed.
#### Verification
- After re-rendering `secrets/bifrost.env` and restarting Bifrost, Ollama model discovery still returns
  the `ollama/*` catalog and `ollama/nomic-embed-text:latest` still serves embeddings through Bifrost.

### Bifrost Ollama provider for local model discovery and embeddings
#### Goal
- Expose the host's locally running Ollama models through the existing Bifrost gateway so clients can
  discover them from `/openai/v1/models` and call local models such as `nomic-embed-text`.
#### Discovery
- The live `ollama` container is already running on the same Docker `mybridge` network as `bifrost`,
  with hostname `ollama` and port `11434`.
- Upstream Bifrost has a native `ollama` provider. Its config supports wildcard model discovery with a
  per-key `ollama_key_config.url`, which is a better fit than treating Ollama as a generic custom
  OpenAI-compatible provider.
#### Decision
- Added `OLLAMA_URL=http://ollama:11434` to `config/bifrost.env.tmpl`.
- Registered a native `ollama` provider in `config/bifrost.config.json.tmpl` with `models: ["*"]`
  and `ollama_key_config.url: env.OLLAMA_URL` so Bifrost can discover whatever models Ollama has
  pulled locally.
- Added `just bifrost-ollama-models` and `just bifrost-ollama-embed` to verify catalog discovery and
  embeddings through Bifrost.
#### Verification
- `curl http://127.0.0.1:11434/api/tags` showed three local Ollama models before wiring:
  `gemma4:e4b`, `qwen3.5:9b`, and `nomic-embed-text:latest`.
- After rendering secrets and restarting Bifrost, `/openai/v1/models` exposes the `ollama/*` models
  and `ollama/nomic-embed-text:latest` returns an embedding successfully through Bifrost.

### codex-oauth reasoning-continuity bug — replaced its HTTP layer with our own proxy
#### Symptom
- OpenClaw agents (gilfoyle's cron loops especially) intermittently got
  `⚠️ Agent couldn't generate a response` — the model returned neither text nor a tool call on
  the turn immediately after a tool result, mid multi-step agentic loop. Near-100% reproducible on
  gilfoyle's real health-watch loop (large system prompt + ~50 real tools); rare (~1-2%) on tiny
  prompts, which is why it wasn't obvious sooner.
#### Root cause
- `openai-oauth` (the `codex-oauth` service's upstream npm package) translates every
  `/v1/chat/completions` call into a fresh Vercel AI SDK `generateText()` call, fully
  re-encoding the whole message history with zero reasoning-continuity metadata each time.
  Reasoning models need their reasoning state threaded across a tool-call round trip or they can
  lose track and stop dead — a real gap, not paranoia (Chat Completions has no field for it at
  all; that's Responses-API-only).
- The obvious server-managed fix (`store:true` + `previous_response_id`) is **not available on
  this backend** — confirmed by direct testing, it hard-rejects `store:true` with
  `"Store must be set to false"`. That's also why `openai-oauth`'s own `/v1/responses` handler
  explicitly blocks `previous_response_id`/`item_reference` ("Stateless Codex responses endpoint
  does not support..."). There is no server-side state here at all.
- The only viable mechanism is stateless client-managed continuity: request
  `include:["reasoning.encrypted_content"]`, capture the raw reasoning + function_call items from
  each tool-calling turn, and splice them back into `input` verbatim on the next call instead of
  reconstructing lossily from the Chat Completions `tool_calls` shape.
- Extra surprise along the way: with `store:false`, the final `response.completed` SSE event's own
  `.output` field comes back **empty** — the incremental `response.output_item.done` events are
  the actual source of truth for output content. Cost an extra debugging pass; documented in
  `proxy-server.mjs`'s `collectCompletedResponseFromSse`.
#### Fix
- Replaced `codex-oauth`'s entrypoint: it now runs
  `projects/dockers/codex-oauth-proxy/proxy-server.mjs` (plain Node, no framework) instead of the
  `openai-oauth` CLI's built-in HTTP server. Only reused piece from `openai-oauth`: its exported
  `createCodexOAuthClient`/`resolveOpenAIOAuthModels` for OAuth + model discovery (deep-imported
  from its bundled dist chunk since they aren't part of its public package exports).
- The proxy caches each tool-calling turn's raw Responses API output items keyed by that turn's
  `tool_call` ids (stable regardless of how a client reformats surrounding JSON), and on replay
  splices the cached reasoning+function_call items back in for any assistant tool-call message it
  recognizes. Cache miss (restart/TTL/unknown turn) falls back to lossy reconstruction — same
  exposure as the original bug for that one leg, not a hard failure.
- An earlier attempt patched `openai-oauth`'s bundled dist to flip on its unused
  `CodexResponsesState` cache — confirmed **inert** for our traffic (it only activates on
  Responses-API-native fields a Chat Completions client never sends) and removed; superseded by
  the proxy rewrite above.
#### Verification
- `tests/test_reasoning_continuity.py` (PEP 723, `uv run`): fresh single-turn, fresh+tools,
  small-scale 2-step tool round trip, and a gilfoyle-scale (~45KB prompt, ~50 real-sized tool
  schemas) 2-step round trip fixture built from gilfoyle's actual workspace files. All green
  repeatedly against the rebuilt production container.
- Real acceptance test: `openclaw agent --agent gilfoyle --message "Run the health-watch
  loop..."` (gilfoyle's actual cron payload) — was 100% failing (76 consecutive cron errors)
  before the fix; now runs cleanly end-to-end through full multi-round loops (10+ tool-call
  rounds, 100K+ token context) repeatedly, ending in the loop's correct `NO_REPLY`.
#### Caveats
- The Chat Completions `stream:true` case is served as a single-chunk fake SSE stream (we collect
  the full upstream response before replying) — correct shape for callers expecting a stream, not
  real token-level streaming.
- Cache-miss fallback (restart, 6h TTL, or a turn from a different process) silently reverts to
  the original lossy behavior for that one leg rather than failing hard — matches the original
  bug's exposure, doesn't add a new failure mode.

## 2026-06-30

### Bifrost Privacy Suffix Aliases
#### Goal
- Let clients ask for privacy routing with small model suffixes instead of provider-specific JSON.
#### Decision
- Extended the custom `model-policy-suffix` Bifrost plugin to parse suffixes for all providers, not
  just OpenRouter.
- Added privacy directives: `[tee]`, `[e2ee]`, `tee=true/false`, `e2ee=true/false`, `privacy=...`,
  and query-form `privacy.tee=true` / `privacy.e2ee=true`.
- For OpenRouter, `[tee]` pins `provider.only=["phala"]`, disables fallbacks, and requests ZDR
  (`zdr=true`, `data_collection=deny`). Example: `openrouter/openai/gpt-oss-20b[tee]`.
- For Venice, `[e2ee]` maps friendly model names to known `e2ee-*` model IDs where possible. Example:
  `venice/gpt-oss-20b[e2ee]` becomes `venice/e2ee-gpt-oss-20b-p`.
- The plugin strips internal privacy markers before sending upstream, so `_privacy`, `tee`, and `e2ee`
  do not leak as random provider params.
#### Caveat
- Bifrost can route to TEE/E2EE model IDs, but it cannot create client-side encryption. Real Venice
  E2EE still needs a client that encrypts payloads and sends Venice's E2EE headers.
#### Verification
- `go test ./...` passes in `/projects/dockers/bifrost-dynamic/policy-model-suffix`.
- Rebuilt and recreated live `bifrost` with `docker compose up -d --build bifrost`.
- Bifrost startup logs show `model-policy-suffix - active`.
- `just bifrost-test` returns `BIFROST_OK`.

### NanoGPT Bifrost Provider
#### Goal
- Add NanoGPT as a Bifrost provider and make its TEE models available to clients while keeping the full
  NanoGPT text catalog routable.
#### Discovery
- NanoGPT's OpenAI-compatible base is `https://nano-gpt.com/api/v1`.
- `GET /models?detailed=true` currently returns 603 models. TEE model IDs include
  `TEE/gpt-oss-20b`, `TEE/deepseek-v4-flash`, `TEE/minimax-m2.5`, and `TEE/kimi-k2.5`.
- The `op://clankers/nanogpt/password` API key is readable and authenticates, but the account currently
  has zero USD balance; a direct tiny TEE completion returned NanoGPT `402 insufficient_balance`.
#### Decision
- Added `NANOGPT_API_KEY` to `config/bifrost.env.tmpl`.
- Added a `nanogpt` Bifrost custom provider with `base_provider_type: openai`, base URL
  `https://nano-gpt.com/api`, `models: ["*"]`, model listing enabled, and chat/text/embedding request
  support.
#### Verification
- Rendered `secrets/bifrost.env` and `secrets/bifrost.config.json` directly with `op inject -f`
  because full `just rs` is currently blocked by an unrelated `config/agent-devbox.env.tmpl`
  reference to a missing `Private` vault.
- Recreated Bifrost with `docker compose up -d bifrost`; `just bifrost-providers` shows `nanogpt`
  active.
- Bifrost model discovery shows 603 NanoGPT models through the gateway.
- Bifrost `/openai/v1/models` includes 31 `nanogpt/TEE/...` models, including
  `nanogpt/TEE/gpt-oss-20b`, `nanogpt/TEE/deepseek-v4-flash`, `nanogpt/TEE/minimax-m2.5`, and
  `nanogpt/TEE/kimi-k2.5`.
- Completion smoke tests reach NanoGPT through Bifrost but currently return upstream `402` because the
  NanoGPT account has zero USD balance.

## 2026-06-29

### Phoenix OTLP Trace Viewer
#### Goal
- Host a standard trace viewer for Azimuth workflow and agent runs, using OTLP as the interchange format.
#### Decision
- Added Phoenix as `phoenix` in `docker-compose.yml`.
- Bound the UI and HTTP OTLP collector to `127.0.0.1:${PHOENIX_PORT:-6006}` and gRPC OTLP to
  `127.0.0.1:${PHOENIX_GRPC_PORT:-4317}`.
- Mounted `./volumes/phoenix:/mnt/data` and set `PHOENIX_WORKING_DIR=/mnt/data`, so Phoenix persists
  its SQLite DB in the standard ignored `volumes/` tree.
- Added `just phoenix-up`, `just phoenix-smoke`, `just phoenix-logs`, and
  `just phoenix-post-autosweep-latest`.
#### Verification
- `docker compose config phoenix` validates.
- `just phoenix-up` pulled and started `arizephoenix/phoenix:latest`.
- `just phoenix-smoke` reaches the UI at `http://127.0.0.1:6006`.
- Posted the 12-run autosweep post-cap batch through `http://127.0.0.1:6006/v1/traces`.
- Phoenix persisted 12 traces and 2502 spans, including workflow events, LLM request/response spans,
  and tool spans.

## 2026-06-27

### Codex/ChatGPT Subscription Through Bifrost (`codex` provider)
#### Goal
- Let Bifrost serve a ChatGPT Plus/Pro (Codex) subscription so any gateway client (SillyTavern,
  opencode, etc.) can use `gpt-5.x` without burning paid API credits. Updates the old "Codex subs
  DON'T work via Bifrost" note below — still true that **Bifrost has no built-in subscription auth**.
#### Discovery
- Bifrost upstream is **not** there yet: feature issues
  [#2316](https://github.com/maximhq/bifrost/issues/2316) (device-code) and
  [#4459](https://github.com/maximhq/bifrost/issues/4459) (auto-detect passthrough) are both OPEN;
  PRs #2715/#2775 were closed. PR #4377 ("adds chatgpt passthrough") merged a static
  `/chatgpt_passthrough` → `chatgpt.com/backend-api/codex` route, but the maintainer says it's "a bit
  different" — it only helps the official Codex CLI (which still holds its own OAuth token), not
  arbitrary OpenAI-compatible clients.
- Proven community path (from #2316 comments): run a small OAuth shim that turns the subscription into
  an OpenAI-compatible `/v1` endpoint, then register it as a custom Bifrost provider.
#### Decision
- Added a `codex-oauth` Compose service wrapping
  [EvanZhouDev/openai-oauth](https://github.com/EvanZhouDev/openai-oauth) (chosen over
  icebear0828/codex-proxy: single-purpose, auditable, leans on OpenAI's official `codex login` rather
  than reimplementing OAuth). It reads a Codex `auth.json`, auto-refreshes, and proxies to
  `chatgpt.com/backend-api/codex`, exposing `/v1/chat/completions` + `/v1/responses` on `:10531`.
- Registered it in Bifrost as the custom provider `codex` (`base_url http://codex-oauth:10531`,
  `base_provider_type: openai`, `models: ["*"]`), same shape as `unsloth`/`speaches`/`deepseek`.
- **Wildcard works for custom providers now** (verified on a throwaway Bifrost of the same image):
  `["*"]` resolves and routes any `codex/<id>` to the shim — `codex/gpt-5.3-codex-spark` returned a
  real completion. This overturns the older NVIDIA-era gotcha ("custom providers can't wildcard");
  no need to hand-maintain a model list. Use `just codex-oauth-models` to see what the account exposes
  (currently `gpt-5.5`, `gpt-5.4`, `gpt-5.4-mini`, `gpt-5.3-codex-spark`, `codex-auto-review`).
- **Dedicated auth**, not the host `~/.codex`: Codex refresh tokens rotate, so a shared `auth.json`
  would have the host CLI and the proxy invalidate each other. The proxy gets its own
  `secrets/codex-oauth/auth.json` via a separate `codex login` (`just codex-oauth-login`).
- Internal only (mybridge; loopback debug port `10531`), single seat, no token pooling — staying
  inside the personal-use lane of OpenAI's terms.
#### Verification
- `docker compose build codex-oauth` builds `ankit/codex-oauth-proxy:local`; `openai-oauth` v1.0.2 and
  `codex` bins present, `--help` OK, busybox `nc` available for the healthcheck.
- Template renders to valid JSON with `codex` among providers; `docker compose config codex-oauth`
  parses; running Bifrost config left untouched (still 7 providers, no `codex` yet).
#### Next steps (manual — need `op` + browser)
1. `just rs` (1Password unlocked) to render `secrets/bifrost.{env,config.json}` with the `codex` provider.
2. `just codex-oauth-login` — dedicated `codex login --device-auth` (prints a code + URL; works
   headless, no localhost:1455 port-forward needed).
3. `just up --build codex-oauth` then `just up bifrost` (env changed → recreate, not restart).
4. Verify: `just codex-oauth-models`, `just bifrost-providers` (codex active), `just codex-oauth-test`.

### OpenClaw Default Model And 2026.6.10 Upgrade
#### Discovery
- The tracked startup patch did not set `agents.defaults.model`, so persisted OpenClaw state controlled
  the default model across restarts.
- The Mimo entry came from the custom `models.providers.opencode` catalog override in
  `volumes/openclaw/openclaw.json`; it was an OpenCode Zen model entry, not the OpenAI/Codex runtime.
- The OpenClaw service image was still running `openclaw@2026.6.1`; Compose did not pin
  `OPENCLAW_VERSION`, so plain rebuilds depended on the Dockerfile default or explicit Just args.
#### Decision
- Declare `openai/gpt-5.4-mini` as the OpenClaw default model in the rendered startup patch.
- Remove the stale OpenCode provider override and OpenCode fallback entries from active config so Mimo
  is not offered as an allowed default-path model.
- Pin the OpenClaw Compose build args to `OPENCLAW_VERSION=2026.6.10` and
  `OPENCLAW_CODEX_VERSION=2026.6.10`.
- Seed missing isolated per-agent Codex homes from the host Codex auth file for `main` and `austin`;
  `gilfoyle` already had one.
#### Verification
- `just upgrade-openclaw 2026.6.10` rebuilt `ankit/openclaw:local` and recreated the running service.
- `openclaw --version` and global npm packages report `openclaw@2026.6.10` and
  `@openclaw/codex@2026.6.10`.
- `openclaw models status --json` reports default/resolved default `openai/gpt-5.4-mini`,
  no fallbacks, and no `opencode/mimo-v2.5-free` allowed model.
- Gateway logs show `agent model: openai/gpt-5.4-mini` and `gateway ready`.
#### Follow-ups
- OpenClaw doctor still reports stale `exa` and `deepseek` plugin config references.
- A post-restart cron run hit the Codex subscription usage limit for `openai/gpt-5.4-mini`; the default
  is correct, but the subscription is currently rate-limited.

### Job Search Dedicated Docker Service
#### Discovery
- The job-search app was previously a child process of homeserver `app-runner`, so Docker stop/restart signals reached the runner rather than the Bun server directly.
- That shape was a poor fit for SQLite WAL durability during host upgrades/reboots because the child process did not own a clear Docker lifecycle.
#### Decision
- Add a dedicated devserver Compose service `job-search` built from `/projects/job-search/Dockerfile` as image `ankit/job-search:local`.
- Persist only the mutable repo directories as bind mounts: `state`, `profile`, `profiles`, `applications`, and `runtime`.
- Pin `agent-browser@0.27.1` in the image so the hostile-page enrichment fallback still works.
- Give the service `init: true`, `restart: unless-stopped`, and `stop_grace_period: 90s` so the app can checkpoint SQLite on stop.
#### Verification
- `docker compose -f docker-compose.yml build job-search` built the image.
- `docker compose -f docker-compose.yml up -d --no-deps job-search` started the container and healthcheck.
- A controlled `docker compose -f docker-compose.yml restart job-search` logged the app SIGTERM shutdown path before restart.
- `docker exec job-search agent-browser --version` reports `agent-browser 0.27.1`.

### Pipeline Dagster NAS degraded-mode mounts
#### Discovery
- After the Pop!_OS 24.04 upgrade, `pipeline-dagster` could not recreate while `/mnt/synologydrive` was offline.
- Docker failed before process start while creating bind source paths for `/landing_zone` and `/aoe4-replays`.
#### Decision
- Temporarily replace the two NAS bind sources in `docker-compose.pipelines.yml` with empty local placeholders under `./volumes/offline-synology/`.
- Leave the original NAS bind lines commented in the compose file for rollback.
#### Verification
- `docker compose -f docker-compose.pipelines.yml up -d pipeline-dagster` recreated the service.
- `pipeline-dagster` reached healthy state with the empty placeholder landing directories.
#### Rollback
- When the NAS is back, restore the commented `/mnt/synologydrive/...` binds and remove the `./volumes/offline-synology/...` binds.

### Bifrost Unsloth Stream Timeout
#### Discovery
- Unsloth's Bifrost `default_request_timeout_in_seconds` was already 600 seconds in both the rendered
  config and live `/api/providers` output.
- The 120-second cap came from Bifrost's streaming idle fallback:
  `DefaultStreamIdleTimeout = 120 * time.Second`. Streaming opencode requests can hit that when the
  local model takes longer than two minutes before sending the next SSE chunk.
#### Decision
- Keep Unsloth's total request timeout at 600 seconds and set only its provider-level stream idle
  timeout to 300 seconds.
- Set opencode's Bifrost provider `timeout` and `chunkTimeout` to 300000 ms as an explicit client-side
  match, even though opencode's schema documents 300000 ms as the request-timeout default.
#### Verification
- Updated `volumes/bifrost/config.db` for provider `unsloth` and restarted the `bifrost` container.
- `/api/providers` now reports `stream_idle_timeout_in_seconds: 300` for `unsloth`, and the container
  is healthy.

### Bifrost 1.6.0 Dynamic Image
#### Discovery
- `maximhq/bifrost:1.6.0` is not published, but `maximhq/bifrost:v1.6.0` is available.
- `maximhq/bifrost:latest` currently resolves to the same amd64/arm64 manifest as
  `maximhq/bifrost:v1.6.0`.
- Upstream source tags are module-scoped; there is no root `v1.6.0` Git tag. The local HTTP transport
  build checks out `transports/v1.6.0`.
#### Decision
- Keep Compose on the local dynamic image so the Go plugin remains available, and update that build
  to check out upstream tag `transports/v1.6.0`.
#### Verification
- `go test ./...` passes for `/projects/dockers/bifrost-dynamic/policy-model-suffix`.
- `docker compose build bifrost` rebuilt `ankit/bifrost-dynamic:local` successfully.
- `docker compose up -d bifrost` recreated the running service; `/api/version` returns `v1.6.0`,
  `/health` returns OK, and `/api/plugins` reports `model-policy-suffix` active.

### Unsloth Studio Tools
#### Decision
- Treat Unsloth Studio as a model server behind Bifrost/OpenCode, not as a nested agent runtime.
- Updated the Windows `win-models` launcher so `win-models unsloth serve` defaults to
  `--disable-tools`. The existing Just recipes pass through extra args, so direct Studio UI sessions
  can still opt in explicitly with `--enable-tools`.
#### Reason
- OpenCode is already the visible tool harness. Leaving Studio server-side tools enabled created a
  hidden second loop (`OpenCode -> Bifrost -> Studio -> terminal`) where Studio executed terminal
  calls with `session_id=None`; OpenCode could not display or audit those calls and Bifrost timed out
  while waiting for stream data.
#### Verification
- Restarted the Windows Studio service; the active process command line includes `--disable-tools`.
- The new Studio log reports server-side tools disabled, and no `execute_tool` entries appeared after
  restart.

## 2026-06-26

### Unsloth Studio Provider
#### Decision
- Added `unsloth` as a Bifrost custom OpenAI-compatible provider for the local Studio service. The
  tracked template uses the short `desktop-win` host; private host suffixes belong in ignored runtime
  config, not committed docs.
- Exposed one model id, `unsloth/default`. Unsloth Studio's OpenAI-compatible chat schema documents
  `model` as informational and uses the active loaded model, so `default` is the stable Bifrost handle.
- Bifrost uses `UNSLOTH_STUDIO_API_KEY` from `config/bifrost.env.tmpl`, sourced from
  `op://clankers/llm-windows/password`; OpenCode exposes it as `bifrost/unsloth/default`.
#### Gotchas
- The Studio API requires `Authorization: Bearer ...`; unauthenticated `GET /v1/models` returns 401.
- If the 1Password value is the Studio login password rather than a long-lived `sk-unsloth-...` API
  key, create an API key via Studio's `/api/auth/api-keys` flow and store that key in the same
  1Password field or a dedicated field before rendering Bifrost secrets.
- Studio 2026.6.9 honors `-c 131072`; earlier 2026.6.7 startup accepted the wrapper flags but still
  launched `llama-server` with `-c 4096`.
- Studio's `/v1/status` can still report `max_context_length:4096` even when `context_length` is
  `131072` and `llama-server` is running with `-c 131072`; use the process command line or a
  long-prompt smoke test as the source of truth.
- Studio currently returns SSE for `/v1/chat/completions` even with `stream:false`; Bifrost streaming
  works, but non-stream Bifrost calls can fail while parsing the SSE body as JSON.
- Gemma 4 thinking: llama.cpp can separate thoughts with `--reasoning-format deepseek`, but Studio
  wraps those deltas as `<think>...</think>` content for its own UI. The Windows `win-models`
  launcher now applies an OpenAI route shim that splits Studio's wrapped stream back into
  `delta.reasoning_content` before it leaves `/v1/chat/completions`, while keeping ordinary
  `delta.content` free of `<think>` tags.
- Bifrost normalizes Unsloth's `delta.reasoning_content` into `delta.reasoning` plus
  `delta.reasoning_details`; clients should display those fields if they want visible thinking.
- `opencode run` currently prints only final content in the terminal smoke test. OpenCode's TUI config
  has a `display_thinking` keybind, so use the TUI surface when you want to inspect reasoning; the
  provider/proxy stream is carrying the data either way.
- Current `just unsloth serve-lan` launch keeps Studio server-side tools disabled by default on the
  LAN-reachable port. Only pass `--enable-tools` for direct Studio UI sessions where Studio should be
  the agent runtime.
#### Verification
- `http://desktop-win:8888/` responds with the Unsloth Studio UI from the host.
- The local Studio OpenAPI document is reachable from inside the Bifrost container and shows
  OpenAI-compatible `/v1/chat/completions`, `/v1/models`, `/v1/completions`,
  `/v1/embeddings`, and `/v1/responses` endpoints behind HTTP Bearer auth.
- Bifrost was recreated with `UNSLOTH_STUDIO_API_KEY` present in the container environment, and
  `/api/providers` reports custom provider `unsloth` active.
- Updated Windows Unsloth Studio to `unsloth==2026.6.9` and `unsloth_zoo==2026.6.7`.
- Updated the Windows llama.cpp runtime to GitHub release `b9821`; Studio status reports
  `llama_cpp_installed_tag:"b9821"` and `llama_cpp_latest_tag:"b9821"`.
- `desktop-win` is running the active model
  `unsloth/gemma-4-26B-A4B-it-qat-GGUF:UD-Q4_K_XL` with `context_length:131072` and `cache_type_kv:q8_0`.
- A Bifrost streaming request to `unsloth/default` with a prompt larger than the old 4096-token
  limit returned HTTP 200 and streamed output.
- Direct Studio streaming test for `unsloth/default` returned separate `reasoning_content` and
  answer content without `<think>` tags. The same request through Bifrost returned a first chunk with
  `delta.reasoning`/`reasoning_details`, then normal `delta.content` chunks.
- The repo-pinned Gemma 4 chat template was compared against the latest public
  `google/gemma-4-26B-A4B-it` template; the custom template is still needed because it carries local
  tool-call hardening and multimodal aliases not present in the upstream template.

### Bifrost Model-Policy Suffix Plugin
#### Decision
- Replaced the "one OpenRouter preset per model/provider" approach with a custom Bifrost image from
  `/projects/dockers/bifrost-dynamic`.
- Bifrost now loads `/app/plugins/model-policy-suffix.so`, which parses a trailing OpenRouter model
  suffix such as `deepseek/deepseek-v4-flash[zdr,provider=digitalocean]`.
- The plugin strips the suffix before provider routing and injects OpenRouter's upstream
  `provider` request-body object via Bifrost `ExtraParams`.
- Active OpenCode default:
  `bifrost/openrouter/deepseek/deepseek-v4-pro[zdr,provider=digitalocean]`.
- DS4 Flash DigitalOcean/ZDR example:
  `bifrost/openrouter/deepseek/deepseek-v4-flash[zdr,provider=digitalocean]`.
#### Suffix syntax
- `zdr` expands to `provider.zdr:true` and `provider.data_collection:"deny"`.
- `provider=digitalocean` expands to `provider.only:["digitalocean"]` and, by default,
  `provider.allow_fallbacks:false`.
- `allow_fallbacks=true|false` overrides that default.
- `order=a|b`, `only=a|b`, and `ignore=a|b` are passed into OpenRouter's `provider` object.
- Dotted directives set arbitrary OpenRouter request fields, e.g.
  `deepseek/deepseek-v4-flash[provider.only=digitalocean,reasoning.effort=high]`.
- Query-style suffixes work for shell-friendly params, e.g.
  `deepseek/deepseek-v4-flash[?provider.only=digitalocean&reasoning.effort=high]`.
- Raw JSON object suffixes are exact arbitrary request-body passthrough, e.g.
  `deepseek/deepseek-v4-flash[{"provider":{"only":["digitalocean"],"allow_fallbacks":false},"reasoning":{"effort":"high"}}]`.
  JSON is not mutated by the shorthand defaults; include `"allow_fallbacks":false` when the intent is
  a hard provider pin.
- Quoted JSON and `json64:...` payloads are accepted for clients or shells that make raw JSON awkward.
- OpenCode requires model ids to be present in its configured model list; arbitrary unlisted suffixes fail
  with `ProviderModelNotFoundError`. A configured JSON64 example is available as
  `bifrost/openrouter/deepseek/deepseek-v4-flash[json64:eyJwcm92aWRlciI6eyJvbmx5IjpbImRpZ2l0YWxvY2VhbiJdLCJhbGxvd19mYWxsYmFja3MiOmZhbHNlfX0]`.
  Clients that call Bifrost directly can send arbitrary suffix strings without OpenCode registration.
#### Deferred syntax decision
- Consider making URI query syntax the primary advanced format because it is familiar and
  percent-encoding gives a standard escape path, e.g.
  `deepseek/deepseek-v4-flash[?provider.only=digitalocean&provider.allow_fallbacks=false]`.
- Keep the query inside the bracket suffix rather than turning the whole model id into
  `model?key=value`; the bracket keeps the policy layer explicit and avoids surprising clients,
  model registries, and logs that treat model ids as opaque strings.
- Keep raw JSON/`json64:` as escape hatches for nested objects and arrays that are awkward in query
  strings. Revisit this before promoting the suffix format beyond local Bifrost/OpenCode usage.
#### Verification
- `/api/plugins` reports `model-policy-suffix` active.
- Negative route test
  `openrouter/deepseek/deepseek-v4-flash[provider=definitely-not-a-provider]` returned OpenRouter
  404 `No allowed providers are available`, proving the suffix reached OpenRouter as provider
  routing policy.
- Direct Bifrost call
  `openrouter/deepseek/deepseek-v4-flash[zdr,provider=digitalocean]` returned
  OpenRouter generation `gen-1782518325-zVFyzTnLvtvl58676pmr`; metadata reported
  `provider_name: DigitalOcean`, model `deepseek/deepseek-v4-flash-20260423`, and `preset_id:null`.
- OpenCode call
  `bifrost/openrouter/deepseek/deepseek-v4-flash[zdr,provider=digitalocean]` returned
  `OPENCODE_SUFFIX_DO_OK`; Bifrost logs show the plugin applied the policy and stripped the suffix.
- JSON suffix negative route test
  `openrouter/deepseek/deepseek-v4-flash[{"provider":{"only":["definitely-not-a-provider"],"allow_fallbacks":false}}]`
  returned OpenRouter 404 `No allowed providers are available`.
- JSON suffix DigitalOcean route
  `openrouter/deepseek/deepseek-v4-flash[{"provider":{"zdr":true,"data_collection":"deny","only":["digitalocean"],"allow_fallbacks":false}}]`
  returned `JSON_SUFFIX_DO_OK`; OpenRouter generation `gen-1782519262-Agsv4Zb3PRmoKoh0DiHy`
  reported `provider_name: DigitalOcean`, model `deepseek/deepseek-v4-flash-20260423`, and
  `preset_id:null`.
- OpenCode run with the configured JSON64 model returned `OPENCODE_JSON64_SUFFIX_OK`; Bifrost logs
  show the plugin applied the suffix and stripped the upstream model to `deepseek/deepseek-v4-flash`.

### OpenCode DeepSeek v4 Pro ZDR Opt-In
#### Status
- Legacy/superseded: OpenRouter presets still exist, but OpenCode now uses Bifrost model-policy
  suffixes so new model/provider combinations do not require new OpenRouter presets.
#### Decision
- Added OpenRouter preset `zdr-deepseek-v4-pro` with `model: deepseek/deepseek-v4-pro` and
  `provider: {zdr:true, data_collection:"deny", only:["digitalocean"], allow_fallbacks:false}`.
  This keeps ZDR selection opt-in by model string instead of turning on OpenRouter account-wide
  privacy enforcement, and pins DS4 to DigitalOcean instead of any ZDR provider.
- Previously, OpenCode defaulted to `bifrost/openrouter/@preset/zdr-deepseek-v4-pro`. That has been
  replaced by the suffix model above. The regular `bifrost/openrouter/deepseek/deepseek-v4-pro`
  model remains listed for explicit opt-out.
- OpenCode is configured with `enabled_providers:["bifrost"]`, so authenticated non-Bifrost
  providers remain on disk but are hidden from OpenCode model/provider selection.
- Previously added `zdr-deepseek-v4-flash-digitalocean` for DS4 Flash. Prefer the suffix model
  `bifrost/openrouter/deepseek/deepseek-v4-flash[zdr,provider=digitalocean]`; keep
  `bifrost/openrouter/deepseek/deepseek-v4-flash` only for default OpenRouter routing.
#### Verification
- `just openrouter-presets-sync` stored the preset live on OpenRouter.
- `just bifrost-test openrouter/@preset/zdr-deepseek-v4-pro` returned `BIFROST_OK`.
- `opencode models` lists only `bifrost/...` models after `enabled_providers:["bifrost"]`.
- OpenRouter generation `gen-1782513184-lflS3t3ad9HW32o4sFYz` reported
  `provider_name: DigitalOcean` and the same preset id after a Bifrost call.
- OpenCode call `bifrost/openrouter/@preset/zdr-deepseek-v4-flash-digitalocean` returned
  `DS4_FLASH_DO_OK` and Bifrost logged model `@preset/zdr-deepseek-v4-flash-digitalocean`.
- Direct Bifrost call with the same preset returned OpenRouter generation
  `gen-1782517414-ACqNHQ3wIkSwQQh0C1YB`; OpenRouter metadata reported
  `provider_name: DigitalOcean` and model `deepseek/deepseek-v4-flash-20260423`.
#### Bifrost-native ZDR management
- Source/discussion check: Bifrost aliases and routing rules centralize model/provider resolution,
  but the stock config schema does not let an alias inject arbitrary OpenRouter request-body fields
  such as `provider.only`, `provider.zdr`, or `provider.data_collection`.
- Bifrost can pass OpenRouter-specific fields with `extra_params`, gated by
  `x-bf-passthrough-extra-params: true`; that is per request and requires client support.
- OpenCode's current AI SDK runtime does not pass `model.options.provider` as raw OpenRouter
  request-body JSON through a custom OpenAI-compatible provider. A DS4 Flash test configured this way
  routed to GMICloud in OpenRouter logs, so the config now treats non-preset DS4 Flash as an explicit
  opt-out/default-route model.
- Added OpenCode plugin `~/.config/opencode/plugins/bifrost-passthrough-headers.js` to attach
  `x-bf-passthrough-extra-params:true` to Bifrost chat requests. The header alone does not enforce
  ZDR; it only permits Bifrost to preserve `extra_params` when a client can send them.
- A true Bifrost-owned policy is possible via a custom plugin (`HTTPTransportPreHook` or
  `PreLLMHook`) that injects the OpenRouter `provider` object before the upstream call. Operational
  catch: the current `maximhq/bifrost:latest` container is statically linked, and Bifrost's Go plugin
  docs require a dynamically linked binary to load `.so` plugins.
- Superseded: OpenCode no longer needs OpenRouter presets for this use case because the dynamic
  Bifrost image now owns suffix-based OpenRouter provider policy.

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
- **Claude/Codex subs DON'T work via Bifrost** (natively): it proxies **API keys only**. Claude Code
  and Codex CLI both prefer their own subscription OAuth and have to be logged out to point at a
  gateway — Bifrost has no built-in subscription pass-through. The `anthropic`/`openai`
  providers here are API-key billed (and the `anthropic-key1` key currently has $0 balance, so
  Claude-via-Bifrost 400s with "credit balance too low" until funded).
  **UPDATE 2026-06-27:** worked around for ChatGPT/Codex by adding the `codex-oauth` shim service +
  Bifrost custom provider `codex` (see the 2026-06-27 "Codex/ChatGPT Subscription Through Bifrost"
  entry above). The same pattern (an OAuth→OpenAI shim as a custom provider) could ride a Claude Max
  subscription if wanted.
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
- **Pointed at Bifrost**: pre-seeded `data/default-user/settings.json` → `main_api=openai` (stock
  default is `koboldhorde`/AI Horde — this is what makes Bifrost the *default* backend),
  `oai_settings.chat_completion_source=custom`, `custom_url=http://bifrost:8080/openai/v1`,
  `custom_model=nvidia/meta/llama-3.1-8b-instruct` (+ placeholder `api_key_custom` in secrets.json;
  Bifrost is keyless). ST proxies the API call server-side, so the internal `bifrost:8080` hostname
  resolves. **Verified end-to-end**: ST → Bifrost `/openai/v1/chat/completions` returns completions.
- To change models in the UI: API Connections → Chat Completion → Custom (OpenAI-compatible); the model
  dropdown is populated live from Bifrost's `/openai/v1/models` (any provider/model id works, e.g.
  `openrouter/...`, `nvidia/...`, `deepseek/deepseek-chat`).

### Follow-ups (2026-06-20): Caddy, DeepSeek-direct, fastmail
- **Caddy**: Web UI + API were added behind the private reverse-proxy path. The concrete route lives
  in the homeserver repo and should stay out of devserver notes.
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

## 2026-06-19 - MCPProxy v0.43.0 search-filter check
- **Goal**: update the shared MCPProxy gateway from v0.35.0 to v0.43.0 and retest whether
  `retrieve_tools` returns `fastmail:update_event` for a write-intent calendar query.
- **Image update**: rebuilt `ankit/mcpproxy:0.43.0` from the upstream v0.43.0 linux-amd64 release
  tarball using the published checksum, then recreated only the `mcpproxy` service.
- **Result**: the admin search endpoint ranks `fastmail:update_event` second for the query, but
  MCP `retrieve_tools` still omits it when `exclude_destructive=true` because the tool lacks an
  explicit `destructiveHint:false` annotation.

## 2026-06-19 - OpenClaw high thinking defaults
- **Goal**: make high thinking the default for newly created OpenClaw agents and for the configured
  `main`, `gilfoyle`, and `austin` agents.
- **Config shape**: OpenClaw uses `agents.defaults.thinkingDefault` for the inherited default and
  `agents.list[].thinkingDefault` for per-agent overrides.
- **Applied files**: updated the rendered startup patch, its template, and the live
  `volumes/openclaw/openclaw.json` state so the setting is effective now and survives future
  `just rs` renders.

## 2026-06-20 - SillyTavern Chat Completion preset copy
- **Finding**: Chat Completion preset JSON files can be installed directly by copying them into
  `volumes/sillytavern/data/default-user/OpenAI Settings/`.
- **Helper**: added `just sillytavern-preset-copy path/to/preset.json` as a thin wrapper around that
  direct copy. Actual preset exports are user data and should not be tracked in this repo.

## 2026-06-20 - SillyTavern image generation through Bifrost
- **Finding**: SillyTavern's built-in OpenAI and OpenRouter image sources are hardcoded to the public
  provider APIs, so they cannot be pointed at Bifrost with settings alone. Its Stable Diffusion WebUI
  source is URL-configurable.
- **Adapter**: added `sillytavern-bifrost-image`, a small A1111-compatible service that exposes
  `/sdapi/v1/*` to SillyTavern. It can forward to Bifrost's OpenAI-compatible image endpoint or to
  OpenRouter's image chat-completions endpoint.
- **SillyTavern settings**: the adapter is selected through SillyTavern's configurable Stable
  Diffusion WebUI source. Concrete URLs, model choices, and model lists belong in ignored secrets or
  live SillyTavern data.
- **2026-06-22 update**: switched the adapter backend for testing with provider credentials loaded
  from ignored secret files. Direct provider routing avoided incompatible chat-tool injection.
- **2026-06-22 model switch**: model choices were moved into ignored runtime env/config rather than
  tracked repo files.
- **Verification**: SillyTavern can reach the adapter health/options/model endpoints. A real `txt2img`
  request returned HTTP 200 and wrote a valid smoke image under runtime output storage.

## 2026-06-22 - SillyTavern image generation through external ComfyUI
- **Finding**: the external ComfyUI endpoint is reachable from devserver. The concrete endpoint and
  model inventory belong in ignored env/config, not tracked notes.
- **Adapter update**: extended `sillytavern-bifrost-image` with `IMAGE_BACKEND=comfyui`. It exposes
  the same A1111-shaped `/sdapi/v1/txt2img` interface and translates requests into a simple ComfyUI
  checkpoint → CLIP encode → KSampler → VAE decode → SaveImage workflow.
- **Runtime config**: concrete endpoint, model, sampler, and scheduler values are loaded from
  ignored env files or SillyTavern's live user settings.
- **Verification**: direct ComfyUI API generation and adapter generation both succeeded during setup.
- **2026-06-22 `/imagine me` fix**: SillyTavern's prompt-generation step for `/imagine me` failed
  when the current chat contained a generated image attachment because `oai_settings.media_inlining`
  was true and the active DeepSeek/OpenRouter chat model does not support image input. Set
  `media_inlining=false`, restarted SillyTavern, and kept ComfyUI image generation unchanged.
  Also added no-op A1111 metadata endpoints for `sd-vae`, `sd-modules`, and `latent-upscale-modes`
  to avoid harmless SillyTavern probe 404s.
- **2026-06-22 timeout test**: `/imagine scene` can stall before ComfyUI because SillyTavern first
  asks the main chat model to convert the scene into image tags. Increased Bifrost's provider
  request timeout in rendered config and live state, then restarted Bifrost.
- **2026-06-22 model switch fix**: SillyTavern changes Stable Diffusion WebUI checkpoints by POSTing
  `sd_model_checkpoint` to `/sdapi/v1/options`; the adapter previously returned success but ignored
  that state, so `/txt2img` fell back to its default model. The adapter now stores the current
  checkpoint, reports it from GET `/options`, and uses it for generation when no per-request model
  override is present. Compose no longer stores concrete model defaults.

## 2026-06-22 - SillyTavern ComfyUI workflows
- **Goal**: copy external ComfyUI API workflows into SillyTavern's live workflow directory.
- **Installed files**: workflow JSON files were copied into live SillyTavern user data. Concrete
  workflow names and source endpoints should stay in ignored runtime data, not tracked notes.
- **Active workflow**: SillyTavern image generation was pointed at the copied ComfyUI workflow with
  prompt expansion disabled.
- **Verification**: the active workflow was submitted directly to ComfyUI and returned a valid image.

## 2026-06-23 - SillyTavern image adapter privacy cleanup
- **Goal**: keep the adapter implementation in the shared Docker image project and keep concrete
  image backend settings out of tracked devserver files.
- **Move**: the adapter build context now points at `/projects/dockers`; devserver only keeps the
  Compose service wiring and ignored env-file reference.
- **Privacy rule**: concrete image model IDs, ComfyUI workflow filenames, private endpoint names, and
  private network addresses belong in ignored secrets or live app data, not tracked repo files.
