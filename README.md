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
└──────────────┬───────────────────────────────┘
               │ GET /documents (paginated)
               ▼
┌──────────────────────────────────────────────┐
│ link_live_state.py  (Phase 2)                │
│   ├─ cortex.swarm_state  (docker context     │
│   │   "aqua-swarm" service ls + service ps)  │
│   ├─ cortex.kanban_state (sqlite3 title +    │
│   │   t_<hex> index, last 30 days)           │
│   └─ cortex.git_state    (git log -since     │
│       30d, SHA + token index)                │
└──────────────────────────────────────────────┘
```

- **Where it runs:** HOME swarm only. The cortex never deploys to Aqua —
  Aqua is app-layer. (See `AquaButler-Server/AGENTS.md`.)
- **What it talks to:** the existing `docker-tools_meilisearch` service
  (already 4GB after the Phase-1 OOM bump), the existing
  `llamacpp_llama-server` (Qwen2.5-1.5B-Instruct-Q4_K_M), the local
  kanban SQLite (`~/.hermes/kanban/boards/<board>/kanban.db`), the
  AquaButler-Server git repo at `~/AquaButler-Server`, and the aqua-swarm
  Docker context. No new deploys.
- **Credentials:** the existing `meili_master_key` Docker secret is the only
  auth. Read at `/run/secrets/meili_master_key`. No new credentials.
- **Schedule:** `deploy/cron-aqua_cortex.yml` runs the indexer once a day
  at 03:00 UTC via swarm-cronjob; the linker runs immediately after in the
  same task slot.

## Phase 2 — Live-state linker

After `index_obsidian.py` has populated the `aqua_cortex` index, run:

```bash
python3 link_live_state.py
```

The linker reads every chunk back out of Meilisearch via the
`/indexes/aqua_cortex/documents` endpoint (NOT `/search` — the latter is
capped at Meili's `pagination.maxTotalHits=1000` default, and our corpus
is already past that), joins each one with three live-state sources, and
ships partial updates back via `POST /indexes/aqua_cortex/documents?merge=true`.

### Linked fields written to every chunk

- `linked_services: string[]` — every aqua-swarm service whose name OR
  stack prefix appears as a substring in the chunk text or `doc_path`.
  e.g. a chunk that mentions `mosquitto_broker-aqua1` AND talks about the
  `supabase` stack will carry `["mosquitto_broker-aqua1", "supabase_*"]`.
- `linked_cards: string[]` — kanban cards in the last 30 days whose title
  shares ≥ 2 tokens with the chunk text, OR whose `t_<hex>` id is
  explicitly mentioned in the chunk. Pulled from
  `~/.hermes/kanban/boards/<board>/kanban.db`.
- `linked_commits: string[]` — AquaButler-Server commits in the last 30
  days whose message shares tokens with the chunk text, OR whose 7-char
  SHA is mentioned in the chunk. Pulled via `git log --since='30 days ago'`.
- `live_state: {service, swarm_state, stack, node, checked_at} | null` —
  the single most-specific running service from `linked_services`. Used
  for the Grafana / CLI "what's deployed right now?" view in Phase 3.

### Env overrides (linker-specific)

```bash
AQUA_CORTEX_BOARD=aquabutlers            # kanban board slug
AQUA_CORTEX_REPO=~/AquaButler-Server     # git repo path
AQUA_CORTEX_SWARM=aqua-swarm             # docker context name
AQUA_CORTEX_SWARM_FILE=/path/to.json    # skip docker, load fixture
AQUA_CORTEX_KANBAN_DB=/path/to/kanban.db # override kanban DB path
```

### Acceptance (verified 2026-08-26)

Running `python3 link_live_state.py` against the live home-swarm Meili
instance (2040 docs) reports `link_pct: 79.7%` (≥ 50% threshold). The
acceptance query
`POST /indexes/aqua_cortex/search {"q":"supabase_storage","limit":3}`
returns documents where `live_state.swarm_state="1/1"` (matching
`docker --context aqua-swarm service ls`) AND `linked_cards` carries a
non-empty list of `t_<hex>` references.

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

## Phase 2 — Live-state linker

`link_live_state.py` reads the existing `aqua_cortex` index, joins every
chunk with live operational state, and writes the enriched document
back. Phase 2 adds four fields to every chunk:

| Field            | Type                | Source                                                       |
|------------------|---------------------|--------------------------------------------------------------|
| `linked_services`| `string[]`          | longest match of swarm service name in chunk text / doc_path |
| `linked_cards`   | `string[]`          | `t_<hex>` regex + title-fragment similarity (>=2 tokens)     |
| `linked_commits` | `string[]`          | explicit 7-char SHA mention + commit-subject similarity      |
| `live_state`     | `object \| null`    | aqua-swarm `service ls` + `service ps` for the matched svc   |

Live-state sources:

- **aqua-swarm**: `docker --context aqua-swarm service ls` + `service ps`
  over SSH. Service stack = prefix before first `_`; node = first
  running task. Tolerates a failed `service ls` (returns empty index
  rather than crashing the linker).
- **Kanban DB**: `~/.hermes/kanban/boards/<board>/kanban.db` (default
  `aquabutlers`). Cards touched in the last 30 days are indexed by
  title tokens (`[a-z0-9]{3,}`, deduped, >=2-of-tokens threshold).
  `t_<hex>` references are extracted verbatim from chunk text and
  must match an existing card id.
- **AquaButler-Server git log**: `git -C ~/AquaButler-Server log
  --since='30 days ago' --pretty=format:'%H %s'`. SHA matching is
  anchored on the **known** short/full SHA — arbitrary hex tokens
  in prose won't spuriously link.

### Run it

```bash
# Local / dev
MEILI_KEY_FILE=/run/secrets/meili_master_key python3 link_live_state.py

# With explicit fixtures (skips docker / git / sqlite3)
AQUA_CORTEX_SWARM_FILE=/tmp/swarm.json \
AQUA_CORTEX_KANBAN_DB=/tmp/kanban.db \
AQUA_CORTEX_REPO=/tmp/repo \
python3 link_live_state.py
```

The linker is idempotent: chunks with no field changes are skipped on
subsequent runs. A Meilisearch update task is enqueued only when at
least one of `linked_services` / `linked_cards` / `linked_commits` /
`live_state` actually changed.

### How it avoids the v1.12 partial-update gotcha

Meilisearch v1.12 does NOT expose PATCH /documents or `merge=true` on
POST /documents (those shipped in v1.13). The linker therefore reads
back every chunk, builds the **full document** with the four
Phase-2 fields overwritten, and POSTs it. The dense embedding is
preserved server-side because `_vectors` is stripped from the body
and the server treats the absent field as "don't touch".

### Acceptance (verified live 2026-08-26)

- `pytest tests/test_link_smoke.py tests/test_smoke_index.py` — both pass.
- `link_live_state.py` against the live `aqua_cortex` index (2040 docs)
  → **1785 chunks enriched (87.5%)**, far above the 50% threshold.
- `curl ... /indexes/aqua_cortex/search?q=supabase_storage` returns
  hits where `live_state.swarm_state == "1/1"` (matches the live
  `docker --context aqua-swarm service ls | grep supabase_storage`)
  AND `linked_cards` populated with `t_<hex>` ids.

## Phase 3 — User-facing surface

Phase 3 ships two complementary surfaces on top of the Phase 2 joined index.
Both ship as part of this repo; neither deploys a new service.

### Lane A — Grafana panel (`patroni-monitoring_grafana`)

Extends the existing home-swarm Grafana service. The panel JSON lives at
`grafana/aqua_cortex_panel.json` and ships three table sub-panels:

| Sub-panel | Source field(s) | What it shows |
|-----------|-----------------|---------------|
| **Doc vs Running** | `linked_services`, `live_state` | Each service's documented / running flags + swarm state |
| **Doc Freshness** | `indexed_at`, git last-modified | Days since a doc was re-indexed; cells > 30 days go red |
| **Recent Activity** | `indexed_at`, `source`, `doc_title` | Last 50 indexed chunks, newest first |

The panel reads from the cortex snapshot — a flat JSON blob that lives at
`grafana/aqua_cortex_snapshot.example.json` (committed reference) or is
regenerated live via `hermes aqua-cortex snapshot`. See
[`deploy/grafana-import-notes.md`](deploy/grafana-import-notes.md) for
the import recipe.

### Lane B — `hermes aqua-cortex` CLI

Terminal surface for fast lookup. Drop
[`hermes-plugin/hermes_aqua_cortex.py`](hermes-plugin/hermes_aqua_cortex.py)
on `$PATH` (e.g. `~/.local/bin/hermes-aqua-cortex`) and the `hermes`
CLI auto-discovers it as `hermes aqua-cortex`. Three subcommands:

| Subcommand | Purpose |
|------------|---------|
| `hermes aqua-cortex query "<question>"` | Hybrid-search the joined index, prompt the existing llama.cpp server, print a citation-bearing answer |
| `hermes aqua-cortex snapshot` | Emit the same JSON blob the Grafana panel reads (useful for the cron-driven import path) |
| `hermes aqua-cortex ping` | Sanity-check Meilisearch + llama.cpp reachability |

The `query` subcommand:

1. Hybrid-searches `https://meili.loc.wallacearizona.us/indexes/aqua_cortex/search`
   with lexical `q` + dense `vector` (both from the same question).
2. Sends the top 5 chunks (configurable via `HERMES_AQUA_CORTEX_TOP_K`) to
   the existing `https://llama.loc.wallacearizona.us/v1/chat/completions`
   with a "answer using ONLY the cited chunks" system prompt.
3. Parses citations out of chunk metadata deterministically — the LLM
   cannot inject fake sources.
4. Falls back to an extractive answer when the LLM goes degenerate
   (Qwen2.5-1.5B occasionally loops); the Sources block is always emitted.

Sample output:

```
$ hermes aqua-cortex query "what is the mosquitto cluster topology"
Top matching context (LLM output was untrustworthy; using extractive fallback):

[Mosquitto — MQTT Topic Hierarchy] ...

Sources:
  - Mosquitto/index.md#-mqtt-topic-hierarchy
  - kanban:t_6823b7e9
  - swarm:mosquitto_broker-aqua1=1/1
```

Configuration is read from the same `aqua_cortex.toml` the indexer and
linker use, so a single edit covers all three surfaces. Per-call env
overrides are documented in
[`hermes-plugin/README.md`](hermes-plugin/README.md).

### Phase 3 acceptance (verified 2026-08-28)

- `tests/test_panel_json.py` — 9 tests pass: validates the Grafana export
  shape (`schemaVersion`, `__inputs`, `__requires`, ≥3 table panels,
  correct datasource UID on every panel).
- `tests/test_cli_smoke.py` — 3 tests pass against fake Meilisearch +
  fake llama.cpp on free localhost ports (offline / no live state).
- `hermes aqua-cortex ping` against the live home swarm — `meili : OK
  (2061 docs)`, `llama : OK (dim=1536)`.
- `hermes aqua-cortex query "what is the mosquitto cluster topology"`
  exits 0, prints answer text, prints ≥1 source citation (a doc path,
  a `kanban:t_<hex>` line, and a `swarm:<svc>=<state>` line).

---

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
