"""TR-007 diversity + concurrency-aware chain resolution — hermetic unit tests.

Never touches the real registry or real state: the fixture registry is a
tmp_path registry.json and the state dir is a tmp_path dir; router_spawn's
REGISTRY / MR are monkeypatched before every resolve() call. A missing-file
state read degrades to "no state" (fail-open), exactly like production when
nothing is configured.
"""
import datetime
import json

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts"))
import router_spawn  # noqa: E402 (path set above)


# ---------------------------------------------------------------- fixtures ----

CATS = ("agent_tick", "debug")  # categories used by the fixture profile reqs


def _mk_db(tmp_path, models):
    """Build a mini text registry (registry.json): models / model_tier /
    task_profiles / task_profile_requirements / projects, faithful to
    router_seed.py's output shape.

    models: list of (provider, model, price) — token_factor 1.0, plan_tier 0,
    all eligible for profile FIXPROF (tier >= level for every requirement).
    """
    reg = {
        "version": 3,
        "generated_at": "fixture",
        "tables": {
            "models": [],
            "model_tier": [],
            "task_profiles": [],
            "task_profile_requirements": [],
            "projects": [],
        },
    }
    for prov, model, price in models:
        reg["tables"]["models"].append({
            "provider": prov, "model": model, "normalized_price": price,
            "token_factor": 1.0, "plan_tier": 0, "data_class": "zdr",
            "valid_to": None, "archive": False})
        for cat in CATS:
            # tier high enough to clear any requirement we seed
            reg["tables"]["model_tier"].append(
                {"model": model, "category": cat, "tier": 5})
    reg["tables"]["task_profiles"].append({
        "id": "FIXPROF", "title": "fixture", "created_at": None,
        "max_consecutive_per_provider": None, "max_total_per_provider": None})
    reg["tables"]["projects"].append(
        {"id": "fixproj", "profile": "FIXPROF", "sensitivity": None})
    reg["tables"]["task_profile_requirements"] = [
        {"task_id": "FIXPROF", "category": "agent_tick", "level": 1},
        {"task_id": "FIXPROF", "category": "debug", "level": 1},
    ]
    path = str(tmp_path / "registry.json")
    with open(path, "w") as f:
        json.dump(reg, f)
    return path


def _state_dir(tmp_path, providers=None, diversity=None, models=None,
               health=None, circuit=None, ledger_rows=None):
    d = tmp_path / "state"
    d.mkdir(exist_ok=True)
    qdoc = {"updated": "test", "providers": providers if providers is not None else {}}
    if diversity is not None:
        qdoc["diversity"] = diversity
    if models is not None:
        # TR-032: the busy-limit gate is opt-in now (soft_gate knob, default
        # OFF). Fixtures that configure per-model limits want it ON.
        qdoc["soft_gate"] = True
        qdoc["models"] = models
    (d / "quota-state.json").write_text(json.dumps(qdoc))
    hdoc = {"providers": health if health is not None else {}}
    (d / "health-state.json").write_text(json.dumps(hdoc))
    cdoc = {"pairs": circuit if circuit is not None else {}}
    (d / "circuit-state.json").write_text(json.dumps(cdoc))
    if ledger_rows is not None:
        with open(d / "ledger.jsonl", "w") as f:
            for row in ledger_rows:
                f.write(json.dumps(row) + "\n")
    return str(d)


def _started(pair, ts=None, tid="tr-x", outcome="started"):
    prov, _, model = pair.partition("/")
    row = {"ts": ts or datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
           "provider": prov, "model": model, "outcome": outcome, "trace_id": tid}
    return row


def _resolve(monkeypatch, tmp_path, db, state, project="fixproj"):
    monkeypatch.setattr(router_spawn, "REGISTRY", db)
    monkeypatch.setattr(router_spawn, "MR", state)
    return router_spawn.resolve(project=project)


def _pair(entry):
    return f"{entry['provider']}/{entry['model']}"


def _excluded_pairs(r):
    return {f"{e['provider']}/{e['model']}" for e in r["exclusions"]}


def _reason_for(r, pair):
    for e in r["exclusions"]:
        if _pair(e) == pair:
            return "; ".join(e["why"])
    return None


# 6 models, 3 providers — provider-A owns price slots 1-3, so caps bite.
MODELS3 = [
    ("prov-a", "a1", 1.0),
    ("prov-a", "a2", 2.0),
    ("prov-a", "a3", 3.0),
    ("prov-b", "b1", 4.0),
    ("prov-b", "b2", 5.0),
    ("prov-c", "c1", 6.0),
]


def _open_providers():
    # production semantics: a provider absent from quota-state 'providers' is
    # NOT open (q.get('status') != 'open' gates it) — fixtures must mark them
    return {p: {"status": "open"} for p in sorted({m[0] for m in MODELS3})}


def test_no_caps_behavior_unchanged(monkeypatch, tmp_path):
    """AC5: no diversity keys anywhere → full chain, zero prune exclusions."""
    db = _mk_db(tmp_path, MODELS3)
    state = _state_dir(tmp_path, providers=_open_providers())
    r = _resolve(monkeypatch, tmp_path, db, state)
    assert "error" not in r
    assert [_pair(c) for c in r["chain"]] == \
        ["prov-a/a1", "prov-a/a2", "prov-a/a3", "prov-b/b1", "prov-b/b2", "prov-c/c1"]
    assert r["exclusions"] == []
    assert r["settings"]["max_consecutive_per_provider"] is None
    assert r["settings"]["max_total_per_provider"] is None
    assert r["settings"]["overrides"] == {"profile": False, "consecutive": False, "total": False}


def test_busy_model_skips_itself_only(monkeypatch, tmp_path):
    """AC2 + Bane's rule: busy a1 (limit 1, 1 in-flight) drops ONLY a1; sibling
    a2 stays AND keeps its cheaper-than-prov-b position."""
    db = _mk_db(tmp_path, MODELS3)
    state = _state_dir(
        tmp_path,
        providers=_open_providers(),
        models={"prov-a/a1": {"concurrency_limit": 1}},
        ledger_rows=[_started("prov-a/a1", tid="tr-busy")],
    )
    r = _resolve(monkeypatch, tmp_path, db, state)
    pairs = [_pair(c) for c in r["chain"]]
    assert "prov-a/a2" in pairs                      # sibling survives
    assert "prov-a/a1" not in pairs                  # busy model out
    assert pairs.index("prov-a/a2") < pairs.index("prov-b/b1")  # still first-provider slot 2
    assert "model busy" in (_reason_for(r, "prov-a/a1") or "")
    assert "in-flight >= limit 1" in (_reason_for(r, "prov-a/a1"))


def test_busy_model_sibling_order_cheap_first(monkeypatch, tmp_path):
    """Bane's pitfall: a busy cheap model must never remove its provider — the
    next-priced sibling becomes head if it's cheaper than other providers."""
    db = _mk_db(tmp_path, [("prov-a", "a1", 1.0), ("prov-b", "b1", 2.0)])
    state = _state_dir(
        tmp_path,
        providers=_open_providers(),
        models={"prov-a/a1": {"concurrency_limit": 1}},
        ledger_rows=[_started("prov-a/a1")],
    )
    r = _resolve(monkeypatch, tmp_path, db, state)
    assert _pair(r["head"]) == "prov-b/b1"   # a1 skipped individually…
    assert [_pair(c) for c in r["chain"]] == ["prov-b/b1"]
    # …and prov-a remains EXCLUDED only at pair level, not gated as a provider


def test_consecutive_cap_drops_third_in_row(monkeypatch, tmp_path):
    """AC4: global consecutive cap 2; prov-a owns slots 1-3 → a3 dropped."""
    db = _mk_db(tmp_path, MODELS3)
    state = _state_dir(tmp_path, providers=_open_providers(),
                       diversity={"max_consecutive_per_provider": 2})
    r = _resolve(monkeypatch, tmp_path, db, state)
    pairs = [_pair(c) for c in r["chain"]]
    assert pairs == ["prov-a/a1", "prov-a/a2", "prov-b/b1", "prov-b/b2", "prov-c/c1"]
    assert "consecutive cap 2" in (_reason_for(r, "prov-a/a3") or "")


def test_consecutive_cap_resets_across_providers(monkeypatch, tmp_path):
    """Consecutive counter resets when provider changes — b-run after a-run can
    reach its own cap independently."""
    db = _mk_db(tmp_path, MODELS3)
    state = _state_dir(tmp_path, providers=_open_providers(),
                       diversity={"max_consecutive_per_provider": 1})
    r = _resolve(monkeypatch, tmp_path, db, state)
    pairs = [_pair(c) for c in r["chain"]]
    # each provider contributes at most 1 hop-in-row; interleaving keeps survivors
    assert pairs == ["prov-a/a1", "prov-b/b1", "prov-c/c1"]
    assert "consecutive cap 1" in (_reason_for(r, "prov-a/a2") or "")
    assert "consecutive cap 1" in (_reason_for(r, "prov-b/b2") or "")


def test_total_cap_drops_overflow(monkeypatch, tmp_path):
    """AC4: total cap 2 over the whole chain; prov-a has 3 eligible → a3 dropped."""
    db = _mk_db(tmp_path, MODELS3)
    state = _state_dir(tmp_path, providers=_open_providers(),
                       diversity={"max_total_per_provider": 2})
    r = _resolve(monkeypatch, tmp_path, db, state)
    pairs = [_pair(c) for c in r["chain"]]
    assert pairs == ["prov-a/a1", "prov-a/a2", "prov-b/b1", "prov-b/b2", "prov-c/c1"]
    assert "chain cap 2" in (_reason_for(r, "prov-a/a3") or "")


def test_both_caps_compound(monkeypatch, tmp_path):
    """Both knobs together: reasons report whichever violated (consecutive fires first)."""
    db = _mk_db(tmp_path, MODELS3)
    state = _state_dir(tmp_path, providers=_open_providers(), diversity={"max_consecutive_per_provider": 2,
                                            "max_total_per_provider": 2})
    r = _resolve(monkeypatch, tmp_path, db, state)
    reason = _reason_for(r, "prov-a/a3")
    assert reason and "consecutive cap 2" in reason  # dropped while run still >2


def test_profile_override_beats_global(monkeypatch, tmp_path):
    """AC4: global consecutive=5 would keep everything; profile says 1 → drop."""
    db = _mk_db(tmp_path, MODELS3)
    with open(db) as f:
        reg = json.load(f)
    for p in reg["tables"]["task_profiles"]:
        if p["id"] == "FIXPROF":
            p["max_consecutive_per_provider"] = 1
    with open(db, "w") as f:
        json.dump(reg, f)
    state = _state_dir(tmp_path, providers=_open_providers(),
                       diversity={"max_consecutive_per_provider": 5})
    r = _resolve(monkeypatch, tmp_path, db, state)
    pairs = [_pair(c) for c in r["chain"]]
    assert pairs == ["prov-a/a1", "prov-b/b1", "prov-c/c1"]
    assert "consecutive cap 1" in (_reason_for(r, "prov-a/a2") or "")
    assert r["settings"]["max_consecutive_per_provider"] == 1
    assert r["settings"]["overrides"]["profile"] is True
    assert r["settings"]["overrides"]["consecutive"] is True


def test_price_order_preserved_among_survivors(monkeypatch, tmp_path):
    """AC4: with caps active, survivor chain strictly increasing effective price."""
    db = _mk_db(tmp_path, MODELS3)
    state = _state_dir(tmp_path, providers=_open_providers(), diversity={"max_consecutive_per_provider": 2,
                                            "max_total_per_provider": 2})
    r = _resolve(monkeypatch, tmp_path, db, state)
    eff = [c["usd_1m"] for c in r["chain"]]
    assert eff == sorted(eff)
    assert len(set(eff)) == len(eff)  # strictly increasing (distinct prices here)


def test_stale_started_row_not_inflight(monkeypatch, tmp_path):
    """'started' older than 30min (STALE_MS) does not make the model busy."""
    db = _mk_db(tmp_path, MODELS3)
    old_ts = (datetime.datetime.now(datetime.timezone.utc)
              - datetime.timedelta(minutes=31)).isoformat(timespec="seconds")
    state = _state_dir(
        tmp_path,
        providers=_open_providers(),
        models={"prov-a/a1": {"concurrency_limit": 1}},
        ledger_rows=[_started("prov-a/a1", ts=old_ts, tid="tr-old")],
    )
    r = _resolve(monkeypatch, tmp_path, db, state)
    assert "prov-a/a1" in [_pair(c) for c in r["chain"]]


def test_recent_terminal_row_not_inflight_but_started_counts(monkeypatch, tmp_path):
    """In-flight = traces whose LAST row is 'started'; once ended, not counted."""
    db = _mk_db(tmp_path, MODELS3)
    fresh = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    state = _state_dir(
        tmp_path,
        providers=_open_providers(),
        models={"prov-a/a1": {"concurrency_limit": 1}},
        ledger_rows=[
            _started("prov-a/a1", ts=fresh, tid="tr-done"),
            {"ts": fresh, "trace_id": "tr-done", "outcome": "success"},  # terminal: provider/model absent
        ],
    )
    r = _resolve(monkeypatch, tmp_path, db, state)
    assert "prov-a/a1" in [_pair(c) for c in r["chain"]]


def test_ledger_missing_file_fail_open(monkeypatch, tmp_path):
    """No ledger.jsonl at all → {} counts, empty chain unaffected (fail-open)."""
    db = _mk_db(tmp_path, MODELS3)
    state = _state_dir(tmp_path, providers=_open_providers(), models={"prov-a/a1": {"concurrency_limit": 3}})
    assert router_spawn.ledger_in_flight(state) == {}
    r = _resolve(monkeypatch, tmp_path, db, state)
    assert len(r["chain"]) == 6


def test_gated_provider_still_gated_with_diversity_on(monkeypatch, tmp_path):
    """Diversity adds pruning — it must not resurrect blocked providers."""
    db = _mk_db(tmp_path, MODELS3)
    provs = {p: {"status": "gated", "reason": "test"} for p, *_ in MODELS3}
    provs["prov-c"] = {"status": "open"}
    state = _state_dir(tmp_path, providers=provs,
                       diversity={"max_total_per_provider": 9})
    r = _resolve(monkeypatch, tmp_path, db, state)
    pairs = [_pair(c) for c in r["chain"]]
    assert pairs == ["prov-c/c1"]
    assert "quota GATED" in (_reason_for(r, "prov-a/a1") or "")
