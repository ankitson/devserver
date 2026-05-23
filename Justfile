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
    if out.exists():
      continue
    print(f"Rendering {tmpl} -> {out}")
    subprocess.run(["op", "inject", "--force", "-i", str(tmpl), "-o", str(out)], check=True)

clean-secrets:
  @rm -f {{SECRETS_DIR}}/*.env
  @echo "Removed generated secrets"

up *args:
  {{COMPOSE}} up -d {{args}}

down *args:
  {{COMPOSE}} down {{args}}

logs *args:
  {{COMPOSE}} logs -f {{args}}
