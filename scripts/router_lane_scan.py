#!/usr/bin/env python3
"""router_lane_scan.py — weekly lane-scan diff: live carrier catalogs vs registry.

TR-246 (2026-11-20): wires the TR-217/218/219 research-lane machinery
(scripts/router_provider_import.py presets + normalize) to a READ-ONLY diff
against data/tables/models.jsonl so a new model family (default: gpt-6.1-sol)
is caught on the weekly lane scan without anybody re-running imports by hand.

WHAT IT DOES (per preset in data/catalogs/*.json)
  * fetches the preset's live catalog (keyless, read-only) and normalizes it
    with the importer's own normalize() — no second source of truth;
  * diffs catalog ids against the registry rows for that provider;
  * reuses router_provider_import.py --dry-run for the official changed/
    removed summary (diff only, writes nothing);
  * prints one SUMMARY line per preset, every diff line touching the scanned
    family, and one GAP SUMMARY line naming the registry gaps for the foreman
    to file. This script NEVER files board rows and NEVER applies imports.

USAGE
  python3 scripts/router_lane_scan.py                 # scan the gpt-6.1 family
  python3 scripts/router_lane_scan.py --family gpt-6.2-sol
  python3 scripts/router_lane_scan.py --preset xkiro  # one preset only

Exit code is 0 even when gaps are found — gaps are REPORTED, not enforced
(the foreman decides what becomes a row).
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))
import router_provider_import as rpi  # noqa: E402

CATALOGS = os.path.join(REPO, "data", "catalogs")
IMPORTER = os.path.join(REPO, "scripts", "router_provider_import.py")
MODELS = os.path.join(REPO, "data", "tables", "models.jsonl")


def registry_provider_models() -> dict[str, set[str]]:
    """provider -> set(model ids) from the committed registry."""
    out: dict[str, set[str]] = {}
    with open(MODELS, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            out.setdefault(str(row.get("provider")), set()).add(
                str(row.get("model")))
    return out


def catalog_ids(preset_path: str) -> tuple[set[str], str | None]:
    """Live catalog model ids for one preset, via the importer's own loader."""
    name = os.path.basename(preset_path).removesuffix(".json")
    preset = rpi.load_preset(name)
    catalog = rpi.fetch_catalog(preset["catalog_url"],
                                preset.get("user_agent", "task-router-lane-scan"))
    lanes = rpi.normalize(catalog, preset)
    return set(lanes), preset.get("id")


def scan_preset(preset_path: str, family: str, reg: dict[str, set[str]]):
    """Return (summary_line, family_lines, gaps) for one preset."""
    pid_file = os.path.basename(preset_path).removesuffix(".json")
    try:
        cat, pid = catalog_ids(preset_path)
    except Exception as e:  # fail-open scan: report, never crash the lane
        return (f"{pid_file}: catalog fetch failed ({type(e).__name__}: {e})",
                [], [])
    reg_ids = reg.get(pid or pid_file, set())
    added = sorted(cat - reg_ids)
    removed = sorted(reg_ids - cat)

    # official changed/removed summary from the importer's dry-run
    p = subprocess.run(
        [sys.executable, IMPORTER, "--preset", pid or pid_file, "--dry-run"],
        capture_output=True, text=True, timeout=180)
    dry = (p.stdout or "") + (p.stderr or "")
    summary = None
    family_lines = []
    for line in dry.splitlines():
        s = line.strip()
        if s.startswith("diff:"):
            summary = f"{pid or pid_file}: {s}"
        if family in s and not s.startswith(("catalog:", "registry:")):
            family_lines.append(f"{pid or pid_file}: {s}")
    if summary is None:
        summary = f"{pid or pid_file}: importer diff line not found"

    gaps = [f"{pid or pid_file}: + {m} (in live catalog, missing from registry)"
            for m in added if family in m]
    return summary, family_lines, gaps


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--family", default="gpt-6.1-sol",
                    help="model-id substring to scan for (default gpt-6.1-sol)")
    ap.add_argument("--preset", default=None,
                    help="scan one preset id (default: every preset in data/catalogs)")
    args = ap.parse_args()

    preset_paths = sorted(glob.glob(os.path.join(CATALOGS, "*.json")))
    if args.preset:
        preset_paths = [os.path.join(CATALOGS, f"{args.preset}.json")]
        if not os.path.exists(preset_paths[0]):
            print(f"no such preset: {args.preset}")
            return 2

    reg = registry_provider_models()
    family_rows = sorted((p, m) for p, models in reg.items()
                         for m in models if args.family in m)
    print(f"lane scan: family={args.family} presets={len(preset_paths)} "
          f"(read-only diff against data/tables/models.jsonl; writes nothing)")
    print(f"registry rows for family: {len(family_rows)} -> "
          + ", ".join(f"{p}/{m}" for p, m in family_rows))

    all_gaps: list[str] = []
    for path in preset_paths:
        summary, family_lines, gaps = scan_preset(path, args.family, reg)
        print(summary)
        for fl in family_lines:
            print(f"  {fl}")
        all_gaps.extend(gaps)

    print("GAP SUMMARY (for the foreman to file — nothing written by this scan):")
    if all_gaps:
        for g in all_gaps:
            print(f"  GAP: {g}")
    else:
        print(f"  no {args.family} catalog-vs-registry gaps across "
              f"{len(preset_paths)} presets")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
