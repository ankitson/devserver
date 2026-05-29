# justfile — devserver-wide commands only.
# Pipeline-specific recipes live in pipelines/Justfile (run them as
# `just pipelines <recipe>`, e.g. `just pipelines up-dagster`).

set shell := ["bash", "-euo", "pipefail", "-c"]

COMPOSE := "docker compose"
TEMPLATE_DIR := "./config"
SECRETS_DIR := "./secrets"

# Pipeline-specific recipes (see pipelines/Justfile).
mod pipelines

# Render config/*.tmpl -> secrets/* using `op inject`
# Supports both *.env.tmpl and *.json.tmpl files. Renders ALL templates
# (pipeline secrets included), so it lives here at the devserver level.
rs: render_secrets
render_secrets strict="false":
  #!/usr/bin/env -S uv run --quiet python3
  import sys, subprocess
  from pathlib import Path

  template_dir = Path("{{TEMPLATE_DIR}}")
  secrets_dir  = Path("{{SECRETS_DIR}}")
  patterns     = ["*.env.tmpl", "*.json.tmpl"]
  strict       = "{{strict}}".lower() == "true"

  secrets_dir.mkdir(parents=True, exist_ok=True)

  templates = []
  for pattern in patterns:
    templates.extend(template_dir.glob(pattern))
  templates = sorted(templates)

  if not templates:
    msg = f"No templates found in {template_dir} for patterns {patterns}"
    if strict:
      print(msg, file=sys.stderr)
      raise SystemExit(1)
    print(msg + "; skipping render.")
    raise SystemExit(0)

  for tmpl in templates:
    out = secrets_dir / tmpl.name.removesuffix(".tmpl")  # foo.env.tmpl -> foo.env
    print(f"Rendering {tmpl} -> {out}")
    subprocess.run(["op", "inject", "--force", "-i", str(tmpl), "-o", str(out)], check=True)

clean-secrets:
  @rm -f {{SECRETS_DIR}}/*.env
  @echo "Removed generated secrets"

# ── Generic compose wrappers (work on any service[s]) ────────────────
# Examples:
#   just up openclaw agent-browser
#   just stop speaches
#   just logs openclaw
#   just build openclaw
#   just restart openclaw
#   just upgrade actualbudget        (pulls latest image + recreates)

up *args:
  {{COMPOSE}} up -d {{args}}

# `down` removes containers + networks; use `stop` if you want to keep them.
down *args:
  {{COMPOSE}} down {{args}}

stop *args:
  {{COMPOSE}} stop {{args}}

restart *args:
  {{COMPOSE}} restart {{args}}

logs *args:
  {{COMPOSE}} logs -f {{args}}

build *args:
  {{COMPOSE}} build {{args}}

# Pull latest image(s) and recreate. --no-deps keeps dependent services running.
# Use this for services with `image:` only. For locally-built services (see
# `upgrade-openclaw` below) the source dependency must be bumped first.
upgrade *services:
  {{COMPOSE}} pull {{services}}
  {{COMPOSE}} up -d --no-deps {{services}}

# Upgrade openclaw (locally-built; build context = ankitson/dockers git repo).
# Resolves the npm version to install (default: latest), passes it as a build
# arg, rebuilds, and recreates. The Dockerfile defaults to OPENCLAW_VERSION=
# latest, so no source pin to bump unless you pass a specific version here
# (useful for rollback: `just upgrade-openclaw 2026.5.25`).
upgrade-openclaw version="":
  #!/usr/bin/env bash
  set -euo pipefail
  V="{{version}}"
  [ -z "$V" ] && V=$(npm view openclaw version)
  echo "openclaw: building with openclaw@$V"
  {{COMPOSE}} build --build-arg OPENCLAW_VERSION="$V" openclaw
  {{COMPOSE}} up -d --no-deps openclaw
  echo
  docker exec openclaw bash -lc 'openclaw --version' 2>/dev/null | grep -v "Agent mode" | head -1 || true

# ── Speaches-specific (no generic compose equivalent) ────────────────
# Preload the default whisper model (downloads weights if not cached).
speaches-pull model="deepdml/faster-whisper-large-v3-turbo-ct2":
  curl -fsS -X POST "http://127.0.0.1:8800/v1/models/{{model}}" && echo
  curl -fsS "http://127.0.0.1:8800/v1/models/{{model}}" | python3 -m json.tool

# Smoke test: transcribe an audio file. Usage: just speaches-test path/to/audio.wav
speaches-test file:
  curl -fsS -X POST http://127.0.0.1:8800/v1/audio/transcriptions \
    -H "Content-Type: multipart/form-data" \
    -F "file=@{{file}}" \
    -F "model=deepdml/faster-whisper-large-v3-turbo-ct2" \
    -F "response_format=json" | python3 -m json.tool
