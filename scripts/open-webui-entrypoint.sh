#!/usr/bin/env bash
set -euo pipefail

# ── Seed mcpproxy as an external MCP tool server via TOOL_SERVER_CONNECTIONS ──
# This runs before the Python app starts so the env var is available at import
# time in open_webui/config.py::TOOL_SERVER_CONNECTIONS.
# Mirrors the unsloth-studio-entrypoint.sh pattern.

MCPPROXY_URL="${MCPPROXY_URL:-http://172.19.0.1:3130/mcp}"
MCPPROXY_ID="${MCPPROXY_ID:-mcpproxy}"
MCPPROXY_DISPLAY_NAME="${MCPPROXY_DISPLAY_NAME:-MCPProxy}"
MCPPROXY_ENABLED="${MCPPROXY_ENABLED:-true}"

TOOL_SERVER_CONNECTIONS_JSON=$(python3 <<PYEOF
import json, os

api_key = os.environ.get('MCPPROXY_API_KEY', '')
url = os.environ.get('MCPPROXY_URL', 'http://172.19.0.1:3130/mcp')
server_id = os.environ.get('MCPPROXY_ID', 'mcpproxy')
name = os.environ.get('MCPPROXY_DISPLAY_NAME', 'MCPProxy')
enabled = os.environ.get('MCPPROXY_ENABLED', 'true').lower() in ('1', 'true', 'yes', 'on')

connections = [{
    'url': url,
    'path': '',
    'type': 'mcp',
    'auth_type': 'bearer',
    'key': api_key,
    'headers': None,
    'config': {'enable': enabled},
    'info': {'id': server_id, 'name': name},
}]
print(json.dumps(connections))
PYEOF
)

export TOOL_SERVER_CONNECTIONS="$TOOL_SERVER_CONNECTIONS_JSON"

# ── Patch stream_wrapper to close on [DONE] (SSE stream-end sentinel) ─────────
# The aiohttp stream reader hangs when the upstream finishes but keeps the TCP
# connection alive (HTTP/1.1 keep-alive).  Detect data: [DONE] and break so the
# finally block calls cleanup_response(), which closes the aiohttp response.
patch_file=/app/backend/open_webui/utils/session_pool.py
if grep -q 'FIX-DONE-SENTINEL' "$patch_file" 2>/dev/null; then
  :
else
  python3 -c "
import re
with open('$patch_file') as f:
  src = f.read()

old = '''    try:
        stream = content_handler(response.content) if content_handler else response.content
        async for chunk in stream:
            yield chunk
    finally:'''

new = '''    try:
        stream = content_handler(response.content) if content_handler else response.content
        async for chunk in stream:
            if b'data: [DONE]' in chunk:
                yield chunk
                break  # FIX-DONE-SENTINEL
            yield chunk
    finally:'''

assert old in src, 'Could not find stream_wrapper body to patch'
src = src.replace(old, new, 1)
with open('$patch_file', 'w') as f:
  f.write(src)
print('Patched stream_wrapper to break on [DONE]')
"
fi

exec bash start.sh
