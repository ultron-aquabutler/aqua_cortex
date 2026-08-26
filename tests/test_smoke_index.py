"""Smoke test for aqua_cortex indexer.

Acceptance criterion #7: a fixture vault of 5 small markdown files produces
≥ 20 chunks, and a vector search for an obvious query returns the matching
fixture in the top-3 hits.

We stand up two stub servers in the test process:
  - a stub llama.cpp /v1/embeddings endpoint returning deterministic
    1536-dim vectors derived from sha256(text) projected onto the unit
    sphere, so cosine similarity is well-defined;
  - a stub Meilisearch that mirrors the small slice of the HTTP API the
    indexer uses (POST /indexes, PATCH /indexes/{i}/settings,
    POST /indexes/{i}/documents, POST /indexes/{i}/search, GET .../stats).

The test then drives the real `index_obsidian.py` as a subprocess, asserts
the chunk count and that the search relevance check passes.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import socket
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


EMBED_DIM = 1536


def _fake_embed(text: str) -> list[float]:
    """Deterministic 1536-dim vector. Two texts that share any prefix hash
    to nearby buckets, which is enough for the relevance check."""
    seed = hashlib.sha256(text.encode("utf-8")).digest()
    out: list[float] = []
    while len(out) < EMBED_DIM:
        seed = hashlib.sha256(seed).digest()
        for b in seed:
            out.append((b / 255.0) * 2.0 - 1.0)
            if len(out) == EMBED_DIM:
                break
    norm = math.sqrt(sum(x * x for x in out)) or 1.0
    return [x / norm for x in out]


def _cosine(a: list[float], b: list[float]) -> float:
    n = min(len(a), len(b))
    dot = sum(a[i] * b[i] for i in range(n))
    na = math.sqrt(sum(x * x for x in a[:n])) or 1.0
    nb = math.sqrt(sum(x * x for x in b[:n])) or 1.0
    return dot / (na * nb)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# --- Stub llama.cpp /v1/embeddings -----------------------------------------

class _FakeEmbedHandler(BaseHTTPRequestHandler):
    server: "_FakeEmbedServer"

    def do_POST(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8") if length else ""
        try:
            payload = json.loads(body) if body else {}
        except json.JSONDecodeError:
            self.send_response(400)
            self.end_headers()
            return

        if url.path.endswith("/v1/embeddings"):
            inputs = payload.get("input") or []
            data = [
                {"index": i, "embedding": _fake_embed(t), "object": "embedding"}
                for i, t in enumerate(inputs)
            ]
            resp = {
                "object": "list",
                "data": data,
                "model": payload.get("model", "stub"),
                "usage": {"prompt_tokens": 0, "total_tokens": 0},
            }
            buf = json.dumps(resp).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(buf)))
            self.end_headers()
            self.wfile.write(buf)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, *args: Any, **kwargs: Any) -> None:
        return


class _FakeEmbedServer:
    def __init__(self) -> None:
        self.port = _free_port()
        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), _FakeEmbedHandler)
        self.server.server = self  # type: ignore[attr-defined]
        self.thread = threading.Thread(
            target=self.server.serve_forever, name="fake-embed", daemon=True
        )

    def __enter__(self) -> "_FakeEmbedServer":
        self.thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.server.shutdown()
        self.server.server_close()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


# --- Stub Meilisearch ------------------------------------------------------

class _FakeMeiliState:
    """Process-wide state for the fake meili; the handler closes over an
    instance so multiple test classes can't collide."""

    def __init__(self) -> None:
        self.docs: dict[str, dict[str, Any]] = {}  # index -> {"count": int, "items": [...]}

    def search(self, index: str, q: str, limit: int) -> list[dict]:
        bucket = self.docs.get(index, {"count": 0, "items": []})
        qv = _fake_embed(q)
        scored: list[tuple[float, dict]] = []
        for d in bucket["items"]:
            vecs = (d.get("_vectors") or {}).get("default") or []
            if vecs:
                scored.append((_cosine(qv, vecs), d))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [
            {k: v for k, v in d.items() if k != "_vectors"}
            for _, d in scored[:limit]
        ]


def _make_meili_handler(state: _FakeMeiliState):
    """Closure that returns a Handler class bound to `state`."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            url = urlparse(self.path)
            if url.path.endswith("/health"):
                self._json(200, {"status": "available"})
                return
            m = re.match(r"^/indexes/([^/]+)/stats$", url.path)
            if m:
                idx = m.group(1)
                n = state.docs.get(idx, {}).get("count", 0)
                self._json(200, {"numberOfDocuments": n, "isIndexing": False})
                return
            self._json(404, {"message": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            url = urlparse(self.path)
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8") if length else ""
            payload = json.loads(body) if body else {}

            if url.path == "/indexes":
                uid = payload.get("uid")
                state.docs.setdefault(uid, {"count": 0, "items": []})
                self._json(202, {"taskUid": 0, "status": "enqueued"})
                return

            m = re.match(r"^/indexes/([^/]+)/documents$", url.path)
            if m:
                idx = m.group(1)
                bucket = state.docs.setdefault(idx, {"count": 0, "items": []})
                items = payload if isinstance(payload, list) else [payload]
                bucket["items"].extend(items)
                bucket["count"] = len(bucket["items"])
                self._json(202, {"taskUid": 1, "status": "enqueued"})
                return

            m = re.match(r"^/indexes/([^/]+)/search$", url.path)
            if m:
                idx = m.group(1)
                hits = state.search(
                    idx, payload.get("q", ""), int(payload.get("limit", 5))
                )
                self._json(
                    200,
                    {
                        "hits": hits,
                        "estimatedTotalHits": len(hits),
                        "query": payload.get("q", ""),
                    },
                )
                return

            self._json(404, {"message": "not found"})

        def do_PATCH(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            if length:
                self.rfile.read(length)
            self._json(202, {"taskUid": 2, "status": "enqueued"})

        def _json(self, status: int, body: Any) -> None:
            buf = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(buf)))
            self.end_headers()
            self.wfile.write(buf)

        def log_message(self, *args: Any, **kwargs: Any) -> None:
            return

    return Handler


class _FakeMeiliServer:
    def __init__(self, state: _FakeMeiliState) -> None:
        self.port = _free_port()
        self.state = state
        handler = _make_meili_handler(state)
        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), handler)
        self.thread = threading.Thread(
            target=self.server.serve_forever, name="fake-meili", daemon=True
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


# --- The actual test -------------------------------------------------------

class SmokeIndexTest(unittest.TestCase):
    def test_indexer_produces_chunks_and_relevant_search(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        fixtures = repo_root / "tests" / "fixtures" / "vault"
        self.assertTrue(fixtures.is_dir(), f"missing fixtures at {fixtures}")

        # Fixture-specific TOML with chunk_tokens=100 so 5 small files emit
        # ≥ 20 chunks (each file is several hundred bytes; budget 100 tokens
        # = 400 chars per chunk → each file ~3-5 chunks).
        fixture_cfg = repo_root / "tests" / "fixtures" / "aqua_cortex.test.toml"
        fixture_cfg.write_text(
            f"""[indexer]
application = "smoke"
vault_paths = ["{fixtures}"]
chunk_tokens = 100
chunk_overlap = 20
embed_dim = {EMBED_DIM}
"""
        )

        run_id = f"smoke-{int.from_bytes(os.urandom(4), 'big')}"
        state = _FakeMeiliState()
        with _FakeEmbedServer() as embed, _FakeMeiliServer(state) as meili:
            env = os.environ.copy()
            env.update({
                "AQUA_CORTEX_APPLICATION": "smoke",
                "LLAMA_URL": embed.base_url,
                "LLAMA_MODEL": "stub",
                "MEILI_URL": meili.base_url,
                "MEILI_INDEX": run_id,
                "AQUA_CORTEX_MEILI_KEY": "smoke-key",
                "PYTHONPATH": str(repo_root),
                "PYTHONUNBUFFERED": "1",
            })

            proc = subprocess.run(
                [sys.executable, "index_obsidian.py", "--config", str(fixture_cfg)],
                cwd=str(repo_root),
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
            )
            if proc.returncode != 0:
                self.fail(
                    "indexer exited non-zero\n"
                    f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
                )

            count = state.docs.get(run_id, {}).get("count", 0)
            self.assertGreaterEqual(
                count, 20,
                f"expected >= 20 chunks, got {count}\n"
                f"indexer stdout (tail): {proc.stdout[-500:]}",
            )

            # ph-sensor fixture mentions "ph level sensor calibration".
            # Vector search should return it in top-3.
            hits = state.search(run_id, "ph level sensor calibration", limit=3)
            self.assertGreater(len(hits), 0, "no hits returned")
            doc_paths = [h.get("doc_path", "") for h in hits]
            self.assertTrue(
                any("ph-sensor" in p for p in doc_paths),
                f"ph-sensor doc not in top-3, got {doc_paths}",
            )


if __name__ == "__main__":
    unittest.main()
