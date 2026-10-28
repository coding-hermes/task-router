"""TR-1000976681059244 — account-wide free-pool exhaustion gate + probe coverage.

Two stacked defects, measured 2026-10-28 on the live registry
(`router_spawn.py duckbrain-sync --format json` returned head =
`xkiro/qwen/qwen3.7-plus:free` at $0.0):

  (1) the free-tier DAILY token pool is per ACCOUNT and covers every ':free'
      SKU on that seat, but the spawn path gated per (provider, model), so a
      sibling lane's `429 ... reached today's free-model token quota` did not
      imply the seat was dry — a spent $0 lane stayed head-eligible and burned
      the first call of every task.
  (2) the health probe capped at MAX_PER_PROVIDER lanes, cheapest-first, so a
      provider with more rankable lanes than the cap never had its tail
      probed — and an unprobed lane is indistinguishable from a healthy one to
      the gate stage, so it could HEAD a chain.

This battery is hermetic (no network): synthetic registry + state fixtures,
resolved through the real router_spawn.resolve().
"""
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts"))
import router_spawn  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(REPO, "data", "tables")

MARKER = "reached today's free-model token quota"
FREE_A = "free-a:free"
FREE_B = "free-b:free"


# ---------------------------------------------------------------- fixtures ----

def _load_tables():
    """The committed registry data as {table: [row...]} — used as the BASE so
    every table the resolver expects exists, then overridden per-test with the
    synthetic slice (the test_visibility.py pattern)."""
    tables = {}
    for fn in sorted(os.listdir(DATA_DIR)):
        if fn.endswith(".jsonl"):
            name = fn[:-len(".jsonl")]
            tables[name] = [json.loads(l) for l in
                            open(os.path.join(DATA_DIR, fn)) if l.strip()]
    return tables


def _lane(provider, model, price, tier=5, ctx=200000):
    return {"provider": provider, "model": model, "normalized_price": price,
            "plan_tier": 0, "context_limit": ctx, "data_class": "public",
            "token_factor": 1.0, "_tier": tier}


def _synthetic_tables():
    """One seat with two ':free' lanes + one paid lane, plus a competing paid
    seat. Cheapest-first order is then: free-a == free-b ($0.0) < paidseats
    paid-b ($0.5) < fpseat paid-a ($0.9)."""
    tables = _load_tables()
    tables["providers"] = [{"id": "fpseat", "plan": "SUB"},
                           {"id": "paidseat", "plan": "SUB"}]
    tables["projects"] = [{"id": "proj-a", "profile": "P1_TEST"}]
    tables["task_profiles"] = [{"id": "P1_TEST", "tag": "P1_TEST"}]
    tables["task_profile_requirements"] = [
        {"task_id": "P1_TEST", "category": "code_gen", "level": 0}]
    tables["models"] = [
        _lane("fpseat", FREE_A, 0.0),
        _lane("fpseat", FREE_B, 0.0),
        _lane("fpseat", "paid-a", 0.9),
        _lane("paidseat", "paid-b", 0.5),
    ]
    tables["model_tier"] = [
        {"model": FREE_A, "category": "code_gen", "tier": 5},
        {"model": FREE_B, "category": "code_gen", "tier": 5},
        {"model": "paid-a", "category": "code_gen", "tier": 5},
        {"model": "paid-b", "category": "code_gen", "tier": 5},
    ]
    tables["fallback_lanes"] = []
    return tables


def _ts():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _state_dir(tmp_path, health_state):
    """State dir with every gate file present: all providers OPEN (TR-203
    fail-closed needs a row), empty circuit, and the supplied health map."""
    d = tmp_path / "state"
    d.mkdir(exist_ok=True)
    json.dump({"updated": "test",
               "providers": {"fpseat": {"status": "open"},
                             "paidseat": {"status": "open"}}},
              open(d / "quota-state.json", "w"))
    json.dump({"providers": health_state}, open(d / "health-state.json", "w"))
    json.dump({"pairs": {}, "v2": {}}, open(d / "circuit-state.json", "w"))
    return str(d)


def _write_registry(tmp_path, tables):
    reg = tmp_path / "registry.json"
    reg.write_text(json.dumps({"version": 3, "tables": tables}))
    return str(reg)


def _resolve(monkeypatch, tmp_path, tables, state):
    monkeypatch.setattr(router_spawn, "REGISTRY", _write_registry(tmp_path, tables))
    monkeypatch.setattr(router_spawn, "MR", state)
    return router_spawn.resolve(project="proj-a")


def _pair(e):
    return f"{e['provider']}/{e['model']}"


def _excluded(r, model):
    """The exclusion rows for `model`, if any."""
    return [e for e in (r.get("exclusions") or []) if e.get("model") == model]


def _why_text(r, model):
    return " | ".join(w for e in _excluded(r, model) for w in (e.get("why") or []))


# ------------------------------------------------- A: account-wide free pool ---

def test_free_pool_gate_excludes_every_free_lane_on_the_seat(monkeypatch, tmp_path):
    """A 429 carrying the quota phrase on ONE :free lane excludes EVERY :free
    lane of that seat, with the reason visible in the payload's exclusions, and
    the chain heads a PAID lane instead."""
    tables = _synthetic_tables()
    health = {
        "fpseat": {
            "status": "OK", "ts": _ts(),
            "models": {
                # the probed sibling: this is the ONLY lane with the 429
                FREE_A: {"status": "DOWN", "ts": _ts(),
                         "error": f"HTTP 429: {MARKER} (4029)"},
                FREE_B: {"status": "OK", "ts": _ts()},
                "paid-a": {"status": "OK", "ts": _ts()},
            },
        },
        "paidseat": {"status": "OK", "ts": _ts(),
                     "models": {"paid-b": {"status": "OK", "ts": _ts()}}},
    }
    r = _resolve(monkeypatch, tmp_path, tables, _state_dir(tmp_path, health))
    assert "error" not in r, r.get("error")
    # every :free lane of the seat is excluded, each naming the marker
    for model in (FREE_A, FREE_B):
        text = _why_text(r, model)
        assert router_spawn.FREE_POOL_MARKER in text, (model, text)
    # ... and the gate is NOT a provider-wide nuke: the paid lane of the SAME
    # seat stays eligible, it is merely dearer than the competitor
    assert not _excluded(r, "paid-a"), r.get("exclusions")
    # no :free lane heads the chain
    assert r["head"] is not None
    assert not str(r["head"].get("model", "")).endswith(":free")
    assert _pair(r["head"]) == "paidseat/paid-b"
    # the seat is reported, with a reset bound, so the gate is never silent
    exhausted = r["free_pool"]["exhausted"]
    assert [e["provider"] for e in exhausted] == ["fpseat"], exhausted
    bound = datetime.datetime.fromisoformat(exhausted[0]["until"])
    assert bound.tzinfo is not None
    assert (bound.hour, bound.minute, bound.second) == (0, 0, 0)
    assert bound > datetime.datetime.now(datetime.timezone.utc)


def test_free_pool_gate_does_not_fire_on_a_bare_429(monkeypatch, tmp_path):
    """Negative control: a plain 429 (no quota phrase) must NOT open the gate —
    a rate limit is transient, an exhausted daily pool is not."""
    tables = _synthetic_tables()
    health = {
        "fpseat": {
            "status": "OK", "ts": _ts(),
            "error": "HTTP 429 Too Many Requests (rate limited)",
            "models": {
                FREE_A: {"status": "OK", "ts": _ts()},
                FREE_B: {"status": "OK", "ts": _ts()},
                "paid-a": {"status": "OK", "ts": _ts()},
            },
        },
        "paidseat": {"status": "OK", "ts": _ts(),
                     "models": {"paid-b": {"status": "OK", "ts": _ts()}}},
    }
    r = _resolve(monkeypatch, tmp_path, tables, _state_dir(tmp_path, health))
    assert "error" not in r, r.get("error")
    for model in (FREE_A, FREE_B):
        assert MARKER not in _why_text(r, model), r.get("exclusions")
    assert r["free_pool"]["exhausted"] == []
    # the cheapest lane heads again — the gate did not fire
    assert _pair(r["head"]) == f"fpseat/{FREE_A}"


def test_free_pool_seats_helper_detects_and_self_expires():
    """Unit contract for the helper: marker detection from a model-level error,
    nothing on a clean health map, and NO gate once the observation's UTC day
    has passed (the pool has refilled)."""
    assert router_spawn.FREE_POOL_MARKER == "free-model token quota"
    hs = {"seat-x": {"ts": "2026-10-28T22:05:00+00:00",
                     "models": {"a:free": {"ts": "2026-10-28T22:05:00+00:00",
                                           "error": f"429 {MARKER}"}}}}
    now = datetime.datetime(2026, 10, 28, 23, 30, tzinfo=datetime.timezone.utc)
    got = router_spawn._free_pool_seats(hs, now)
    assert list(got) == ["seat-x"], got
    assert got["seat-x"] == "2026-10-29T00:00:00+00:00"
    # no marker anywhere -> nothing gated
    assert router_spawn._free_pool_seats(
        {"seat-x": {"models": {"a:free": {"error": "HTTP 500"}}}}, now) == {}
    # malformed / absent health -> fail-open
    assert router_spawn._free_pool_seats(None, now) == {}
    assert router_spawn._free_pool_seats({"seat-x": "not-a-dict"}, now) == {}
    # the observation's UTC day is over (pool refilled) -> the gate has EXPIRED,
    # so a stale snapshot can never strand a healthy seat
    for later in (datetime.datetime(2026, 10, 29, 0, 0, tzinfo=datetime.timezone.utc),
                  datetime.datetime(2026, 10, 30, 12, 0, tzinfo=datetime.timezone.utc)):
        assert router_spawn._free_pool_seats(hs, later) == {}, later


# ------------------------------------------------------- C: probe coverage ----

def test_unprobed_lane_never_heads_a_chain(monkeypatch, tmp_path):
    """Coverage invariant: when the seat WAS probed this snapshot but a lane
    carries no stamp, that lane sorts BEHIND its probed sibling instead of
    heading the chain."""
    tables = _synthetic_tables()
    health = {
        "fpseat": {
            "status": "OK", "ts": _ts(),
            "models": {
                # FREE_B is probed and healthy; FREE_A has NO stamp at all
                FREE_B: {"status": "OK", "ts": _ts()},
                "paid-a": {"status": "OK", "ts": _ts()},
            },
        },
        "paidseat": {"status": "OK", "ts": _ts(),
                     "models": {"paid-b": {"status": "OK", "ts": _ts()}}},
    }
    r = _resolve(monkeypatch, tmp_path, tables, _state_dir(tmp_path, health))
    assert "error" not in r, r.get("error")
    assert _pair(r["head"]) == f"fpseat/{FREE_B}", r["head"]
    # the unprobed lane is reported, never silently reordered
    unprobed = {u["model"] for u in r["probe_coverage"]["unprobed_eligible"]}
    assert FREE_A in unprobed, r["probe_coverage"]
    # a lane with no stamp is still a usable FALLBACK hop (demoted, not banned)
    assert not _excluded(r, FREE_A)


def test_unprobed_lane_sinks_even_with_a_bigger_context():
    """The coverage slot sits BEFORE the context tie-break, so a large context
    on an unprobed lane cannot rescue its head position."""
    probed = {"provider": "seat", "model": "probed", "normalized_price": 0.0,
              "plan_tier": 0, "context_limit": 1000}
    unprobed = {"provider": "seat", "model": "unprobed", "normalized_price": 0.0,
                "plan_tier": 0, "context_limit": 10_000_000}
    router_spawn._stamp_probe_coverage([probed, unprobed],
                                       {"probed_models": {"seat": {"probed"}}})
    assert unprobed.get("_probe_unprobed")
    assert not probed.get("_probe_unprobed")
    assert router_spawn._legacy_sort_key(probed) < router_spawn._legacy_sort_key(unprobed)


def test_stamp_probe_coverage_only_stamps_untouched_siblings():
    """Unit contract: a lane is stamped only when ITS provider was probed and
    it has no stamp; an unprobed provider demotes nothing ('unknown is not
    low')."""
    a = {"provider": "p", "model": "a", "normalized_price": 0.0, "plan_tier": 0}
    b = {"provider": "p", "model": "b", "normalized_price": 0.0, "plan_tier": 0}
    c = {"provider": "q", "model": "c", "normalized_price": 0.0, "plan_tier": 0}
    router_spawn._stamp_probe_coverage([a, b, c], {"probed_models": {"p": {"a"}}})
    assert not a.get("_probe_unprobed")
    assert b.get("_probe_unprobed")
    assert not c.get("_probe_unprobed")  # provider q was not probed
    # the slot is real: identical otherwise, the probed lane sorts first
    assert router_spawn._legacy_sort_key(a) < router_spawn._legacy_sort_key(b)
    # no coverage context at all -> nothing stamped (historical ordering)
    d = {"provider": "p", "model": "d", "normalized_price": 0.0, "plan_tier": 0}
    router_spawn._stamp_probe_coverage([d], None)
    assert not d.get("_probe_unprobed")
