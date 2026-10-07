"""TR-201 — null-debt PREVENTION invariants: red/green acceptance evidence.

Hermetic: every test drives scripts/null_debt_invariants.py against a SCRATCH
fixture dir via ROUTING_DATA_DIR (the pattern TR-005 pinned, reused by
test_null_census.py), never the committed tables — except the one test that
pins "the gate passes on the real data today".

Per invariant (INV1 wipe-shape, INV2 dead column, INV3 anonymous lifecycle
date): a deliberately-broken fixture must FAIL the gate (exit 1, violation
named with table/lane/fraction) and the repaired twin must PASS (exit 0).
That red/green pairing is the acceptance evidence — a gate that only ever
ran green proves nothing about whether it can fail.

Design pins also covered:
  * TR-197 STRUCTURAL columns (designed absence: perf_*, thinking, ...) are
    EXCLUDED from the INV1/INV2 measurement — a live lane whose only nulls
    are structural must NOT trip INV1 (mutation guard on the deviation this
    gate deliberately takes from a naive all-columns fraction).
  * NULL_OK allows a 100%-null column only WITH a reason string.
  * --publish writes {timestamp, git_sha, unexplained, inv1, inv2, inv3}
    where `unexplained` is data_null_census.py's own live-UNEXPLAINED count
    (delegated subprocess, not a re-implementation).
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "scripts", "null_debt_invariants.py")
CENSUS = os.path.join(REPO, "scripts", "data_null_census.py")
PY = sys.executable

_spec = importlib.util.spec_from_file_location("null_debt_invariants", SCRIPT)
ndi = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ndi)


def _run(tables_dir, *args):
    env = {k: v for k, v in os.environ.items() if k != "ROUTING_DATA_DIR"}
    env["ROUTING_DATA_DIR"] = str(tables_dir)
    return subprocess.run(
        [PY, SCRIPT, *args],
        capture_output=True, text=True, timeout=120, env=env, cwd=REPO,
    )


def _fixture(tmp_path, tables):
    """{filename: [row, ...]} -> scratch tables dir; returns the dir path."""
    d = tmp_path / "tables"
    d.mkdir(parents=True, exist_ok=True)
    for fn, rows in tables.items():
        with open(d / fn, "w") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return d


# --------------------------------------------------------------------------
# INV1 — live lane >= 50% null across measured (non-structural) columns
# --------------------------------------------------------------------------

def _models_pair(broken_values):
    """Two live models.jsonl rows sharing a 6-key vocabulary; the broken one
    carries `broken_values`. Measured cols = the 6 keys (none structural)."""
    base = {
        "provider": "prov-a",
        "api_type": "openai-chat",
        "normalized_price": 1.5,
        "price_evidence": "board-row-123",
        "token_factor": 1.0,
        "valid_from": "2026-01-01",
    }
    clean = dict(base, model="clean-lane")
    broken = dict(base, model="wiped-lane")
    broken.update(broken_values)
    return clean, broken


def test_inv1_red_wiped_live_lane_fails(tmp_path):
    clean, broken = _models_pair(
        {"api_type": None, "normalized_price": None, "price_evidence": None})
    d = _fixture(tmp_path, {"models.jsonl": [clean, broken]})
    r = _run(d, "--json")
    assert r.returncode == 1, r.stdout + r.stderr
    doc = json.loads(r.stdout)
    v = doc["inv1"]["violations"]
    assert len(v) == 1, v
    assert v[0]["table"] == "models.jsonl"
    assert v[0]["lane"] == "prov-a/wiped-lane"
    assert v[0]["fraction"] >= 0.50
    assert "price_evidence" in v[0]["null_fields"]
    # the clean twin is never named
    assert all(x["lane"] != "prov-a/clean-lane" for x in v)


def test_inv1_green_repaired_lane_passes(tmp_path):
    clean, broken = _models_pair({})
    d = _fixture(tmp_path, {"models.jsonl": [clean, broken]})
    assert _run(d).returncode == 0


def test_inv1_green_structural_only_nulls_never_trip(tmp_path):
    """A live lane whose ONLY nulls are TR-197 STRUCTURAL (perf_*, thinking,
    plan_tier, ...) is designed absence, not wipe debt — INV1 must stay
    green. Mutation guard for the structural-exclusion deviation."""
    clean, broken = _models_pair({})
    broken.update({
        "perf_agent_tick": None,
        "perf_debug": None,
        "thinking": None,
        "vision": None,
        "plan_tier": None,
        "release_date": None,
        "lifecycle_source": None,
    })
    d = _fixture(tmp_path, {"models.jsonl": [clean, broken]})
    assert _run(d).returncode == 0


# --------------------------------------------------------------------------
# INV2 — 100%-null column without a NULL_OK reason
# --------------------------------------------------------------------------

def _audit_rows(note_value):
    return [
        {"provider": "prov-a", "model": "m1", "note_x": note_value, "status": "a"},
        {"provider": "prov-a", "model": "m2", "note_x": note_value, "status": "b"},
    ]


def test_inv2_red_dead_column_fails(tmp_path):
    d = _fixture(tmp_path, {"probe_audit.jsonl": _audit_rows(None)})
    r = _run(d, "--json")
    assert r.returncode == 1, r.stdout + r.stderr
    v = json.loads(r.stdout)["inv2"]["violations"]
    assert len(v) == 1, v
    assert v[0]["table"] == "probe_audit.jsonl"
    assert v[0]["column"] == "note_x"
    assert v[0]["carriers"] == 2
    assert v[0]["fraction"] == 1.0
    # empty string counts as null too (same rule as the census)
    d2 = _fixture(tmp_path / "s2", {"probe_audit.jsonl": _audit_rows("")})
    d2.parent.mkdir(exist_ok=True)
    r2 = _run(d2, "--json")
    assert r2.returncode == 1
    assert any(x["column"] == "note_x" for x in json.loads(r2.stdout)["inv2"]["violations"])


def test_inv2_green_column_carries_values(tmp_path):
    d = _fixture(tmp_path, {"probe_audit.jsonl": _audit_rows("filled by TR-201")})
    assert _run(d).returncode == 0


def test_inv2_green_null_ok_allowlist_with_reason():
    """NULL_OK admits a dead column only when the (table, column) entry
    carries a reason string, and only for that exact pair. Checked on the
    gate function directly: the gate script runs in a subprocess, so a
    monkeypatched allow-list cannot cross the process boundary — but the
    file's own entries are already proven end-to-end by the real-data
    green tests above/below."""
    rows = _audit_rows(None)
    red = ndi.check_inv2({"probe_audit.jsonl": rows}, null_ok={})
    assert red and red[0]["column"] == "note_x" and red[0]["carriers"] == 2, red
    reason = "row contract: note_x stays explicitly null until audited"
    ok = {("probe_audit.jsonl", "note_x"): reason}
    assert ndi.check_inv2({"probe_audit.jsonl": rows}, null_ok=ok) == []
    # a DIFFERENT column is not covered by that entry
    rows_other = _audit_rows(None)
    for r in rows_other:
        r["other_col"] = None
    v = ndi.check_inv2({"probe_audit.jsonl": rows_other}, null_ok=ok)
    assert [x["column"] for x in v] == ["other_col"], v
    # an entry whose value is NOT a reason string admits nothing
    assert ndi.check_inv2({"probe_audit.jsonl": _audit_rows(None)},
                          null_ok={("probe_audit.jsonl", "note_x"): ""}) != []


# --------------------------------------------------------------------------
# INV3 — anonymous lifecycle dates (delegates to lifecycle_gate, TR-199 R4)
# --------------------------------------------------------------------------

def _discount_rows(sourced):
    row = {"provider": "prov-a", "model": "m1", "valid_to": "2026-12-31"}
    if sourced:
        row["lifecycle_source"] = "board row TR-199 stamp"
    return [row]


def test_inv3_red_anonymous_date_fails(tmp_path):
    d = _fixture(tmp_path, {"temporary_discounts.jsonl": _discount_rows(False)})
    r = _run(d, "--json")
    assert r.returncode == 1, r.stdout + r.stderr
    v = json.loads(r.stdout)["inv3"]["violations"]
    assert len(v) == 1, v
    assert v[0]["table"] == "temporary_discounts.jsonl"
    assert v[0]["dated_fields"] == {"valid_to": "2026-12-31"}
    assert v[0]["lifecycle_source"] == "<unset>"


def test_inv3_green_sourced_date_passes(tmp_path):
    d = _fixture(tmp_path, {"temporary_discounts.jsonl": _discount_rows(True)})
    assert _run(d).returncode == 0


def test_inv3_undated_row_needs_no_source(tmp_path):
    """R4 gates DATES only: an undated row with no lifecycle_source is fine."""
    d = _fixture(tmp_path, {"temporary_discounts.jsonl": [
        {"provider": "prov-a", "model": "m1", "valid_to": None},
    ]})
    assert _run(d).returncode == 0


# --------------------------------------------------------------------------
# the whole gate on the REAL committed tables (acceptance: exits 0 today)
# --------------------------------------------------------------------------

def test_real_tables_gate_green():
    env = {k: v for k, v in os.environ.items() if k != "ROUTING_DATA_DIR"}
    r = subprocess.run([PY, SCRIPT], capture_output=True, text=True,
                       timeout=300, env=env, cwd=REPO)
    assert r.returncode == 0, "gate failed on real data/tables:\n%s\n%s" % (
        r.stdout[-3000:], r.stderr[-2000:])
    assert "PASS" in r.stdout


def test_real_data_offenders_are_allowlisted():
    """The known live offenders must be covered by explicit, reasoned
    entries — ALLOWED and NULL_OK are the audit surface, not dead config."""
    assert "model_catalog.jsonl" in ndi.ALLOWED, ndi.ALLOWED
    assert ndi.ALLOWED["model_catalog.jsonl"] >= 0.50
    assert ("fallback_lanes.jsonl", "valid_to") in ndi.NULL_OK
    assert isinstance(ndi.NULL_OK[("fallback_lanes.jsonl", "valid_to")], str)
    assert ndi.NULL_OK[("fallback_lanes.jsonl", "valid_to")]
    assert ndi.DEFAULT_MAX == 0.50
    # every unlisted live table must clear the default on real data
    tables = ndi.load(os.path.join(REPO, "data", "tables"))
    for fn in tables:
        if fn in ndi.ALLOWED or fn not in tables or not tables[fn]:
            continue
        structural = set(ndi.census.STRUCTURAL.get(fn, {}))
        live = [r for r in tables[fn]
                if isinstance(r, dict) and ndi.census.is_live(r)]
        cols = set()
        for r in live:
            cols |= set(r.keys())
        cols -= structural
        if not cols:
            continue
        for r in live:
            n = sum(1 for k in cols if k in r and (r[k] is None or r[k] == ""))
            assert n / len(cols) < ndi.DEFAULT_MAX, (fn, r.get("provider"), r.get("model"))


# --------------------------------------------------------------------------
# --publish (AC4 watch hook): state doc with census-delegated unexplained
# --------------------------------------------------------------------------

def _publish_fixture(tmp_path):
    """Clean-lane tables carrying exactly ONE live UNEXPLAINED null
    (price_evidence=None on a live models row, unstamped, structural-safe:
    a twin row carries the same key with a value so INV2 stays quiet)."""
    base = {
        "provider": "prov-a",
        "api_type": "openai-chat",
        "normalized_price": 1.5,
        "token_factor": 1.0,
        "valid_from": "2026-01-01",
    }
    models = [
        dict(base, model="m-unexplained", price_evidence=None),
        dict(base, model="m-explained", price_evidence="board-row-9"),
    ]
    return _fixture(tmp_path, {"models.jsonl": models})


def test_publish_writes_state_doc(tmp_path):
    d = _publish_fixture(tmp_path)
    out = tmp_path / "state" / "null-census-latest.json"
    r = _run(d, "--publish", str(out))
    assert r.returncode == 0, r.stdout + r.stderr
    assert out.exists()
    doc = json.loads(out.read_text())
    assert set(doc) == {"timestamp", "git_sha", "unexplained", "inv1", "inv2", "inv3"}
    assert doc["inv1"] == 0 and doc["inv2"] == 0 and doc["inv3"] == 0
    assert doc["git_sha"] and doc["git_sha"] != "<unknown>"
    # the unexplained number IS the census's number (delegated, not re-derived)
    env = dict(os.environ, ROUTING_DATA_DIR=str(d))
    c = subprocess.run([PY, CENSUS, "--json"], capture_output=True,
                       text=True, env=env, cwd=REPO, timeout=120)
    assert c.returncode == 0
    assert doc["unexplained"] == json.loads(c.stdout)["live_unexplained_count"] == 1


def test_publish_records_violations_when_gate_fails(tmp_path):
    clean, broken = _models_pair(
        {"api_type": None, "normalized_price": None, "price_evidence": None})
    d = _fixture(tmp_path, {"models.jsonl": [clean, broken]})
    out = tmp_path / "null-census-latest.json"
    r = _run(d, "--publish", str(out))
    assert r.returncode == 1
    doc = json.loads(out.read_text())
    assert doc["inv1"] == 1
    assert doc["unexplained"] >= 3  # the wiped lane's nulls are also unexplained
