"""TR-197 — the NULL census: every null on a LIVE lane carries a reason.

Hermetic: drives scripts/data_null_census.py against SCRATCH data dirs
(ROUTING_DATA_DIR override — the same pattern TR-005 pinned for
router_spawn.py), never the committed tables. Pins:

  AC1  the census reports per table/column null counts and classifies each
       null MEANINGFUL (with a reason) or UNEXPLAINED
  AC2  the MEANINGFUL reason vocabulary is exactly the finite 7-member set
  AC3  an unstamped, non-structural live-lane null IS unexplained (mutation
       RED: a lane whose stamps are stripped goes UNEXPLAINED)
  AC4  --fail-on-unexplained exits 1 while any live-lane null is unexplained
       and 0 once the stamp lands (the gate flips, not the assertion)
  the vocabulary gate: a null_reasons.jsonl row with a reason outside the
       finite set must LOUDLY refuse (never silently classify)
"""

import json
import os
import shutil
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")
CENSUS = os.path.join(SCRIPTS, "data_null_census.py")
PY = sys.executable


def _scratch(tmp_path, *, with_stamp):
    """Tables copy + (optionally) the real stamp registry, via env override."""
    tables = tmp_path / "tables"
    shutil.copytree(os.path.join(REPO, "data", "tables"), tables)
    if with_stamp:
        shutil.copy(
            os.path.join(REPO, "data", "null_reasons.jsonl"),
            tmp_path / "null_reasons.jsonl",
        )
    return tables


def _run(tables_dir, *args):
    env = dict(os.environ)
    env["ROUTING_DATA_DIR"] = str(tables_dir)
    return subprocess.run(
        [PY, CENSUS, *args],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        cwd=REPO,
    )


def _models(tables_dir):
    return _load(tables_dir, "models.jsonl")


def _load(tables_dir, name):
    out = []
    with open(os.path.join(tables_dir, name)) as f:
        for line in f:
            if line.strip():
                out.append(json.loads(line))
    return out


def _write_models(tables_dir, rows):
    with open(os.path.join(tables_dir, "models.jsonl"), "w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# AC2: the vocabulary is the finite set, on the REAL stamp registry
# ---------------------------------------------------------------------------


def test_stamp_registry_vocabulary_is_the_finite_ac2_set():
    path = os.path.join(REPO, "data", "null_reasons.jsonl")
    with open(path) as f:
        rows = [json.loads(line) for line in f if line.strip()]
    assert rows, "the stamp registry must exist and carry rows"
    allowed = {
        "disabled",
        "archived",
        "retired-by-date",
        "not-published-by-provider",
        "unpriced-pending-sticker",
        "no-sample-yet",
        "unknown-by-design",
    }
    reasons = {r["reason"] for r in rows}
    assert reasons <= allowed, reasons - allowed
    # the census module declares the same set
    sys.path.insert(0, SCRIPTS)
    import data_null_census as nc  # noqa: E402

    assert set(nc.REASONS) == allowed
    # every stamp targets a field the row actually leaves null (proven truth)
    models = {
        (m["provider"], m["model"]): m
        for m in _models(os.path.join(REPO, "data", "tables"))
    }
    for r in rows:
        m = models.get((r["provider"], r["model"]))
        assert m is not None, (r["provider"], r["model"])
        assert m.get(r["field"]) is None, (r["provider"], r["model"], r["field"])


# ---------------------------------------------------------------------------
# AC1 + AC4: classification and the gate, on scratch copies
# ---------------------------------------------------------------------------


def test_census_counts_nulls_per_table_and_column(tmp_path):
    tables = _scratch(tmp_path, with_stamp=True)
    r = _run(tables, "--json")
    assert r.returncode == 0, r.stderr
    doc = json.loads(r.stdout)
    assert doc["metric"].startswith("live-lane UNEXPLAINED")
    models = doc["tables"]["models.jsonl"]
    assert models["rows"] == len(_models(tables))
    # every null column of the real data appears in the per-column census
    nulls = {}
    for m in _models(tables):
        for k, v in m.items():
            if v is None or v == "":
                nulls[k] = nulls.get(k, 0) + 1
    for col, n in nulls.items():
        assert models["by_column"].get(col) == n, col
    # classification is total: live nulls across ALL tables split exactly into
    # MEANINGFUL (by_reason) + UNEXPLAINED (the metric)
    classified = doc["live_nulls_by_reason"]
    total = sum(classified.values()) + doc["live_unexplained_count"]
    live_nulls = 0
    for fn, rows in (
        (f, _load(tables, f)) for f in os.listdir(tables) if f.endswith(".jsonl")
    ):
        for m in rows:
            if _is_live(m):
                live_nulls += sum(1 for v in m.values() if v is None or v == "")
    assert total == live_nulls


def _is_live(m):
    return not m.get("archive") and not m.get("disabled") and not m.get("valid_to")


def test_gate_fails_then_flips_green_on_stamp(tmp_path):
    """AC3/AC4 mutation RED: strip the stamps for one lane -> the census must
    call its nulls UNEXPLAINED and --fail-on-unexplained must exit 1; restore
    the stamp -> exit 0. The gate flips on the data, not on the assertion."""
    tables = _scratch(tmp_path, with_stamp=True)
    stamp_path = tmp_path / "null_reasons.jsonl"

    # pick a stamped lane with a price-class null
    rows = [json.loads(line) for line in open(stamp_path) if line.strip()]
    target = next(
        r
        for r in rows
        if r["field"] == "normalized_price"
        and r["reason"] == "not-published-by-provider"
    )
    key = (target["provider"], target["model"])

    # ARM 1 — stamps stripped for that lane's null fields: RED expected
    kept = [
        r
        for r in rows
        if (r["provider"], r["model"]) != key
        or r["field"]
        not in (
            "normalized_price",
            "public_price",
            "public_in_per_m",
            "public_out_per_m",
        )
    ]
    with open(stamp_path, "w") as f:
        for r in kept:
            f.write(json.dumps(r) + "\n")
    doc = json.loads(_run(tables, "--json").stdout)
    unexp = {(u["provider"], u["model"], u["field"]) for u in doc["unexplained_rows"]}
    for field in (
        "normalized_price",
        "public_price",
        "public_in_per_m",
        "public_out_per_m",
    ):
        assert (key[0], key[1], field) in unexp, field
    gate = _run(tables, "--fail-on-unexplained")
    assert gate.returncode == 1

    # ARM 2 — stamp restored (the real registry): GREEN
    shutil.copy(os.path.join(REPO, "data", "null_reasons.jsonl"), stamp_path)
    doc = json.loads(_run(tables, "--json").stdout)
    assert doc["live_unexplained_count"] == 0
    assert _run(tables, "--fail-on-unexplained").returncode == 0


def test_live_metric_ignores_non_live_lanes(tmp_path):
    """A null on a retired/disabled/archived lane is MEANINGFUL (its lane
    state), never part of the live metric."""
    tables = _scratch(tmp_path, with_stamp=True)
    rows = _models(tables)
    target = next(
        m for m in rows if not _is_live(m) and m.get("normalized_price") is None
    )
    target["price_evidence"] = "no story at all"
    _write_models(tables, rows)
    doc = json.loads(_run(tables, "--json").stdout)
    hits = [
        u
        for u in doc["unexplained_rows"]
        if (u["provider"], u["model"]) == (target["provider"], target["model"])
        and u["field"] == "normalized_price"
    ]
    assert hits == []


def test_vocabulary_violation_refuses_loudly(tmp_path):
    """A stamp carrying a reason outside the finite AC2 set must LOUDLY
    refuse (SystemExit), never silently classify."""
    tables = _scratch(tmp_path, with_stamp=True)
    with open(tmp_path / "null_reasons.jsonl", "a") as f:
        f.write(
            json.dumps(
                {
                    "provider": "x",
                    "model": "y",
                    "field": "z",
                    "reason": "because-i-said-so",
                    "evidence": "none",
                    "ts": "2026-10-01",
                }
            )
            + "\n"
        )
    r = _run(tables)
    assert r.returncode != 0
    assert "outside the AC2 vocabulary" in (r.stdout + r.stderr)


def test_unstamped_unstructural_live_null_is_unexplained(tmp_path):
    """A live lane with a null no stamp covers and no structural meaning is
    UNEXPLAINED — the census must NOT bless it."""
    tables = _scratch(tmp_path, with_stamp=True)
    rows = _models(tables)
    live_priced = next(
        m for m in rows if _is_live(m) and m.get("price_evidence") is not None
    )
    live_priced["price_evidence"] = None
    _write_models(tables, rows)
    doc = json.loads(_run(tables, "--json").stdout)
    assert any(
        u["field"] == "price_evidence"
        and (u["provider"], u["model"])
        == (live_priced["provider"], live_priced["model"])
        for u in doc["unexplained_rows"]
    )
    assert _run(tables, "--fail-on-unexplained").returncode == 1
