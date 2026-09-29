"""TR-199 — spec R4 "no anonymous dates" on every write path.

Spec: docs/specs/SPEC-MODEL-LIFECYCLE.md R4 — a lifecycle date without
`lifecycle_source` is invalid. TR-199 measured 21 rows (20 models.jsonl +
1 temporary_discounts.jsonl) that carried a retirement date with no
provenance, because the writers only checked provenance at TWO entry points
(seed lifecycle-overlay ingest, UI registry_edit) while nine direct table
writers could stamp or carry a date un-checked.

These tests pin the fix on BOTH ends:
  * the shared predicate (scripts/lifecycle_gate.py) refuses an anonymous
    date, including blank-string provenance, and skips non-lifecycle tables;
  * every wired writer calls it (unit level, via ROUTING_DATA_DIR scratch
    copies) — each test below fails if its writer's gate is removed;
  * the committed data carries zero anonymous dates (red at HEAD~1, where
    the 21 rows lived);
  * E2E: clinepass sync refuses to persist an anonymous discount date, and a
    seed run refuses to republish one from the committed base data.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import lifecycle_gate  # noqa: E402

PY = ("/home/kara/.hermes/venvs/board/bin/python3"
      if os.path.exists("/home/kara/.hermes/venvs/board/bin/python3")
      else sys.executable)
SEED_TIMEOUT = 600  # duckdb seed: worst-load budget (conftest parity)

ANON_MODELS_ROW = {"provider": "anon-prov", "model": "anon-model",
                   "normalized_price": 1.0, "price_evidence": "test",
                   "valid_to": "2026-12-01"}
SRC = "provider-announcement:test-suite"


# ---------------------------------------------------------------- predicate #

def test_gate_refuses_anonymous_date():
    with pytest.raises(lifecycle_gate.LifecycleGateError) as ei:
        lifecycle_gate.gate_rows("models", [dict(ANON_MODELS_ROW)])
    msg = str(ei.value)
    assert "anon-prov/anon-model" in msg
    assert "valid_to" in msg and "R4" in msg


def test_gate_refuses_blank_string_provenance():
    row = dict(ANON_MODELS_ROW, lifecycle_source="   ")
    with pytest.raises(lifecycle_gate.LifecycleGateError):
        lifecycle_gate.gate_rows("models", [row])


def test_gate_accepts_sourced_date_and_undated_rows():
    lifecycle_gate.gate_rows("models", [
        dict(ANON_MODELS_ROW, lifecycle_source=SRC),
        {"provider": "p", "model": "undated", "valid_to": None},
        {"provider": "p", "model": "no-key"},
    ])


def test_gate_ignores_non_lifecycle_tables():
    # benchmarks.valid_from is a sample date, not a lifecycle date: not gated.
    lifecycle_gate.gate_rows("benchmarks",
                             [{"model": "m", "category": "c", "valid_from": "2026-01-01"}])
    assert lifecycle_gate.find_offenders(
        "benchmarks", [{"model": "m", "valid_to": "2026-01-01"}]) == []


def test_available_from_is_also_a_lifecycle_date():
    with pytest.raises(lifecycle_gate.LifecycleGateError):
        lifecycle_gate.gate_rows("models", [
            {"provider": "p", "model": "m", "available_from": "2026-12-01"}])


# ------------------------------------------------------------ committed data #

def test_committed_tables_carry_no_anonymous_dates():
    """RED at the pre-TR-199 tree: 20 models rows + 1 discount row were dated
    with no lifecycle_source."""
    offenders = []
    for table, keys in lifecycle_gate.TABLE_KEYS.items():
        path = REPO / "data" / "tables" / f"{table}.jsonl"
        with open(path, encoding="utf-8") as fh:
            for i, line in enumerate(fh, 1):
                if not line.strip():
                    continue
                row = json.loads(line)
                if any(row.get(k) for k in keys) and not str(
                        row.get("lifecycle_source") or "").strip():
                    offenders.append(f"{table}:{i} {row.get('provider')}/{row.get('model')}")
    assert offenders == [], (
        f"{len(offenders)} anonymous date(s) (R4, TR-199): {offenders[:5]}")


# ------------------------------------------------------- writers (unit level) #

def _scratch_tables(tmp_path):
    data = tmp_path / "tables"
    shutil.copytree(REPO / "data" / "tables", data)
    return data


def _plan_sweep_env(data):
    return {"ROUTING_DATA_DIR": str(data)}


def test_plan_sweep_write_path_refuses_anonymous_date(tmp_path, monkeypatch):
    import router_plan_sweep as rps
    data = _scratch_tables(tmp_path)
    monkeypatch.setattr(rps, "DATA_DIR", str(data))
    rows = [json.loads(l) for l in (data / "models.jsonl").read_text().splitlines() if l.strip()]
    rows.append(dict(ANON_MODELS_ROW))
    with pytest.raises(lifecycle_gate.LifecycleGateError):
        rps._write("models", rows)
    assert "anon-model" not in (data / "models.jsonl").read_text(), \
        "the refused row must not reach the file"


def test_pricing_write_path_refuses_anonymous_date(tmp_path, monkeypatch):
    import router_pricing as rp
    data = _scratch_tables(tmp_path)
    monkeypatch.setattr(rp, "DATA_DIR", str(data))
    rows = [json.loads(l) for l in (data / "models.jsonl").read_text().splitlines() if l.strip()]
    rows.append(dict(ANON_MODELS_ROW))
    with pytest.raises(lifecycle_gate.LifecycleGateError):
        rp._write("models", rows)
    assert "anon-model" not in (data / "models.jsonl").read_text()


def test_muse_code_write_path_refuses_anonymous_date(tmp_path, monkeypatch):
    import router_muse_code as rm
    data = _scratch_tables(tmp_path)
    monkeypatch.setattr(rm, "DATA_DIR", str(data))
    rows = [json.loads(l) for l in (data / "models.jsonl").read_text().splitlines() if l.strip()]
    rows.append(dict(ANON_MODELS_ROW))
    with pytest.raises(lifecycle_gate.LifecycleGateError):
        rm._write("models", rows)
    assert "anon-model" not in (data / "models.jsonl").read_text()


def test_clinepass_write_path_refuses_anonymous_discount(tmp_path, monkeypatch):
    """temporary_discounts is the table that carried TR-199's 21st offender."""
    import router_clinepass as rc
    data = _scratch_tables(tmp_path)
    monkeypatch.setattr(rc, "DATA_DIR", str(data))
    rows = [json.loads(l) for l in
            (data / "temporary_discounts.jsonl").read_text().splitlines() if l.strip()]
    rows.append({"provider": "clinepass", "model": "anon:free",
                 "discount_type": "free", "value": 1.0,
                 "valid_from": "2026-09-29", "valid_to": "2026-10-06",
                 "source": "clinepass-api :free lane"})
    with pytest.raises(lifecycle_gate.LifecycleGateError):
        rc._write("temporary_discounts", rows)
    assert "anon:free" not in (data / "temporary_discounts.jsonl").read_text()


def test_modelsdev_write_path_refuses_anonymous_date(tmp_path, monkeypatch):
    import router_modelsdev as md
    data = _scratch_tables(tmp_path)
    monkeypatch.setattr(md, "DATA_DIR", str(data))
    rows = [json.loads(l) for l in (data / "models.jsonl").read_text().splitlines() if l.strip()]
    rows.append(dict(ANON_MODELS_ROW))
    with pytest.raises(lifecycle_gate.LifecycleGateError):
        md._write_rows(str(data / "models.jsonl"), rows)
    assert "anon-model" not in (data / "models.jsonl").read_text()
    # ...but the same rows through the NON-gated table still write
    md._write_rows(str(data / "model_catalog.jsonl"),
                   [{"provider": "p", "model": "m", "valid_to": "2026-01-01"}])


def test_provider_import_write_path_refuses_anonymous_date(tmp_path):
    """apply_lanes rewrites the whole models file: an anonymous DATED row
    already in the base data flows through untouched and would be silently
    republished by the import — the gate must refuse before the rewrite.
    (Net-new lanes cannot carry the defect: apply_lanes stamps them
    valid_to=None by contract.)"""
    import router_provider_import as rpi
    data = _scratch_tables(tmp_path)
    mpath = data / "models.jsonl"
    rows = [json.loads(l) for l in mpath.read_text().splitlines() if l.strip()]
    rows.insert(0, dict(ANON_MODELS_ROW))
    mpath.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    new_lanes = {"some-live-model": {"provider": "anon-prov", "model": "some-live-model",
                                     "normalized_price": 2.0, "price_evidence": "cat"}}
    with pytest.raises(lifecycle_gate.LifecycleGateError):
        rpi.apply_lanes(str(mpath), "anon-prov", new_lanes, 0, "test-evidence")


def test_release_backfill_write_refuses_anonymous_date(tmp_path, monkeypatch):
    """--commit write helper: anonymous date in the rows -> raise before the
    file is touched."""
    import router_release_backfill as rrb
    data = _scratch_tables(tmp_path)
    monkeypatch.setattr(rrb, "DATA_DIR", str(data))
    mpath = data / "models.jsonl"
    original = mpath.read_text()
    with pytest.raises(lifecycle_gate.LifecycleGateError):
        rrb._write_models([dict(ANON_MODELS_ROW)])
    assert mpath.read_text() == original, "the refused write must not touch the file"


def test_maintain_reprice_mirror_refuses_anonymous_date(tmp_path, monkeypatch):
    import router_maintain as rman
    data = _scratch_tables(tmp_path)
    mpath = data / "models.jsonl"
    rows = [json.loads(l) for l in mpath.read_text().splitlines() if l.strip()]
    anon_idx = len(rows)
    rows.append(dict(ANON_MODELS_ROW))
    mpath.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    doc = {"tables": {"models": [
        {"provider": r["provider"], "model": r["model"],
         "normalized_price": 9.0, "price_evidence": "or-spot-test"}
        for r in rows]}}
    monkeypatch.setattr(rman, "DATA_DIR", str(data))
    with pytest.raises(lifecycle_gate.LifecycleGateError):
        rman._sync_reprice_to_data(doc)
    assert "anon-model" in mpath.read_text()  # still there: write refused


def test_web_discount_edit_carries_provenance_contract():
    """router_web imports the gate and the discount editor's whitelist carries
    lifecycle_source (handler-level contract; the full HTTP E2E lives in
    test_web.py, which owns the server harness)."""
    src = (SCRIPTS / "router_web.py").read_text()
    assert "lifecycle_gate.DATE_KEYS" in src
    assert '"source", "note", "lifecycle_source"' in src


def test_seed_admission_gate_refuses_anonymous_date_in_base_data(tmp_path, monkeypatch):
    """E2E: a DATED row with no lifecycle_source sitting in the committed
    tables aborts the seed BEFORE any write (spec R4 / TR-199)."""
    env = {"ROUTING_DATA_DIR": str(_scratch_tables(tmp_path)),
           "ROUTER_STATE_DIR": str(tmp_path / "state"),
           "ROUTING_REGISTRY": str(tmp_path / "registry.json")}
    models = Path(env["ROUTING_DATA_DIR"]) / "models.jsonl"
    rows = [json.loads(l) for l in models.read_text().splitlines() if l.strip()]
    rows.append(dict(ANON_MODELS_ROW))
    models.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    p = subprocess.run([PY, str(SCRIPTS / "router_seed.py")],
                       capture_output=True, text=True, timeout=SEED_TIMEOUT,
                       env={**os.environ, **env})
    assert p.returncode != 0, "a seed must not republish an anonymous date (R4)"
    blob = p.stderr + p.stdout
    assert "R4" in blob and "anon-prov/anon-model" in blob
    assert not Path(env["ROUTING_REGISTRY"]).exists(), "nothing written on refusal"


def test_seed_accepts_the_committed_stamped_tables(tmp_path):
    """Green twin: after the TR-199 stamps, the real committed data seeds fine
    (the 21 provenance rows pass the admission gate)."""
    env = {"ROUTING_DATA_DIR": str(_scratch_tables(tmp_path)),
           "ROUTER_STATE_DIR": str(tmp_path / "state"),
           "ROUTING_REGISTRY": str(tmp_path / "registry.json")}
    p = subprocess.run([PY, str(SCRIPTS / "router_seed.py")],
                       capture_output=True, text=True, timeout=SEED_TIMEOUT,
                       env={**os.environ, **env})
    assert p.returncode == 0, (p.stderr or p.stdout)[-500:]


def test_validate_gate_reports_anonymous_discount_date(tmp_path):
    """The read-side validate gate flags an anonymous VALID_TO in a non-exempt
    table (temporary_discounts) — and passes on the committed tree. valid_from
    alone is NOT a lifecycle date (discounts always carry an open start)."""
    data = _scratch_tables(tmp_path)
    td = data / "temporary_discounts.jsonl"
    rows = [json.loads(l) for l in td.read_text().splitlines() if l.strip()]
    rows.append({"provider": "p", "model": "anon:free", "discount_type": "free",
                 "value": 1.0, "valid_from": "2026-09-29", "valid_to": "2026-10-06"})
    td.write_text("".join(json.dumps(r) + "\n" for r in rows))
    bad = lifecycle_gate.gate_data_dir(str(data))
    assert set(bad) == {"temporary_discounts"}, sorted(bad)
    # valid_from-only rows are fine (an open discount window is not anonymous)
    rows[-1]["valid_to"] = None
    td.write_text("".join(json.dumps(r) + "\n" for r in rows))
    assert lifecycle_gate.gate_data_dir(str(data)) == {}
    # committed tree: clean under the read gate (models exempt, no other dates)
    assert lifecycle_gate.gate_data_dir(str(REPO / "data" / "tables")) == {}
