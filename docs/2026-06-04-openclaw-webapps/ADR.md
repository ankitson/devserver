# ADR: OpenClaw Web App Deployment Surface

## Status
Accepted on 2026-06-04.

## Context
OpenClaw needs to deploy simple static pages and small backend web apps without broad control over
the devserver or homeserver infrastructure. The homeserver already has an `html-bag`, an
`app-runner`, and standalone Docker services, but giving OpenClaw write access to the homeserver repo,
Caddy config, or `/var/run/docker.sock` would be too broad.

## Decision
Run a separate `openclaw-app-runner` service in the devserver Compose project. Its deployment
workspace is:

`/home/ankit/hroot/cybernetics/agents/openclaw-webapps`

OpenClaw may write app files and `apps.json` there. The runner reads that manifest, serves static
apps, starts small process apps, and exposes one HTTP router on the shared `mybridge` network. Caddy's
dev wildcard falls through to this runner after explicit infrastructure routes.

Apps are addressed as:

`https://<slug>.dev.ankitson.com`

Existing explicit dev routes such as `openclaw.dev.ankitson.com`, `mcp.dev.ankitson.com`, and
`dagster.dev.ankitson.com` keep precedence.

## Consequences
- OpenClaw does not receive Docker socket access.
- OpenClaw does not need write access to homeserver Caddy files.
- Static apps and small process apps become a manifest/file operation.
- Full Docker deployments remain an approval workflow: OpenClaw can prepare a patch or request, but
  should not apply arbitrary Compose changes itself.
- Slug collisions with existing dev routes are avoided by Caddy route precedence; explicit routes win.

## Follow-Up Decisions To Evaluate
- Consolidate Cybernetics `AGENTS.md` files where they duplicate global guidance. Started with
  `areas/`, `projects/`, and `resources/`.
- Keep OpenClaw's default workspace at `/cybernetics/agents/main`; see `workspace-adr.md`.
- Keep `volumes/openclaw` as runtime state; migrate only durable authored artifacts into
  Cybernetics. See `workspace-adr.md`.
