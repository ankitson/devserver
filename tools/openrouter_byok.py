#!/usr/bin/env python3
"""Sync declarative OpenRouter BYOK provider credentials
(config/openrouter-byok.json) to the OpenRouter account.

BYOK = "bring your own key": OpenRouter routes a model to a provider's
first-party endpoint using YOUR provider API key + credits (OpenRouter takes a
small fee). Once a credential is registered, OpenRouter auto-prioritizes it for
that provider, so e.g. `openrouter/deepseek/deepseek-chat-v3.1` (or pinned with
provider.only:["deepseek"]) bills your DeepSeek account instead of OpenRouter's.

IMPORTANT: the BYOK API (`POST /api/v1/byok`) needs an OpenRouter MANAGEMENT /
provisioning key — a normal sk-or- key returns 401 "Invalid management key".
Create one in the dashboard (Settings -> Provisioning/Management keys), store it
in 1Password, and point `management_key_op` at it (or pass OPENROUTER_MGMT_KEY).

Provider API keys are read from 1Password (`op read`) at sync time; nothing
secret is written to disk. If you'd rather not manage a provisioning key, add the
credentials by hand in the dashboard: https://openrouter.ai/settings/integrations

Usage:
    ./openrouter_byok.py [--list] [path]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

API = "https://openrouter.ai/api/v1/byok"
HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE.parent / "config" / "openrouter-byok.json"


def op_read(ref: str) -> str:
    out = subprocess.run(["op", "read", ref], capture_output=True, text=True)
    if out.returncode != 0:
        sys.exit(f"op read failed for {ref}: {out.stderr.strip()}")
    return out.stdout.strip()


def api(method: str, key: str, path: str = "", body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method)
    req.add_header("Authorization", f"Bearer {key}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or "{}")
        except Exception:
            return e.code, {"error": "non-json error body"}


def load_mgmt_key(cfg: dict) -> str:
    key = os.environ.get("OPENROUTER_MGMT_KEY")
    if key:
        return key.strip()
    ref = cfg.get("management_key_op")
    if ref:
        return op_read(ref)
    sys.exit("No management key: set OPENROUTER_MGMT_KEY or management_key_op in the config.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("config", nargs="?", default=str(DEFAULT_CONFIG))
    ap.add_argument("--list", action="store_true", help="List existing BYOK credentials and exit.")
    args = ap.parse_args()

    cfg = json.loads(Path(args.config).read_text())
    mgmt = load_mgmt_key(cfg)

    status, existing = api("GET", mgmt)
    if status == 401:
        sys.exit("401 Invalid management key — this needs an OpenRouter provisioning/management "
                 "key, not a normal sk-or- key. See the module docstring.")
    have = {c.get("provider") for c in (existing.get("data") or existing.get("keys") or [])} \
        if isinstance(existing, dict) else set()

    if args.list:
        print(json.dumps(existing, indent=2)[:2000])
        return 0

    rc = 0
    for cred in cfg.get("credentials", []):
        prov, name = cred["provider"], cred.get("name", f"{cred['provider']}-byok")
        if prov in have:
            print(f"[{prov}] already configured — skipping")
            continue
        body = {"provider": prov, "name": name, "key": op_read(cred["key_op"])}
        st, res = api("POST", mgmt, body=body)
        if st in (200, 201):
            print(f"[{prov}] created ({name})")
        else:
            print(f"[{prov}] FAILED {st}: {json.dumps(res)[:300]}")
            rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
