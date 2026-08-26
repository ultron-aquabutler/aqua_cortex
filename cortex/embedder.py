"""llama.cpp /v1/embeddings client (OpenAI-compatible)."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import httpx


log = logging.getLogger("aqua_cortex.embedder")


@dataclass
class Embedder:
    base_url: str
    model: str
    dim: int
    batch_size: int = 16
    timeout: float = 60.0
    max_retries: int = 3
    retry_base_delay: float = 2.0

    def _endpoint(self) -> str:
        base = self.base_url.rstrip("/")
        if not base.endswith("/v1"):
            base = base + "/v1"
        return f"{base}/embeddings"

    def embed(self, texts: list[str]) -> list[list[float] | None]:
        """Return one vector per input text, or None for inputs that the
        upstream refused after retries (the caller decides whether to
        skip them — we never want one bad chunk to kill a 900-chunk run).

        Batches inputs to keep request payloads small (the llama.cpp RPC
        backend on the home swarm has been observed to crash under large
        batches — see https://github.com/ggml-org/llama.cpp RPC
        `Remote RPC server crashed or returned malformed response`. We
        default to batch_size=1 here so a single bad chunk doesn't poison
        the run; raise it cautiously once the RPC backend stabilises).
        On 5xx, retries with exponential backoff; on persistent failure,
        returns None for the whole batch.
        """
        if not texts:
            return []
        out: list[list[float] | None] = []
        with httpx.Client(timeout=self.timeout) as client:
            for i in range(0, len(texts), self.batch_size):
                batch = texts[i : i + self.batch_size]
                out.extend(self._embed_batch_with_retry(client, batch))
        return out

    def _embed_batch_with_retry(
        self, client: httpx.Client, batch: list[str]
    ) -> list[list[float] | None]:
        """Returns one entry per input — None where the input failed after
        all retries. Caller decides whether to skip or fail.
        """
        attempt = 0
        delay = self.retry_base_delay
        while True:
            try:
                resp = client.post(
                    self._endpoint(),
                    json={"input": batch, "model": self.model},
                )
                resp.raise_for_status()
                payload = resp.json()
                data = sorted(payload.get("data", []), key=lambda d: d.get("index", 0))
                vecs: list[list[float] | None] = []
                for item in data:
                    vec = item.get("embedding")
                    if vec is None:
                        vecs.append(None)
                        continue
                    if len(vec) != self.dim:
                        log.warning(
                            "embedder: expected dim=%d, got %d (model changed?)",
                            self.dim,
                            len(vec),
                        )
                    vecs.append(vec)
                # Pad any missing positions so the index matches the batch.
                while len(vecs) < len(batch):
                    vecs.append(None)
                return vecs
            except httpx.HTTPStatusError as exc:
                attempt += 1
                if attempt > self.max_retries:
                    log.error(
                        "embedder: gave up on batch of %d after %d retries (status=%s) — returning None for all",
                        len(batch),
                        self.max_retries,
                        exc.response.status_code,
                    )
                    return [None] * len(batch)
                log.warning(
                    "embedder: batch retry %d/%d after status=%s",
                    attempt,
                    self.max_retries,
                    exc.response.status_code,
                )
                time.sleep(delay)
                delay *= 2