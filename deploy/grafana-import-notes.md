# Importing the aqua_cortex Grafana panel

The panel is shipped as `grafana/aqua_cortex_panel.json`. It extends the
existing `patroni-monitoring_grafana` service on the home swarm
(`https://grafana.loc.wallacearizona.us`). No new Grafana service.

## Prerequisites

1. The `marcusolsson-json-datasource` Grafana plugin is installed on the
   `patroni-monitoring_grafana` container. If missing:

   ```bash
   docker exec $(docker ps -q -f name=patroni-monitoring_grafana) \
     grafana-cli plugins install marcusolsson-json-datasource
   # then restart the service to pick up the new plugin
   docker service update --force patroni-monitoring_grafana
   ```

2. The aqua_cortex snapshot is reachable from the Grafana container.
   Two viable patterns:

   **Pattern A — point at the cortex snapshot endpoint over HTTPS**
   The CLI ships a `snapshot` subcommand; for v1 the cleanest version is
   to run it on a cron and write to a static file, then serve that file
   over the `docker-tools` overlay (e.g. via an `nginx` sidecar or an
   `aqua_cortex_api` Swarm service — see "Future work" below).

   The Grafana JSON datasource URL becomes something like
   `http://<snapshot-host>/aqua_cortex_snapshot.json` and the panel
   targets read `$.doc_vs_running`, `$.doc_freshness`, `$.recent_activity`.

   **Pattern B — use the committed example snapshot for now**
   The repo ships `grafana/aqua_cortex_snapshot.example.json` (a real
   snapshot generated against the live AquaButler corpus on 2026-08-26).
   Host it anywhere Grafana can reach, or import it directly as a
   reference while you wire up Pattern A.

## Import

```bash
# From the repo root
docker exec $(docker ps -q -f name=patroni-monitoring_grafana) \
  curl -sS -X POST \
    -H "Content-Type: application/json" \
    -u "$GRAFANA_ADMIN_USER:$GRAFANA_ADMIN_PASS" \
    --data @grafana/aqua_cortex_panel.json \
    http://localhost:3000/api/dashboards/import
```

Or, in the Grafana UI:

1. Dashboards → New → Import
2. Upload `grafana/aqua_cortex_panel.json`
3. When prompted for the JSON API datasource, either pick an existing
   one or create a new one pointing at the snapshot URL.
4. Finish; the dashboard lands as **aqua_cortex** (uid `aqua-cortex`).

## Acceptance

After import, the panel renders three sub-panels:

* **Doc vs Running** — table of services with documented/running flags.
* **Doc Freshness** — table of doc paths with their indexed_at; cells
  highlight red for docs older than 30 days.
* **Recent Activity** — last 50 indexed events (when / source / summary).

If the datasource is empty / unreachable, panels will show "No data"
until the snapshot endpoint is reachable. That is fine for v1.

## Cluster isolation

The panel reads only. No writes to Meilisearch, Obsidian, kanban DB,
or the Aqua swarm. The Grafana container already runs on the home
swarm and is wired to the same Traefik overlay as the cortex snapshot
host.

## Future work (out of v1 scope)

* **Live JSON API service** — replace the static snapshot file with a
  long-running `aqua_cortex_api` Swarm service that emits the snapshot
  over HTTP on demand (and supports parameterized filters). Until then,
  the cron-driven file approach is sufficient.
* **Streamed updates** — Grafana Live / WebSocket for sub-30m freshness.
* **Per-card drill-down** — clicking a `kanban:t_<hex>` row opens the
  kanban card in the homelab web UI.
