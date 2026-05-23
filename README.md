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
