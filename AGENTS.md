# devserver — agent notes

## Bifrost: adding providers & managing virtual keys (the right way)

Learned the hard way on 2026-07-11 (parakeet-asr rollout). Follow this order.

### Adding a provider

Providers are **declared in config templates, not via the admin API**:

1. Add the provider block to `config/bifrost.config.json.tmpl` (copy an existing
   local provider like `nemotron-asr`: keys → `env.X_API_KEY`, `network_config`
   with the mybridge `base_url`, `custom_provider_config.base_provider_type:
   "openai"`, and only the `allowed_requests` it actually serves).
2. Add the matching `X_API_KEY=...` line to `config/bifrost.env.tmpl`
   (local noauth services still need a dummy key entry — bifrost requires one).
3. Render: `just rs` — if it fails on an unrelated template (op auth), render
   just the two bifrost files: `op inject -f -i config/bifrost.config.json.tmpl
   -o secrets/bifrost.config.json` (same for `bifrost.env.tmpl`).
4. `docker compose up -d --force-recreate bifrost` — a plain restart does NOT
   reload `env_file`, and the new provider's key resolves from process env.
5. Verify: `just bifrost-providers`, then a real request with a VK.

### Granting a VK access to the new provider

**Do NOT use `PUT /api/governance/virtual-keys/<id>` with provider_configs.**
As of the current bifrost build it has two data-loss traps:

- Any provider_config sent **without an explicit `keys` array silently deletes
  that config's key links** (rows in `governance_virtual_key_provider_config_keys`).
- `allow_all_keys: true` is accepted but **not persisted** (stays 0), and PUTs
  that include `keys` still don't reliably write the link rows.

The result is "no keys found for provider: X and model: Y" on **every** provider
of that VK, not just the new one.

Safe procedure (sqlite, offline):

```bash
cd ~/hroot/devserver
docker compose stop bifrost
cp volumes/bifrost/config.db "volumes/bifrost/config.db.before-<change>-$(date +%Y%m%d%H%M%S).bak"
sqlite3 volumes/bifrost/config.db   # then:
```

```sql
-- 1) add the provider config to the VK (find VK id in governance_virtual_keys)
INSERT INTO governance_virtual_key_provider_configs
  (virtual_key_id, provider, weight, allowed_models, blacklisted_models, allow_all_keys)
VALUES ('<vk-uuid>', '<provider>', 1.0, '["*"]', '[]', 0);

-- 2) link it to the provider's key(s) — without this, requests fail with
--    "no keys found for provider"
INSERT OR IGNORE INTO governance_virtual_key_provider_config_keys
  (table_virtual_key_provider_config_id, table_key_id)
SELECT pc.id, k.id
FROM governance_virtual_key_provider_configs pc
JOIN config_keys k ON k.provider = pc.provider
WHERE pc.virtual_key_id = '<vk-uuid>' AND pc.provider = '<provider>';
```

```bash
docker compose start bifrost
```

Then verify with a real request through the VK. If a VK is mysteriously broken
across all providers, check for empty key links:

```sql
SELECT pc.provider, count(l.table_key_id)
FROM governance_virtual_key_provider_configs pc
LEFT JOIN governance_virtual_key_provider_config_keys l
  ON l.table_virtual_key_provider_config_id = pc.id
WHERE pc.virtual_key_id = '<vk-uuid>' GROUP BY pc.id;
```

Notes:
- The Web UI (http://127.0.0.1:8090 → Virtual Keys) is also OK for VK edits;
  it manages the key links correctly. Prefer it over the REST API.
- `config.db` holds Web-UI/API edits and governance; `secrets/bifrost.config.json`
  is merged at boot. Provider/key definitions belong in the template so they
  survive re-renders; VK grants live only in the DB.
- Backups from past surgeries: `volumes/bifrost/config.db.*.bak`.
