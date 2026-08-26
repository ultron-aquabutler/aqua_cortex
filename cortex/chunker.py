"""Markdown chunker. Heading-aware, with token-budget enforcement.

Token counting uses a small regex-based estimator (~ 1 token per 4 chars for
English) so the indexer has zero external dependencies beyond the stdlib. This
keeps the container image slim and CI runs hermetic.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


def estimate_tokens(text: str) -> int:
    """Approximate token count without an external tokenizer.

    Empirical rule of thumb for English + Markdown: 1 token ~ 4 chars. Cheap,
    deterministic, and avoids pulling tiktoken (10MB+) into the runtime image.
    """
    return max(1, len(text) // 4)


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
_PARAGRAPH_RE = re.compile(r"\n\s*\n+")


@dataclass
class Chunk:
    text: str
    section: str | None
    section_anchor: str | None
    chunk_index: int
    token_count: int


def _slugify(heading: str) -> str:
    s = heading.lower().strip()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"\s+", "-", s)
    return s or "section"


def chunk_markdown(
    md: str,
    target_tokens: int = 500,
    overlap_tokens: int = 100,
) -> list[Chunk]:
    """Split markdown into chunks no larger than target_tokens.

    Strategy:
      1. Split on heading boundaries — keep each heading with its content.
      2. Within a section, split on blank-line paragraph breaks until
         target_tokens is respected. We use a tail-overlap window so context
         is preserved across cuts.
      3. Hard cut anything that still exceeds the budget (pathological
         paragraphs like code dumps or huge lists).
    """
    chunks: list[Chunk] = []

    # Walk headings in order, slicing the text into (heading, body) blocks.
    matches = list(_HEADING_RE.finditer(md))
    if not matches:
        sections: list[tuple[str, str]] = [("", md)]
    else:
        sections = []
        if matches[0].start() > 0:
            sections.append(("", md[: matches[0].start()]))
        for i, m in enumerate(matches):
            heading = m.group(2).strip()
            body_start = m.end()
            body_end = matches[i + 1].start() if i + 1 < len(matches) else len(md)
            sections.append((heading, md[body_start:body_end]))

    for section_title, body in sections:
        anchor = f"#{_slugify(section_title)}" if section_title else None

        paragraphs = _PARAGRAPH_RE.split(body)
        paragraphs = [p.strip() for p in paragraphs if p.strip()]

        buffer: list[str] = []
        buf_tokens = 0

        def flush(buf: list[str], section_title: str, anchor: str | None) -> None:
            if not buf:
                return
            text = "\n\n".join(buf)
            chunks.append(
                Chunk(
                    text=text,
                    section=section_title or None,
                    section_anchor=anchor,
                    chunk_index=len(chunks),
                    token_count=estimate_tokens(text),
                )
            )

        for para in paragraphs:
            p_tokens = estimate_tokens(para)

            # Single paragraph exceeds budget — hard-cut it.
            if p_tokens > target_tokens:
                flush(buffer, section_title, anchor)
                buffer = []
                buf_tokens = 0
                step = max(1, target_tokens - overlap_tokens) * 4  # chars
                for i in range(0, len(para), step):
                    piece = para[i : i + target_tokens * 4]
                    if not piece:
                        continue
                    chunks.append(
                        Chunk(
                            text=piece,
                            section=section_title or None,
                            section_anchor=anchor,
                            chunk_index=len(chunks),
                            token_count=estimate_tokens(piece),
                        )
                    )
                continue

            if buf_tokens + p_tokens > target_tokens and buffer:
                flush(buffer, section_title, anchor)
                # Tail-overlap — keep the tail of the previous buffer.
                tail_chars = overlap_tokens * 4
                joined = "\n\n".join(buffer)
                tail = joined[-tail_chars:] if len(joined) > tail_chars else joined
                buffer = [tail] if tail else []
                buf_tokens = estimate_tokens(tail) if tail else 0

            buffer.append(para)
            buf_tokens += p_tokens

        flush(buffer, section_title, anchor)

    # Re-index chunk_index after all flushes.
    for i, c in enumerate(chunks):
        c.chunk_index = i
    return chunks