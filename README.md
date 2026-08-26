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
```

The indexer service is declared in `deploy/docker-stack.yml` with
`replicas: 0` and the `swarm.cronjob.*` schedule labels attached to the
service. The cluster-wide `docker-tools_cronjob` daemon (crazymax/swarm-cronjob
in the `docker-tools` stack, running on ironman) discovers those labels and
scales the service 0 -> 1 at the scheduled time, then back to 0 when the
task exits.

`deploy/cron-aqua_cortex.yml` exists as documentation + escape hatch — it
declares the same scheduler container as a standalone service, but deploying
it creates an IPAM collision with the existing `docker-tools_cronjob`
daemon (verified live 2026-08-26: `invalid pool request: Pool overlaps with
other one on this address space`). The intended production path is to
**not** deploy that file; rely on `docker-tools_cronjob`.

**Schedule:** `swarm.cronjob.schedule=@daily` + `schedule-timezone=UTC`.
Note that the `docker-tools_cronjob` daemon runs with its own `TZ=America/Phoenix`
env and does NOT honour the per-service `schedule-timezone` label, so
the actual fire time is **midnight America/Phoenix (UTC-7) = 07:00 UTC**
on the schedule date — not the labelled 03:00 UTC. If you need a strict
03:00 UTC trigger, change the daemon's TZ env in the docker-tools stack,
or override the schedule in `deploy/docker-stack.yml` to a specific
UTC cron expression that maps to your desired hour in MST
(e.g. `0 0 20 * * *` = 20:00 UTC = 13:00 MST).

Both stacks mount the existing `meili_master_key` Docker secret at
`/run/secrets/meili_master_key` (the indexer reads it on startup) and
join the `traefik_proxy` overlay network. Meilisearch is on that same
overlay (DNS alias `docker-tools_meilisearch` works directly), while
llama.cpp lives on a separate `arr-servers` overlay — for that reason
the indexer is pointed at the Traefik-fronted public hostname
`https://llama.loc.wallacearizona.us` rather than the in-overlay VIP.

The vault is bind-mounted read-only at `/vault` from the homelab NFS
share (`/mnt/Stor1/appdata/obsidian/Obsidian Vault`).

> **Historical note:** on 2026-08-26 cluster-wide image pulls from
> `registry.loc.wallacearizona.us` were briefly returning
> `401 invalid authorization credential` from non-hulk nodes; the auth
> config was fixed before this README was updated. If you hit a fresh
> auth error, run `docker login registry.loc.wallacearizona.us` on the
> affected node.

## Repository

`github.com/ultron-aquabutler/aqua_cortex` — kept on the fork per
`AquaButler-Server/AGENTS.md`. PRs target `master`.

## License

MIT. See `LICENSE`.
