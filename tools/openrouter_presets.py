#!/usr/bin/env python3
"""Sync declarative OpenRouter presets (config/openrouter-presets.json) to the
OpenRouter account.

Presets store inference config (model + provider routing) server-side on
OpenRouter and are referenced as `@preset/<slug>`. Because the routing lives on
OpenRouter's side, presets survive gateways like Bifrost that strip the
request-body `provider` object — which is the whole reason we use them here to
pin `deepseek/deepseek-v4-flash` onto the ZDR `wafer` (BYOK) endpoint.

Creating/updating a preset does NOT run inference (the `messages` array is
ignored), so a sync costs nothing.

Usage:
    OPENROUTER_API_KEY=sk-or-... ./openrouter_presets.py [--check] [path]

If OPENROUTER_API_KEY is unset, falls back to reading it from
../secrets/bifrost.env relative to this file.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

API = "https://openrouter.ai/api/v1/presets"
HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE.parent / "config" / "openrouter-presets.json"
SECRETS_ENV = HERE.parent / "secrets" / "bifrost.env"


def load_key() -> str:
    key = os.environ.get("OPENROUTER_API_KEY")
    if key:
        return key.strip()
    if SECRETS_ENV.exists():
        for line in SECRETS_ENV.read_text().splitlines():
            if line.startswith("OPENROUTER_API_KEY="):
                return line.split("=", 1)[1].strip()
    sys.exit("OPENROUTER_API_KEY not set and not found in secrets/bifrost.env")


def request(method: str, url: str, key: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {key}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {"_http_error": e.code, "_body": e.read().decode(errors="replace")}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("config", nargs="?", default=str(DEFAULT_CONFIG))
    ap.add_argument("--check", action="store_true",
                    help="Only print the live preset config; do not write.")
    args = ap.parse_args()

    key = load_key()
    presets = json.loads(Path(args.config).read_text()).get("presets", [])
    if not presets:
        print("No presets defined.")
        return 0

    rc = 0
    for p in presets:
        slug = p["slug"]
        cfg = p["config"]
        if args.check:
            live = request("GET", f"{API}/{slug}", key)
            ver = live.get("data", {}).get("designated_version", {})
            print(f"[{slug}] live config: {json.dumps(ver.get('config', live))}")
            continue
        # The create/update endpoint captures config from a chat-completions-shaped
        # body and ignores `messages`. POSTing is idempotent per slug.
        body = {**cfg, "messages": [{"role": "user", "content": "preset-sync"}]}
        res = request("POST", f"{API}/{slug}/chat/completions", key, body)
        if "_http_error" in res:
            print(f"[{slug}] FAILED {res['_http_error']}: {res['_body'][:300]}")
            rc = 1
            continue
        d = res.get("data", {})
        stored = d.get("designated_version", {}).get("config", {})
        print(f"[{slug}] synced (v{d.get('designated_version', {}).get('version')}): "
              f"{json.dumps(stored)}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
