"""Smoke test for the `hermes aqua-cortex` CLI plugin.

Acceptance (Phase 3, Lane B):
  * `query "<question>"` exits 0, prints answer text, prints ≥1 source
    citation.
  * `ping` exits 0 when both Meilisearch and llama.cpp are reachable.
  * `snapshot` emits valid JSON with the expected sections.

Strategy
--------
We stand up a fake Meilisearch + fake llama.cpp on free localhost ports
(using the same `_FakeServer` pattern from `test_smoke_index.py`) and
point the CLI at them via env overrides. The fake llama returns a
deterministic vector; the fake Meilisearch holds a small corpus with
realistic chunk metadata so the citation parser has something to chew on.

This test does NOT talk to the live home swarm — it must pass offline
and on CI. The live acceptance check happens at deploy time via
`hermes aqua-cortex ping`.
"""

from __future__ import annotations

import json
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
from urllib.parse import parse_qsl, urlparse


REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN = REPO_ROOT / "hermes-plugin" / "hermes_aqua_cortex.py"

EMBED_DIM = 1536


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _fake_embed(text: str) -> list[float]:
    """Deterministic 1536-dim unit vector."""
    import hashlib
    seed = hashlib.sha256(text.encode("utf-8")).digest()
    out: list[float] = []
    while len(out) < EMBED_DIM:
        seed = hashlib.sha256(seed).digest()
        for b in seed:
            out.append((b / 255.0) * 2.0 - 1.0)
            if len(out) == EMBED_DIM:
                break
    norm = sum(x * x for x in out) ** 0.5 or 1.0
    return [x / norm for x in out]


# ---------------------------------------------------------------------------
# Fake Meilisearch
# ---------------------------------------------------------------------------


_CORPUS = [
    {
        "id": "doc-001",
        "source": "obsidian",
        "application": "pool",
        "doc_path": "Mosquitto/index.md",
        "doc_title": "Mosquitto MQTT Cluster",
        "section": "Cluster Topology",
        "section_anchor": "#cluster-topology",
        "chunk_index": 0,
        "chunk_text": (
            "Mosquitto cluster runs two independent broker instances "
            "(broker-aqua1, broker-aqua2) on Aqua Swarm behind a "
            "Traefik TCP load balancer."
        ),
        "chunk_tokens": 24,
        "linked_services": ["mosquitto_broker-aqua1", "mosquitto_broker-aqua2"],
        "linked_cards": ["t_6823b7e9"],
        "linked_commits": ["abc1234"],
        "live_state": {
            "service": "mosquitto_broker-aqua1",
            "swarm_state": "1/1",
            "stack": "mosquitto",
            "node": "Aqua1",
            "checked_at": "2026-08-26T15:45:50+00:00",
        },
        "indexed_at": "2026-08-26T14:45:13+00:00",
    },
    {
        "id": "doc-002",
        "source": "obsidian",
        "application": "pool",
        "doc_path": "Supabase/index.md",
        "doc_title": "Supabase Stack",
        "section": "Overview",
        "section_anchor": "#overview",
        "chunk_index": 0,
        "chunk_text": (
            "Supabase runs on Aqua Swarm with bind mounts from TrueNAS "
            "at 10.0.10.100. All four services are worker-only."
        ),
        "chunk_tokens": 21,
        "linked_services": ["supabase_auth", "supabase_storage"],
        "linked_cards": ["t_80557f7d"],
        "linked_commits": ["def5678"],
        "live_state": {
            "service": "supabase_auth",
            "swarm_state": "1/1",
            "stack": "supabase",
            "node": "Aqua4",
            "checked_at": "2026-08-26T15:45:50+00:00",
        },
        "indexed_at": "2026-08-26T14:45:14+00:00",
    },
    {
        "id": "doc-003",
        "source": "kanban_comment",
        "application": "pool",
        "doc_path": "",
        "doc_title": "kanban comment",
        "section": None,
        "section_anchor": "",
        "chunk_index": 0,
        "chunk_text": "Patroni failover is configured via DCS, see cards t_x and t_y.",
        "chunk_tokens": 12,
        "linked_services": [],
        "linked_cards": ["t_patroni1", "t_patroni2"],
        "linked_commits": [],
        "live_state": None,
        "indexed_at": "2026-08-26T14:46:00+00:00",
    },
]


class _FakeMeiliState:
    def __init__(self) -> None:
        self.docs = {d["id"]: d for d in _CORPUS}


class _FakeMeiliHandler(BaseHTTPRequestHandler):
    state: _FakeMeiliState = _FakeMeiliState()  # class-level for the server

    def log_message(self, format: str, *args: Any) -> None:  # silence noisy stderr
        return

    def _json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        url = urlparse(self.path)
        if url.path == "/health":
            self._json(200, {"status": "available"})
            return
        if url.path == "/indexes/aqua_cortex/stats":
            self._json(200, {"numberOfDocuments": len(self.state.docs)})
            return
        if url.path == "/indexes/aqua_cortex/documents":
            qs = dict(parse_qsl(url.query))
            limit = int(qs.get("limit", "20"))
            offset = int(qs.get("offset", "0"))
            items = list(self.state.docs.values())[offset : offset + limit]
            self._json(200, {"results": items, "offset": offset, "limit": limit})
            return
        self._json(404, {"error": "not_found", "path": url.path})

    def do_POST(self) -> None:
        url = urlparse(self.path)
        length = int(self.headers.get("Content-Length", "0"))
        body_raw = self.rfile.read(length).decode("utf-8") if length else "{}"
        try:
            body = json.loads(body_raw)
        except Exception:
            body = {}

        if url.path == "/indexes/aqua_cortex/search":
            # Naive lexical match for the test corpus.
            q = (body.get("q") or "").lower()
            ranked = []
            for d in self.state.docs.values():
                score = 0
                hay = " ".join([
                    d.get("doc_title") or "",
                    d.get("section") or "",
                    d.get("chunk_text") or "",
                ]).lower()
                for tok in q.split():
                    if tok in hay:
                        score += 1
                if score:
                    ranked.append((score, d))
            ranked.sort(key=lambda r: r[0], reverse=True)
            limit = int(body.get("limit", 5))
            hits = [
                {**d, "_rankingScore": 1.0 - i * 0.01}
                for i, (_, d) in enumerate(ranked[:limit])
            ]
            self._json(200, {"hits": hits, "limit": limit, "estimatedTotalHits": len(hits)})
            return

        self._json(404, {"error": "not_found", "path": url.path})


# ---------------------------------------------------------------------------
# Fake llama.cpp
# ---------------------------------------------------------------------------


class _FakeLlamaHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        return

    def _json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if urlparse(self.path).path == "/v1/models":
            self._json(200, {"data": [{"id": "Qwen2.5-1.5B-Instruct-Q4_K_M.gguf"}]})
            return
        self._json(404, {"error": "not_found"})

    def do_POST(self) -> None:
        url = urlparse(self.path)
        length = int(self.headers.get("Content-Length", "0"))
        body_raw = self.rfile.read(length).decode("utf-8") if length else "{}"
        try:
            body = json.loads(body_raw)
        except Exception:
            body = {}

        if url.path == "/v1/embeddings":
            inputs = body.get("input") or []
            if isinstance(inputs, str):
                inputs = [inputs]
            data = [{"embedding": _fake_embed(i), "index": idx} for idx, i in enumerate(inputs)]
            self._json(200, {"data": data, "model": body.get("model")})
            return

        if url.path == "/v1/chat/completions":
            # Return a deliberately degenerate response to exercise the
            # extractive-fallback path. The CLI must still exit 0 with
            # ≥1 source citation.
            content = "to to to to to to to to to to to to to to to to to"
            self._json(200, {
                "choices": [{
                    "finish_reason": "stop",
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                }],
                "model": body.get("model"),
                "usage": {"completion_tokens": len(content.split())},
            })
            return

        self._json(404, {"error": "not_found", "path": url.path})


# ---------------------------------------------------------------------------
# Test harness
# ---------------------------------------------------------------------------


def _run_cli(*args: str, env: dict[str, str], timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(PLUGIN), *args],
        env=env, capture_output=True, text=True, timeout=timeout,
    )


def _make_key_file(tmpdir: Path) -> str:
    p = tmpdir / "meili_key"
    p.write_text("test-key-not-secret\n")
    return str(p)


class TestHermesAquaCortexCLI(unittest.TestCase):
    """Smoke tests for the `hermes aqua-cortex` plugin.

    These tests start fake Meilisearch + fake llama.cpp on free ports
    and exercise the CLI against them. No network, no live state.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.meili_port = _free_port()
        cls.llama_port = _free_port()

        cls.meili_server = ThreadingHTTPServer(("127.0.0.1", cls.meili_port), _FakeMeiliHandler)
        cls.llama_server = ThreadingHTTPServer(("127.0.0.1", cls.llama_port), _FakeLlamaHandler)

        cls._meili_thread = threading.Thread(target=cls.meili_server.serve_forever, daemon=True)
        cls._llama_thread = threading.Thread(target=cls.llama_server.serve_forever, daemon=True)
        cls._meili_thread.start()
        cls._llama_thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.meili_server.shutdown()
        cls.llama_server.shutdown()
        cls.meili_server.server_close()
        cls.llama_server.server_close()

    def _env(self, tmp_key_file: str) -> dict[str, str]:
        env = os.environ.copy()
        env["HERMES_AQUA_CORTEX_MEILI_URL"] = f"http://127.0.0.1:{self.meili_port}"
        env["HERMES_AQUA_CORTEX_MEILI_INDEX"] = "aqua_cortex"
        env["HERMES_AQUA_CORTEX_MEILI_KEY_FILE"] = tmp_key_file
        env["HERMES_AQUA_CORTEX_LLAMA_URL"] = f"http://127.0.0.1:{self.llama_port}"
        env["HERMES_AQUA_CORTEX_LLAMA_MODEL"] = "Qwen2.5-1.5B-Instruct-Q4_K_M.gguf"
        env["HERMES_AQUA_CORTEX_VERIFY_TLS"] = "0"
        # The repo's aqua_cortex.toml points at the home-swarm overlay
        # hostname; override so the CLI never tries DNS for it.
        env["AQUA_CORTEX_CONFIG"] = str(REPO_ROOT / "aqua_cortex.toml")
        return env

    def test_query_exits_zero_and_cites(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            key_file = _make_key_file(Path(td))
            env = self._env(key_file)
            proc = _run_cli(
                "query", "what is the mosquitto cluster topology",
                env=env, timeout=30,
            )
            self.assertEqual(
                proc.returncode, 0,
                msg=f"CLI exited {proc.returncode}\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}",
            )
            # Body has answer text + a Sources: section with ≥1 citation.
            self.assertIn("Sources:", proc.stdout)
            sources_section = proc.stdout.split("Sources:", 1)[1]
            citation_lines = [
                ln.strip() for ln in sources_section.splitlines()
                if ln.strip().startswith("- ")
            ]
            self.assertGreaterEqual(
                len(citation_lines), 1,
                msg=f"Expected ≥1 citation; got {citation_lines!r}",
            )
            # At least one citation should be an obsidian doc path, since
            # the corpus has docs for mosquitto and supabase.
            self.assertTrue(
                any("Mosquitto" in c or "Supabase" in c for c in citation_lines),
                msg=f"No doc citations in {citation_lines!r}",
            )

    def test_ping_exits_zero(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            key_file = _make_key_file(Path(td))
            env = self._env(key_file)
            proc = _run_cli("ping", env=env, timeout=15)
            self.assertEqual(proc.returncode, 0, msg=proc.stderr)
            self.assertIn("meili  : OK", proc.stdout)
            self.assertIn("llama  : OK", proc.stdout)

    def test_snapshot_emits_valid_json_with_sections(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            key_file = _make_key_file(Path(td))
            env = self._env(key_file)
            proc = _run_cli("snapshot", env=env, timeout=15)
            self.assertEqual(proc.returncode, 0, msg=proc.stderr)
            data = json.loads(proc.stdout)
            for key in ("doc_vs_running", "doc_freshness", "recent_activity", "counts"):
                self.assertIn(key, data, msg=f"snapshot missing {key}")
            self.assertGreater(data["counts"]["documents"], 0)


if __name__ == "__main__":
    unittest.main()
