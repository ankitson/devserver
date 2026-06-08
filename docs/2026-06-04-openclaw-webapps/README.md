# OpenClaw Web Apps

OpenClaw can publish lightweight web apps by writing into:

`/cybernetics/agents/openclaw-webapps`

On the host this is:

`/home/ankit/hroot/cybernetics/agents/openclaw-webapps`

## Static App

Create files under `static/<slug>/` and add an `apps.json` entry:

```json
{
  "my-page": {
    "type": "static",
    "root": "static/my-page",
    "dashboard": {
      "label": "My Page",
      "description": "Static page"
    }
  }
}
```

Then visit:

`https://my-page.dev.ankitson.com`

## Process App

Create files under `apps/<slug>/` and add:

```json
{
  "my-api": {
    "type": "process",
    "runtime": "custom",
    "install": "bun install",
    "start": "bun run src/server.ts",
    "root": "apps/my-api"
  }
}
```

The runner sets `PORT` and `HOST=0.0.0.0`. Apps should listen on those values.

## Limits

The runner has no Docker socket. Full container deployments should be prepared as a patch/request and
applied by a trusted operator.
