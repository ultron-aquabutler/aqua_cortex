"""Meilisearch index management + upload."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import httpx


log = logging.getLogger("aqua_cortex.meili")


@dataclass
class MeiliClient:
    base_url: str
    index: str
    api_key: str
    dim: int

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    def health(self) -> bool:
        with httpx.Client(timeout=10.0) as client:
            r = client.get(f"{self.base_url.rstrip('/')}/health")
            return r.status_code == 200 and r.json().get("status") == "available"

    def ensure_index(self) -> None:
        """Create the index (if missing) and apply searchable / filterable
        settings + vector embedder config."""
        base = self.base_url.rstrip("/")
        with httpx.Client(timeout=30.0) as client:
            # Create or no-op.
            r = client.post(
                f"{base}/indexes",
                json={"uid": self.index, "primaryKey": "id"},
                headers=self._headers(),
            )
            if r.status_code not in (200, 201, 202, 400, 404):
                r.raise_for_status()
            # 400 with code 'index_already_exists' is fine.
            if r.status_code == 400:
                body = r.json()
                if body.get("code") != "index_already_exists":
                    log.warning("ensure_index: unexpected 400 body=%s", body)

            # Settings: searchable / filterable / sortable.
            # NOTE: For `source: "userProvided"` Meilisearch does NOT
            # accept a `distance` field — that only applies to ollama /
            # rest / huggingface sources. Cosine is the default for
            # userProvided vectors so we omit it.
            settings = {
                "searchableAttributes": [
                    "chunk_text",
                    "doc_title",
                    "section",
                    "linked_services",
                    "linked_cards",
                    "linked_commits",
                    "live_state.service",
                    "live_state.stack",
                ],
                "filterableAttributes": [
                    "application",
                    "source",
                    "linked_services",
                    "linked_cards",
                    "linked_commits",
                    "live_state.service",
                    "live_state.swarm_state",
                    "live_state.stack",
                    "live_state.node",
                    "indexed_at",
                ],
                "sortableAttributes": ["indexed_at", "chunk_index"],
                "embedders": {
                    "default": {
                        "source": "userProvided",
                        "dimensions": self.dim,
                    }
                },
            }
            r = client.patch(
                f"{base}/indexes/{self.index}/settings",
                json=settings,
                headers=self._headers(),
            )
            if r.status_code not in (200, 202):
                log.warning(
                    "ensure_index: settings PATCH returned %s body=%s",
                    r.status_code,
                    r.text[:200],
                )

    def upsert_documents(self, docs: list[dict]) -> None:
        if not docs:
            return
        base = self.base_url.rstrip("/")
        with httpx.Client(timeout=60.0) as client:
            r = client.post(
                f"{base}/indexes/{self.index}/documents",
                json=docs,
                headers=self._headers(),
            )
            r.raise_for_status()

    def upsert_partial_documents(self, docs: list[dict]) -> None:
        """Update a batch of documents, preserving the dense embedding.

        Meilisearch v1.12 does NOT support partial-update endpoints
        (PATCH and POST with `merge=true` both 405/400 respectively —
        the merge parameter shipped in v1.13). We work around this by:

          1. POSTing the full document with `_vectors` stripped.
          2. Meilisearch preserves the existing embedding server-side
             because the request body never claims to update it
             (verified live 2026-08-26 against `aqua_cortex` index).

        This means each input `doc` MUST be a complete document (every
        field Meilisearch has on the original). The caller (the linker)
        builds these by reading back the existing hit and overlaying
        its new `linked_*` / `live_state` fields.

        Setting a field to `null` in the body REMOVES that field on
        the server — used by the linker to clear stale `live_state`
        when a service disappears from the swarm.
        """
        if not docs:
            return
        # Strip _vectors from the body — server-side preservation handles
        # the embedding; sending it would just waste bandwidth.
        payload: list[dict] = []
        for d in docs:
            cleaned = {k: v for k, v in d.items() if k != "_vectors"}
            payload.append(cleaned)
        base = self.base_url.rstrip("/")
        with httpx.Client(timeout=60.0) as client:
            r = client.post(
                f"{base}/indexes/{self.index}/documents",
                json=payload,
                headers=self._headers(),
            )
            r.raise_for_status()

    def count(self) -> int:
        with httpx.Client(timeout=10.0) as client:
            r = client.get(
                f"{self.base_url.rstrip('/')}/indexes/{self.index}/stats",
                headers=self._headers(),
            )
            r.raise_for_status()
            return int(r.json().get("numberOfDocuments", 0))

    def search(
        self,
        query: str,
        limit: int = 5,
        filter_application: str | None = None,
    ) -> list[dict]:
        body: dict = {"q": query, "limit": limit}
        if filter_application:
            body["filter"] = f'application = "{filter_application}"'
        with httpx.Client(timeout=15.0) as client:
            r = client.post(
                f"{self.base_url.rstrip('/')}/indexes/{self.index}/search",
                json=body,
                headers=self._headers(),
            )
            r.raise_for_status()
            return r.json().get("hits", [])


def load_api_key(path: str) -> str:
    """Read the master key.

    Lookup order:
      1. If `path` is non-empty and the file exists, read it (this is the
         Swarm secret path, mounted at /run/secrets/<name>).
      2. Else `AQUA_CORTEX_MEILI_KEY` env var.
      3. Else raise — explicit failure beats silent empty-string auth.
    """
    if path and os.path.exists(path):
        with open(path) as fh:
            return fh.read().strip()
    env = os.environ.get("AQUA_CORTEX_MEILI_KEY")
    if env:
        return env.strip()
    raise RuntimeError(
        f"meili: no key found at {path!r} and AQUA_CORTEX_MEILI_KEY env var unset"
    )