"""Smoke test for the live-state linker.

Acceptance criterion #3 (Phase 2): the linker runs end-to-end against a
fake Meilisearch + fake live-state sources and produces documents where
>=50% of chunks have at least one of `linked_services`, `linked_cards`,
`linked_commits`, or `live_state` populated.

We reuse the existing `_FakeMeiliState` / `_FakeMeiliServer` pattern from
`test_smoke_index.py` (redefined here — keeping them independent avoids
test coupling). The chunk corpus is built inline (3 small chunks, each
deliberately seeded with content that should match the live-state
fixtures).

Live-state fixtures are provided to the linker by monkey-patching the
`build_swarm`, `load_kanban`, and `load_git` entrypoints to return
hand-built indices without touching docker / sqlite3 / git.

Concretely, the test asserts:
  1. The linker runs without exception.
  2. After running, every chunk in the index has at least one linkage.
  3. A chunk seeded with "supabase_storage" + a known service name
     gains a `live_state.service == "supabase_storage"` field AND a
     non-empty `linked_cards` containing a known `t_<hex>` reference
     (the canonical Phase-2 acceptance check).
"""
from __future__ import annotations

import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlparse


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------------------
# Stub Meilisearch (re-implemented minimally to avoid test coupling)
# ---------------------------------------------------------------------------


class _FakeMeiliState:
    def __init__(self) -> None:
        self.docs: dict[str, dict[str, Any]] = {}  # index -> {"items": [...]}

    def upsert_full(self, index: str, items: list[dict]) -> None:
        bucket = self.docs.setdefault(index, {"items": []})
        by_id = {d["id"]: i for i, d in enumerate(bucket["items"])}
        for it in items:
            if it["id"] in by_id:
                bucket["items"][by_id[it["id"]]] = it
            else:
                bucket["items"].append(it)

    def upsert_partial(self, index: str, items: list[dict]) -> None:
        """Merge fields into existing docs (Meilisearch PATCH / merge=true semantics).

        Per Meilisearch v1.12 docs: "Updates only the specified fields of
        the documents, leaving other fields untouched." `null` values
        REMOVE the field (this is how the linker clears stale
        `live_state` when a service disappears from swarm).
        """
        bucket = self.docs.setdefault(index, {"items": []})
        by_id = {d["id"]: i for i, d in enumerate(bucket["items"])}
        for patch in items:
            i = by_id.get(patch["id"])
            if i is None:
                # New doc — create with only patch fields.
                bucket["items"].append(dict(patch))
                by_id[patch["id"]] = len(bucket["items"]) - 1
                continue
            existing = bucket["items"][i]
            for k, v in patch.items():
                if v is None and k in existing:
                    del existing[k]
                else:
                    existing[k] = v
            # Meilisearch merge=true preserves fields NOT in the patch.
            # The stub must mirror that — nothing else to do here because
            # `existing` already holds every previously-set field.

    def get(self, index: str) -> list[dict]:
        return list(self.docs.get(index, {"items": []})["items"])

    def stats(self, index: str) -> int:
        return len(self.docs.get(index, {"items": []})["items"])

    def search(self, index: str, q: str, limit: int) -> list[dict]:
        # Trivial: match by substring on chunk_text / doc_title.
        bucket = self.docs.get(index, {"items": []})
        hits = []
        q_lower = (q or "").lower()
        for d in bucket["items"]:
            text = (d.get("chunk_text") or "").lower()
            title = (d.get("doc_title") or "").lower()
            if not q_lower or q_lower in text or q_lower in title:
                hits.append({k: v for k, v in d.items() if k != "_vectors"})
        return hits[:limit]

    def fetch_all(self, index: str, batch: int) -> list[dict]:
        bucket = self.docs.get(index, {"items": []})
        return [{k: v for k, v in d.items() if k != "_vectors"} for d in bucket["items"]]


def _make_handler(state: _FakeMeiliState):
    class Handler(BaseHTTPRequestHandler):
        def _json(self, status: int, body: Any) -> None:
            buf = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(buf)))
            self.end_headers()
            self.wfile.write(buf)

        def do_GET(self) -> None:  # noqa: N802
            url = urlparse(self.path)
            if url.path.endswith("/health"):
                self._json(200, {"status": "available"})
                return
            m = re.match(r"^/indexes/([^/]+)/stats$", url.path)
            if m:
                self._json(200, {"numberOfDocuments": state.stats(m.group(1)),
                                 "isIndexing": False})
                return
            # GET /indexes/<idx>/documents — paginated doc fetch. The
            # real Meilisearch endpoint supports `limit`/`offset` query
            # params and returns {"results": [...], "offset": N, ...}.
            # The linker calls this to read back every chunk before
            # computing Phase-2 enrichment, so the stub MUST mirror it.
            m = re.match(r"^/indexes/([^/]+)/documents$", url.path)
            if m:
                idx = m.group(1)
                qs = dict(parse_qsl(url.query, keep_blank_values=True))
                limit = int(qs.get("limit", 20))
                offset = int(qs.get("offset", 0))
                docs = state.fetch_all(idx, batch=limit)
                page = docs[offset:offset + limit]
                self._json(200, {
                    "results": page,
                    "offset": offset,
                    "limit": limit,
                    "total": len(docs),
                })
                return
            self._json(404, {"message": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            url = urlparse(self.path)
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8") if length else ""
            payload = json.loads(body) if body else {}
            if url.path == "/indexes":
                state.docs.setdefault(payload.get("uid", "x"), {"items": []})
                self._json(202, {"taskUid": 0, "status": "enqueued"})
                return
            m = re.match(r"^/indexes/([^/]+)/documents$", url.path)
            if m:
                idx = m.group(1)
                items = payload if isinstance(payload, list) else [payload]
                # Meilisearch v1.12 distinguishes full vs partial by query
                # param `merge=true` on POST /documents (the only path the
                # linker actually uses). Full POST replaces the doc;
                # merge=true preserves fields not in the body.
                if url.query == "merge=true":
                    state.upsert_partial(idx, items)
                else:
                    state.upsert_full(idx, items)
                self._json(202, {"taskUid": 1, "status": "enqueued"})
                return
            m = re.match(r"^/indexes/([^/]+)/search$", url.path)
            if m:
                idx = m.group(1)
                hits = state.search(idx, payload.get("q", ""),
                                    int(payload.get("limit", 5)))
                self._json(200, {
                    "hits": hits,
                    "estimatedTotalHits": len(hits),
                    "query": payload.get("q", ""),
                })
                return
            self._json(404, {"message": "not found"})

        def do_PATCH(self) -> None:  # noqa: N802
            url = urlparse(self.path)
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8") if length else ""
            payload = json.loads(body) if body else {}
            m = re.match(r"^/indexes/([^/]+)/documents$", url.path)
            if m:
                idx = m.group(1)
                items = payload if isinstance(payload, list) else [payload]
                state.upsert_partial(idx, items)
                self._json(202, {"taskUid": 2, "status": "enqueued"})
                return
            m = re.match(r"^/indexes/([^/]+)/settings$", url.path)
            if m:
                self._json(202, {"taskUid": 3, "status": "enqueued"})
                return
            self._json(404, {"message": "not found"})

        def log_message(self, *args: Any, **kwargs: Any) -> None:
            return

    return Handler


class _FakeMeiliServer:
    def __init__(self, state: _FakeMeiliState) -> None:
        self.port = _free_port()
        self.state = state
        handler = _make_handler(state)
        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), handler)
        self.thread = threading.Thread(
            target=self.server.serve_forever, name="fake-meili-2", daemon=True
        )

    def __enter__(self) -> "_FakeMeiliServer":
        self.thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.server.shutdown()
        self.server.server_close()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


# Threading import shim (the global threading import below keeps the
# closure variables lean).


# ---------------------------------------------------------------------------
# Kanban SQLite fixture
# ---------------------------------------------------------------------------


def _make_kanban_db(tmpdir: Path) -> Path:
    """Build a tiny SQLite kanban DB with 3 cards seeded for matching."""
    db = tmpdir / "kanban.db"
    con = sqlite3.connect(str(db))
    con.executescript(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            assignee TEXT,
            priority INTEGER,
            body TEXT
        );
        CREATE TABLE task_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            payload TEXT,
            created_at TEXT NOT NULL
        );
        """
    )
    # Three cards, all `done`, all touched recently.
    now = "2026-08-26T15:00:00+00:00"
    cards = [
        ("t_aaaa111", "Supabase storage bucket migration plan", "done", now),
        ("t_bbbb222", "OpenBalena Traefik routing refactor", "done", now),
        ("t_cccc333", "Pool controller MQTT bridge cleanup", "done", now),
    ]
    for cid, title, status, ts in cards:
        con.execute(
            "INSERT INTO tasks(id,title,status,created_at) VALUES(?,?,?,?)",
            (cid, title, status, ts),
        )
        con.execute(
            "INSERT INTO task_events(task_id,kind,created_at) VALUES(?,?,?)",
            (cid, "completed", ts),
        )
    con.commit()
    con.close()
    return db


# ---------------------------------------------------------------------------
# Git fixture (a tiny throwaway repo)
# ---------------------------------------------------------------------------


def _make_git_repo(tmpdir: Path) -> Path:
    """Init a bare-minimum git repo with 3 commits containing
    deterministic subject lines. Returns the path to the repo dir."""
    repo = tmpdir / "repo"
    repo.mkdir()
    env = os.environ.copy()
    env.update({
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "t@x",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "t@x",
        # Disable any global hooks / GPG signing that might trip the test.
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
    })
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True, env=env)
    for i, msg in enumerate(
        ["supabase storage init script",
         "openbalena s3 routing fix",
         "poolcontroller telemetry endpoint"], start=1
    ):
        f = repo / f"file{i}.txt"
        f.write_text(f"line {i}\n")
        subprocess.run(["git", "-C", str(repo), "add", f.name],
                       check=True, env=env)
        subprocess.run(["git", "-C", str(repo), "commit", "-m", msg],
                       check=True, env=env)
    return repo


# ---------------------------------------------------------------------------
# Chunk corpus (seeded so the linker can deterministically match)
# ---------------------------------------------------------------------------


def _seed_chunks() -> list[dict]:
    """Return 3 chunks designed to hit every linkage type."""
    return [
        {
            "id": "chunk-supabase-storage-0001",
            "source": "obsidian",
            "application": "smoke",
            "doc_path": "Supabase/Storage.md",
            "doc_title": "Supabase Storage",
            "section": "Buckets",
            "section_anchor": "#buckets",
            "chunk_index": 0,
            "chunk_text": (
                "The supabase_storage service backs S3-compatible buckets. "
                "See plan t_aaaa111 for the migration timeline."
            ),
            "chunk_tokens": 30,
            "linked_services": ["supabase_storage"],
            "linked_cards": [],
            "linked_commits": [],
        },
        {
            "id": "chunk-openbalena-traefik-0001",
            "source": "obsidian",
            "application": "smoke",
            "doc_path": "OpenBalena/Traefik.md",
            "doc_title": "OpenBalena Traefik",
            "section": "Routing",
            "section_anchor": "#routing",
            "chunk_index": 0,
            "chunk_text": (
                "openbalena_traefik-sidecar terminates TLS for the openbalena "
                "fleet. Card t_bbbb222 tracks the s3.aquabutlers.com work."
            ),
            "chunk_tokens": 28,
            "linked_services": ["openbalena_traefik-sidecar"],
            "linked_cards": [],
            "linked_commits": [],
        },
        {
            "id": "chunk-poolcontroller-mqtt-0001",
            "source": "obsidian",
            "application": "smoke",
            "doc_path": "PoolController/MQTT.md",
            "doc_title": "PoolController MQTT",
            "section": "Bridge",
            "section_anchor": "#bridge",
            "chunk_index": 0,
            "chunk_text": (
                "mosquitto_broker-aqua1 hosts the poolcontroller telemetry "
                "topic. Cleanup tracked under t_cccc333."
            ),
            "chunk_tokens": 24,
            "linked_services": ["mosquitto_broker-aqua1"],
            "linked_cards": [],
            "linked_commits": [],
        },
    ]


def _seed_swarm_file(tmpdir: Path) -> Path:
    """Write a tiny swarm snapshot for the linker to load via
    $AQUA_CORTEX_SWARM_FILE (skips the live Docker call)."""
    f = tmpdir / "swarm.json"
    f.write_text(json.dumps({
        "supabase_storage": {
            "service": "supabase_storage",
            "swarm_state": "1/1",
            "stack": "supabase",
            "node": "aqua3",
            "checked_at": "2026-08-26T15:30:00+00:00",
        },
        "openbalena_traefik-sidecar": {
            "service": "openbalena_traefik-sidecar",
            "swarm_state": "1/1",
            "stack": "openbalena",
            "node": "aqua1",
            "checked_at": "2026-08-26T15:30:00+00:00",
        },
        "mosquitto_broker-aqua1": {
            "service": "mosquitto_broker-aqua1",
            "swarm_state": "1/1",
            "stack": "mosquitto",
            "node": "aqua1",
            "checked_at": "2026-08-26T15:30:00+00:00",
        },
    }))
    return f


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------


class LinkSmokeTest(unittest.TestCase):
    def test_linker_enriches_chunks_with_live_state(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent

        # 1. Spin up fake meili + seed chunks.
        state = _FakeMeiliState()
        run_id = f"link-{int.from_bytes(os.urandom(4), 'big')}"
        with _FakeMeiliServer(state) as meili:
            seed = _seed_chunks()
            state.upsert_full(run_id, seed)

            # 2. Build fixture kanban DB + git repo + swarm file in tmpdir.
            with tempfile.TemporaryDirectory() as tmpd:
                tmp = Path(tmpd)
                kanban_db = _make_kanban_db(tmp)
                git_repo = _make_git_repo(tmp)
                swarm_file = _seed_swarm_file(tmp)

                # 3. Set env so the linker uses fixtures + points at fake meili.
                env = os.environ.copy()
                env.update({
                    "LLAMA_URL": "http://127.0.0.1:1",   # unused; linker doesn't embed
                    "LLAMA_MODEL": "stub",
                    "MEILI_URL": meili.base_url,
                    "MEILI_INDEX": run_id,
                    # Don't set MEILI_KEY_FILE — load_api_key falls back to env.
                    "AQUA_CORTEX_MEILI_KEY": "smoke-key",
                    "AQUA_CORTEX_KANBAN_DB": str(kanban_db),
                    "AQUA_CORTEX_REPO": str(git_repo),
                    "AQUA_CORTEX_SWARM_FILE": str(swarm_file),
                    "PYTHONPATH": str(repo_root),
                    "PYTHONUNBUFFERED": "1",
                })

                # 4. Run the linker.
                proc = subprocess.run(
                    [sys.executable, "link_live_state.py"],
                    cwd=str(repo_root),
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                if proc.returncode != 0:
                    self.fail(
                        "linker exited non-zero\n"
                        f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
                    )

                # 5. Assert all chunks got at least one linkage.
                final = state.get(run_id)
                self.assertEqual(len(final), 3, f"expected 3 chunks, got {final}")
                linked_count = sum(
                    1 for d in final
                    if d.get("linked_services") or d.get("linked_cards")
                    or d.get("linked_commits") or d.get("live_state")
                )
                self.assertGreaterEqual(
                    linked_count, 2,
                    f"expected >=2/3 chunks linked, got {linked_count}/{len(final)}\n"
                    f"chunks: {final}",
                )
                pct = 100.0 * linked_count / len(final)
                self.assertGreaterEqual(
                    pct, 50.0,
                    f"expected >=50% linked, got {pct:.1f}%",
                )

                # 6. The canonical Phase-2 check: the supabase chunk must
                #    have `live_state.service == "supabase_storage"` AND
                #    `linked_cards` containing t_aaaa111.
                supe = next(
                    d for d in final
                    if "supabase" in d.get("doc_path", "").lower()
                )
                ls = supe.get("live_state") or {}
                self.assertEqual(
                    ls.get("service"), "supabase_storage",
                    f"expected live_state.service == 'supabase_storage', got {ls}",
                )
                self.assertIn(
                    "t_aaaa111", supe.get("linked_cards", []),
                    f"expected t_aaaa111 in linked_cards, got {supe.get('linked_cards')}",
                )
                self.assertRegex(
                    ls.get("swarm_state", ""), r"^\d+/\d+$",
                    f"expected swarm_state like '1/1', got {ls.get('swarm_state')!r}",
                )

                # 7. Git linkage: the supabase chunk should also pick up
                #    the supabase-storage commit because the chunk text
                #    shares >=2 tokens ("supabase", "storage") with the
                #    commit subject.
                self.assertTrue(
                    supe.get("linked_commits"),
                    f"expected non-empty linked_commits, got {supe.get('linked_commits')}",
                )

                # 8. Explicit t_<hex> extraction: t_bbbb222 should land
                #    on the openbalena chunk even though its title tokens
                #    alone wouldn't trigger the 2-token threshold.
                ob = next(d for d in final if "openbalena" in d.get("doc_path", "").lower())
                self.assertIn(
                    "t_bbbb222", ob.get("linked_cards", []),
                    f"expected explicit t_bbbb222 match, got {ob.get('linked_cards')}",
                )


if __name__ == "__main__":
    unittest.main()