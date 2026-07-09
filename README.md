# devserver

Docker stack for the dev server — data pipelines (Garmin / banking / Playnite /
AoE4-replay on Dagster, plus DBOS / Restate experiments) and
supporting services.

## Docs

- [`docs/CHANGELOG.md`](docs/CHANGELOG.md) — devserver-wide changelog
- [`docs/NOTES.md`](docs/NOTES.md) — devserver-wide open threads / gotchas
- [`pipelines/docs/`](pipelines/docs/) — data-pipeline detail (changelog, notes,
  per-tool plans, inspection guide)

## Commands

- `just` — devserver-wide recipes (`rs`, `up`, `down`, `logs`, `clean-secrets`)
- `just pipelines <recipe>` — pipeline recipes (see [`pipelines/Justfile`](pipelines/Justfile))

## Local S3-Compatible Downloads

- Start MinIO: `just minio-up`
- API endpoint: `http://127.0.0.1:39000`
- Console: `http://127.0.0.1:39001`
- Tailnet API endpoint: `https://minio.dev.ankitson.com`
- Tailnet console: `https://minio-console.dev.ankitson.com`
- Public download bucket: `files`
- Storage path: `/mnt/store-ext4/minio`
- Upload one file and print its public URL: `just minio-put path/to/file`
- Print AWS-compatible client env vars: `just minio-client-env`

Example client call:

```sh
eval "$(just minio-client-env)"
aws --endpoint-url "$AWS_ENDPOINT_URL_S3" s3 ls s3://files
```
