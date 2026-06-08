# ADR: OpenClaw Workspace And State Boundaries

## Status
Accepted on 2026-06-04.

## Context
OpenClaw currently mounts the whole Cybernetics vault at `/cybernetics`, but its default workspace is
`/cybernetics/agents/main`. Runtime state lives in `volumes/openclaw`, including config, credentials,
device pairing, delivery queues, cron runs, media, memory databases, plugin installs, and legacy
workspace content.

The question is whether to make `/cybernetics` itself the OpenClaw workspace and whether to unify
`volumes/openclaw` with Cybernetics.

## Decision
Keep OpenClaw's default workspace as `/cybernetics/agents/main`.

Do not move all of `volumes/openclaw` into Cybernetics. Treat it as application runtime state.

Use Cybernetics for durable, human-meaningful artifacts:

- `agents/main/` for OpenClaw identity, curated memory, heartbeat, and raw daily memory logs.
- `agents/openclaw-webapps/` for lightweight web app deployments.
- `projects/`, `areas/`, and `resources/` for vault canon.

## Rationale
- `/cybernetics` is a broad Obsidian vault. Making it the default agent workspace increases search
  noise and raises the chance of accidental edits across canon.
- `/cybernetics/agents/main` is already the operating-memory home and contains the right local
  `AGENTS.md` for the OpenClaw main session.
- `volumes/openclaw` contains secrets-adjacent and machine-runtime state that should not be mixed
  with vault canon or committed by accident.
- OpenClaw can still read/write the wider vault through absolute paths when a task calls for it.

## Follow-Up Migration
- Review `volumes/openclaw/workspace` for old durable artifacts worth moving into Cybernetics.
- Leave credentials, identity/device auth, delivery queues, logs, SQLite runtime DBs, media, npm
  installs, and plugin state in `volumes/openclaw`.
- If an OpenClaw feature needs durable authored files, prefer a Cybernetics path instead of adding
  more content under `volumes/openclaw/workspace`.
