#!/usr/bin/env python3
"""Indexer entrypoint.

Reads markdown from configured vault paths, chunks, embeds via llama.cpp,
uploads to Meilisearch. Phase 1 covers the Obsidian source; Phase 2 wires
in kanban / git / swarm-event sources.

Usage:
    python3 index_obsidian.py
    AQUA_CORTEX_VAULT_PATHS=/path/to/vault/AquaButler python3 index_obsidian.py

Env overrides (see cortex/config.py for the full list):
    AQUA_CORTEX_CONFIG    path to aqua_cortex.toml
    AQUA_CORTEX_VAULT_PATHS  colon-separated vault roots
    LLAMA_URL, LLAMA_MODEL
    MEILI_URL, MEILI_INDEX, MEILI_KEY_FILE
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Allow running as a script without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from cortex import __version__
from cortex.chunker import chunk_markdown
from cortex.config import IndexerConfig
from cortex.embedder import Embedder
from cortex.meili import MeiliClient, load_api_key
from cortex.schema import CortexDocument, make_chunk_id
from cortex.wiki_links import parse_wikilinks, resolve_services


log = logging.getLogger("aqua_cortex")


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-5s %(name)s %(message)s",
        datefmt="%H:%M:%S",
    )


def _resolve_vault_paths(cfg: IndexerConfig) -> list[Path]:
    roots: list[Path] = []
    for raw in cfg.vault_paths:
        p = Path(raw).expanduser()
        if not p.exists():
            log.warning("vault path does not exist, skipping: %s", p)
            continue
        roots.append(p)
    if not roots:
        # Default: look for the homelab NFS mount on a known layout.
        for guess in [
            Path("/mnt/Stor1/appdata/obsidian/Obsidian Vault/AquaButler"),
            Path("/mnt/Stor1/appdata/obsidian/Obsidian Vault"),
        ]:
            if guess.exists():
                roots.append(guess)
    return roots


def _gather_markdown(roots: list[Path]) -> list[tuple[Path, Path]]:
    """Return (vault_root, file_path) tuples for every .md under each root."""
    out: list[tuple[Path, Path]] = []
    for root in roots:
        for path in sorted(root.rglob("*.md")):
            if path.is_file():
                out.append((root, path))
    return out


def _doc_title_for(path: Path) -> str:
    """Best-effort H1 from the file, falling back to the filename stem."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("# "):
                    return line[2:].strip()
    except OSError:
        pass
    return path.stem


def run(cfg: IndexerConfig) -> int:
    roots = _resolve_vault_paths(cfg)
    if not roots:
        log.error("no vault paths resolved; set vault_paths in aqua_cortex.toml")
        return 2

    log.info("aqua_cortex %s starting", __version__)
    log.info("application=%s vault_roots=%d", cfg.application, len(roots))
    for r in roots:
        log.info("  - %s", r)

    files = _gather_markdown(roots)
    log.info("found %d markdown files", len(files))

    meili_key = load_api_key(cfg.meili_key_file)
    meili = MeiliClient(
        base_url=cfg.meili_url,
        index=cfg.meili_index,
        api_key=meili_key,
        dim=cfg.embed_dim,
    )
    if not meili.health():
        log.error("meilisearch not healthy at %s", cfg.meili_url)
        return 3
    meili.ensure_index()

    embedder = Embedder(
        base_url=cfg.llama_url,
        model=cfg.llama_model,
        dim=cfg.embed_dim,
        batch_size=cfg.embed_batch_size,
    )

    docs: list[dict] = []
    chunks_total = 0
    started = time.monotonic()

    for root, path in files:
        rel = path.relative_to(root)
        doc_title = _doc_title_for(path)
        try:
            md = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            log.warning("read failed: %s (%s)", path, exc)
            continue

        chunks = chunk_markdown(
            md,
            target_tokens=cfg.chunk_tokens,
            overlap_tokens=cfg.chunk_overlap,
        )
        if not chunks:
            continue

        # Build vector batch.
        vectors = embedder.embed([c.text for c in chunks])
        if len(vectors) != len(chunks):
            log.error(
                "embedder returned %d vectors for %d chunks in %s",
                len(vectors),
                len(chunks),
                path,
            )
            return 4

        for chunk, vec in zip(chunks, vectors):
            if vec is None:
                # Upstream refused this chunk after retries. Skip it so one
                # bad input doesn't kill the run.
                log.warning(
                    "skipping chunk %s[%d] — embedder refused",
                    rel,
                    chunk.chunk_index,
                )
                continue
            wikilinks = parse_wikilinks(chunk.text)
            services = resolve_services(wikilinks, root)
            doc_id = make_chunk_id(str(rel), chunk.chunk_index)
            doc = CortexDocument(
                id=doc_id,
                source="obsidian",
                application=cfg.application,
                doc_path=str(rel),
                doc_title=doc_title,
                section=chunk.section,
                section_anchor=chunk.section_anchor,
                chunk_index=chunk.chunk_index,
                chunk_text=chunk.text,
                chunk_tokens=chunk.token_count,
                vec=vec,
                linked_services=services,
            )
            docs.append(doc.to_dict())

        chunks_total += len(chunks)
        log.debug("indexed %s -> %d chunks", rel, len(chunks))

    log.info("generated %d chunks across %d files", chunks_total, len(files))

    if not docs:
        log.error("no documents produced")
        return 5

    meili.upsert_documents(docs)
    elapsed = time.monotonic() - started
    log.info(
        "upserted %d documents to %s in %.1fs",
        len(docs),
        cfg.meili_index,
        elapsed,
    )
    log.info("index now reports %d documents", meili.count())
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aqua_cortex_indexer")
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
        log.exception("indexer crashed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())