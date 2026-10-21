#!/usr/bin/env python3
"""wiring_score.py — TR-282: the Hilo-backed wiring score consumer.

One instrument for the wiring_pct quality metric. It reads the repo's
dependency graph — `.vfs/graph/edges.jsonl` as written by `hilo graph warm`
(the same store the `hilo graph related|impact|untested|module|stats` verbs
read) — and produces a wiring report: per-surface inbound/outbound
connection counts, connection deltas vs a baseline graph, surfaces with no
test edge, and a per-file classification (entrypoint/service/lib/test/dead),
rolled up into one wiring score in 0..100.

THE FORMULA (mechanical, and carried verbatim in every JSON output):

    score = 100 * (W_CALL * called_share + W_TEST * tested_share)
    called_share = |surfaces with >= 1 inbound repo edge| / |surfaces|
    tested_share = |surfaces with >= 1 test edge|           / |surfaces|
    wired_share  = |surfaces called AND tested|             / |surfaces|
    W_CALL = W_TEST = 0.5

    surfaces = non-test repo *.py files in scope, taken from the on-disk
    walk intersected with the graph's world; test files are classified but
    are never wiring subjects.

A file with no caller and no test drags BOTH shares down (anti-vacuity);
wiring one up raises them. `wired_share` (the "both" share named in the
board row) is reported alongside but is not the scored number.

Source honesty (stated, never implied):

* The graph source is `.vfs/graph/edges.jsonl` — the deterministic
  artifact `hilo graph warm` writes. If it is missing the consumer exits 3
  with `WIRING-UNMEASURABLE` and a concrete reason (`hilo init` +
  `hilo graph warm` fix it); it never fabricates connection counts.
* The graph is FILE-level and IMPORTS-only: a "connection" is an import
  edge, not a runtime call. Function-level caller/test-edge granularity is
  the Hilo board's GAP-113 (connection-count/delta export); until that
  lands this consumer stays at file granularity and says so in
  `limitations`.
* A partial warm (the known degenerate shape: ~1 edge across a
  many-file repo) is detected and flagged `graph_valid: false` with a
  limitation naming the shortfall — the score still computes, but the
  output says it is NOT full-repo truth.

Usage:
    python3 scripts/wiring_score.py                    # human report
    python3 scripts/wiring_score.py --json             # machine report
    python3 scripts/wiring_score.py --scope scripts/
    python3 scripts/wiring_score.py --baseline OLD/edges.jsonl
    python3 scripts/wiring_score.py --graph PATH --root PATH
    python3 scripts/wiring_score.py --probe-hilo       # + `hilo graph stats` echo

Exit 0 when measured; 3 when the graph is unavailable (named
WIRING-UNMEASURABLE line on stderr, reason in the JSON error body).
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import subprocess
import sys
from pathlib import Path, PurePosixPath

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_GRAPH = REPO_ROOT / ".vfs" / "graph" / "edges.jsonl"

W_CALL = 0.5
W_TEST = 0.5
FORMULA = (
    "score = 100 * (0.5*called_share + 0.5*tested_share); "
    "called = >=1 inbound repo import edge; tested = >=1 test edge "
    "(test file <-> surface, either direction); surfaces = non-test repo "
    ".py files in scope (on-disk walk); wired_share (both) reported, not scored"
)
SOURCE = ".vfs/graph/edges.jsonl as written by `hilo graph warm` (Hilo file-level import graph)"

# ch:trace row=TR-282 spec=none:current-row-contract wave=task-router-foreman-2026-10-21-20-51-29#serial test=tests/test_wiring_score.py doc=docs/quality-ladder.md prompt=/tmp/TR-282-brief.txt evidence=data/state/quality-artifact.json witness=none:pre-implementation verdict=none:pending commit=none:pending-foreman-trace memory=none:pending

UNMEASURABLE = "WIRING-UNMEASURABLE"

#: directories never walked for repo surfaces (mirrors router_context_call.py)
EXCLUDE_DIRS = {".git", ".vfs", ".venv", "__pycache__", "node_modules",
                "vendor", "build", "dist", ".pytest_cache", ".ruff_cache"}

#: below this many on-disk files the degenerate-shape check is not applied
MIN_FILES_FOR_VALIDITY = 20
#: a warm this thin over a repo this size is the known partial-graph shape
MIN_EDGES_FOR_VALIDITY = 10


def is_test_path(path: str) -> bool:
    """True for test files: tests/ dir, test_*.py or *_test.py."""
    p = PurePosixPath(path)
    return ("tests" in p.parts or p.name.startswith("test_")
            or p.name.endswith("_test.py"))


def load_edges(graph_path) -> tuple[list[dict], int]:
    """Parse an edges.jsonl -> (edge dicts, torn/blank lines skipped).

    Each valid line is a JSON object with at least `from` and `to`.
    Blank lines and unparseable lines are skipped and counted — the store
    is append-only and a torn tail must not fail the measurement.
    """
    edges: list[dict] = []
    skipped = 0
    with open(graph_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                skipped += 1
                continue
            if not isinstance(row, dict) or not row.get("from") or not row.get("to"):
                skipped += 1
                continue
            edges.append(row)
    return edges, skipped


def walk_repo_py_files(root: Path) -> list[str]:
    """Repo-root-relative POSIX paths of every *.py file under root.

    Hidden directories (dot-prefixed) are excluded — mirroring Hilo's own
    discovery, which skips them (a warm over this repo reports its hidden
    files as "excluded (hidden)"), so the surface universe is the same
    world the graph was built from.
    """
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames
                             if d not in EXCLUDE_DIRS and not d.startswith("."))
        for name in sorted(filenames):
            if not name.endswith(".py"):
                continue
            rel = Path(dirpath, name).resolve().relative_to(root).as_posix()
            found.append(rel)
    return found


def normalize(path: str) -> str:
    """Edges carry repo-root-relative POSIX paths; strip ./ noise."""
    return PurePosixPath(path.replace(os.sep, "/")).as_posix().lstrip("./")


def repo_module_index(repo_files: list[str]) -> dict:
    """module dotted-name -> repo-relative file, mirroring Hilo's resolver.

    Hilo emits import edges as `pkg:<dotted module>` nodes, so whether an
    edge is internal depends on whether the MODULE names a repo file.
    This index reproduces `hilo-graph`'s `python_module_for_file`:

    * a file under a regular-package chain (dirs with __init__.py) is
      `<chain>.<stem>` (an __init__.py itself IS the package node);
    * PEP-420 namespace dirs (no __init__.py) between the file and the
      package chain are dotted in;
    * a file with no package ancestor at all is a flat module: its stem
      (GAP-104), importable via sys.path[0] when run as a script.

    The flat rule is why scripts/quality_artifact.py is reachable only as
    `pkg:quality_artifact` — package-style imports of scripts/ (`import
    scripts.x`) are NOT attributed to it. Stated in `limitations`.
    """
    fileset = set(repo_files)
    index: dict[str, str] = {}
    for rel in repo_files:
        parts = PurePosixPath(rel).parts
        dirs, fname = parts[:-1], parts[-1]
        # module stem: hilo's file_stem() — the filename WITHOUT .py
        stem = fname[:-3] if fname.endswith(".py") else fname
        # climb: nearest ancestor dir (relative) that is a regular package
        chain_at = None
        namespace: list[str] = []
        i = len(dirs)
        while i > 0:
            if "/".join(dirs[:i] + ("__init__.py",)) in fileset:
                chain_at = i
                break
            namespace.insert(0, dirs[i - 1])
            i -= 1
        if chain_at is None:
            # flat (top-level) module: exactly its stem
            if stem != "__init__":
                index.setdefault(stem, rel)
            continue
        # regular-package chain upward while __init__.py exists above
        pkg_parts: list[str] = []
        j = chain_at
        while True:
            pkg_parts.insert(0, dirs[j - 1])
            if j - 1 > 0 and "/".join(dirs[:j - 1] + ("__init__.py",)) in fileset:
                j -= 1
            else:
                break
        base = ".".join(pkg_parts)
        if stem == "__init__":
            index.setdefault(base, rel)
        else:
            index.setdefault(".".join([base] + namespace + [stem]), rel)
    return index


def is_external(path: str, index: dict) -> bool:
    """True when an edge endpoint names nothing in the repo.

    `pkg:<module>` is internal exactly when `<module>` is a repo module;
    everything else (pkg:os, sys:..., other languages' nodes) is outside.
    """
    if path.startswith("pkg:"):
        return path[4:] not in index
    return path not in index


def _edge_target(to_raw: str, index: dict) -> str | None:
    """Resolve an edge destination to a repo file, or None if external."""
    if to_raw.startswith("pkg:"):
        return index.get(to_raw[4:])
    to = normalize(to_raw)
    return to if to in index.values() else None


def build_surfaces(edges: list[dict], repo_files: list[str], scope: str | None,
                   index: dict | None = None) -> dict:
    """Fold edges into per-surface connection counts.

    Returns {surface_path: {inbound, outbound, external_outbound,
    test_edge}} for every non-test repo file in scope (disk walk is the
    surface universe). Import edges are `pkg:<module>` nodes resolved
    through the module index; an edge whose destination names no repo
    module is external (it widens nothing).
    """
    if index is None:
        index = repo_module_index(repo_files)
    in_scope = set(repo_files)
    if scope:
        scope = scope.rstrip("/")
        in_scope = {f for f in in_scope
                    if f.startswith(scope + "/") or fnmatch.fnmatch(f, scope)
                    or fnmatch.fnmatch(f, scope + "/*") or f == scope}

    stats = {f: {"inbound": 0, "outbound": 0, "external_outbound": 0,
                 "test_edge": False} for f in sorted(in_scope)}

    for edge in edges:
        src = normalize(str(edge.get("from", "")))
        target = _edge_target(str(edge.get("to", "")), index)
        src_in = src in stats
        if target is None:
            # external: counts as external-outbound evidence for the source
            if src_in:
                stats[src]["external_outbound"] += 1
            continue
        tgt_test = is_test_path(target)
        src_test = is_test_path(src)
        if src_in and target != src:
            stats[src]["outbound"] += 1
            if tgt_test:
                stats[src]["test_edge"] = True
        if target in stats and target != src:
            stats[target]["inbound"] += 1
            if src_test:
                stats[target]["test_edge"] = True
    return stats


def classify(path: str, stat: dict) -> str:
    """One of entrypoint/service/lib/test/dead, mechanically:

    test       — test path (never a wiring subject)
    dead       — on disk but the graph has no edge touching it at all
    entrypoint — nothing in-repo imports it (a root: invoked directly)
    lib        — imported in-repo AND imports in-repo (a middle layer)
    service    — imported in-repo, imports nothing in-repo (a leaf adapter)
    """
    if is_test_path(path):
        return "test"
    if stat["inbound"] == 0 and stat["outbound"] == 0 and not stat["test_edge"]:
        return "dead"
    if stat["inbound"] == 0:
        return "entrypoint"
    if stat["outbound"] > 0:
        return "lib"
    return "service"


def score_surfaces(stats: dict) -> dict:
    """The formula, applied. Surfaces = non-test entries of `stats`."""
    subjects = {f: s for f, s in stats.items() if not is_test_path(f)}
    total = len(subjects)
    if total == 0:
        raise ValueError("no scoring surfaces in scope — nothing to score")
    called = sum(1 for s in subjects.values() if s["inbound"] >= 1)
    tested = sum(1 for s in subjects.values() if s["test_edge"])
    wired = sum(1 for s in subjects.values()
                if s["inbound"] >= 1 and s["test_edge"])
    called_share = called / total
    tested_share = tested / total
    wired_share = wired / total
    return {
        "score": round(100.0 * (W_CALL * called_share + W_TEST * tested_share), 2),
        "called_share": round(called_share, 4),
        "tested_share": round(tested_share, 4),
        "wired_share": round(wired_share, 4),
        "surfaces_total": total,
        "called": called,
        "tested": tested,
        "wired": wired,
    }


def baseline_deltas(stats: dict, baseline_edges: list[dict],
                    index: dict | None = None) -> dict:
    """Per-surface {inbound, outbound} delta: now minus baseline graph.

    Rebuilds baseline counts with the same folding rules over the baseline
    edge list; surfaces absent from the baseline score 0 there (so wiring
    a file up shows a positive inbound delta).
    """
    if index is None:
        index = repo_module_index(sorted(stats))
    base_files = sorted(stats)
    base_stats = {f: {"inbound": 0, "outbound": 0, "external_outbound": 0,
                      "test_edge": False} for f in base_files}
    for edge in baseline_edges:
        src = normalize(str(edge.get("from", "")))
        target = _edge_target(str(edge.get("to", "")), index)
        if target is None:
            continue
        if src in base_stats and target != src:
            base_stats[src]["outbound"] += 1
        if target in base_stats and target != src:
            base_stats[target]["inbound"] += 1

    deltas = {}
    for f, s in stats.items():
        b = base_stats[f]
        deltas[f] = {"inbound": s["inbound"] - b["inbound"],
                     "outbound": s["outbound"] - b["outbound"]}
    return deltas


def measure(root=REPO_ROOT, graph_path=None, scope=None,
            baseline_path=None, probe_hilo=False) -> dict:
    """Full wiring report dict (also the --json payload)."""
    root = Path(root).resolve()
    graph_path = Path(graph_path) if graph_path else root / ".vfs/graph/edges.jsonl"
    limitations: list[str] = [
        "file-level import graph: a 'connection' is an import edge, not a "
        "runtime call; function-level caller/test-edge granularity is "
        "GAP-113 (hilo board) — deltas land when that export does",
        "fresh checkouts have no graph until `hilo init` + `hilo graph warm` "
        "run; the store is untracked runtime state, rebuildable by design",
    ]

    if not graph_path.exists():
        fallback = root / ".vfs" / "graph" / "edges.jsonl"
        if fallback.exists():
            graph_path = fallback
        else:
            return {
                "error": UNMEASURABLE,
                "reason": (f"wiring graph not found at {graph_path} — run "
                           "`hilo init` (if needed) then `hilo graph warm`, or "
                           "pass --graph PATH; no connection counts are "
                           "fabricated without a real graph"),
                "formula": FORMULA,
                "source": SOURCE,
                "limitations": limitations,
            }

    edges, skipped = load_edges(graph_path)
    repo_files = walk_repo_py_files(root)
    index = repo_module_index(repo_files)
    stats = build_surfaces(edges, repo_files, scope, index)

    def _internal(node_raw: str) -> bool:
        return not is_external(normalize(str(node_raw)), index)

    graph_repo_nodes = {normalize(str(e["from"])) for e in edges
                        if _internal(e["to"])}
    graph_repo_nodes |= {normalize(str(e["to"])) for e in edges
                         if _internal(e["to"]) and not str(e["to"]).startswith("pkg:")}
    covered = sum(1 for f in repo_files if f in graph_repo_nodes)
    graph_valid = not (len(repo_files) >= MIN_FILES_FOR_VALIDITY
                       and len(edges) < MIN_EDGES_FOR_VALIDITY)
    if not graph_valid:
        limitations.append(
            f"PARTIAL GRAPH: warm produced {len(edges)} edges over a "
            f"{len(repo_files)}-file repo ({covered} files covered) — the "
            "known degenerate-warm shape; this score is NOT full-repo truth")
    if skipped:
        limitations.append(f"{skipped} blank/torn line(s) skipped in edges store")

    try:
        rollup = score_surfaces(stats)
    except ValueError as exc:
        return {
            "error": UNMEASURABLE,
            "reason": str(exc),
            "formula": FORMULA,
            "source": SOURCE,
            "limitations": limitations,
        }

    classes = {"entrypoint": 0, "service": 0, "lib": 0, "test": 0, "dead": 0}
    surfaces = []
    untested = []
    for f, s in stats.items():
        cls = classify(f, s)
        classes[cls] += 1
        surfaces.append({
            "module": f,
            "class": cls,
            "inbound": s["inbound"],
            "outbound": s["outbound"],
            "external_outbound": s["external_outbound"],
            "test_edge": s["test_edge"],
        })
        if cls not in ("test",) and not s["test_edge"]:
            untested.append(f)

    report = {
        "schema_version": 1,
        "source": SOURCE,
        "formula": FORMULA,
        "weights": {"caller": W_CALL, "test": W_TEST},
        "graph": {
            "path": str(graph_path),
            "edges": len(edges),
            "repo_files_on_disk": len(repo_files),
            "repo_files_covered_by_graph": covered,
            "graph_valid": graph_valid,
            "torn_lines_skipped": skipped,
        },
        "scope": scope,
        **rollup,
        "classes": classes,
        "untested_surfaces": sorted(untested),
        "surfaces": surfaces,
        "limitations": limitations,
    }

    if baseline_path is not None:
        baseline_edges, base_skipped = load_edges(baseline_path)
        report["baseline"] = {
            "path": str(baseline_path),
            "edges": len(baseline_edges),
            "torn_lines_skipped": base_skipped,
        }
        report["deltas"] = baseline_deltas(stats, baseline_edges)

    if probe_hilo:
        try:
            proc = subprocess.run(
                ["hilo", "graph", "stats"], cwd=str(root),
                capture_output=True, text=True, timeout=30, check=False)
            report["hilo_stats"] = {
                "returncode": proc.returncode,
                "output": proc.stdout.strip()[:2000],
            }
        except (OSError, subprocess.TimeoutExpired) as exc:
            report["hilo_stats"] = {"error": f"hilo graph stats failed: {exc}"}

    return report


def render(report: dict) -> str:
    """Human-readable wiring report."""
    if "error" in report:
        return (f"{report['error']}: {report['reason']}\n")
    out = ["== wiring score (TR-282) =="]
    scope = report.get("scope") or "(whole repo)"
    out.append(f"scope: {scope}  surfaces: {report['surfaces_total']}")
    out.append(
        f"score: {report['score']:.2f}/100   "
        f"called {100 * report['called_share']:.1f}%   "
        f"tested {100 * report['tested_share']:.1f}%   "
        f"wired(both) {100 * report['wired_share']:.1f}%")
    c = report["classes"]
    out.append("classes: entrypoint={entrypoint} service={service} "
               "lib={lib} test={test} dead={dead}".format(**c))
    out.append(f"graph: {report['graph']['edges']} edges, "
               f"{report['graph']['repo_files_covered_by_graph']}/"
               f"{report['graph']['repo_files_on_disk']} files covered, "
               f"valid={report['graph']['graph_valid']}")
    out.append(f"formula: {report['formula']}")
    out.append(f"source: {report['source']}")
    untested = report["untested_surfaces"]
    if untested:
        out.append(f"surfaces with NO test edge ({len(untested)}, first 10):")
        for f in untested[:10]:
            out.append(f"  {f}")
    if "deltas" in report:
        moved = {f: d for f, d in report["deltas"].items()
                 if d["inbound"] or d["outbound"]}
        out.append(f"deltas vs baseline ({len(moved)} surface(s) moved):")
        for f in sorted(moved)[:10]:
            d = moved[f]
            out.append(f"  {f}: inbound {d['inbound']:+d}, outbound {d['outbound']:+d}")
    for lim in report["limitations"]:
        out.append(f"limitation: {lim}")
    return "\n".join(out) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="TR-282 wiring score: Hilo graph -> wiring report + 0..100 score")
    ap.add_argument("--json", action="store_true",
                    help="emit the machine report (the artifact's input)")
    ap.add_argument("--scope", default=None,
                    help="restrict surfaces: dir prefix (scripts/) or glob (scripts/x_*.py)")
    ap.add_argument("--baseline", default=None, type=Path,
                    help="edges.jsonl of a baseline graph -> per-surface deltas")
    ap.add_argument("--graph", default=None, type=Path,
                    help="edges.jsonl to read (default: <root>/.vfs/graph/edges.jsonl)")
    ap.add_argument("--root", default=None, type=Path,
                    help="repo root to walk (default: this script's repo)")
    ap.add_argument("--probe-hilo", action="store_true",
                    help="also run `hilo graph stats` and echo it in the report")
    args = ap.parse_args(argv)

    root = args.root if args.root else REPO_ROOT
    report = measure(root=root, graph_path=args.graph, scope=args.scope,
                     baseline_path=args.baseline, probe_hilo=args.probe_hilo)

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        sys.stdout.write(render(report))

    if "error" in report:
        print(f"{UNMEASURABLE}: {report['reason']}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
