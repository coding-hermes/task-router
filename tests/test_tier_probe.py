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
    # AC2 vocabulary, not the harness's own class name (see
    # test_stamped_reasons_are_in_the_ac2_vocabulary); the class survives as detail
    assert by_pair[("broke", "laneprovider")] == "not-published-by-provider"
    assert all(r["field"] == "model_tier" for r in rows)
    # data_dir is ALWAYS the tables dir: the sidecar is data/null_reasons.jsonl
    # (one level up), which is the path data_null_census.py reads.
    parent = os.path.dirname(str(d))
    rows_w = tp.stamp_reasons(targets, excluded, commit=True, data_dir=str(d))
    assert len(rows_w) == 2
    sidecar = os.path.join(parent, "null_reasons.jsonl")
    assert os.path.exists(sidecar)
    # idempotent: a second stamped run writes nothing new
    rows2 = tp.stamp_reasons(targets, excluded, commit=True, data_dir=str(d))
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

# ------------------------------------------------- probeability wiring ----
# TR-221/TR-233 regression class: a lane that IS measurable must never be
# excluded by a stale transport reason, and the battery must be handed the
# exact lanes select_targets picked (it skips partially-tiered lanes by
# default, so a missing handoff silently filled nothing).

def test_header_gated_provider_probeable_when_header_resolves(tmp_path, monkeypatch):
    d = _write_tables(tmp_path, models=[_lane("opencode-go", "glm-5.3-flash")],
                      tiers=[{"model": "glm-5.3-flash", "category": "mechanical"}])
    tp = _import_module(tmp_path, monkeypatch, d)
    run = tp._run
    # header absent -> named skip, header present -> probeable
    assert run.skip_reason("opencode-go", {}) == (
        "requires x-opencode-session session header")
    assert run.skip_reason("opencode-go", {"x-opencode-session": "ses_x"}) is None
    # a provider block (401/402/403 class) still wins
    assert run.skip_reason("minimax", {"whatever": "x"}) is not None


def test_resolve_merges_probe_headers_over_config_route(tmp_path):
    """config.yaml owns base/key; probe_providers.jsonl owns the measurement
    contract header. Listing the provider in config must not drop the header."""
    sys.path.insert(0, SCRIPTS)
    import router_probe_run as run
    prov_cfg = {"opencode-go": ("https://example.test/v1", "OC_KEY", {})}
    probe = ({"opencode-go": ("https://example.test/v1", "OC_KEY",
                              {"x-opencode-session": "ses_router_health_probe"})}, {})
    base, key, extra = run.resolve("opencode-go", prov_cfg, {"OC_KEY": "k"}, probe)
    assert base == "https://example.test/v1" and key == "k"
    assert extra.get("x-opencode-session") == "ses_router_health_probe"
    # config-supplied headers win over the data file (route truth first)
    prov_cfg2 = {"opencode-go": ("https://example.test/v1", "OC_KEY",
                                 {"x-opencode-session": "ses_config"})}
    assert run.resolve("opencode-go", prov_cfg2, {"OC_KEY": "k"},
                       probe)[2]["x-opencode-session"] == "ses_config"


def test_probe_blocked_row_refuses_probe_but_stays_enabled(tmp_path):
    """A measured block (401 chat / 402 plan / attestation 403) must stop the
    battery while leaving the row enabled, so the hourly health probe keeps
    watching the carrier for recovery."""
    sys.path.insert(0, SCRIPTS)
    import router_probe_run as run
    path = tmp_path / "probe_providers.jsonl"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"id": "blockedprov", "base_url": "https://b.test/v1",
                             "key_env": "B_KEY", "enabled": True,
                             "probe_blocked": "chat 401 invalid token"}) + "\n")
        fh.write(json.dumps({"id": "okprov", "base_url": "https://c.test/v1",
                             "key_env": "C_KEY", "enabled": True}) + "\n")
    enabled, disabled = run.load_probe_providers(str(path))
    assert "blockedprov" not in enabled
    assert disabled["blockedprov"] == "chat 401 invalid token"
    assert run.resolve("blockedprov", {}, {"B_KEY": "k"},
                       (enabled, disabled))[:1] == (None,)
    assert enabled["okprov"][0] == "https://c.test/v1"


def test_run_batteries_hands_selected_models_to_the_battery(tmp_path, monkeypatch):
    """select_targets picks lanes missing ANY category; the battery skips any
    lane WITH a tier row. Without the explicit --models handoff the wave
    reports success and fills nothing."""
    d = _write_tables(tmp_path, models=[_lane("prov", "m1")])
    tp = _import_module(tmp_path, monkeypatch, d)
    calls = []
    monkeypatch.setattr(tp.subprocess, "call", lambda cmd, **kw: calls.append(cmd) or 0)
    args = type("A", (), {"date": "2026-01-01", "tag": "", "out_dir": str(tmp_path / "o"),
                          "workers": 4, "batteries": "agentic", "providers": "prov",
                          "limit": 0, "models": "", "commit": False})()
    rc = tp.run_batteries(args, [("prov", "m1"), ("prov", "m2")])
    assert rc == 0
    probe_cmd = calls[0]
    assert "--models" in probe_cmd
    assert probe_cmd[probe_cmd.index("--models") + 1] == "m1,m2"


def _fill_args(**over):
    """A Namespace shaped like run_batteries' expectations for a --models fill."""
    import argparse
    base = dict(date="2026-11-07", tag="t", out_dir="/tmp/does-not-matter",
                providers="openrouter", limit=0, models="some/model",
                batteries="agentic,extended2,extended3", workers=1,
                commit=False)
    base.update(over)
    return argparse.Namespace(**base)


def test_run_batteries_asks_for_agentic_under_models_fill(tmp_path, monkeypatch, capsys):
    """A --models fill list must still measure the six agentic categories.

    router_probe_run suppresses the base v5+agentic battery when it is given a
    fill list (fill_only), so the harness has to ask for it explicitly. Without
    this the 12-category default wave silently delivered 6: the agentic results
    file was empty and the ingest printed "rows to add: 0" while the run
    reported success.
    """
    tp = _import_module(tmp_path, monkeypatch, tmp_path / "data" / "tables")
    seen = []
    monkeypatch.setattr(tp.subprocess, "call", lambda cmd, **kw: seen.append(cmd) or 0)

    tp.run_batteries(_fill_args(), [("openrouter", "some/model")])
    assert seen and "--agentic" in seen[0]

    # ext-only waves keep the fill semantics: no agentic flag, no agentic spend
    seen.clear()
    tp.run_batteries(_fill_args(batteries="extended2,extended3"),
                     [("openrouter", "some/model")])
    assert seen and "--agentic" not in seen[0]


def test_probe_run_exposes_agentic_override():
    """The flag the harness above depends on must exist on the runner itself."""
    out = subprocess.run([PY, os.path.join(SCRIPTS, "router_probe_run.py"), "--help"],
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0
    assert "--agentic" in out.stdout


def test_stamped_reasons_are_in_the_ac2_vocabulary(tmp_path, monkeypatch):
    """A stamp the census refuses is worse than no stamp.

    data_null_census.REASONS is the AC2 authority and the TR-362 INV4 diff gate
    enforces it at push time. The harness once stamped 'unreachable-provider',
    which is not in that vocabulary, so 110 rows went off-vocabulary and the
    push carrying them was refused.
    """
    tp = _import_module(tmp_path, monkeypatch, tmp_path / "data" / "tables")
    census = __import__("data_null_census")
    assert tp.REASON_UNREACHABLE in census.REASONS
    assert tp.REASON_NO_SAMPLE in census.REASONS
    assert tp.UNREACHABLE_DETAIL not in census.REASONS  # detail, never a reason
