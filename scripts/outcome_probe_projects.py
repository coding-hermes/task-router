#!/usr/bin/env python3
"""Derive the active project set (primary lanes) from the registry tables.

Used by scripts/outcome_probes.py: a probe library needs ONE probe per active
project, and "active project" is DERIVED from data/tables/projects.jsonl the
same way the fleet derives it (lane suffixes strip to the family primary),
never hand-enumerated.

Run standalone it prints the sorted primary ids, one per line.
"""
from __future__ import annotations
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECTS = os.path.join(REPO, "data", "tables", "projects.jsonl")

#: Lane suffixes that mark a SATELLITE lane of a project family. A lane whose
#: id ends with one of these belongs to the family primary (the id with the
#: suffix stripped). Mirrors the fleet's own family rule: one foreman primary
#: + its -qa/-pm/-sync/-dogfood/-perf/-releng/-review/-docs/-readme lanes.
SATELLITE_SUFFIXES = (
    "-foreman", "-qa", "-pm", "-sync", "-dogfood",
    "-perf", "-releng", "-review", "-docs", "-readme",
)


def primary_of(lane_id: str) -> str:
    """Strip the longest matching satellite suffix (longest first so
    '-foreman' wins over a bare suffix collision)."""
    for suf in sorted(SATELLITE_SUFFIXES, key=len, reverse=True):
        if lane_id.endswith(suf) and len(lane_id) > len(suf):
            return lane_id[: -len(suf)]
    return lane_id


def load_primary_projects(path: str = PROJECTS) -> list[str]:
    """Sorted unique family primaries from the registry table."""
    primaries = set()
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            pid = row.get("id")
            if pid:
                primaries.add(primary_of(str(pid)))
    return sorted(primaries)


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else PROJECTS
    for pid in load_primary_projects(path):
        print(pid)
