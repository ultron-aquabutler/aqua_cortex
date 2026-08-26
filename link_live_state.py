#!/usr/bin/env python3
"""Live-state linker for aqua_cortex.

Phase 2 entrypoint. Reads existing Meilisearch chunks (indexed by
`index_obsidian.py`) and joins each one with:

  - aqua-swarm service state  -> `live_state` + `linked_services`
  - local kanban DB (last 30d) -> `linked_cards`
  - AquaButler-Server git log -> `linked_commits`

The linker's contract is **idempotent enrichment**: chunks that already
have these fields populated are kept as-is if the new join produces no
new entries; chunks where live state has changed (e.g. a service crashed
overnight) are overwritten with fresh data.

Run after `index_obsidian.py`:

    python3 link_live_state.py

Env / config overrides (see cortex/config.py for the full list, which is
shared with the indexer):

    AQUA_CORTEX_CONFIG    path to aqua_cortex.toml
    AQUA_CORTEX_BOARD     kanban board slug (default "aquabutlers")
    AQUA_CORTEX_REPO      path to AquaButler-Server clone
    AQUA_CORTEX_SWARM     docker context name (default "aqua-swarm")
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

# Allow running as a script without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from cortex import __version__
from cortex.config import IndexerConfig
from cortex.git_state import GitIndex, load as load_git
from cortex.kanban_state import KanbanIndex, load as load_kanban
from cortex.meili import MeiliClient, load_api_key
from cortex.schema import LiveState
from cortex.swarm_state import (
    SwarmServiceState,
    build_index as build_swarm,
    load_from_json as load_swarm_json,
)


log = logging.getLogger("aqua_cortex.link")


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-5s %(name)s %(message)s",
        datefmt="%H:%M:%S",
    )


def _default_kanban_db_path() -> Path:
    """Resolve the local kanban SQLite DB.

    Default layout: `~/.hermes/kanban/boards/<board>/kanban.db`. Override
    via $AQUA_CORTEX_KANBAN_DB.
    """
    override = os.environ.get("AQUA_CORTEX_KANBAN_DB")
    if override:
        return Path(override).expanduser()
    board = os.environ.get("AQUA_CORTEX_BOARD", "aquabutlers")
    return Path(f"~/.hermes/kanban/boards/{board}/kanban.db").expanduser()


def _default_repo_path() -> Path:
    override = os.environ.get("AQUA_CORTEX_REPO")
    if override:
        return Path(override).expanduser()
    return Path("~/AquaButler-Server").expanduser()


def _services_for_chunk(
    chunk_text: str,
    chunk_doc_path: str,
    swarm_index: dict[str, SwarmServiceState],
) -> list[str]:
    """Return every service / stack name referenced by this chunk.

    A match happens when either:
      - a service name (e.g. ``supabase_storage``) appears as a substring
        in the chunk text or its ``doc_path``, OR
      - a stack prefix (e.g. ``supabase`` from ``supabase_storage`` or
        ``mosquitto`` from ``mosquitto_broker-aqua1``) appears as a
        substring.

    Substring matching is intentional: services and stack names in this
    cluster are unique identifiers (``supabase_storage`` only collides
    with itself), and chunk text / doc paths don't quote them. The cost
    of a false positive (``openbalena_api`` matching the word ``api``)
    is acceptable — the worst case is one extra entry in the
    ``linked_services`` array.

    Returns a deterministic list sorted longest-first then alphabetically.
    De-duplicated against itself.
    """
    if not swarm_index:
        return []
    haystacks = [chunk_text.lower(), chunk_doc_path.lower()]
    seen: set[str] = set()
    matches: list[str] = []
    for name, st in swarm_index.items():
        nl = name.lower()
        sl = st.stack.lower() if st.stack and st.stack != name else ""
        for h in haystacks:
            hit = (nl and nl in h) or (sl and sl in h)
            if hit:
                if name not in seen:
                    seen.add(name)
                    matches.append(name)
                break
    matches.sort(key=lambda n: (-len(n), n))
    return matches


def _pick_live_state(
    matches: list[str],
    swarm_index: dict[str, SwarmServiceState],
) -> SwarmServiceState | None:
    """Pick the single best swarm state to attach as ``live_state``.

    Prefers the running service with the longest matching name (most
    specific). Falls back to the first match if none are running.
    """
    if not matches:
        return None
    # Prefer running services first, then longest match.
    running = [swarm_index[m] for m in matches if swarm_index[m].swarm_state.startswith(("1/", "2/", "3/"))]
    if running:
        running.sort(key=lambda s: (-len(s.service), s.service))
        return running[0]
    return swarm_index[matches[0]]


def _ensure_link_fields(d: dict) -> dict:
    """Normalise a raw Meilisearch hit to have every linked_* field as a
    list. Older chunks (pre-Phase-2) may be missing some of these; treat
    missing as empty rather than KeyError."""
    for k in ("linked_services", "linked_cards", "linked_commits"):
        v = d.get(k)
        if v is None:
            d[k] = []
        elif not isinstance(v, list):
            d[k] = list(v)
    return d


def _merge_lists(existing: list[str], new: list[str]) -> list[str]:
    """Preserve order, dedupe, prefer the new list's ordering for overlap."""
    seen: set[str] = set()
    out: list[str] = []
    for s in new + existing:
        if s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def _build_updates(
    hits: list[dict],
    swarm: dict[str, SwarmServiceState],
    kanban: KanbanIndex,
    git: GitIndex,
) -> list[dict]:
    """For each existing chunk, build the FULL document body to upsert.

    On Meilisearch v1.12 the only update path is a full document POST
    (PATCH and merge=true both unavailable). We therefore ship every
    existing field back, with the four Phase-2 fields overwritten in
    place. The server-side vec is preserved because we don't include
    `_vectors` in the body (MeiliClient.upsert_partial_documents strips
    it; the server keeps the prior embedding because we never claim to
    write it).

    Skips chunks where no field actually changed — no churn, no
    unnecessary re-indexing tasks.
    """
    updates: list[dict] = []
    for d in hits:
        d = _ensure_link_fields(d)
        chunk_text = d.get("chunk_text", "")
        doc_path = d.get("doc_path", "")

        # Swarm linkage
        sw_matches = _services_for_chunk(chunk_text, doc_path, swarm)
        new_live: LiveState | None = None
        new_linked_services: list[str] = list(d.get("linked_services", []))
        for name in sw_matches:
            if name not in new_linked_services:
                new_linked_services.append(name)
        picked = _pick_live_state(sw_matches, swarm)
        if picked is not None:
            new_live = LiveState(
                service=picked.service,
                swarm_state=picked.swarm_state,
                stack=picked.stack,
                node=picked.node,
                checked_at=picked.checked_at,
            )

        # Kanban linkage
        kanban_matches = kanban.find_matches(chunk_text)
        new_linked_cards = _merge_lists(
            list(d.get("linked_cards", [])), kanban_matches
        )

        # Git linkage
        git_matches = git.find_matches(chunk_text)
        new_linked_commits = _merge_lists(
            list(d.get("linked_commits", [])), git_matches
        )

        # Has anything actually changed? If not, skip the upsert.
        prev_live = d.get("live_state")
        unchanged_live = (
            new_live is None
            and prev_live is None
        ) or (
            new_live is not None
            and prev_live is not None
            and prev_live.get("service") == new_live.service
            and prev_live.get("swarm_state") == new_live.swarm_state
            and prev_live.get("node") == new_live.node
        )
        if (
            not new_live
            and not kanban_matches
            and not git_matches
            and list(d.get("linked_cards", [])) == new_linked_cards
            and list(d.get("linked_commits", [])) == new_linked_commits
            and list(d.get("linked_services", [])) == new_linked_services
            and unchanged_live
        ):
            continue

        # Build the full document body. Carry forward every field we
        # don't touch; replace the four Phase-2 fields. Strip None
        # `live_state` so it gets removed server-side.
        update = dict(d)  # shallow copy preserves every existing field
        update["linked_services"] = new_linked_services
        update["linked_cards"] = new_linked_cards
        update["linked_commits"] = new_linked_commits
        if new_live is not None:
            update["live_state"] = new_live.to_dict()
        elif prev_live is not None:
            update["live_state"] = None  # Meili interprets as field delete
        updates.append(update)
    return updates


def _fetch_all_chunks(meili: MeiliClient, batch: int = 1000) -> list[dict]:
    """Pull every chunk out of the index.

    Uses the `/indexes/<idx>/documents` endpoint, which is paginated by
    `limit`/`offset` (NOT by search hits) and is not capped at Meili's
    default `pagination.maxTotalHits=1000`. Each hit's `_vectors` is
    stripped to keep payload small (we don't need the dense vec for
    linkage — only chunk_text + metadata).
    """
    import httpx

    out: list[dict] = []
    offset = 0
    base = f"{meili.base_url.rstrip('/')}/indexes/{meili.index}/documents"
    headers = {"Authorization": f"Bearer {meili.api_key}"}
    with httpx.Client(timeout=60.0) as client:
        while True:
            r = client.get(base, params={"limit": batch, "offset": offset},
                           headers=headers)
            r.raise_for_status()
            payload = r.json()
            hits = payload.get("results", [])
            for h in hits:
                h.pop("_vectors", None)
                out.append(h)
            if len(hits) < batch:
                break
            offset += batch
    return out


def _stats(updates: list[dict], hits: list[dict]) -> dict:
    """Compute the linker run statistics for the closing log line."""
    total = len(hits)
    if total == 0:
        return {"total": 0, "updated": 0, "linked_pct": 0.0}
    n_linked = 0
    for d in hits:
        if (
            d.get("linked_services")
            or d.get("linked_cards")
            or d.get("linked_commits")
            or d.get("live_state")
        ):
            n_linked += 1
    return {
        "total": total,
        "updated": len(updates),
        "linked": n_linked,
        "linked_pct": round(100.0 * n_linked / total, 1),
    }


def run(cfg: IndexerConfig) -> int:
    log.info("aqua_cortex linker %s starting", __version__)

    meili_key = load_api_key(cfg.meili_key_file)
    meili = MeiliClient(
        base_url=cfg.meili_url,
        index=cfg.meili_index,
        api_key=meili_key,
        dim=cfg.embed_dim,
    )
    if not meili.health():
        log.error("meilisearch not healthy at %s", cfg.meili_url)
        return 2

    # 1. Load live-state sources.
    swarm_file = os.environ.get("AQUA_CORTEX_SWARM_FILE")
    if swarm_file:
        log.info("reading swarm snapshot from %s (skips docker context)", swarm_file)
        swarm = load_swarm_json(swarm_file)
    else:
        swarm_ctx = os.environ.get("AQUA_CORTEX_SWARM", "aqua-swarm")
        log.info("reading aqua-swarm services via context=%s", swarm_ctx)
        swarm = build_swarm(swarm_ctx)
    log.info(
        "swarm: %d services (e.g. %s)",
        len(swarm),
        next(iter(swarm), "(none)"),
    )

    kanban_db = _default_kanban_db_path()
    log.info("reading kanban DB at %s", kanban_db)
    kanban = load_kanban(kanban_db)

    repo = _default_repo_path()
    log.info("reading git log at %s", repo)
    git = load_git(repo)

    # 2. Pull all existing chunks from Meilisearch.
    log.info("fetching existing chunks from index %s", cfg.meili_index)
    started = time.monotonic()
    hits = _fetch_all_chunks(meili, batch=1000)
    log.info("fetched %d chunks in %.1fs", len(hits), time.monotonic() - started)

    # 3. Compute updates.
    updates = _build_updates(hits, swarm, kanban, git)

    stats_before = {
        "with_live_state": sum(1 for h in hits if h.get("live_state")),
        "with_cards": sum(1 for h in hits if h.get("linked_cards")),
        "with_commits": sum(1 for h in hits if h.get("linked_commits")),
        "with_services": sum(1 for h in hits if h.get("linked_services")),
    }
    log.info("link stats pre-update: %s", stats_before)

    # 4. Apply.
    if updates:
        log.info("pushing %d chunk updates", len(updates))
        meili.upsert_partial_documents(updates)
    else:
        log.info("no updates required")

    stats_after = _stats(updates, hits)
    log.info("link stats: %s", stats_after)
    log.info("done in %.1fs", time.monotonic() - started)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aqua_cortex_link")
    parser.add_argument(
        "--config",
        default=os.environ.get("AQUA_CORTEX_CONFIG"),
        help="path to aqua_cortex.toml",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    _setup_logging(args.verbose)
    cfg = IndexerConfig.load(args.config)
    try:
        return run(cfg)
    except Exception:
        log.exception("linker crashed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())