"""aqua-swarm live-state reader.

Queries the `aqua-swarm` Docker context for current service state and builds
a `service_name -> {swarm_state, stack, node}` map used by the linker to
populate `live_state` and `linked_services` on each Meilisearch chunk.

Output contract:

    {
        "<service_name>": {
            "service": "<service_name>",
            "swarm_state": "<running>/<total>",     # e.g. "1/1", "0/1"
            "stack": "<prefix before first '_'>",   # e.g. "supabase"
            "node": "<hostname of running replica>",
            "checked_at": "<ISO8601 UTC, second-precision>",
        },
        ...
    }

If a service has zero running replicas, `node` is an empty string (no
candidate replica to pick). The linker never fails on a degraded state —
it logs and continues.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


log = logging.getLogger("aqua_cortex.swarm_state")


@dataclass
class SwarmServiceState:
    service: str
    swarm_state: str
    stack: str
    node: str
    checked_at: str


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _docker_bin() -> str:
    """Locate the docker binary. Honor explicit override; fall back to PATH."""
    return shutil.which("docker") or "/usr/bin/docker"


def _run(cmd: list[str], timeout: float = 30.0) -> str:
    """Run a subprocess, return stdout. Raise CalledProcessError on non-zero."""
    log.debug("$ %s", " ".join(cmd))
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(
            proc.returncode, cmd, output=proc.stdout, stderr=proc.stderr
        )
    return proc.stdout


def list_services(context: str = "aqua-swarm") -> list[SwarmServiceState]:
    """Read `docker --context <context> service ls` and return service states.

    Parses lines of the form `<name> <running>/<total>` produced by:
        docker --context <ctx> service ls --format '{{.Name}} {{.Replicas}}'
    Empty / unparseable lines are skipped (with a debug log) rather than
    raising — partial live state is better than a crashed linker run.
    """
    docker = _docker_bin()
    try:
        out = _run(
            [
                docker,
                "--context",
                context,
                "service",
                "ls",
                "--format",
                "{{.Name}} {{.Replicas}}",
            ]
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
        log.warning("swarm service ls failed: %s", exc)
        return []

    states: list[SwarmServiceState] = []
    checked_at = _utc_now_iso()
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            log.debug("skipping unparseable line: %r", line)
            continue
        name = parts[0]
        replicas = parts[1]
        if not re.match(r"^\d+/\d+$", replicas):
            log.debug("skipping line with non-canonical replicas: %r", line)
            continue
        stack = name.split("_", 1)[0] if "_" in name else name
        states.append(
            SwarmServiceState(
                service=name,
                swarm_state=replicas,
                stack=stack,
                node="",  # filled in by `attach_nodes`
                checked_at=checked_at,
            )
        )
    return states


def attach_nodes(
    states: list[SwarmServiceState],
    context: str = "aqua-swarm",
) -> list[SwarmServiceState]:
    """For each service with running replicas > 0, query `service ps` and
    record the hostname of the first running task. Idempotent in-place.

    Errors per-service are logged + skipped so one broken service doesn't
    fail the whole run.
    """
    docker = _docker_bin()
    for st in states:
        try:
            running, total = (int(x) for x in st.swarm_state.split("/", 1))
        except ValueError:
            continue
        if running <= 0:
            continue
        try:
            out = _run(
                [
                    docker,
                    "--context",
                    context,
                    "service",
                    "ps",
                    "--format",
                    "{{.Name}} {{.Node}} {{.CurrentState}}",
                    st.service,
                ]
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            log.warning("service ps %s failed: %s", st.service, exc)
            continue

        node = ""
        for line in out.splitlines():
            # The first line is the header row; skip it. Task rows look like
            # `<name>.<idx> <node> Running since ...`.
            parts = line.split()
            if len(parts) < 3:
                continue
            if parts[2] != "Running":
                continue
            node = parts[1]
            break
        if node:
            st.node = node
    return states


def build_index(
    context: str = "aqua-swarm",
) -> dict[str, SwarmServiceState]:
    """High-level helper. Returns a `service_name -> SwarmServiceState` map.

    Tolerates total failure of `service ls` by returning an empty map —
    callers (the linker) handle that gracefully and skip live-state linkage
    while still running kanban + git joins.
    """
    states = list_services(context)
    states = attach_nodes(states, context)
    return _states_to_dict(states)


def _states_to_dict(states: list[SwarmServiceState]) -> dict[str, SwarmServiceState]:
    out: dict[str, SwarmServiceState] = {}
    for st in states:
        out[st.service] = st
    log.info("swarm_state: %d services indexed", len(out))
    return out


def load_from_json(path: str | Path) -> dict[str, SwarmServiceState]:
    """Read a swarm snapshot from a JSON file (test fixture / debug use).

    JSON shape matches `to_jsonable`: each entry is
        {<service_name>: {service, swarm_state, stack, node, checked_at}}
    Missing fields default to empty strings. Used by the linker when
    $AQUA_CORTEX_SWARM_FILE is set (lets CI run without a Docker context).
    """
    p = Path(path).expanduser()
    if not p.exists():
        log.warning("swarm file not found at %s — returning empty index", p)
        return {}
    with p.open("r", encoding="utf-8") as fh:
        raw = json.load(fh)
    states: list[SwarmServiceState] = []
    for name, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        states.append(
            SwarmServiceState(
                service=str(entry.get("service", name)),
                swarm_state=str(entry.get("swarm_state", "0/0")),
                stack=str(entry.get("stack", "")),
                node=str(entry.get("node", "")),
                checked_at=str(entry.get("checked_at", _utc_now_iso())),
            )
        )
    log.info("swarm_state: %d services loaded from %s", len(states), p)
    return _states_to_dict(states)


def stack_services(index: dict[str, SwarmServiceState], stack: str) -> dict[str, SwarmServiceState]:
    """Return only entries for a given stack (e.g. `supabase`)."""
    return {k: v for k, v in index.items() if v.stack == stack}


def to_jsonable(index: dict[str, SwarmServiceState]) -> dict[str, dict]:
    """Drop-in dict shape suitable for serialisation / logs."""
    return {
        name: {
            "service": st.service,
            "swarm_state": st.swarm_state,
            "stack": st.stack,
            "node": st.node,
            "checked_at": st.checked_at,
        }
        for name, st in index.items()
    }


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print(json.dumps(to_jsonable(build_index()), indent=2))