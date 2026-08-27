"""Meilisearch document schema. One document == one chunk."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class LiveState:
    """Live swarm-state metadata joined to a chunk in Phase 2.

    Stored inline as a single nested value (not a list) because at most one
    service per chunk matches a real aqua-swarm service. Meilisearch treats
    nested objects as opaque filter targets — we serialize/deserialize via
    `to_dict` / `from_dict` below.
    """

    service: str
    swarm_state: str  # e.g. "1/1", "0/1"
    stack: str
    node: str
    checked_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "service": self.service,
            "swarm_state": self.swarm_state,
            "stack": self.stack,
            "node": self.node,
            "checked_at": self.checked_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "LiveState":
        return cls(
            service=str(d.get("service", "")),
            swarm_state=str(d.get("swarm_state", "")),
            stack=str(d.get("stack", "")),
            node=str(d.get("node", "")),
            checked_at=str(d.get("checked_at", "")),
        )


@dataclass
class CortexDocument:
    """A single indexed chunk.

    `vec` is the dense embedding produced by the configured llama.cpp model.
    Field names match the Meilisearch index settings in `meili.py` so the
    searchable / filterable / sortable attributes line up.
    """

    id: str
    source: str  # "obsidian" | "kanban_card" | "kanban_comment" | "git_commit" | "swarm_event"
    application: str
    doc_path: str
    doc_title: str
    section: str | None
    section_anchor: str | None
    chunk_index: int
    chunk_text: str
    chunk_tokens: int
    vec: list[float]
    linked_services: list[str] = field(default_factory=list)
    linked_cards: list[str] = field(default_factory=list)
    linked_commits: list[str] = field(default_factory=list)
    live_state: LiveState | None = None
    indexed_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )

    def to_dict(self) -> dict[str, Any]:
        """Meilisearch-friendly dict.

        Drops None section so it doesn't pollute the filterable attributes
        with empty-string hits. The dense embedding goes into `_vectors`
        keyed by the configured embedder name (default `default`) — this is
        the contract Meilisearch requires when the embedder is configured
        with `source: userProvided`. See
        https://www.meilisearch.com/docs/learn/vector_search/embeddings
        """
        d: dict[str, Any] = {
            "id": self.id,
            "source": self.source,
            "application": self.application,
            "doc_path": self.doc_path,
            "doc_title": self.doc_title,
            "chunk_index": self.chunk_index,
            "chunk_text": self.chunk_text,
            "chunk_tokens": self.chunk_tokens,
            "linked_services": self.linked_services,
            "linked_cards": self.linked_cards,
            "linked_commits": self.linked_commits,
            "indexed_at": self.indexed_at,
            "_vectors": {"default": self.vec},
        }
        if self.section is not None:
            d["section"] = self.section
        if self.section_anchor is not None:
            d["section_anchor"] = self.section_anchor
        if self.live_state is not None:
            d["live_state"] = self.live_state.to_dict()
        return d


def make_chunk_id(doc_path: str, chunk_index: int) -> str:
    """Stable, content-addressable id. Re-indexing the same chunk produces
    the same id, which lets Meilisearch dedupe via primary-key upsert."""
    h = hashlib.sha256()
    h.update(doc_path.encode("utf-8"))
    h.update(b"\x00")
    h.update(str(chunk_index).encode("utf-8"))
    return h.hexdigest()[:32]