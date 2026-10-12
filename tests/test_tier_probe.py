"""TR-221 — router_tier_probe.py unit + CLI tests.

Hermetic: every test points the harness at a tmp data dir (ROUTING_DATA_DIR
is read at import time, so unit tests import the module after setting the
env; CLI tests pass a subprocess env). No real network, no repo writes.
The one real-data test (dry-run selection over the committed tables) is
read-only by construction and asserts shape, not counts.
"""
import json
import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")
PY = (
    "/home/kara/.hermes/venvs/board/bin/python3"
    if os.path.exists("/home/kara/.hermes/venvs/board/bin/python3")
    else sys.executable
)

CATS = ["mechanical", "guard", "agent_tick"]


def _write_tables(tmp_path, models, tiers=(), gaps=(), excludes=()):
    d = tmp_path / "data" / "tables"
    d.mkdir(parents=True)
    for name, rows in (("models", models), ("model_tier", tiers),
                       ("probe_gaps", gaps), ("probe_excludes", excludes)):
        with open(d / f"{name}.jsonl", "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")
    return d


def _lane(prov, model, **over):
    row = {"provider": prov, "model": model, "normalized_price": 1.0,
           "archive": False, "valid_to": None, "disabled": None,
           "public_out_per_m": 2.0}
    row.update(over)
    return row


# ---------------------------------------------------------------- unit ----

def _import_module(tmp_path, monkeypatch, data_dir):
    """Import router_tier_probe with ROUTING_DATA_DIR pinned to tmp."""
    monkeypatch.setenv("ROUTING_DATA_DIR", str(data_dir))
    for mod in list(sys.modules):
        if mod == "router_tier_probe":
            del sys.modules[mod]
    sys.path.insert(0, SCRIPTS)
    import router_tier_probe
    return router_tier_probe


def test_lane_state_lifecycle(tmp_path, monkeypatch):
    tp = _import_module(tmp_path, monkeypatch, tmp_path / "data" / "tables")
    assert tp.lane_state(_lane("p", "m")) == "active"
    assert tp.lane_state(_lane("p", "m", archive=True)) == "archived"
    assert tp.lane_state(_lane("p", "m", valid_to="2026-01-01")) == "retired"
    assert tp.lane_state(_lane("p", "m", disabled=True)) == "disabled"
    assert tp.lane_state(_lane("p", "m", normalized_price=None)) == "unpriced"


def test_gap_dead_ids_parses_evidence_classes_only(tmp_path, monkeypatch):
    d = _write_tables(tmp_path, [], gaps=[
        {"provider": "p", "model": "dead-a", "error": "[not_served] HTTP 404"},
        {"provider": "p", "model": "dead-b", "error": "[not_in_plan] plan"},
        {"provider": "p", "model": "alive", "error": "[rate_limited] 429"},
        {"provider": "p", "model": "alive2", "error": "[http_520] edge"},
        {"provider": "p", "model": "noattr", "error": "no class token"},
    ])
    tp = _import_module(tmp_path, monkeypatch, d)
    dead = tp.gap_dead_ids()
    assert ("p", "dead-a") in dead and ("p", "dead-b") in dead
    assert ("p", "alive") not in dead and ("p", "alive2") not in dead
    assert ("p", "noattr") not in dead


def test_select_targets_names_every_exclusion(tmp_path, monkeypatch):
    d = _write_tables(
        tmp_path,
        models=[
            _lane("prov", "unranked"),                       # target
            _lane("prov", "tiered"),                         # fully-tiered
            _lane("prov", "dead"),                           # dead-id
            _lane("prov", "exc"),                            # probe-excluded
            _lane("prov", "disabled", disabled=True),        # disabled
            _lane("prov", "archived", archive=True),         # archived
            _lane("prov", "retired", valid_to="2026-01-01"), # retired
            _lane("prov", "unpriced", normalized_price=None),# unpriced
            _lane("skips", "lane"),                          # unprobeable-provider
        ],
        tiers=[
            {"model": "tiered", "category": c} for c in CATS
        ],
        gaps=[{"provider": "prov", "model": "dead", "error": "[not_served] x"}],
        excludes=[{"provider": "prov", "model": "exc"}],
    )
    tp = _import_module(tmp_path, monkeypatch, d)
    monkeypatch.setattr(tp._run, "SKIP_REASON", {"skips": "transport"})
    monkeypatch.setattr(tp, "_resolvable_providers", lambda *a, **k: {"prov": True})
    targets, excluded = tp.select_targets(CATS)
    assert targets == [("prov", "unranked")]
    reasons = {reason: dict(pairs) for reason, pairs in
               ((k, [(p, m) for p, m in v]) for k, v in excluded.items())}
    assert ("prov", "tiered") in [p for p in excluded["fully-tiered"]]
    assert ("prov", "dead") in [p for p in excluded["dead-id"]]
    assert ("prov", "exc") in [p for p in excluded["probe-excluded"]]
    assert ("prov", "disabled") in [p for p in excluded["disabled"]]
    assert ("prov", "archived") in [p for p in excluded["archived"]]
    assert ("prov", "retired") in [p for p in excluded["retired"]]
    assert ("prov", "unpriced") in [p for p in excluded["unpriced"]]
    assert ("skips", "lane") in [p for p in excluded["unprobeable-provider"]]


def test_select_targets_no_credentials_excluded(tmp_path, monkeypatch):
    d = _write_tables(tmp_path, models=[_lane("broke", "lane")])
    tp = _import_module(tmp_path, monkeypatch, d)
    monkeypatch.setattr(tp._run, "SKIP_REASON", {})
    monkeypatch.setattr(tp, "_resolvable_providers", lambda *a, **k: {"broke": False})
    targets, excluded = tp.select_targets(CATS)
    assert targets == []
    assert ("broke", "lane") in [p for p in excluded["no-credentials"]]


def test_category_census_counts_active_lanes_only(tmp_path, monkeypatch):
    d = _write_tables(
        tmp_path,
        models=[_lane("p", "live"), _lane("p", "gone", archive=True)],
        tiers=[{"model": "live", "category": "mechanical"},
               {"model": "gone", "category": "mechanical"},
               {"model": "live", "category": "guard"}],
    )
    tp = _import_module(tmp_path, monkeypatch, d)
    census = tp.category_census(["mechanical", "guard", "agent_tick"])
    assert census == {"mechanical": 1, "guard": 1, "agent_tick": 0}


def test_estimate_costs_uses_lane_sticker(tmp_path, monkeypatch):
    d = _write_tables(tmp_path, models=[
        _lane("p", "pricy", public_out_per_m=3.0),
        _lane("p", "free", public_out_per_m=0.0),
        _lane("p", "nosticker", public_out_per_m=None),
    ])
    tp = _import_module(tmp_path, monkeypatch, d)
    est = tp.estimate_costs([_lane("p", "pricy", public_out_per_m=3.0),
                             _lane("p", "free", public_out_per_m=0.0),
                             _lane("p", "nosticker", public_out_per_m=None)],
                            [("p", "pricy"), ("p", "free"), ("p", "nosticker")])
    # 12 calls x 3000 tokens at $3/M = 0.108 for pricy; 0 for the others
    assert est["p"] == pytest.approx(0.108, abs=1e-6)


def test_stamp_reasons_classifies_and_idempotent(tmp_path, monkeypatch):
    d = _write_tables(tmp_path, models=[
        _lane("ok", "reachable"),
        _lane("broke", "laneprovider"),
        _lane("ok", "gone", archive=True),      # NOT live: never stamped
    ])
    tp = _import_module(tmp_path, monkeypatch, d)
    targets = [("ok", "reachable"), ("broke", "laneprovider")]
    excluded = {"no-credentials": [("broke", "laneprovider")]}
    rows = tp.stamp_reasons(targets, excluded, commit=False, data_dir=str(d))
    assert len(rows) == 2
    by_pair = {(r["provider"], r["model"]): r["reason"] for r in rows}
    assert by_pair[("ok", "reachable")] == "no-sample-yet"
    assert by_pair[("broke", "laneprovider")] == "unreachable-provider"
    assert all(r["field"] == "model_tier" for r in rows)
    # commit writes the sidecar OUTSIDE data/tables? No: null_reasons lives at
    # data/null_reasons.jsonl — data_dir here IS the tables dir, so pass its parent.
    parent = os.path.dirname(str(d))
    rows_w = tp.stamp_reasons(targets, excluded, commit=True, data_dir=parent)
    assert len(rows_w) == 2
    sidecar = os.path.join(parent, "null_reasons.jsonl")
    assert os.path.exists(sidecar)
    # idempotent: a second stamped run writes nothing new
    rows2 = tp.stamp_reasons(targets, excluded, commit=True, data_dir=parent)
    assert rows2 == []
    with open(sidecar, encoding="utf-8") as fh:
        on_disk = [json.loads(l) for l in fh if l.strip()]
    assert len(on_disk) == 2


# ---------------------------------------------------------------- CLI ----

def test_cli_dry_run_projection_and_no_writes(tmp_path, monkeypatch):
    d = _write_tables(tmp_path, models=[_lane("fakeprov", "m1")])
    # fakeprov with NO resolvable credential anywhere -> excluded with reason
    env = dict(os.environ)
    env["ROUTING_DATA_DIR"] = str(d)
    before = sorted(os.listdir(d))
    out = subprocess.run(
        [PY, os.path.join(SCRIPTS, "router_tier_probe.py"), "--dry-run"],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert "selection:" in out.stdout
    assert "coverage BEFORE" in out.stdout
    assert "DRY RUN" in out.stdout
    assert "no-credentials" in out.stdout
    assert sorted(os.listdir(d)) == before          # no files created


def test_cli_save_and_baseline_census(tmp_path, monkeypatch):
    d = _write_tables(tmp_path, models=[_lane("fakeprov", "m1")])
    env = dict(os.environ)
    env["ROUTING_DATA_DIR"] = str(d)
    census = tmp_path / "census.json"
    out1 = subprocess.run(
        [PY, os.path.join(SCRIPTS, "router_tier_probe.py"), "--dry-run",
         "--save-census", str(census)],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=120)
    assert out1.returncode == 0 and census.exists()
    doc = json.loads(census.read_text(encoding="utf-8"))
    assert "census" in doc and doc["census"]["mechanical"] == 0
    out2 = subprocess.run(
        [PY, os.path.join(SCRIPTS, "router_tier_probe.py"), "--dry-run",
         "--baseline-census", str(census)],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=120)
    assert out2.returncode == 0
    assert "coverage AFTER vs baseline" in out2.stdout
    assert "TOTAL" in out2.stdout


def test_cli_dry_run_and_commit_mutually_exclusive(tmp_path):
    d = _write_tables(tmp_path, models=[])
    env = dict(os.environ)
    env["ROUTING_DATA_DIR"] = str(d)
    out = subprocess.run(
        [PY, os.path.join(SCRIPTS, "router_tier_probe.py"),
         "--dry-run", "--commit"],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 2


def test_cli_unknown_battery_rejected(tmp_path):
    d = _write_tables(tmp_path, models=[])
    env = dict(os.environ)
    env["ROUTING_DATA_DIR"] = str(d)
    out = subprocess.run(
        [PY, os.path.join(SCRIPTS, "router_tier_probe.py"), "--commit",
         "--batteries", "nope", "--providers", "fakeprov"],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode != 0
    assert "unknown battery" in (out.stderr + out.stdout)


def test_in_process_main_resolved_provider_targets_and_stamps(tmp_path, monkeypatch, capsys):
    """Full main() path with a RESOLVED provider (monkeypatched), preview stamps."""
    d = _write_tables(tmp_path, models=[_lane("prov", "m1")])
    tp = _import_module(tmp_path, monkeypatch, d)
    monkeypatch.setattr(tp._run, "SKIP_REASON", {})
    monkeypatch.setattr(tp, "_resolvable_providers", lambda *a, **k: {"prov": True})
    rc = tp.main(["--dry-run", "--reasons-only"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "1 lanes need measurement" in captured.out
    assert "reason stamps preview" in captured.out
    assert "no-sample-yet" in captured.out
    # nothing written
    assert not os.path.exists(os.path.join(os.path.dirname(str(d)),
                                           "null_reasons.jsonl"))


def test_committed_tables_real_census_shape():
    """Read-only sanity over the COMMITTED tables: the census covers every
    wave category and mechanical is counted (the AC's headline number)."""
    sys.path.insert(0, SCRIPTS)
    import router_tier_probe as tp
    census = tp.category_census(list(tp.WAVE_CATEGORIES))
    assert set(census) == set(tp.WAVE_CATEGORIES)
    assert census["mechanical"] >= 0
