#!/usr/bin/env bash
set -euo pipefail

export HOME="${HOME:-/data/home}"
export UNSLOTH_STUDIO_HOME="${UNSLOTH_STUDIO_HOME:-/data/home}"
export HF_HOME="${HF_HOME:-/data/cache/huggingface}"
export XDG_CACHE_HOME="${XDG_CACHE_HOME:-/data/cache}"
export PYTHONUNBUFFERED=1
export UNSLOTH_STUDIO_ACCESS_LOG_DEDUP_MS=0
export UNSLOTH_STUDIO_ACCESS_LOG_POLL_DEDUP_MS=0

mkdir -p "$UNSLOTH_STUDIO_HOME" "$HF_HOME" /data/logs /data/bin

if ! command -v pgrep >/dev/null 2>&1; then
  cat > /data/bin/pgrep <<'SH'
#!/usr/bin/env bash
exit 1
SH
  chmod +x /data/bin/pgrep
  export PATH="/data/bin:$PATH"
fi

/data/venv/bin/python - <<'PY'
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

studio_home = Path(os.environ["UNSLOTH_STUDIO_HOME"])
db = studio_home / "studio.db"
db.parent.mkdir(parents=True, exist_ok=True)
now = datetime.now(timezone.utc).isoformat()
conn = sqlite3.connect(db)
try:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS llm_providers (
            id TEXT NOT NULL PRIMARY KEY,
            provider_type TEXT NOT NULL,
            display_name TEXT NOT NULL,
            base_url TEXT NOT NULL,
            is_enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        INSERT INTO llm_providers (id, provider_type, display_name, base_url, is_enabled, created_at, updated_at)
        VALUES (?, ?, ?, ?, 1, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            provider_type = excluded.provider_type,
            display_name = excluded.display_name,
            base_url = excluded.base_url,
            is_enabled = 1,
            updated_at = excluded.updated_at
        """,
        (
            "bifrost",
            "custom",
            "Bifrost",
            os.environ.get("UNSLOTH_BIFROST_BASE_URL", "http://bifrost:8080/openai/v1"),
            now,
            now,
        ),
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mcp_servers (
            id TEXT NOT NULL PRIMARY KEY,
            display_name TEXT NOT NULL,
            url TEXT NOT NULL,
            headers_json TEXT,
            is_enabled INTEGER NOT NULL DEFAULT 1,
            use_oauth INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    cols = {row[1] for row in conn.execute("PRAGMA table_info(mcp_servers)").fetchall()}
    if "use_oauth" not in cols:
        conn.execute("ALTER TABLE mcp_servers ADD COLUMN use_oauth INTEGER NOT NULL DEFAULT 0")

    mcp_url = os.environ.get("UNSLOTH_MCP_PROXY_URL", "http://172.19.0.1:3130/mcp/call").strip()
    mcp_token = (
        os.environ.get("UNSLOTH_MCP_PROXY_TOKEN")
        or os.environ.get("MCPPROXY_API_KEY")
        or ""
    ).strip()
    mcp_enabled = os.environ.get("UNSLOTH_MCP_PROXY_ENABLED", "1").lower() not in {"0", "false", "no", "off"}
    headers_json = json.dumps({"Authorization": f"Bearer {mcp_token}"}, separators=(",", ":")) if mcp_token else None
    if mcp_url:
        conn.execute(
            """
            INSERT INTO mcp_servers
                (id, display_name, url, headers_json, is_enabled, use_oauth, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 0, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                display_name = excluded.display_name,
                url = excluded.url,
                headers_json = excluded.headers_json,
                is_enabled = excluded.is_enabled,
                use_oauth = 0,
                updated_at = excluded.updated_at
            """,
            (
                os.environ.get("UNSLOTH_MCP_PROXY_ID", "mcpproxy-search"),
                os.environ.get("UNSLOTH_MCP_PROXY_DISPLAY_NAME", "MCPProxy Search"),
                mcp_url,
                headers_json,
                int(mcp_enabled and bool(headers_json)),
                now,
                now,
            ),
        )
    conn.commit()
finally:
    conn.close()
PY

# Reconcile Studio auth from secrets/unsloth-studio.env.
# The Studio web UI is password-only and authenticates the built-in "unsloth"
# account, so keep exactly that user and set its password from the secret file.
if [[ -n "${UNSLOTH_STUDIO_PASSWORD:-}" ]]; then
  cd /src/studio/backend
  /data/venv/bin/python - <<'PY'
import os
import secrets
import sqlite3
import sys

sys.path.insert(0, "/src/studio/backend")

from auth import hashing, storage

username = "unsloth"
password = os.environ["UNSLOTH_STUDIO_PASSWORD"]

# Ensure tables exist before direct cleanup queries.
conn = storage.get_connection()
conn.close()

record = storage.get_user_and_secret(username)
if record is None:
    try:
        storage.create_initial_user(
            username=username,
            password=password,
            jwt_secret=secrets.token_urlsafe(64),
            must_change_password=False,
        )
    except sqlite3.IntegrityError:
        pass
else:
    salt, pwd_hash, _jwt_secret, must_change_password = record
    if must_change_password or not hashing.verify_password(password, salt, pwd_hash):
        storage.update_password(username, password)

conn = storage.get_connection()
try:
    conn.execute("DELETE FROM refresh_tokens WHERE username != ?", (username,))
    conn.execute("DELETE FROM api_keys WHERE username != ?", (username,))
    conn.execute("DELETE FROM auth_user WHERE username != ?", (username,))
    conn.commit()
finally:
    conn.close()

storage.clear_bootstrap_password()
print("Studio auth reconciled for built-in user: unsloth")
PY
fi


cd /src/studio/backend
exec /data/venv/bin/python run.py \
  --host 0.0.0.0 \
  --port "${UNSLOTH_STUDIO_PORT:-8892}" \
  --frontend /src/studio/frontend/dist \
  --no-cloudflare
