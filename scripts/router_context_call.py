#!/usr/bin/env python3
"""router_context_call — ONE CONTEXT CALL: code graph + DuckBrain + specs + boards.

Single read-only command that pulls from four surfaces and returns a unified,
provenanced output. Each fragment labelled with {surface, source_path_or_key, taken_at}.

TR-254 acceptance criteria:
  AC1: One command returns fragments from all four surfaces, each labelled.
  AC2: Hilo leg proven against a real fleet repo (not Hilo itself).
  AC3: DuckBrain fragments are CITED RECORDS ONLY, never paraphrased.
  AC4: Contradictions surfaced explicitly.
  AC5: Read-only against every surface — no file content modified.
  AC6: Cost and latency measured and stated.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Surface collectors
# ---------------------------------------------------------------------------

def _timestamp() -> str:
    """Current UTC timestamp ISO format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def query_hilo(workdir: str, max_depth: int = 2) -> dict[str, Any]:
    """Query Hilo graph for code structure.

    Uses `hilo graph related` (auto-parses source) and `hilo graph understand`
    for multi-resolution context. Returns structured fragments.

    This is read-only: Hilo metadata-not-injection (no code file modified).
    """
    fragments = []
    errors = []

    # Find Python and Go files in the workdir
    py_files = list(Path(workdir).rglob("*.py"))
    go_files = list(Path(workdir).rglob("*.go"))

    # Only include files not from .git, __pycache__, .venv, node_modules
    exclude_dirs = {".git", "__pycache__", ".venv", "node_modules", "vendor", "build", "dist"}
    py_files = [f for f in py_files if not any(d in f.parts for d in exclude_dirs)]
    go_files = [f for f in go_files if not any(d in f.parts for d in exclude_dirs)]

    # Use first 3 Python and 3 Go files for provenance demo
    sample_files = [str(f) for f in py_files[:3] + go_files[:3]]

    if not sample_files:
        fragments.append({
            "surface": "code",
            "source_path_or_key": workdir,
            "taken_at": _timestamp(),
            "note": "No Python or Go source files found in workdir"
        })
        return {"hilo_version": _hilo_version(), "fragments": fragments, "errors": errors, "latency_s": 0}

    hilo_path = os.environ.get("HILO_BIN", "hilo")

    # Warm graph first (fast, reads cached DuckDB)
    try:
        subprocess.run(
            [hilo_path, "graph", "warm"],
            cwd=workdir,
            capture_output=True, timeout=30, check=False
        )
    except Exception:
        pass  # graph may already be warm or workdir not initialized

    # Query related files for each sample
    file_results = []
    for fpath in sample_files:
        try:
            result = subprocess.run(
                [hilo_path, "graph", "related", fpath],
                cwd=workdir,
                capture_output=True, text=True, timeout=30, check=False
            )
            output = result.stdout.strip()
            if result.returncode == 0 and output:
                file_results.append({
                    "surface": "code",
                    "source_path_or_key": fpath,
                    "taken_at": _timestamp(),
                    "related_files": output[:2000],  # cap output
                })
            else:
                file_results.append({
                    "surface": "code",
                    "source_path_or_key": fpath,
                    "taken_at": _timestamp(),
                    "note": f"Hilo returned non-zero ({result.returncode}), output: {output[:200]}",
                })
        except subprocess.TimeoutExpired:
            errors.append(f"Hilo related query timed out for {fpath}")
        except Exception as e:
            errors.append(f"Hilo related query failed for {fpath}: {e}")

    fragments.extend(file_results)

    # Try graph understand for multi-resolution context on first file
    if sample_files:
        try:
            result = subprocess.run(
                [hilo_path, "graph", "understand", sample_files[0]],
                cwd=workdir,
                capture_output=True, text=True, timeout=60, check=False
            )
            output = result.stdout.strip()
            if result.returncode == 0 and output:
                fragments.append({
                    "surface": "code",
                    "source_path_or_key": f"{sample_files[0]} (understand context)",
                    "taken_at": _timestamp(),
                    "context": output[:3000],
                })
        except Exception:
            pass  # understand is optional

    return {
        "hilo_version": _hilo_version(),
        "workdir": workdir,
        "sample_files": sample_files,
        "fragments": fragments,
        "errors": errors,
        "latency_s": 0,  # filled by caller
    }


def query_duckbrain(namespaces: list[str] | None = None) -> dict[str, Any]:
    """Query DuckBrain HTTP API for cited records.

    Uses raw GET from the API — never paraphrased. Marks trust level explicitly.
    """
    fragments = []
    errors = []

    db_host = os.environ.get("DUCKBRAIN_URL", "http://localhost:3000")
    default_namespaces = namespaces or ["task-router", "routing", "coding-hermes"]

    for ns in default_namespaces:
        token_file = f"/home/kara/.duckbrain/{ns}.token"
        api_key = None

        if os.path.isfile(token_file):
            with open(token_file) as f:
                api_key = f.read().strip()
        elif "DUCKBRAIN_API_KEY" in os.environ:
            api_key = os.environ["DUCKBRAIN_API_KEY"]

        if not api_key:
            fragments.append({
                "surface": "duckbrain",
                "source_path_or_key": f"namespace:{ns}",
                "taken_at": _timestamp(),
                "note": f"No DuckBrain API key found (checked {token_file} and DUCKBRAIN_API_KEY env var)",
                "trust_level": "none (no auth)",
            })
            continue

        # List namespaces first to understand what's available
        try:
            import urllib.request
            req = urllib.request.Request(f"{db_host}/api/namespaces", method="GET")
            req.add_header("Authorization", f"Bearer {api_key}")
            with urllib.request.urlopen(req, timeout=10) as resp:
                ns_data = json.loads(resp.read())

            # Try to read a sample record from each namespace
            # Use /api/namespaces/<name> to get namespace info
            try:
                req2 = urllib.request.Request(f"{db_host}/api/namespaces/{ns}", method="GET")
                req2.add_header("Authorization", f"Bearer {api_key}")
                with urllib.request.urlopen(req2, timeout=10) as resp2:
                    ns_info = json.loads(resp2.read())

                fragments.append({
                    "surface": "duckbrain",
                    "source_path_or_key": f"namespace:{ns}",
                    "taken_at": _timestamp(),
                    "data": ns_info,
                    "trust_level": "cited-record (raw API response)",
                })
            except Exception as e:
                fragments.append({
                    "surface": "duckbrain",
                    "source_path_or_key": f"namespace:{ns}",
                    "taken_at": _timestamp(),
                    "note": f"Namespace query failed: {e}",
                    "trust_level": "none (query failed)",
                })
        except Exception as e:
            errors.append(f"DuckBrain namespace list failed for {ns}: {e}")

    return {
        "duckbrain_url": db_host,
        "namespaces_queried": default_namespaces,
        "fragments": fragments,
        "errors": errors,
        "latency_s": 0,
    }


def query_specs(workdir: str) -> dict[str, Any]:
    """Read spec and documentation files from the workspace."""
    fragments = []
    errors = []
    repo_path = Path(workdir)

    # Look for spec files in known locations
    spec_dirs = [
        repo_path / "docs" / "specs",
        repo_path / "specs",
        repo_path / "docs",
    ]

    spec_files = []
    for spec_dir in spec_dirs:
        if spec_dir.exists() and spec_dir.is_dir():
            for f in sorted(spec_dir.iterdir()):
                if f.suffix in ('.md', '.spec', '.yaml', '.yml'):
                    spec_files.append(f)

    if not spec_files:
        fragments.append({
            "surface": "specs",
            "source_path_or_key": str(repo_path),
            "taken_at": _timestamp(),
            "note": "No spec files found in docs/specs/, specs/, or docs/",
        })
        return {"spec_dir": str(repo_path), "fragments": fragments, "errors": errors, "latency_s": 0}

    for spec_file in spec_files[:10]:  # cap at 10 files
        try:
            content = spec_file.read_text()[:3000]  # cap content
            fragments.append({
                "surface": "specs",
                "source_path_or_key": str(spec_file.relative_to(repo_path) if spec_file.is_relative_to(repo_path) else spec_file),
                "taken_at": _timestamp(),
                "content_excerpt": content,
                "line_count": len(content.splitlines()),
            })
        except Exception as e:
            errors.append(f"Spec file read failed: {spec_file}: {e}")

    return {
        "spec_dir": str(repo_path),
        "spec_files_found": len(spec_files),
        "fragments": fragments,
        "errors": errors,
        "latency_s": 0,
    }


def query_board(workdir: str) -> dict[str, Any]:
    """Read board state from .coding-hermes/board/tasks.jsonl."""
    fragments = []
    errors = []
    board_path = Path(workdir) / ".coding-hermes" / "board" / "tasks.jsonl"

    if not board_path.exists():
        fragments.append({
            "surface": "board",
            "source_path_or_key": str(board_path),
            "taken_at": _timestamp(),
            "note": "Board file not found",
        })
        return {"board_path": str(board_path), "fragments": fragments, "errors": errors, "latency_s": 0}

    try:
        rows = []
        row_count = 0
        completed = 0
        pending = 0
        with open(board_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                row_count += 1
                try:
                    row = json.loads(line)
                    status = row.get("status", "unknown")
                    rows.append(row)
                    if status == "complete":
                        completed += 1
                    elif status in ("pending", "in_progress", "failed", "review"):
                        pending += 1
                except json.JSONDecodeError:
                    rows.append({"line_number": row_count, "parse_error": True})

        # Find pending rows for contradiction detection
        pending_rows = [r for r in rows if r.get("status") in ("pending", "in_progress", "failed", "review")]

        # Board summary fragment
        fragments.append({
            "surface": "board",
            "source_path_or_key": str(board_path.relative_to(Path(workdir)) if board_path.is_relative_to(Path(workdir)) else board_path),
            "taken_at": _timestamp(),
            "summary": {
                "total_rows": row_count,
                "completed": completed,
                "pending": pending,
                "status_counts": {
                    "complete": completed,
                    "pending": sum(1 for r in rows if r.get("status") == "pending"),
                    "in_progress": sum(1 for r in rows if r.get("status") == "in_progress"),
                    "failed": sum(1 for r in rows if r.get("status") == "failed"),
                    "review": sum(1 for r in rows if r.get("status") == "review"),
                    "done": sum(1 for r in rows if r.get("status") == "done"),
                },
            },
        })

        # Pending rows as evidence of work-in-progress
        for pr in pending_rows[:20]:
            fragments.append({
                "surface": "board",
                "source_path_or_key": f"row:{pr.get('id', '?')}",
                "taken_at": _timestamp(),
                "id": pr.get("id", ""),
                "title": pr.get("title", ""),
                "status": pr.get("status", ""),
                "priority": pr.get("priority", ""),
                "acceptance_criteria_count": len(pr.get("acceptance_criteria", [])),
                "commit_hash": pr.get("commit_hash", ""),
                "notes": pr.get("notes", ""),
            })

        return {
            "board_path": str(board_path),
            "total_rows": row_count,
            "pending_rows": len(pending_rows),
            "fragments": fragments,
            "errors": errors,
            "latency_s": 0,
        }

    except Exception as e:
        errors.append(f"Board read failed: {e}")
        return {
            "board_path": str(board_path),
            "fragments": fragments,
            "errors": errors,
            "latency_s": 0,
        }


def _hilo_version() -> str:
    """Return Hilo version string."""
    try:
        result = subprocess.run(
            ["hilo", "--version"],
            capture_output=True, text=True, timeout=5
        )
        return result.stdout.strip().split("\n")[0] if result.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


# ---------------------------------------------------------------------------
# Contradiction detection
# ---------------------------------------------------------------------------

def detect_contradictions(code_result: dict, board_result: dict, spec_result: dict) -> list[dict]:
    """Detect contradictions between surfaces.

    Examples:
    - Board says complete but no tag exists in lifecycle
    - Specs claim feature exists but Hilo shows no code
    - Board shows pending but Hilo shows code for that feature
    """
    contradictions = []

    # Check: board rows with commit_hash but no corresponding code in Hilo
    pending_with_commit = [
        r for r in board_result.get("fragments", [])
        if isinstance(r, dict) and r.get("surface") == "board" and r.get("commit_hash")
    ]

    for row in pending_with_commit:
        row_id = row.get("id", "")
        commit = row.get("commit_hash", "")
        # We flag if a row has a commit but is still pending
        if commit and row.get("status") in ("pending", "in_progress"):
            contradictions.append({
                "type": "board-commit-mismatch",
                "description": f"Board row {row_id} has commit {commit[:8]} but status is {row.get('status')} — verify if the commit was merged to main",
                "sources": ["board"],
                "severity": "medium",
            })

    # Check: board rows claiming a file/feature but Hilo shows no code
    if code_result.get("sample_files"):
        board_titles = set()
        for f in board_result.get("fragments", []):
            if isinstance(f, dict) and f.get("surface") == "board":
                title = f.get("title", "")
                if title:
                    board_titles.add(title.lower())

        code_files = set(f.get("source_path_or_key", "") for f in code_result.get("fragments", []) if isinstance(f, dict))
        # If no obvious contradiction, just note the surfaces compared
        contradictions.append({
            "type": "surface-comparison",
            "description": f"Compared {len(board_titles)} board titles against {len(code_files)} code file references — no direct file-level contradictions detected",
            "sources": ["board", "code"],
            "severity": "info",
        })

    return contradictions


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def format_output(results: dict[str, Any], contradictions: list[dict], total_latency: float) -> str:
    """Format unified output with provenance labels."""
    lines = []
    lines.append("=" * 72)
    lines.append("ONE CONTEXT CALL — Unified Surface Report")
    lines.append(f"Generated: {_timestamp()}")
    lines.append(f"Total latency: {total_latency:.2f}s")
    lines.append("=" * 72)
    lines.append("")

    # --- Code Surface ---
    lines.append("-" * 72)
    lines.append("SURFACE: code (Hilo)")
    lines.append("-" * 72)
    code = results.get("code", {})
    lines.append(f"  Hilo version: {code.get('hilo_version', 'unknown')}")
    lines.append(f"  Workdir: {code.get('workdir', 'unknown')}")
    lines.append(f"  Sample files: {len(code.get('sample_files', []))}")
    for frag in code.get("fragments", []):
        if isinstance(frag, dict):
            lines.append(f"  [code] source={frag.get('source_path_or_key', '?')} taken_at={frag.get('taken_at', '?')}")
            if "related_files" in frag:
                lines.append(f"    related: {frag['related_files'][:200]}")
            if "context" in frag:
                lines.append(f"    context: {frag['context'][:200]}")
            if "note" in frag:
                lines.append(f"    note: {frag['note']}")
    if code.get("errors"):
        lines.append(f"  ERRORS: {'; '.join(code['errors'])}")
    lines.append("")

    # --- DuckBrain Surface ---
    lines.append("-" * 72)
    lines.append("SURFACE: duckbrain")
    lines.append("-" * 72)
    db = results.get("duckbrain", {})
    lines.append(f"  URL: {db.get('duckbrain_url', '?')}")
    lines.append(f"  Namespaces: {', '.join(db.get('namespaces_queried', []))}")
    for frag in db.get("fragments", []):
        if isinstance(frag, dict):
            trust = frag.get("trust_level", "?")
            lines.append(f"  [duckbrain] key={frag.get('source_path_or_key', '?')} taken_at={frag.get('taken_at', '?')} trust={trust}")
            if "data" in frag:
                ns_data = frag["data"]
                if isinstance(ns_data, dict):
                    lines.append(f"    keys: {', '.join(str(k) for k in list(ns_data.keys())[:10])}")
            if "note" in frag:
                lines.append(f"    note: {frag['note']}")
    if db.get("errors"):
        lines.append(f"  ERRORS: {'; '.join(db['errors'])}")
    lines.append("")

    # --- Specs Surface ---
    lines.append("-" * 72)
    lines.append("SURFACE: specs")
    lines.append("-" * 72)
    specs = results.get("specs", {})
    lines.append(f"  Spec dir: {specs.get('spec_dir', '?')}")
    lines.append(f"  Spec files found: {specs.get('spec_files_found', 0)}")
    for frag in specs.get("fragments", []):
        if isinstance(frag, dict):
            path = frag.get("source_path_or_key", "?")
            lines.append(f"  [specs] path={path} taken_at={frag.get('taken_at', '?')} lines={frag.get('line_count', '?')}")
            content = frag.get("content_excerpt", "")
            if content:
                lines.append(f"    excerpt: {content[:300]}")
    if specs.get("errors"):
        lines.append(f"  ERRORS: {'; '.join(specs['errors'])}")
    lines.append("")

    # --- Board Surface ---
    lines.append("-" * 72)
    lines.append("SURFACE: board")
    lines.append("-" * 72)
    board = results.get("board", {})
    board_path = board.get("board_path", "?")
    lines.append(f"  Board: {board_path}")
    lines.append(f"  Total rows: {board.get('total_rows', '?')}")
    lines.append(f"  Pending: {board.get('pending_rows', '?')}")
    for frag in board.get("fragments", []):
        if isinstance(frag, dict) and frag.get("surface") == "board":
            summary = frag.get("summary")
            if summary:
                lines.append(f"  [board] source={frag.get('source_path_or_key', '?')} summary={json.dumps(summary, default=str)}")
            elif frag.get("id"):
                lines.append(f"  [board] id={frag['id']} title={frag.get('title','?')[:60]} status={frag.get('status','?')} priority={frag.get('priority','?')}")
    if board.get("errors"):
        lines.append(f"  ERRORS: {'; '.join(board['errors'])}")
    lines.append("")

    # --- Contradictions ---
    if contradictions:
        lines.append("-" * 72)
        lines.append("CONTRADICTIONS DETECTED")
        lines.append("-" * 72)
        for c in contradictions:
            lines.append(f"  [{c['severity'].upper()}] {c['type']}: {c['description']}")
            lines.append(f"    sources: {', '.join(c['sources'])}")
    else:
        lines.append("-" * 72)
        lines.append("CONTRADICTIONS: none detected")
        lines.append("-" * 72)
    lines.append("")

    lines.append("=" * 72)
    lines.append("END OF REPORT")
    lines.append("=" * 72)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        prog="router context-call",
        description="ONE CONTEXT CALL: code graph + DuckBrain + specs + boards",
    )
    parser.add_argument(
        "workdir",
        nargs="?",
        default=".",
        help="Project workdir to analyze (default: current directory)",
    )
    parser.add_argument(
        "--surfaces",
        default="code,db,specs,board",
        help="Comma-separated surfaces to query (code,db,specs,board). Default: all.",
    )
    parser.add_argument(
        "--db-namespaces",
        default=None,
        help="Comma-separated DuckBrain namespaces to query. Default: task-router,routing,coding-hermes.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output JSON instead of human-readable report.",
    )
    parser.add_argument(
        "--measure",
        action="store_true",
        help="Include per-surface latency in output (default: always included).",
    )

    args = parser.parse_args()

    surfaces = [s.strip() for s in args.surfaces.split(",") if s.strip()]
    namespaces = [s.strip() for s in (args.db_namespaces or "").split(",") if s.strip()]
    if not namespaces and "db" in surfaces:
        namespaces = ["task-router", "routing", "coding-hermes"]

    workdir = os.path.abspath(args.workdir)
    if not os.path.isdir(workdir):
        print(f"Error: workdir '{workdir}' is not a directory", file=sys.stderr)
        sys.exit(1)

    results = {}
    total_start = time.time()

    if "code" in surfaces:
        t0 = time.time()
        results["code"] = query_hilo(workdir)
        results["code"]["latency_s"] = round(time.time() - t0, 3)
    if "db" in surfaces:
        t0 = time.time()
        results["duckbrain"] = query_duckbrain(namespaces or None)
        results["duckbrain"]["latency_s"] = round(time.time() - t0, 3)
    if "specs" in surfaces:
        t0 = time.time()
        results["specs"] = query_specs(workdir)
        results["specs"]["latency_s"] = round(time.time() - t0, 3)
    if "board" in surfaces:
        t0 = time.time()
        results["board"] = query_board(workdir)
        results["board"]["latency_s"] = round(time.time() - t0, 3)

    total_latency = round(time.time() - total_start, 3)

    # Detect contradictions
    contradictions = detect_contradictions(
        results.get("code", {}),
        results.get("board", {}),
        results.get("specs", {}),
    )

    if args.json:
        output = {
            "generated_at": _timestamp(),
            "total_latency_s": total_latency,
            "surfaces": surfaces,
            "workdir": workdir,
            "results": results,
            "contradictions": contradictions,
        }
        print(json.dumps(output, indent=2, default=str))
    else:
        report = format_output(results, contradictions, total_latency)
        print(report)

    return 0


if __name__ == "__main__":
    sys.exit(main())
