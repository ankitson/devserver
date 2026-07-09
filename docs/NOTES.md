# Audio Stack Integration Notes

## Overview

The devserver stack provides a full local voice/AI pipeline integrating:

- **bifrost** — OpenAI/Anthropic-compatible LLM gateway
- **speaches** — Whisper STT + Kokoro TTS (CUDA)
- **audiocpp** — ggml-based audio engine (Qwen3 ASR/TTS, Chatterbox, Silero VAD)
- **Open WebUI** — voice-chat frontend chaining STT → LLM → TTS
- **nemotron-asr** — NVIDIA Nemotron 3.5 ASR shim (built, not running)

## VRAM Budget (RTX 2070 SUPER, 8 GB)

| Service | VRAM | Notes |
|---|---|---|
| Desktop/host processes | ~2.1 GB | cosmic, sunshine, ollama |
| **speaches** (Whisper Turbo) | ~1.9 GB | float16, unloads after 300s idle |
| **speaches** (Kokoro ONNX) | ~0.7 GB | TTS model, loaded on demand |
| **audiocpp** (ggml CUDA scratch) | ~5.1 GB | Allocated at process start |
| audiocpp (Qwen3-ASR 0.6B) | ~2.8 GB | Weights + inference |
| audiocpp (Qwen3-TTS 0.6B) | ~4.3 GB | Weights + attention scratch |
| audiocpp (PocketTTS 100M) | ~1.3 GB | Very lightweight TTS |
| audiocpp (PocketTTS + Qwen3-ASR) | ~4.1 GB | Both fit: PocketTTS 0.3GB + ASR ~2.8GB + scratch |

**Key constraint:** audiocpp's ggml CUDA backend pre-allocates ~5 GB scratch buffer.
For larger models (Qwen3-TTS 0.6B), total exceeds 8 GB.
PocketTTS (100M params) is lightweight enough to coexist with Qwen3-ASR.

**Workaround:** Run either speaches OR audiocpp at a time, not both.
Within audiocpp, PocketTTS + Qwen3-ASR can coexist; Qwen3-TTS must run solo.

## Test Results (2026-07-08)

### TTS Latency (Kokoro-82M via speaches)

| Path | Round 1 | Round 2 | Round 3 | Avg |
|---|---|---|---|---|
| Direct (speaches) | 0.19s | 0.15s | 0.14s | **0.16s** |
| Via bifrost | 0.14s | 0.14s | 0.14s | **0.14s** |

Bifrost adds ~0–50ms overhead (within noise). Both produce valid MP3 output
(MPEG ADTS layer III, 64 kbps, 24 kHz, mono).

### STT Latency (Whisper Turbo via speaches)

| Path | Round 1 | Round 2 | Round 3 | Avg |
|---|---|---|---|---|
| Direct (speaches) | 0.62s | 0.60s | 0.59s | **0.60s** |
| Via bifrost | 0.57s | 0.56s | 0.57s | **0.57s** |

Transcription accuracy on LibriSpeech test-clean: "Concord returned to its
place amidst the tents." (exact match, all models).

### VRAM Utilization

speaches starts at ~2.1 GB baseline, loads Whisper → ~3.8 GB, loads Kokoro → ~4.5 GB.
Both unload sequentially (stt_model_ttl=300s, tts_model_ttl=300s).

### Bifrost Overhead

- TTS: < 0.05s added (noise level)
- STT: < 0.05s added (noise level)

Bifrost adds measurable but negligible latency. The main benefit (centralized
auth, model routing, logging) is free.

### Conversation Simulation (TTS→STT→TTS→STT, 4 rounds)

| Round | TTS Latency | STT Latency | Transcript |
|---|---|---|---|
| 1 | 0.41s | 0.60s | "Testing the full audio round trip through Bifrost and speech" |
| 2 | 0.37s | 0.59s | "Testing the full audio round trip through Bifrost and speech" |
| 3 | 0.29s | 0.64s | "Testing the Full Audio Round Trip Through Bifrost and Speech" |
| 4 | 0.24s | 0.59s | "Testing the full audio round trip through Bifrost and speech" |

All rounds passed with high accuracy (minor capitalization differences).
TTS speeds up as model stays warm in memory.

### Full Latency Matrix (3-run average, warmed)

| Provider | STT (cold) | STT (warm) | TTS (cold) | TTS (warm) | VRAM |
|---|---|---|---|---|---|
| speaches (Whisper Turbo + Kokoro 82M) | 4.28s | 0.57s | 2.11s | 0.78s | 4427 MiB |
| audiocpp (Qwen3-ASR 0.6B) | 3.57s | 0.25s | N/A | N/A | 4962 MiB |
| audiocpp (Qwen3-TTS 0.6B) | N/A | N/A | 11.99s | 0.60s | 6368 MiB |
| audiocpp (PocketTTS 100M) | N/A | N/A | ~2s | ~0.5s | 3374 MiB |

### Indian English ASR Accuracy (Nexdata dataset, 5 samples)

| File | Ground Truth | Whisper Turbo | Qwen3-ASR |
|---|---|---|---|
| G0009S1001 | After I finish up here... | ✓ exact | ✓ exact |
| G0009S5398 | zero five five one... | ✗ → "055 1782 501" | ✓ exact |
| G00849S4374 | Repeat The Song Please | ✓ (case diff) | ✓ (case diff) |
| G0407S2309 | Replay the song Still Ain't Bound | ✗ → "Still and Bound" | ✗ → "Still and Bound" |
| G0650S3361 | Play that song again and again | ✓ (period added) | ✓ (period added) |

**Key findings:**
- Qwen3-ASR significantly better at isolated digit sequences (✓ exact vs ✗ collapsed to phone number)
- Both models comparable on natural sentences
- Both struggle with song/album title boundaries vs punctuation
- Neither model gets capitalization perfectly (minor, expected)

---

# Latest Status (2026-07-08)

## Provider Matrix

| Provider | STT | TTS | VAD | VRAM (loaded) | Fits alongside |
|---|---|---|---|---|---|
| **speaches** (Whisper Turbo + Kokoro 82M) | ✅ ~0.6s | ✅ ~0.8s | — | 4.4 GB | Runs solo |
| **audiocpp** (Qwen3-ASR 0.6B) | ✅ ~0.25s | — | — | 5.0 GB | PocketTTS |
| **audiocpp** (Qwen3-TTS 0.6B) | — | ✅ ~0.6s (needs voice_ref+ref_text) | — | 6.4 GB | Must run solo |
| **audiocpp** (PocketTTS 100M) | — | ✅ ~0.5s (voice_id=alba) | — | 3.4 GB | Qwen3-ASR |
| **audiocpp** (Silero VAD) | — | — | ✅ | negligible | Any |
| **speaches** (STT + TTS simultaneous) | ✅ ~0.6s | ✅ ~0.8s | — | 4.4 GB | Both models coexist |

## VRAM Reality (8 GB total, ~2.1 GB baseline)

| Configuration | VRAM Use | Works? | Notes |
|---|---|---|---|---|
| speaches STT + TTS | ~4.4 GB | ✅ Both fit | Simultaneous, tested |
| audiocpp Qwen3-ASR + PocketTTS | **~5.6 GB** | **✅ Both fit** | **Verified: TTS→STT roundtrip exact match** |
| audiocpp Qwen3-ASR only | ~5.0 GB | ✅ | |
| audiocpp PocketTTS only | ~3.4 GB | ✅ | |
| audiocpp Qwen3-TTS only | ~6.4 GB | ✅ Solo | |
| audiocpp Qwen3-ASR + Qwen3-TTS | ~9.3 GB | ❌ OOM | Exceeds 8 GB |
| speaches + audiocpp (any) | >8 GB | ❌ OOM | Shared GPU contention |

## Key Findings

1. **PocketTTS is the VRAM king** — 100M params, ~1.3 GB loaded, fits alongside Qwen3-ASR
2. **Qwen3-ASR beats Whisper on digits** — Indian English test: got exact "zero five five one seven eight two five zero one" vs Whisper's "055 1782 501"
3. **Bifrost adds < 50ms overhead** — essentially free; TTS both ~0.4s, STT both ~0.6s
4. **VRAM rules everything** — on 8 GB you run EITHER speaches OR audiocpp, not both
5. **`voice: "alloy"` preset** — critical fix for bifrost to work with audiocpp TTS
6. **PocketTTS needs `voice_id`** (built-in "alba") while Qwen3-TTS needs `voice_ref`+`reference_text`
7. **Cold start matters** — first request 3-12s (model load), subsequent requests 0.25-0.8s

## Recommended Default Configuration

For voice chat via Open WebUI with speaches running:
- STT: `speaches/deepdml/faster-whisper-large-v3-turbo-ct2`
- TTS: `speaches/speaches-ai/Kokoro-82M-v1.0-ONNX` (voice: af_heart)

For batch ASR with audiocpp (when speaches is stopped):
- STT: `audiocpp/qwen3-asr` (better digit accuracy)
- TTS: `audiocpp/pocket-tts` (lightweight, or qwen3-tts for quality)

## Provider Configuration

### bifrost → speaches (STT + TTS)

- STT model: `speaches/deepdml/faster-whisper-large-v3-turbo-ct2`
- TTS model: `speaches/speaches-ai/Kokoro-82M-v1.0-ONNX`
- TTS voice: `af_heart` (female, American English)

The model alias `tts-1 → speaches-ai/Kokoro-82M-v1.0-ONNX` exists in speaches
but NOT in bifrost. Always use the full model ID with provider prefix.

### bifrost → audiocpp (STT only on 8 GB)

- STT: `audiocpp/qwen3-asr` (Qwen3-ASR-0.6B, multilingual)
- TTS: not viable on RTX 2070 SUPER (VRAM OOM)

Compiled model loaders in this checkout:
- `qwen3_asr` ✅
- `qwen3_tts` (needs voice ref, two ~VoiceCloning~ not zero-shot)
- `chatterbox` (also needs voice ref)
- `pocket_tts` (gated HF repo, needs approval)
- `silero_vad` ✅ (bundled asset)
- `citrinet_asr` (English only, no model downloaded)

Models NOT compiled (commented out in registry):
- `parakeet_tdt`
- `kokoro_tts`
- `moss_tts`
- `higgs_tts`

### Virtual Keys (VKs) in bifrost

The VK system controls which models each API key can access.
`.secret.env` contains `VK=sk-bf-...` (the `dev` VK).

To add providers/models to a VK, update the `governance_virtual_key_provider_configs`
table in `volumes/bifrost/config.db`, or use the bifrost Web UI at
http://127.0.0.1:8090 (admin interface) → Virtual Keys.

The `dev` VK was updated to allow `["*"]` for audiocpp and speaches providers.
Models with provider prefixes (e.g., `speaches/...`) must be explicitly named
or covered by `["*"]`.

## Open WebUI Configuration

All settings are managed via env vars in `docker-compose.yml` with `ENABLE_PERSISTENT_CONFIG=false`
so env vars always take precedence over the database.

### Env Vars

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

### Web / RAG Loader

Playwright (v1.60.0) with Chromium is installed in the Open WebUI container.
Pages fetched for RAG use Playwright for JavaScript rendering.
Chromium binary: `~/.cache/ms-playwright/chromium-1223`.

### ComfyUI Image Generation

ComfyUI runs at `https://comfy.win.ankitson.com` (may be offline).
To use it, uncomment these lines in `docker-compose.yml`:

```
- IMAGE_GENERATION_ENGINE=comfyui
- COMFYUI_BASE_URL=https://comfy.win.ankitson.com
```

Currently, image generation uses `openai` engine via bifrost → openrouter
with model `openrouter/black-forest-labs/flux.2-klein-4b`.

### MCP (Model Context Protocol) Integration

mcpproxy runs on host network and is reachable from mybridge containers at:
`http://172.19.0.1:3130/mcp`

To use MCP tools in Open WebUI:
1. Go to **Workspace → MCP Servers → Add Server**
2. Set URL to `http://172.19.0.1:3130/mcp`
3. Leave auth empty (mcpproxy handles auth via its own API key if needed)

All MCP tools registered in mcpproxy (search, fetch, etc.) will then be
available to the LLM as function-calling tools in any chat.

### Calendar & Channels

Both enabled via env vars (`ENABLE_CALENDAR=true`, `ENABLE_CHANNELS=true`).
Calendar appears in the sidebar for event management.
Channels appear as a team-communication space.

Open WebUI shows all bifrost providers: anthropic, openai, openrouter, deepseek,
nanogpt, nvidia, ollama, opencode-zen, unsloth, speaches, audiocpp, etc.
`BYPASS_MODEL_ACCESS_CONTROL=true` ensures no filtering.

## Nemotron 3.5 ASR

Built at `ankit/nemotron-asr:latest` but not running due to VRAM constraints.
The shim wraps `nvidia/nemotron-3.5-asr-streaming-0.6b` via NeMo and exposes
`POST /v1/audio/transcriptions`.

To start:
```bash
docker compose up -d nemotron-asr
```

Then use model `nemotron-asr/nvidia/nemotron-3.5-asr-streaming-0.6b` via bifrost.

## Recommended Daily Usage

For the best experience on 8 GB VRAM:

1. **STT + TTS**: Run speaches (covers both)
2. **Chat**: Any openrouter/anthropic model via bifrost
3. **Voice chat**: Open WebUI with built-in mic/speaker buttons

For GPU-free operation:
- speaches can run on CPU (set `WHISPER__INFERENCE_DEVICE=cpu`)
- Kokoro TTS also runs on CPU (it uses ONNX, not CUDA)

## Provider Errors

### OpenRouter Free Model Rate Limits

Chat `10af2a9f-8c92-4e90-b287-ba8c01364e3d` returned `"Provider returned error"`
for model `openrouter/google/gemma-4-26b-a4b-it:free`.

**Cause:** OpenRouter free-tier models have rate limits and may fail under
load. The error is stored per-message in the chat history:
```json
{"error": {"content": "Provider returned error"}}
```

**Fix:** Switch to a non-free model (e.g. `openrouter/google/gemma-4-26b-a4b-it`)
or retry later when rate limits reset.
