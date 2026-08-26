"""git-log reader for the AquaButler-Server repo.

Phase 2 joins each Meilisearch chunk to the recent (last 30 days) commit
history of `~/AquaButler-Server`. Two matching strategies:

  1. Explicit 7-char SHA prefix mentioned in chunk text. Hex match, case
     insensitive. We accept any 7..40 hex chars (git short SHAs can be 7+
     chars depending on repo size) but require the full short SHA to be
     present in the commit index — otherwise an arbitrary hex prefix in
     prose could spuriously link.

  2. Title-fragment similarity: keywords from the commit subject line,
     matched the same way as kanban titles (>= 2 tokens shared with chunk).

The output is a `GitIndex` that exposes:
  - `extract_short_shas(text) -> set[str]` — explicit SHAs in text
  - `find_matches(text) -> list[str]` — SHAs matched by either strategy

Notes:
  - We use `git log --since=30 days ago --pretty=format:%H %s` which is
    stable and dependency-free (no pygit2, no GitPython).
  - The caller (link_live_state) decides whether to fetch this from the
    AquaButler-Server repo or skip it (e.g. in CI without the repo on
    disk). Failure to run git logs is logged + tolerated.
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path


log = logging.getLogger("aqua_cortex.git_state")


_SHORT_SHA_RE = re.compile(r"\b[0-9a-f]{7,40}\b")
_TOKEN_RE = re.compile(r"[a-z0-9]{3,}")
_MIN_COMMIT_TOKENS = 2


@dataclass
class CommitEntry:
    sha: str  # full 40-char SHA
    short_sha: str  # first 7 chars
    subject: str
    tokens: list[str] = field(default_factory=list)  # deduped, lowercased

    def matches_text(self, text: str) -> bool:
        if not self.tokens:
            return False
        lower = text.lower()
        return sum(1 for tok in self.tokens if tok in lower) >= _MIN_COMMIT_TOKENS


@dataclass
class GitIndex:
    commits: list[CommitEntry] = field(default_factory=list)
    by_sha: dict[str, CommitEntry] = field(default_factory=dict)  # full
    by_short: dict[str, CommitEntry] = field(default_factory=dict)  # 7-char
    by_token: dict[str, set[str]] = field(default_factory=dict)  # token -> short SHA
    cutoff: str = ""

    def extract_short_shas(self, text: str) -> set[str]:
        """Pull candidate 7..40-char hex tokens out of `text` and return the
        subset that match a known commit's short or full SHA.

        Important: we DO NOT match arbitrary hex prefixes — a candidate
        must equal a known short SHA (7 chars) or be a prefix of a known
        full SHA. This avoids spurious matches on `0xdeadbeef` style
        literals in prose.
        """
        found: set[str] = set()
        for m in _SHORT_SHA_RE.finditer(text):
            cand = m.group(0).lower()
            # Try exact short SHA lookup.
            if cand in self.by_short:
                found.add(self.by_short[cand].short_sha)
                continue
            # Try full-SHA prefix match (>=7 chars is enough).
            for full, entry in self.by_sha.items():
                if full.startswith(cand) and len(cand) >= 7:
                    found.add(entry.short_sha)
                    break
        return found

    def find_matches(self, text: str) -> list[str]:
        seen: set[str] = set()
        ordered: list[str] = []
        for short in self.extract_short_shas(text):
            if short in seen:
                continue
            seen.add(short)
            ordered.append(short)
        lower = text.lower()
        if lower.strip():
            cand_tokens = set(_TOKEN_RE.findall(lower))
            hit_counts: dict[str, int] = {}
            for tok in cand_tokens:
                for short in self.by_token.get(tok, ()):
                    if short in seen:
                        continue
                    hit_counts[short] = hit_counts.get(short, 0) + 1
            for short, n in hit_counts.items():
                if n >= _MIN_COMMIT_TOKENS and short not in seen:
                    seen.add(short)
                    ordered.append(short)
        return ordered


def _tokenize_subject(subject: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for tok in _TOKEN_RE.findall(subject.lower()):
        if tok in seen:
            continue
        seen.add(tok)
        out.append(tok)
    return out


def load(
    repo_path: str | Path,
    days: int = 30,
    git_bin: str | None = None,
) -> GitIndex:
    """Read `git log --since=<days> --pretty=format:'%H %s'` for the
    given repo path.

    Returns an empty GitIndex (with `cutoff` set) if:
      - the path is not a git repo,
      - git is not on PATH,
      - the subprocess fails for any reason.

    Callers (the linker) treat an empty index as "no commit linkage", not
    as a hard error — the rest of the linkage still proceeds.
    """
    p = Path(repo_path).expanduser()
    cutoff_iso = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(
        timespec="seconds"
    )
    if not p.exists():
        log.warning("git repo not found at %s — returning empty git index", p)
        return GitIndex(cutoff=cutoff_iso)
    if not (p / ".git").exists() and not p.joinpath(".git").exists():
        # Plain `git -C <path>` will work even if `.git` is a file
        # (worktree), but log clearly either way.
        log.debug("git repo path %s has no .git dir/file; trying anyway", p)

    git = git_bin or shutil.which("git") or "/usr/bin/git"
    cmd = [
        git,
        "-C",
        str(p),
        "log",
        f"--since={days} days ago",
        "--pretty=format:%H %s",
    ]
    log.debug("$ %s", " ".join(cmd))
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        log.warning("git log failed: %s — returning empty index", exc)
        return GitIndex(cutoff=cutoff_iso)

    if proc.returncode != 0:
        log.warning(
            "git log exited %d stderr=%s — returning empty index",
            proc.returncode,
            proc.stderr.strip()[:200],
        )
        return GitIndex(cutoff=cutoff_iso)

    commits: list[CommitEntry] = []
    by_sha: dict[str, CommitEntry] = {}
    by_short: dict[str, CommitEntry] = {}
    by_token: dict[str, set[str]] = {}

    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        # Split on first space: subject may itself contain spaces.
        sha, _, subject = line.partition(" ")
        sha = sha.lower()
        if not re.match(r"^[0-9a-f]{40}$", sha):
            log.debug("skipping line with non-full sha: %r", line[:60])
            continue
        short = sha[:7]
        tokens = _tokenize_subject(subject)
        entry = CommitEntry(sha=sha, short_sha=short, subject=subject, tokens=tokens)
        commits.append(entry)
        by_sha[sha] = entry
        by_short[short] = entry
        for tok in tokens:
            by_token.setdefault(tok, set()).add(short)

    log.info("git_state: %d commits indexed (cutoff=%s)", len(commits), cutoff_iso)
    return GitIndex(
        commits=commits,
        by_sha=by_sha,
        by_short=by_short,
        by_token=by_token,
        cutoff=cutoff_iso,
    )


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    idx = load(Path("~/AquaButler-Server").expanduser())
    print(f"commits: {len(idx.commits)}")
    for c in idx.commits[:5]:
        print(f"  {c.short_sha}  {c.subject}")