#!/usr/bin/env python3
"""Blocking capability-vocabulary parity gate (TR-382).

Asserts that the set of capability categories declared in
data/tables/task_profile_requirements.jsonl (field ``category``) exactly
matches the set of ``perf_*`` columns present in data/tables/models.jsonl.

Exit 0 on parity; exit 1 with a clear message listing missing/extra
dimensions on divergence. Stdlib only.

Usage:
    python3 scripts/test_vocabulary_parity.py [repo_root]

repo_root defaults to the parent of this script's directory. When invoked
as a module (``from test_vocabulary_parity import check_parity``),
``check_parity()`` returns ``(ok, message)``.
"""

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REQUIREMENTS = REPO_ROOT / "data" / "tables" / "task_profile_requirements.jsonl"
MODELS = REPO_ROOT / "data" / "tables" / "models.jsonl"
PREFIX = "perf_"


def load_jsonl(path):
    rows = []
    with open(path, "r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(
                    f"FAIL: {path}:{lineno}: invalid JSON ({exc})"
                )
    return rows


def check_parity(requirements_path=REQUIREMENTS, models_path=MODELS):
    """Return (ok, message) for the vocabulary parity check."""
    req_rows = load_jsonl(requirements_path)
    req_cats = {row["category"] for row in req_rows}

    perf_cols = set()
    for row in load_jsonl(models_path):
        for key in row:
            if key.startswith(PREFIX):
                perf_cols.add(key[len(PREFIX):])

    missing = sorted(req_cats - perf_cols)   # declared but no perf_ column
    extra = sorted(perf_cols - req_cats)     # perf_ column never declared

    if missing or extra:
        lines = ["VOCABULARY PARITY FAILURE (EXTEND contract TR-382):"]
        if missing:
            lines.append(
                "  declared in task_profile_requirements but MISSING a "
                "perf_ column in models.jsonl: " + ", ".join(missing)
            )
        if extra:
            lines.append(
                "  perf_ column in models.jsonl but NEVER DECLARED in "
                "task_profile_requirements: " + ", ".join(extra)
            )
        lines.append(
            f"  declared={len(req_cats)} perf_columns={len(perf_cols)}"
        )
        return False, "\n".join(lines)

    return True, (
        f"VOCABULARY PARITY OK: {len(req_cats)} categories, "
        f"declared set == perf_* column set."
    )


def main(argv):
    if len(argv) > 1:
        root = Path(argv[1]).resolve()
        req = root / "data" / "tables" / "task_profile_requirements.jsonl"
        mdl = root / "data" / "tables" / "models.jsonl"
    else:
        req, mdl = REQUIREMENTS, MODELS

    for path in (req, mdl):
        if not path.is_file():
            print(f"FAIL: required table not found: {path}")
            return 1

    ok, message = check_parity(req, mdl)
    print(message)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
