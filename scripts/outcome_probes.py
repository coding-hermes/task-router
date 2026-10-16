#!/usr/bin/env python3
"""outcome_probes.py — TR-251 mechanism (3): the outcome-probe library.

The criterion that would have caught TR-011 is an OUTCOME probe — a check on
the deliverable's effect, authored BEFORE dispatch by the coordinator, not by
the completing agent. Authorship is the whole point: TR-011 had four criteria
and a PASS guard and still lied, because the completing agent graded its own
homework.

This module is the library and its audit surface:

  * probes/<project>.json — one probe file per active project (derived from
    the scheduler DB's enabled lanes via outcome_probe_projects.py, collapsed
    to family primaries). Each file carries the probe statements plus the
    authorship record (authored_by, authored_at) that makes authorship
    auditable (criterion 4: "the row records who authored it and when").
  * `check` — validate a probe file's shape (id, statement, kind).
  * `ensure` — create any missing probe file from the template (the library
    can be brought to full coverage mechanically; the STATEMENTS are then
    edited by the author).
  * `coverage` — report projects with/without a probe file.
  * `record` — append the authorship record to a board row's fields
    (probe_authored_by / probe_authored_at / probe_id) so the ROW carries
    the auditable authorship, not just the library.

A probe KIND is one of:
  outcome    — a check on the deliverable's effect in the world
               ("routed_share moved", "the ingress resolves with >=1 hop")
  invariant  — a check that must hold on every run (regression shape)
  measure    — a number to read and record (the probe is the READ, not the fix)

ch:trace row=TR-251 spec=docs/traceability-doctrine.md#hard-rules test=tests/test_outcome_probes.py evidence=docs/evidence-run-store-decision.md
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
from datetime import datetime, timezone

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))

from outcome_probe_projects import load_primary_projects  # noqa: E402

PROBES_DIR = os.path.join(REPO, "outcome-probes")
PROJECTS_PATH_FALLBACK = os.path.join(REPO, "data", "tables", "projects.jsonl")
DEFAULT_AUTHOR = os.environ.get("TR251_PROBE_AUTHOR", "board-coordinator")

PROBE_KINDS = ("outcome", "invariant", "measure")

#: The template statement every auto-created probe starts with. It is
#: deliberately a PLACEHOLDER the author must replace: an auto-filled probe
#: that claimed a real outcome would be manufacturing the very evidence this
#: library exists to prevent.
TEMPLATE_STATEMENT = "TODO(author): state the outcome this project's next dispatch must show"


def probe_path(project: str) -> str:
    return os.path.join(PROBES_DIR, f"{project}.json")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _flock_write(path: str, payload: dict) -> None:
    """Write one probe file, flock-serialized + fsync'd (same discipline as
    board_complete_gate's appends — probes are read by dispatch, written by
    authors, and must never tear)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            fh.seek(0)
            fh.truncate()
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _load_probe(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def validate_probe(payload: dict) -> list[str]:
    """Shape errors [] for one probe payload; non-empty = invalid."""
    errors = []
    if not payload.get("project"):
        errors.append("missing field: project")
    author = payload.get("authored_by")
    if not author or not str(author).strip():
        errors.append("missing field: authored_by (authorship must be auditable)")
    if not payload.get("authored_at"):
        errors.append("missing field: authored_at")
    probes = payload.get("probes")
    if not isinstance(probes, list) or not probes:
        errors.append("missing field: probes (at least one probe per project)")
        return errors
    seen_ids = set()
    for i, p in enumerate(probes):
        if not isinstance(p, dict):
            errors.append(f"probes[{i}]: not an object")
            continue
        pid = p.get("id")
        if not pid:
            errors.append(f"probes[{i}]: missing id")
        elif pid in seen_ids:
            errors.append(f"probes[{i}]: duplicate id {pid!r}")
        else:
            seen_ids.add(pid)
        stmt = p.get("statement")
        if not isinstance(stmt, str) or not stmt.strip():
            errors.append(f"probes[{i}] ({pid}): missing statement")
        elif stmt.strip() == TEMPLATE_STATEMENT:
            errors.append(
                f"probes[{i}] ({pid}): statement is still the template placeholder — "
                "an authored probe states a real outcome; replace it before dispatch")
        if p.get("kind") not in PROBE_KINDS:
            errors.append(
                f"probes[{i}] ({pid}): kind must be one of {PROBE_KINDS}")
    return errors


def ensure_probe(project: str, author: str) -> tuple[str, bool]:
    """Create the probe file for project when missing. Returns (path, created)."""
    path = probe_path(project)
    if os.path.exists(path):
        return path, False
    payload = {
        "project": project,
        "authored_by": author,
        "authored_at": now_iso(),
        "probes": [
            {"id": f"{project}-P1", "kind": "outcome", "statement": TEMPLATE_STATEMENT}
        ],
    }
    _flock_write(path, payload)
    return path, True


def cmd_ensure(args: argparse.Namespace) -> int:
    projects = load_primary_projects(args.projects_table or PROJECTS_PATH_FALLBACK)
    created, existing = [], 0
    for pid in projects:
        path, made = ensure_probe(pid, args.author)
        if made:
            created.append(pid)
        else:
            existing += 1
    print(f"probe library: {len(projects)} active projects, "
          f"{created and len(created)} created, {existing} already present")
    for pid in created:
        print(f"  created: {probe_path(pid)}")
    return 0


def cmd_coverage(args: argparse.Namespace) -> int:
    projects = load_primary_projects(args.projects_table or PROJECTS_PATH_FALLBACK)
    have, missing = [], []
    for pid in projects:
        (have if os.path.exists(probe_path(pid)) else missing).append(pid)
    print(f"coverage: {len(have)}/{len(projects)} active projects carry a probe file")
    if missing:
        print("missing: " + ", ".join(missing))
    if args.json:
        print(json.dumps({"have": have, "missing": missing}))
    return 0 if not missing else 1


def cmd_check(args: argparse.Namespace) -> int:
    rc = 0
    for path in args.paths:
        try:
            payload = _load_probe(path)
        except (OSError, ValueError) as exc:
            print(f"FAIL {path}: {exc}")
            rc = 1
            continue
        errors = validate_probe(payload)
        if errors:
            for e in errors:
                print(f"FAIL {path}: {e}")
            rc = 1
        else:
            print(f"OK {path}: {len(payload['probes'])} probe(s), "
                  f"authored_by={payload['authored_by']} at {payload['authored_at']}")
    return rc


def cmd_check_all(args: argparse.Namespace) -> int:
    paths = sorted(
        os.path.join(PROBES_DIR, f) for f in os.listdir(PROBES_DIR)
        if f.endswith(".json")
    ) if os.path.isdir(PROBES_DIR) else []
    if not paths:
        print("no probe files present — run `ensure` first")
        return 1
    args.paths = paths
    return cmd_check(args)


def cmd_record(args: argparse.Namespace) -> int:
    """Record the authorship on a BOARD ROW via boardctl (never hand-edit the
    JSONL): the row gains probe_id / probe_authored_by / probe_authored_at so
    a later auditor can walk row -> probe file -> author."""
    path = probe_path(args.project)
    if not os.path.exists(path):
        print(f"FAIL: no probe file for {args.project} — run ensure first", file=sys.stderr)
        return 1
    payload = _load_probe(path)
    errors = validate_probe(payload)
    if errors:
        print(f"FAIL: probe file for {args.project} is not valid: {errors[0]}", file=sys.stderr)
        return 1
    boardctl = args.boardctl or "boardctl"
    cmd = [
        boardctl, "update", args.task_id,
        "--note",
        f"probe authored pre-dispatch: probe_id={args.probe_id or payload['probes'][0]['id']} "
        f"authored_by={payload['authored_by']} authored_at={payload['authored_at']} "
        f"statement={payload['probes'][0]['statement']}",
    ]
    rc = os.spawnvpe(os.P_WAIT, boardctl, cmd, os.environ)
    if rc != 0:
        print(f"FAIL: boardctl update exited {rc}", file=sys.stderr)
        return rc
    print(f"recorded: {args.task_id} <- probe {payload['probes'][0]['id']} "
          f"(authored_by={payload['authored_by']} at {payload['authored_at']})")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="outcome_probes.py", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--projects-table", default=None,
                       help="path to projects.jsonl (default: the repo table)")

    p = sub.add_parser("ensure", help="create missing probe files (template statements)")
    common(p)
    p.add_argument("--author", default=DEFAULT_AUTHOR)
    p.set_defaults(func=cmd_ensure)

    p = sub.add_parser("coverage", help="report projects with/without a probe file")
    common(p)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_coverage)

    p = sub.add_parser("check", help="validate probe file shape(s)")
    p.add_argument("paths", nargs="+")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("check-all", help="validate every probe file in the library")
    p.set_defaults(func=cmd_check_all)

    p = sub.add_parser("record", help="record a probe's authorship on a board row (via boardctl)")
    p.add_argument("task_id")
    p.add_argument("project")
    p.add_argument("--probe-id", default=None)
    p.add_argument("--boardctl", default=None)
    p.set_defaults(func=cmd_record)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
