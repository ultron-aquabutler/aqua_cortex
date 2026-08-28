# hermes aqua-cortex plugin

Terminal surface for the [aqua_cortex](../README.md) RAG index.

## What it does

```
$ hermes aqua-cortex query "what is the mosquitto cluster topology"
The Mosquitto cluster runs N nodes on Aqua Swarm. Auth uses Mosquitto's
native password file mounted from a ConfigMap. Topic tree is
<application>/<customer_id>/{config,telemetry,chem,dosing,status,health,
cmd,restore}/#. The pool deployment uses application="pool" today.

Sources:
  - AquaButler/Mosquitto/index.md#cluster-topology
  - kanban:t_6823b7e9
  - swarm:mosquitto_broker=3/3
```

Under the hood:

1. Hybrid search on `https://meili.loc.wallacearizona.us/indexes/aqua_cortex/search`
   (lexical `q` + dense `vector` from the same query).
2. Top-5 chunks (configurable via `HERMES_AQUA_CORTEX_TOP_K`) are sent to
   the existing `https://llama.loc.wallacearizona.us/v1/chat/completions`
   with a strict "answer using ONLY the cited chunks" system prompt.
3. Citations are parsed out of chunk metadata deterministically — the LLM
   cannot invent sources.

## Install

Drop `hermes_aqua_cortex.py` somewhere on `$PATH` (e.g. `~/.local/bin/`)
and make it executable:

```bash
install -m 0755 hermes_aqua_cortex.py ~/.local/bin/hermes-aqua-cortex
```

The `hermes` CLI (>= 0.4) auto-discovers plugins by name. Verify with:

```bash
hermes aqua-cortex --help
hermes aqua-cortex ping
```

If `hermes` is not on `$PATH` yet, you can run the plugin directly:

```bash
./hermes_aqua_cortex.py query "what is the mosquitto cluster topology"
```

The plugin reads its base config from `aqua_cortex.toml` next to the repo
root — i.e. the same file the indexer and linker read. Keep them in sync
via the repo, or override per-call with env vars (see below).

## Subcommands

| Command | Purpose |
|---|---|
| `hermes aqua-cortex query "<question>"` | Answer + citations |
| `hermes aqua-cortex snapshot` | Emit Grafana-datasource JSON to stdout |
| `hermes aqua-cortex ping` | Health-check Meilisearch + llama.cpp |

## Env overrides

All defaults are inherited from `aqua_cortex.toml` so a single config
edit covers indexer + linker + CLI. Per-call overrides:

| Var | Effect |
|---|---|
| `HERMES_AQUA_CORTEX_MEILI_URL` | Override `[indexer].meili_url` |
| `HERMES_AQUA_CORTEX_MEILI_INDEX` | Override `[indexer].meili_index` |
| `HERMES_AQUA_CORTEX_MEILI_KEY_FILE` | Override `[indexer].meili_key_file` |
| `HERMES_AQUA_CORTEX_LLAMA_URL` | Override `[indexer].llama_url` |
| `HERMES_AQUA_CORTEX_LLAMA_MODEL` | Override `[indexer].llama_model` |
| `HERMES_AQUA_CORTEX_LLAMA_API_KEY` | Optional bearer token (default unauthenticated) |
| `HERMES_AQUA_CORTEX_TOP_K` | Number of chunks to retrieve (default 5) |
| `HERMES_AQUA_CORTEX_TIMEOUT_S` | HTTP timeout (default 30) |
| `HERMES_AQUA_CORTEX_VERIFY_TLS=0` | Disable TLS verification (matches swarm-container behavior) |

## Smoke check

```bash
$ hermes aqua-cortex ping
meili  : OK (524 docs in 'aqua_cortex')
llama  : OK (dim=1536)
```

## Cluster isolation

* **Read-only.** No writes to Meilisearch, no writes to Obsidian, no
  writes to the kanban DB, no writes to the Aqua swarm.
* Runs anywhere with HTTPS to `meili.loc.wallacearizona.us` and
  `llama.loc.wallacearizona.us`. The operator's terminal, a CI runner,
  or a swarm container on any overlay.
* Does NOT deploy as a Swarm service in v1. The `snapshot` subcommand
  is suitable for piping to a cron-driven JSON file if you want a
  point-in-time view for the Grafana panel.

## See also

- `../README.md` — architecture, Phase 1 + Phase 2 details
- `../grafana/aqua_cortex_panel.json` — Grafana panel import
- `../deploy/grafana-import-notes.md` — how to import the panel into
  `patroni-monitoring_grafana`
