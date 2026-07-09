# Open WebUI Configuration

All settings are managed via env vars in `docker-compose.yml` with `ENABLE_PERSISTENT_CONFIG=false`
so env vars always take precedence over the database.

## Env Vars

| Feature | Env Var | Value |
|---|---|---|
| Persistence | `ENABLE_PERSISTENT_CONFIG` | `false` (env vars always win) |
| LLM API base | `OPENAI_API_BASE_URL` | `http://bifrost:8080/openai/v1` |
| Model access | `BYPASS_MODEL_ACCESS_CONTROL` | `true` (show ALL providers) |
| Model fallback | `ENABLE_CUSTOM_MODEL_FALLBACK` | `true` |
| Default pinned models | `DEFAULT_PINNED_MODELS` | comma-separated list of model IDs |
| Admin email | `WEBUI_ADMIN_EMAIL` | `selfhosted@ankitson.com` (from 1Password) |
| Admin password | `WEBUI_ADMIN_PASSWORD` | from 1Password `op://clankers/local-service` |
| WebUI secret key | `WEBUI_SECRET_KEY` | same as admin password (from 1Password) |
| STT engine | `AUDIO_STT_ENGINE` | `openai` |
| STT base URL | `AUDIO_STT_OPENAI_API_BASE_URL` | `http://bifrost:8080/openai/v1` |
| STT model | `AUDIO_STT_MODEL` | `speaches/deepdml/faster-whisper-large-v3-turbo-ct2` |
| TTS engine | `AUDIO_TTS_ENGINE` | `openai` |
| TTS base URL | `AUDIO_TTS_OPENAI_API_BASE_URL` | `http://bifrost:8080/openai/v1` |
| TTS model | `AUDIO_TTS_MODEL` | `speaches/speaches-ai/Kokoro-82M-v1.0-ONNX` |
| TTS voice | `AUDIO_TTS_VOICE` | `af_heart` |
| Auto-play TTS | `ENABLE_FORCED_TTS_AUTO_PLAY` | `true` |
| Base models cache | `ENABLE_BASE_MODELS_CACHE` | `true` |
| Embeddings engine | `RAG_EMBEDDING_ENGINE` | `openai` (via bifrost) |
| Embeddings base URL | `RAG_EMBEDDING_OPENAI_BASE_URL` | `http://bifrost:8080/openai/v1` |
| Embeddings model | `RAG_EMBEDDING_MODEL` | `ollama/nomic-embed-text` |
| Web fetch for RAG | `ENABLE_RAG_LOCAL_WEB_FETCH` | `true` |
| Web search enabled | `ENABLE_WEB_SEARCH` | `true` |
| Search engine | `WEB_SEARCH_ENGINE` | `searxng` |
| SearXNG query URL | `SEARXNG_QUERY_URL` | `http://searxng:8080/search` |
| Search result count | `WEB_SEARCH_RESULT_COUNT` | `10` |
| Search concurrent requests | `WEB_SEARCH_CONCURRENT_REQUESTS` | `2` |
| Image gen engine | `IMAGE_GENERATION_ENGINE` | `openai` |
| Image gen base URL | `IMAGE_GENERATION_OPENAI_API_BASE_URL` | `http://bifrost:8080/openai/v1` |
| Image gen model | `IMAGE_GENERATION_MODEL` | `openrouter/black-forest-labs/flux.2-klein-4b` |
| Sign-up | `ENABLE_SIGNUP` | `true` |
| Default user role | `DEFAULT_USER_ROLE` | `user` |
| Memories | `ENABLE_MEMORIES` | `true` |
| Memory system context | `ENABLE_MEMORY_SYSTEM_CONTEXT` | `true` |
| Memory background review | `ENABLE_MEMORY_BACKGROUND_REVIEW` | `true` |
| Folders | `ENABLE_FOLDERS` | `true` |
| Notes | `ENABLE_NOTES` | `true` |
| Channels | `ENABLE_CHANNELS` | `true` |
| Calendar | `ENABLE_CALENDAR` | `true` |
| Automations | `ENABLE_AUTOMATIONS` | `true` |
| WebUI URL | `WEBUI_URL` | `https://chat.ankitson.com` |
| Version | (image) | `ghcr.io/open-webui/open-webui:main` (tracks latest) |

## Web / RAG Loader

Playwright (v1.60.0) with Chromium is installed in the Open WebUI container.
Pages fetched for RAG use Playwright for JavaScript rendering.
Chromium binary: `~/.cache/ms-playwright/chromium-1223`.

## ComfyUI Image Generation

ComfyUI runs at `https://comfy.win.ankitson.com` (may be offline).
To use it, uncomment these lines in `docker-compose.yml`:

```
- IMAGE_GENERATION_ENGINE=comfyui
- COMFYUI_BASE_URL=https://comfy.win.ankitson.com
```

Currently, image generation uses `openai` engine via bifrost → openrouter
with model `openrouter/black-forest-labs/flux.2-klein-4b`.

## MCP (Model Context Protocol) Integration

mcpproxy runs on host network and is reachable from mybridge containers at
`http://172.19.0.1:3130/mcp`. The connection is seeded automatically at
container startup by `scripts/open-webui-entrypoint.sh` (mirrors the
unsloth-studio pattern) via the `TOOL_SERVER_CONNECTIONS` env var.

The entrypoint reads `MCPPROXY_URL`, `MCPPROXY_DISPLAY_NAME`,
`MCPPROXY_TOKEN` (falls back to `MCPPROXY_API_KEY` from
`secrets/mcpproxy.env`), and builds the JSON config for Open WebUI's
`tool_server.connections` config key.

All MCP tools registered in mcpproxy (search, fetch, etc.) are available
to the LLM as function-calling tools in any chat.

To add additional MCP servers manually:
1. Go to **Workspace → MCP Servers → Add Server**
2. Set URL and auth as required by the new server

### Overrides

| Env Var | Default | Description |
|---|---|---|
| `MCPPROXY_URL` | `http://172.19.0.1:3130/mcp` | MCP server URL |
| `MCPPROXY_ID` | `mcpproxy` | Internal server ID |
| `MCPPROXY_DISPLAY_NAME` | `MCPProxy` | Display name in UI |
| `MCPPROXY_TOKEN` | `$MCPPROXY_API_KEY` | Bearer token for auth |

## Calendar & Channels

Both enabled via env vars (`ENABLE_CALENDAR=true`, `ENABLE_CHANNELS=true`).
Calendar appears in the sidebar for event management.
Channels appear as a team-communication space.

Open WebUI shows all bifrost providers: anthropic, openai, openrouter, deepseek,
nanogpt, nvidia, ollama, opencode-zen, unsloth, speaches, audiocpp, etc.
`BYPASS_MODEL_ACCESS_CONTROL=true` ensures no filtering.
