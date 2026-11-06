"""TR-363 — profiles resolve from capability DATA, not literal candidate lists.

Pins the four behaviors the board row requires:
  AC1/AC2  eligibility comes from registry capability data only — a lane is
           never admitted because it sits on a literal list: two providers
           carrying the same model with equal evidence are BOTH eligible,
           and a new provider carrying the same evidence becomes eligible
           with no code or table change.
  AC3      the why-lane report derives requested capabilities, carrying
           lanes (with their own evidence values) and per-excluded-lane
           reasons from registry data, reproducibly; --format json and text
           both serve it.
  AC4      P3_DOCS / P4_SECURITY / P5_VISION_E2E each resolve >= 3 hops
           across >= 2 distinct providers. Hermetic: repo tables, an
           all-OPEN policy plane, no health/circuit file — the pin is on
           DATA, never on live health flap.
  AC5      a requirement naming an unknown capability is a visible
           INVALID_REQUIREMENT naming it; the flag pointer follows the
           TR-369 three-state contract (False = unsupported,
           None = unresearched is not support, True passes).

The stub tables use synthetic ids (prov_a/m1 ...) — never real model names:
the whole point is that no literal candidate list exists to lean on.
"""
import json
import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))
import router_spawn as rs  # noqa: E402

# ---------------------------------------------------------------- fixtures ----

def _stub_tables():
    """Minimal registry: ONE model on TWO providers, equal evidence.

    The capability evidence lives on the rows (model_tier security=3,
    vision=True) — the resolver must read it from there, never from a list.
    """
    return {
        "models": [
            {"provider": "prov_a", "model": "m1",
             "normalized_price": 1.0, "vision": True},
            {"provider": "prov_b", "model": "m1",
             "normalized_price": 1.0, "vision": True},
        ],
        "model_tier": [
            {"model": "m1", "category": "security", "tier": 3},
        ],
        "task_profiles": [{"id": "P_PTR", "title": "capability-pointer"}],
        "task_profile_requirements": [
            {"task_id": "P_PTR", "category": "vision", "level": 1},
            {"task_id": "P_PTR", "category": "security", "level": 2},
        ],
        "providers": [{"id": "prov_a"}, {"id": "prov_b"}],
        "fallback_lanes": [], "plan_terms": [], "projects": [],
    }


def _open_policy_plane(monkeypatch, tmp_path, tables):
    """Hermetic gate state (house pattern, tests/test_spawn_board_profile.py):
    every provider in the tables OPEN; no health/circuit file (gates open,
    fail-open). An empty state dir would gate every lane fail-closed."""
    providers = [r.get("id") for r in tables.get("providers") or [] if r.get("id")]
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    (state / "quota-state.json").write_text(json.dumps({
        "updated": "test",
        "providers": {p: {"status": "open"} for p in providers},
    }))
    monkeypatch.setattr(rs, "MR", str(state))
    return state


# ------------------------------- AC4: the three profiles resolve wide --------

@pytest.mark.parametrize("profile_id", ["P3_DOCS", "P4_SECURITY", "P5_VISION_E2E"])
def test_profiles_resolve_multi_provider_multi_hop(monkeypatch, tmp_path, profile_id):
    tables, _src, _fb, _warn = rs._load_registry_with_meta()
    _open_policy_plane(monkeypatch, tmp_path, tables)
    r = rs.resolve(profile_id=profile_id, use_health=False, sort="price")
    assert not r.get("error"), r
    chain = r.get("chain") or []
    assert len(chain) >= 3, f"{profile_id}: hops={len(chain)}"
    assert len({c["provider"] for c in chain}) >= 2, (
        f"{profile_id}: providers="
        f"{sorted({c['provider'] for c in chain})}")


def test_profiles_resolve_multi_provider_multi_hop_cli(tmp_path):
    """AC4 via the real CLI on an isolated state dir: ROUTER_STATE_DIR
    bootstraps an all-OPEN policy plane (TR-345), so the chain is decided by
    registry data alone."""
    env = dict(os.environ, ROUTER_STATE_DIR=str(tmp_path / "state"))
    out = subprocess.run(
        [sys.executable, os.path.join(REPO, "scripts", "router_spawn.py"),
         "--profile", "P5_VISION_E2E", "--format", "json"],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    payload = json.loads(out.stdout)
    chain = payload.get("chain") or []
    assert len(chain) >= 3
    assert len({c["provider"] for c in chain}) >= 2


# ------------------- AC1/AC2: equal evidence -> both eligible, no lists ------

def test_equal_evidence_both_providers_eligible(monkeypatch, tmp_path):
    tables = _stub_tables()
    monkeypatch.setattr(rs, "_load_registry_with_meta",
                        lambda: (tables, "test", False, None))
    _open_policy_plane(monkeypatch, tmp_path, tables)
    r = rs.resolve(profile_id="P_PTR", use_health=False, sort="price")
    assert not r.get("error"), r
    provs = {c["provider"] for c in r["chain"]}
    assert {"prov_a", "prov_b"} <= provs, provs


def test_new_provider_with_same_evidence_needs_no_list(monkeypatch, tmp_path):
    """AC5's negative space: a THIRD provider carrying the same weights with
    the same evidence becomes eligible by DATA alone — nothing to edit in
    code, and no candidate table to append to."""
    tables = _stub_tables()
    tables["models"].append({"provider": "prov_c", "model": "m1",
                             "normalized_price": 1.0, "vision": True})
    tables["providers"].append({"id": "prov_c"})
    monkeypatch.setattr(rs, "_load_registry_with_meta",
                        lambda: (tables, "test", False, None))
    _open_policy_plane(monkeypatch, tmp_path, tables)
    r = rs.resolve(profile_id="P_PTR", use_health=False, sort="price")
    assert not r.get("error"), r
    provs = {c["provider"] for c in r["chain"]}
    assert {"prov_a", "prov_b", "prov_c"} <= provs, provs


def test_flag_pointer_three_state_contract():
    """The vision pointer follows the TR-369 contract exactly: evidenced True
    passes, False fails as unsupported, None fails as unknown (unresearched
    is not support) — every failure a structured record, nothing silent."""
    tables = _stub_tables()
    tables["models"] += [
        {"provider": "prov_x", "model": "m1",
         "normalized_price": 1.0, "vision": False},
        {"provider": "prov_y", "model": "m1", "normalized_price": 1.0},
    ]
    misses = []
    rows = rs._build_chain(tables, [("vision", 1)], misses_out=misses)
    assert {(p, m) for _h, p, m, _pr, _dc, _r in rows} == {
        ("prov_a", "m1"), ("prov_b", "m1")}
    codes = {c for rec in misses for c in rec["codes"]}
    assert "capability-flag-unsupported" in codes
    assert "capability-flag-unknown" in codes
    by_prov = {rec["provider"]: " ".join(rec["reasons"]) for rec in misses}
    assert "unsupported" in by_prov["prov_x"]
    assert "unknown" in by_prov["prov_y"]
    assert "vision" in by_prov["prov_x"]


# ---------------------------- AC5: unknown capability is a visible error -----

def test_unknown_capability_rejected_with_reason():
    tables = _stub_tables()
    reqs, err = rs._validate_adhoc(["nosuchcap=1"], tables)
    assert reqs is None
    assert err, "a bogus capability must be a visible error, never dropped"
    assert err.get("code") == "INVALID_REQUIREMENT"
    assert "nosuchcap" in err.get("error", "")


def test_flag_only_pointer_accepted_boolean_only():
    tables = _stub_tables()
    reqs, err = rs._validate_adhoc(["thinking=1"], tables)
    assert err is None, err
    assert ("thinking", 1) in reqs
    reqs, err = rs._validate_adhoc(["thinking=2"], tables)
    assert reqs is None and err
    assert err.get("code") == "INVALID_REQUIREMENT"
    assert "thinking" in err.get("error", "")


# --------------------------------- AC3: the why-lane report, from data -------

def _build_why(tables, reqs):
    misses, carrying = [], []
    rs._build_chain(tables, reqs, misses_out=misses, eligible_out=carrying)
    return rs.build_why_report(tables=tables, reqs=reqs, misses=misses,
                               eligible=carrying, chain=[], exclusions=[],
                               profile_id="P_PTR")


def test_why_report_exclusion_reasons_from_data():
    tables = _stub_tables()
    tables["models"] += [
        {"provider": "prov_x", "model": "m1",
         "normalized_price": 1.0, "vision": False},
        # retired by date: a LIFECYCLE fact, not a candidate-list decision
        {"provider": "prov_ret", "model": "m1", "normalized_price": 1.0,
         "vision": True, "valid_to": "2026-01-01"},
        # unpriced: no normalized_price on record
        {"provider": "prov_unpriced", "model": "m1",
         "normalized_price": None, "vision": True},
    ]
    reqs = [("vision", 1), ("security", 2)]
    why = _build_why(tables, reqs)

    kinds = {r["category"]: r["resolution"] for r in why["requested"]}
    assert kinds == {"vision": "capability-flag", "security": "tier"}

    by_prov = {}
    for e in why["excluded"]:
        by_prov.setdefault(e["provider"], " ".join(e["reasons"]))
    assert "unsupported" in by_prov.get("prov_x", "")
    assert "retired" in by_prov.get("prov_ret", "")
    assert "2026-01-01" in by_prov.get("prov_ret", "")
    assert "normalized_price" in by_prov.get("prov_unpriced", "")

    carrying = {(e["provider"], e["model"]): e["evidence"]
                for e in why["carrying"]}
    assert carrying[("prov_a", "m1")]["vision"] is True
    assert carrying[("prov_a", "m1")]["security"] == 3


def test_why_report_is_reproducible_from_data():
    tables = _stub_tables()
    reqs = [("vision", 1), ("security", 2)]
    assert _build_why(tables, reqs) == _build_why(tables, reqs)


def test_why_report_text_and_json_shapes():
    tables = _stub_tables()
    reqs = [("vision", 1), ("security", 2)]
    why = _build_why(tables, reqs)
    text = rs.format_why_text(why)
    assert "requested:" in text
    assert "capability-flag" in text
    assert "carrying (pre-gate):" in text
    assert "excluded:" in text
    # json shape: round-trips through json.dumps (the --format json surface)
    assert json.loads(json.dumps(why))["profile"] == "P_PTR"


def test_why_report_absent_unless_requested(monkeypatch, tmp_path):
    """The payload's key set is untouched when --why is off."""
    tables = _stub_tables()
    monkeypatch.setattr(rs, "_load_registry_with_meta",
                        lambda: (tables, "test", False, None))
    _open_policy_plane(monkeypatch, tmp_path, tables)
    r = rs.resolve(profile_id="P_PTR", use_health=False, sort="price")
    assert "why" not in r
    r2 = rs.resolve(profile_id="P_PTR", use_health=False, sort="price", why=True)
    assert isinstance(r2.get("why"), dict)
    assert r2["why"]["requested"]


def test_fallback_requirements_unmet_respects_flag_pointer():
    """The degraded path uses the same requirement split: a curated vision
    lane with an evidenced vision=True flag is NOT flagged requirements_unmet
    (it used to be flagged for having no vision tier row)."""
    tables = _stub_tables()
    open_plane = {"prov_a": {"status": "open"}, "prov_x": {"status": "open"}}
    tables["fallback_lanes"] = [
        {"provider": "prov_a", "model": "m1", "order": 1, "key_env": "K"},
    ]
    lanes = rs._resolve_fallback(tables, open_plane, {}, {}, [("vision", 1)],
                                 profile_id="P_PTR")
    assert lanes, "the curated lane must resolve"
    assert lanes[0]["requirements_unmet"] == []
    # and a TEXT-only lane (vision False) IS flagged, with the flag value
    tables["fallback_lanes"] = [
        {"provider": "prov_x", "model": "m1", "order": 1, "key_env": "K"},
    ]
    tables["models"].append({"provider": "prov_x", "model": "m1",
                             "normalized_price": 1.0, "vision": False})
    lanes = rs._resolve_fallback(tables, open_plane, {}, {}, [("vision", 1)],
                                 profile_id="P_PTR")
    assert lanes and lanes[0]["requirements_unmet"] == [("vision", 1, False)]
