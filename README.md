# aqua_cortex

AquaButler operational awareness layer. Reads the Obsidian vault (and,
eventually, live state + kanban), chunks it, embeds it via the home-swarm
llama.cpp server, and indexes it into the existing Meilisearch instance.

Phase 1 (this repo) ships the Obsidian indexer MVP. Phase 2 adds live-state
linking (services → kanban cards → git commits). Phase 3 adds a Grafana panel
and a CLI.

## Architecture

```
┌──────────────────────────────┐
│ Obsidian vault (NFS share)   │
│ /mnt/Stor1/appdata/obsidian  │
└──────────────┬───────────────┘
               │ read-only bind mount
               ▼
┌──────────────────────────────────────────────┐
│ aqua_cortex_indexer  (Swarm service / cron)   │
│  index_obsidian.py                           │
│   ├─ cortex.chunker    (heading-aware, 350t) │
│   ├─ cortex.embedder   (httpx → llama.cpp)   │
│   ├─ cortex.wiki_links (parse [[refs]])      │
│   └─ cortex.meili      (upsert documents)    │
└──────────────┬───────────────────────────────┘
               │ Bearer (Docker secret)
               ▼
┌──────────────────────────────────────────────┐
│ Meilisearch (docker-tools stack)             │
│ index "aqua_cortex"  +  1536-dim embedder    │
└──────────────────────────────────────────────┘
```

- **Where it runs:** HOME swarm only. The cortex never deploys to Aqua —
  Aqua is app-layer. (See `AquaButler-Server/AGENTS.md`.)
- **What it talks to:** the existing `docker-tools_meilisearch` service
  (already 4GB after the Phase-1 OOM bump) and the existing
  `llamacpp_llama-server` (Qwen2.5-1.5B-Instruct-Q4_K_M). No new deploys.
- **Credentials:** the existing `meili_master_key` Docker secret is the only
  auth. Read at `/run/secrets/meili_master_key`. No new credentials.
- **Schedule:** `deploy/cron-aqua_cortex.yml` runs the indexer once a day
  at 03:00 UTC via swarm-cronjob.

## Adding a new application

Adding an application is a one-file change. The indexer treats `application`
as a pure string tag, never as a code path:

1. Copy `aqua_cortex.toml` to a new file (e.g. `aqua_cortex.greenhouse.toml`).
2. Edit:
   ```toml
   application = "greenhouse"            # <-- only this needs to differ
   vault_paths = [
     "/mnt/Stor1/appdata/obsidian/Obsidian Vault/Greenhouse",
   ]
   ```
3. Run `python3 index_obsidian.py --config aqua_cortex.greenhouse.toml`.

The same code paths and Meilisearch index are reused — `application` becomes
a filter on the search index. No Python edits, no compose edits.

## Embedding model — honest note

The home-swarm llama.cpp server runs
`Qwen2.5-1.5B-Instruct-Q4_K_M.gguf` with `APP_EMBEDDING=true`. It returns
**1536-dim embeddings** (verified 2026-08-26 — the card's initial 896-dim
assumption was wrong; the actual model produces a 1536-dim vector).

Tradeoffs vs a purpose-built embedding model (e.g. `nomic-embed-text-v1.5`,
`bge-large-en-v1.5`):

| Aspect             | Qwen2.5-1.5B (current)         | nomic-embed-text / bge-large       |
|--------------------|--------------------------------|------------------------------------|
| Quality on prose   | Decent for short queries, but  | Strong — trained for retrieval     |
|                    | Qwen is a chat model, not      | specifically.                      |
|                    | trained for retrieval.         |                                    |
| Dimension          | 1536 (larger index footprint)  | 768                                |
| Latency on CPU     | Acceptable for cron jobs       | Lower; smaller model.              |
| Already deployed   | Yes                            | No — would need a new model pull,  |
|                    |                                | an image rebuild, and an embedder  |
|                    |                                | swap in `aqua_cortex.toml`.        |

**Recommended path:** Phase 1 ships with Qwen2.5-1.5B to keep this phase
zero-touch on infra. If Phase-2 evaluation shows weak top-K relevance for
the operational queries we actually care about (e.g. "where is the supabase
kong JWT verified?"), plan a Phase-2.5 swap to `nomic-embed-text-v1.5`. The
embedder is config-only — change `llama_model` and `embed_dim` in
`aqua_cortex.toml`, re-run, and re-index. The Meilisearch embedder config
will re-shape on the next `ensure_index()` call.

## Deployment

### Quick start (local)

```bash
pip install httpx
python3 index_obsidian.py --config aqua_cortex.toml
```

The indexer reads the master key from `MEILI_KEY_FILE` (default
`/run/secrets/meili_master_key`) or, failing that, from
`AQUA_CORTEX_MEILI_KEY`. For local dev, point it at a key file:

```bash
echo "$MEILI_KEY" > /tmp/meili.key
MEILI_KEY_FILE=/tmp/meili.key python3 index_obsidian.py
```

### Swarm (home)

```bash
docker build -t registry.loc.wallacearizona.us/aqua_cortex_indexer:latest .
docker push registry.loc.wallacearizona.us/aqua_cortex_indexer:latest

docker --context home-swarm-manager stack deploy \
  -c deploy/docker-stack.yml aqua_cortex
docker --context home-swarm-manager stack deploy \
  -c deploy/cron-aqua_cortex.yml aqua_cortex
```

The indexer service is declared in `deploy/docker-stack.yml` as a
**replicated-job-mode** service (one replica, no restart) so the swarm
scheduler is ready to run it on demand. The `deploy/cron-aqua_cortex.yml`
stack adds the `crazymax/swarm-cronjob` scheduler that spawns the indexer
task daily at 03:00 UTC.

Both stacks mount the existing `meili_master_key` Docker secret at
`/run/secrets/meili_master_key` (the indexer reads it on startup) and
join the `traefik_proxy` overlay network so they can reach
`docker-tools_meilisearch:7700` and `llamacpp_llama-server:18080` over
the swarm overlay.

The vault is bind-mounted read-only at `/vault` from the homelab NFS
share (`/mnt/Stor1/appdata/obsidian/Obsidian Vault`).

> **Heads-up:** cluster-wide image pulls from `registry.loc.wallacearizona.us`
> require every swarm node to have valid registry credentials in
> `/root/.docker/config.json` (or equivalent). On 2026-08-26 several nodes
> were returning `401 invalid authorization credential` from the registry
> during pull attempts, so cluster-wide deploys were failing. Run
> `docker login registry.loc.wallacearizona.us` on each node, or coordinate
> the auth fix via the registry stack separately.

## Repository

`github.com/ultron-aquabutler/aqua_cortex` — kept on the fork per
`AquaButler-Server/AGENTS.md`. PRs target `master`.

## License

MIT. See `LICENSE`.
