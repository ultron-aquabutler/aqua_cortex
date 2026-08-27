"""Local kanban DB reader.

Reads the SQLite kanban database (single-board, default `aquabutlers`),
extracts recent cards, builds a title-fragment index for fast lookup, and
exposes helpers for matching chunks against cards.

Design choices:

  - We talk to sqlite3 directly (no ORM) because the schema is small,
    stable, and the kanban DB is local-only. Adding SQLAlchemy for one
    reader would be overkill.

  - We read ONLY the `tasks` table for the title index. `task_comments`
    is reserved for Phase-3 RAG context — Phase 2 just joins titles.

  - Card IDs are matched two ways:

      (a) Explicit `t_<hex>` references extracted from chunk text via
          regex. These are unambiguous.

      (b) Title-fragment similarity: split each card title into
          lowercase alpha tokens (length >= 3, deduplicated), and
          consider a card "linked" if at least 2 of its tokens appear
          in the chunk text.

    The 2-token threshold filters out trivially-common words like
    "the", "and", "set", "fix" while still catching
    "supabase auth" / "supabase storage" etc.

  - We index the LAST 30 DAYS only, per the spec. Older cards are
    ignored — the linker is for live operational context, not archive
    search.
"""
from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path


log = logging.getLogger("aqua_cortex.kanban_state")


_CARD_ID_RE = re.compile(r"\bt_[0-9a-f]{6,16}\b")
_TOKEN_RE = re.compile(r"[a-z0-9]{3,}")
_MIN_TITLE_TOKENS = 2  # need at least 2 tokens present in the chunk


@dataclass
class KanbanCard:
    id: str
    title: str
    status: str
    indexed_at: str  # ISO8601 UTC second-precision
    tokens: list[str] = field(default_factory=list)  # deduped, lowercased

    def matches_text(self, text: str) -> bool:
        """Return True iff >= _MIN_TITLE_TOKENS of this card's tokens
        appear in `text` (case-insensitive)."""
        if not self.tokens:
            return False
        lower = text.lower()
        hits = sum(1 for tok in self.tokens if tok in lower)
        return hits >= _MIN_TITLE_TOKENS


@dataclass
class KanbanIndex:
    cards: list[KanbanCard] = field(default_factory=list)
    by_id: dict[str, KanbanCard] = field(default_factory=dict)
    by_token: dict[str, set[str]] = field(default_factory=dict)
    cutoff: str = ""

    def extract_explicit_ids(self, text: str) -> set[str]:
        """Pull `t_<hex>` references out of chunk text. Always case-insensitive
        on the hex part — we normalise to lowercase before lookup."""
        return {m.group(0).lower() for m in _CARD_ID_RE.finditer(text)}

    def find_matches(self, text: str) -> list[str]:
        """Return card ids matched against `text`.

        Combines:
          - explicit `t_<hex>` ids (must exist in `by_id`),
          - title-fragment similarity (>= _MIN_TITLE_TOKENS tokens present).
        Deduped, deterministic order.
        """
        seen: set[str] = set()
        ordered: list[str] = []

        for cid in self.extract_explicit_ids(text):
            if cid in self.by_id and cid not in seen:
                seen.add(cid)
                ordered.append(cid)

        # Title-fragment match.
        lower = text.lower()
        if lower.strip():
            candidate_tokens = set(_TOKEN_RE.findall(lower))
            # Count per-card hits.
            hit_counts: dict[str, int] = {}
            for tok in candidate_tokens:
                for cid in self.by_token.get(tok, ()):
                    if cid in seen:
                        continue
                    hit_counts[cid] = hit_counts.get(cid, 0) + 1
            for cid, n in hit_counts.items():
                if n >= _MIN_TITLE_TOKENS and cid not in seen:
                    seen.add(cid)
                    ordered.append(cid)

        return ordered


def _tokenize_title(title: str) -> list[str]:
    """Lowercase, split on alnum runs >= 3 chars, dedupe while preserving order."""
    seen: set[str] = set()
    out: list[str] = []
    for tok in _TOKEN_RE.findall(title.lower()):
        if tok in seen:
            continue
        seen.add(tok)
        out.append(tok)
    return out


def _to_unix_ts(value: Any) -> int:
    """Coerce a DB value to a UNIX-seconds int.

    The real kanban DB stores `created_at` as an integer. Test fixtures
    may write ISO8601 strings ("2026-08-26T15:00:00+00:00"). Handle both.
    Anything else returns 0 (filtered as out-of-window, which is the
    safe default).
    """
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return 0
        if s.isdigit():
            return int(s)
        # ISO 8601 — tolerate trailing Z.
        try:
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except ValueError:
            return 0
    return 0


def load(
    db_path: str | Path,
    days: int = 30,
    statuses: tuple[str, ...] = ("ready", "running", "blocked", "done"),
) -> KanbanIndex:
    """Read the local kanban SQLite DB and return a populated index.

    Args:
      db_path: absolute path to the kanban SQLite file. Resolved at call
        time — the linker reads this fresh on every run.
      days: include cards touched within this many days (default 30, per
        the spec). "Touched" means created_at OR most recent task_event.
      statuses: include cards in any of these statuses. Default is the
        lifecycle states a card can be in once it's left `todo`.

    Note on timestamps: the kanban DB stores `created_at` as a UNIX
    integer (seconds), not ISO8601. We compute the cutoff as an int
    and compare in integer space. task_events.created_at is *also*
    stored as an integer (verified 2026-08-26 against the aquabutlers
    board — `1787757487` = 2026-08-26 15:18 UTC). Mixing string and
    integer comparisons in SQLite would silently exclude everything.
    """
    p = Path(db_path).expanduser()
    if not p.exists():
        log.warning("kanban db not found at %s — returning empty index", p)
        return KanbanIndex(cutoff=_utc_now_iso())

    cutoff_dt = datetime.now(timezone.utc) - timedelta(days=days)
    cutoff_int = int(cutoff_dt.timestamp())  # unix seconds (UTC)
    cutoff_iso = cutoff_dt.isoformat(timespec="seconds")
    placeholders = ",".join("?" * len(statuses))

    # Pull cards that were either created within the window OR have a
    # recent task_event touching them. We do this with a single query that
    # selects `MAX(task_events.created_at)` over events per task.
    sql = f"""
        WITH recent_events AS (
          SELECT task_id, MAX(created_at) AS last_event
          FROM task_events
          GROUP BY task_id
        )
        SELECT
          t.id,
          t.title,
          t.status,
          COALESCE(re.last_event, t.created_at) AS touched_at
        FROM tasks t
        LEFT JOIN recent_events re ON re.task_id = t.id
        WHERE COALESCE(re.last_event, t.created_at) >= ?
          AND t.status IN ({placeholders})
        ORDER BY touched_at DESC
    """
    params: list = [cutoff_int, *statuses]

    with sqlite3.connect(str(p)) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(sql, params)
        rows = cur.fetchall()

    cards: list[KanbanCard] = []
    by_id: dict[str, KanbanCard] = {}
    by_token: dict[str, set[str]] = {}

    for r in rows:
        card_id = str(r["id"]).lower()
        title = str(r["title"] or "")
        if not card_id or not title:
            continue
        touched = _to_unix_ts(r["touched_at"])
        tokens = _tokenize_title(title)
        card = KanbanCard(
            id=card_id,
            title=title,
            status=str(r["status"] or ""),
            indexed_at=datetime.fromtimestamp(touched, tz=timezone.utc).isoformat(
                timespec="seconds"
            ) if touched else "",
            tokens=tokens,
        )
        cards.append(card)
        by_id[card_id] = card
        for tok in tokens:
            by_token.setdefault(tok, set()).add(card_id)

    log.info(
        "kanban_state: %d cards indexed (cutoff=%s, statuses=%s)",
        len(cards),
        cutoff_iso,
        ",".join(statuses),
    )
    return KanbanIndex(cards=cards, by_id=by_id, by_token=by_token, cutoff=cutoff_iso)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    idx = load(Path("~/.hermes/kanban/boards/aquabutlers/kanban.db").expanduser())
    print(f"cards: {len(idx.cards)}")
    for c in idx.cards[:5]:
        print(f"  {c.id}  {c.status:<10}  {c.title}")