"""Config loader. Reads aqua_cortex.toml from the repo root (or path in $AQUA_CORTEX_CONFIG)."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore


@dataclass
class IndexerConfig:
    """All runtime knobs. No application-specific logic lives here."""

    application: str = ""  # must be set via aqua_cortex.toml or AQUA_CORTEX_APPLICATION env
    vault_paths: list[str] = field(default_factory=list)
    llama_url: str = "http://localhost:18080"
    llama_model: str = "Qwen2.5-1.5B-Instruct-Q4_K_M.gguf"
    meili_url: str = "http://localhost:7700"
    meili_index: str = "aqua_cortex"
    meili_key_file: str = "/run/secrets/meili_master_key"
    chunk_tokens: int = 350
    chunk_overlap: int = 75
    embed_batch_size: int = 16
    embed_dim: int = 1536

    @classmethod
    def _default(cls) -> "IndexerConfig":
        """Build a defaults-only instance, sidestepping the default_factory
        descriptor problem when accessing class-level defaults."""
        return cls()

    @classmethod
    def load(cls, path: str | os.PathLike | None = None) -> "IndexerConfig":
        if path is None:
            path = os.environ.get(
                "AQUA_CORTEX_CONFIG",
                str(Path(__file__).resolve().parent.parent / "aqua_cortex.toml"),
            )
        p = Path(path)
        defaults = cls._default()
        if not p.exists():
            cfg = defaults
            cfg.vault_paths = list(defaults.vault_paths)
            # Fall through to env overrides below.
        else:
            with p.open("rb") as fh:
                data = tomllib.load(fh)
            idx = data.get("indexer", {})
            # Env vars override TOML values (last-wins), so CI / local dev
            # can swap the secret path or endpoint without editing the file.
            cfg = cls(
                application=os.environ.get("AQUA_CORTEX_APPLICATION") or idx.get("application", defaults.application),
                vault_paths=[s.strip() for s in os.environ.get(
                    "AQUA_CORTEX_VAULT_PATHS",
                    ":".join(idx.get("vault_paths", list(defaults.vault_paths))),
                ).split(":") if s.strip()],
                llama_url=os.environ.get("LLAMA_URL") or idx.get("llama_url", defaults.llama_url),
                llama_model=os.environ.get("LLAMA_MODEL") or idx.get("llama_model", defaults.llama_model),
                meili_url=os.environ.get("MEILI_URL") or idx.get("meili_url", defaults.meili_url),
                meili_index=os.environ.get("MEILI_INDEX") or idx.get("meili_index", defaults.meili_index),
                meili_key_file=os.environ.get("MEILI_KEY_FILE")
                or os.environ.get("AQUA_CORTEX_MEILI_KEY_FILE")
                or idx.get("meili_key_file", defaults.meili_key_file),
                chunk_tokens=int(idx.get("chunk_tokens", defaults.chunk_tokens)),
                chunk_overlap=int(idx.get("chunk_overlap", defaults.chunk_overlap)),
                embed_batch_size=int(
                    idx.get("embed_batch_size", defaults.embed_batch_size)
                ),
                embed_dim=int(idx.get("embed_dim", defaults.embed_dim)),
            )
        # Vault paths may also come from env (colon-separated) when no TOML exists.
        if not cfg.vault_paths:
            env_vaults = os.environ.get("AQUA_CORTEX_VAULT_PATHS")
            if env_vaults:
                cfg.vault_paths = [s.strip() for s in env_vaults.split(":") if s.strip()]
        return cfg