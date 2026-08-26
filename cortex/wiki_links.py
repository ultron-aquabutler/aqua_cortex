"""[[wiki-link]] parser + service-target filtering.

Obsidian stores cross-references as `[[Foo Bar]]` or `[[Foo Bar|alias]]`. We
parse them out of chunk text so the schema's `linked_services` field is
populated. To keep the filterable attribute useful we ONLY promote a link
when the resolved file lives under `Services/` (per the vault convention
used in the AquaButler repo) OR matches a known service-name pattern.

Plain content references (no Services/ folder) are dropped from
`linked_services` but kept in the parsed-wikilinks list for downstream use.
"""
from __future__ import annotations

import re
from pathlib import Path


_WIKILINK_RE = re.compile(r"\[\[([^\]\|]+?)(?:\|[^\]]+)?\]\]")

# Service names we recognise when seen as a bare wikilink (folder-less
# references). Lower-case, hyphen-tolerant.
_SERVICE_ALIASES: set[str] = {
    "supabase",
    "supabase_auth",
    "supabase_storage",
    "supabase_realtime",
    "supabase_kong",
    "supabase_studio",
    "mosquitto",
    "patroni",
    "openbalena",
    "balena",
    "balena_cloud",
    "open-balena",
    "chem_controller",
    "pool_controller",
    "njs_pc",
    "rem",
    "sequent_hat",
    "traefik",
    "postgres",
    "postgrest",
    "gotrue",
    "imgproxy",
}


def parse_wikilinks(text: str) -> list[str]:
    """Return the resolved *target* for each `[[link]]` in text.

    The alias part (after `|`) is stripped. Targets are returned lowercased
    and whitespace-collapsed, not slugified — callers decide.
    """
    out: list[str] = []
    for m in _WIKILINK_RE.finditer(text):
        target = m.group(1).strip()
        # Drop the leading '#' for section anchors — those aren't file refs.
        if target.startswith("#"):
            continue
        # Strip trailing backslashes — Obsidian-on-Windows exports sometimes
        # smuggle them into the link target.
        target = target.rstrip("\\").strip()
        if not target:
            continue
        out.append(target)
    return out


def resolve_services(
    wikilinks: list[str],
    vault_root: Path,
) -> list[str]:
    """Reduce wikilinks to a deduped list of service names.

    Rules:
      1. If a wikilink resolves to a file under `vault_root/Services/`, take
         the top-level folder name as the service.
      2. Otherwise, if the lowercased wikilink target is in SERVICE_ALIASES,
         include it verbatim.
      3. Anything else is dropped (no `Services/` path, no alias match).
    """
    services: list[str] = []
    services_root = vault_root / "Services"
    for raw in wikilinks:
        slug = raw.lower().replace(" ", "-")
        # Try Services/<slug>.md or Services/<slug>/index.md
        candidate = (
            services_root / f"{raw}.md"
            if (services_root / f"{raw}.md").exists()
            else None
        )
        if candidate is None:
            sub = services_root / raw / "index.md"
            if sub.exists():
                candidate = sub
        if candidate is not None:
            try:
                rel = candidate.relative_to(services_root)
                top = rel.parts[0]
                services.append(top)
                continue
            except ValueError:
                pass

        if slug in _SERVICE_ALIASES:
            services.append(slug)

    # Dedup, preserve order.
    seen: set[str] = set()
    deduped: list[str] = []
    for s in services:
        if s not in seen:
            seen.add(s)
            deduped.append(s)
    return deduped