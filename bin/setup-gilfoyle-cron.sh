#!/usr/bin/env bash
# Register gilfoyle's ops cron jobs in the running openclaw gateway.
#
# Cron definitions live in the gitignored openclaw state volume
# (~/.openclaw/cron/jobs.json), so this script is the source of truth for them.
# It is idempotent: jobs are matched by name; an existing job is left alone.
# To change a job, remove it first:  docker exec openclaw openclaw cron remove <id>
# then re-run this script.
#
# Usage:  bin/setup-gilfoyle-cron.sh        (or `just gil-cron-setup`)
set -euo pipefail

CHANNEL="channel:1513643336161034402"   # #homeserver-ops
AGENT="gilfoyle"

# Names of jobs we manage, so we can check existence before adding.
existing_json="$(docker exec openclaw openclaw cron list --agent "$AGENT" --json 2>/dev/null || echo '[]')"

job_exists() {
  # $1 = job name. Returns 0 if a job with that name already exists.
  printf '%s' "$existing_json" | python3 -c '
import json, sys
name = sys.argv[1]
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(1)
jobs = data.get("jobs", data) if isinstance(data, dict) else data
sys.exit(0 if any((j or {}).get("name") == name for j in jobs) else 1)
' "$1"
}

add_job() {
  # $1 = name; remaining args = cron add flags
  local name="$1"; shift
  if job_exists "$name"; then
    echo "✓ $name already registered — skipping"
    return 0
  fi
  echo "+ adding $name"
  docker exec openclaw openclaw cron add --name "$name" --agent "$AGENT" "$@"
}

# ── health-watch: every 15 min, isolated. --no-deliver: the agent posts findings
#    itself via the message tool (to threads); the runner must NOT fallback-deliver
#    the terminal summary (that leaked internal "posted a finding…" status to chat).
#    --channel/--to still set so the agent's message tool has a route.
add_job "gilfoyle-health-watch" \
  --every 15m \
  --session isolated \
  --no-deliver --channel discord --to "$CHANNEL" \
  --message "Run the health-watch loop: read /cybernetics/agents/gilfoyle/loops/health-watch.md and execute it. Observe only; never mutate without an approval reply. Stay quiet if nothing is new."

# ── image-update: daily 08:00 PT, isolated, --no-deliver (see health-watch note) ─
add_job "gilfoyle-image-update" \
  --cron "0 8 * * *" --tz "America/Los_Angeles" \
  --session isolated \
  --no-deliver --channel discord --to "$CHANNEL" \
  --message "Run the image-update loop: read /cybernetics/agents/gilfoyle/loops/image-update.md and execute it. Detect updates via digest comparison (no pulls); report available updates with the exact apply command; do not pull or recreate without an approval reply."

echo
echo "Done. Current gilfoyle jobs:"
docker exec openclaw openclaw cron list --agent "$AGENT"
