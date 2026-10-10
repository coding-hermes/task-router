"""TR-210 regression battery — quota L3: the pacing gate.

The quota plane could SEE a window (L0 declaration, L1 readback, L2
accounting view: TR-206..209) but nothing DECIDED with it. "N calls in a
burst, then a 429 wall" is a policy failure, not a visibility failure.
scripts/router_quota_pacing.py is that policy: admit a call only if

    spent_in_window + est_cost <= limit * safety_margin

with soft-spacing (delay) preferred over refusal, an exhausted window
refusing EXPLICITLY with the window named and COUNTED, cross-account rotation
only to a sibling with its OWN headroom, unknown-limit windows contributing
NO constraint while RECORDING that fact, and never an unbounded wait on the
request path (docs/quota-layers-spec.md §5).

Covered (the ACs):
  AC1  the invariant: random traffic over synthetic windows never pushes
       admitted spend past limit*safety_margin; the margin line is exact.
  AC2  soft-spacing is COMPUTED (fraction of time-to-reset), preferred over
       refusal while it fits the cap, and every refusal names the window and
       is counted.
  AC3  cross-account rotation moves ONLY to a sibling whose own window admits
       the spend; a paced-out account with no headroom anywhere refuses.
  AC4  an unknown/unusable limit contributes NO constraint and the fact is
       recorded (counter + reason) — silence stays visible, never capacity.
  AC5  the caller's EXISTING admission counters are reused and incremented
       exactly once per decision (incl. the natural empty-dict caller and an
       object-shaped counter); the module never sleeps; L3 writes NOTHING
       (no second store).
  AC6  reachable as `router quota pacing` (task_router.cli -> router_quota.py
       delegate), exit codes 0/2 and the JSON shape.

Hermetic: every test builds its own blocks/ledgers/limits under tmp_path and
pins ROUTING_OUTCOMES_FILE / ROUTER_QUOTA_LIMITS_FILE there. The live ledger
is NEVER read.
"""
import datetime
import json
import os
import subprocess
import sys
import threading

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")

if REPO not in sys.path:
    sys.path.insert(0, REPO)
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import router_outcomes  # noqa: E402
import router_quota_accounting as acc  # noqa: E402
import router_quota_pacing as pacing  # noqa: E402

UTC = datetime.timezone.utc
T0 = 1780000000.0            # 2026-05-29T07:46:40Z — fixed clock for all tests


# ------------------------------------------------------------------ helpers --

def _iso(epoch):
    return datetime.datetime.fromtimestamp(epoch, UTC).isoformat(timespec="seconds")


def _block(limit=10.0, spent=0.0, resets_in=1000.0, provider="p1",
           account="default", window="rolling_5h", limit_usd=True):
    """A synthetic L2 block with the REAL L2 shape (see account_provider)."""
    blk = {"provider": provider, "window": window, "as_of": _iso(T0),
           "n_rows": 3, "spent_usd": spent,
           "limit_usd": limit if limit_usd else None,
           "remaining_usd": (None if limit is None else max(0.0, limit - spent)),
           "remaining_reason": None, "resets_at": None,
           "headroom_usd_per_s": 0.0}
    if account is not None:
        blk["account"] = account
    if resets_in is not None:
        blk["resets_at"] = _iso(T0 + resets_in)
    if limit is None:
        blk["remaining_usd"] = None
        blk["remaining_reason"] = "no limit configured for provider p1"
    return blk


def _row(provider="p1", model="m1", ts=T0 - 60, cost=0.5, session="s", **extra):
    r = {"source_system": "test", "session_id": session, "provider": provider,
         "model": model, "tokens_in": 100, "tokens_out": 50,
         "tokens_reasoning": 0, "cost_usd": cost, "success": None, "ts": ts}
    r.update(extra)
    return r


def _ledger(tmp_path, rows):
    p = tmp_path / "outcomes.jsonl"
    with open(p, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return str(p)


def _limits(tmp_path, providers=None):
    p = tmp_path / "quota_limits.json"
    p.write_text(json.dumps({"providers": providers or {}}))
    return str(p)


def _limits_usd(tmp_path, limit=10.0, window="rolling_5h", provider="p1"):
    return _limits(tmp_path, {provider: {"window": window,
                                         "limit_usd": limit}})


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, tmp_path):
    """No test may touch the live ledger, limits or price map."""
    monkeypatch.setenv("ROUTING_OUTCOMES_FILE", str(tmp_path / "unused.jsonl"))
    monkeypatch.setenv("ROUTER_QUOTA_LIMITS_FILE",
                       str(tmp_path / "unused-limits.json"))
    monkeypatch.setattr(router_outcomes, "_PRICE_MAP", {})


class _FakeLock:
    """Records that the caller's lock was actually taken (AC5)."""

    def __init__(self):
        self.entered = 0

    def __enter__(self):
        self.entered += 1
        return self

    def __exit__(self, *exc):
        return False


class _ObjectCounters:
    """The object-shaped counter (attribute, not dict) — AC5."""

    def __init__(self):
        self.accepted = 0
        self.rejected = 0
        self.waiting = 0
        self.no_limit_contribution = 0


# ==== AC1 — the admission invariant =========================================

def test_ac1_margin_line_is_exact():
    """spent + est <= limit*margin admits; one cent over refuses."""
    g = pacing.PacingGate(counters={}, margin=0.9)
    blk = _block(limit=10.0, spent=9.0)             # remaining 1.0, line at 9.0
    assert g.decide(blk, 0.0, now_ts=T0)["outcome"] == "admitted"
    assert g.decide(blk, 0.5, now_ts=T0)["outcome"] == "refused"   # 9.5 > 9.0
    assert g.decide(blk, 0.0, now_ts=T0)["outcome"] == "admitted"
    # margin 1.0 degenerates to remaining >= est — never above the ceiling
    g1 = pacing.PacingGate(counters={}, margin=1.0)
    assert g1.decide(blk, 1.0, now_ts=T0)["outcome"] == "admitted"
    assert g1.decide(blk, 1.01, now_ts=T0)["outcome"] == "refused"


def test_ac1_invalid_margin_refused_at_construction():
    with pytest.raises(ValueError):
        pacing.PacingGate(margin=0.0)
    with pytest.raises(ValueError):
        pacing.PacingGate(margin=1.5)


def test_ac1_fuzz_random_traffic_never_breaks_the_invariant():
    """200 windows x 200 random calls: admitted spend stays under the line.

    The admitted spend is accumulated by the TEST from the gate's own
    verdicts, so the invariant is checked against real decisions, not against
    a copy of the margin formula.
    """
    import random
    rnd = random.Random(20261010)
    margins = [0.5, 0.9, 0.95, 1.0]
    for trial in range(200):
        limit = rnd.choice([0.5, 1.0, 5.0, 10.0, 100.0])
        margin = rnd.choice(margins)
        g = pacing.PacingGate(counters={}, margin=margin)
        spent = 0.0
        for _ in range(200):
            est = round(rnd.uniform(0.0, limit), 6)
            blk = _block(limit=limit, spent=spent, resets_in=None)
            dec = g.decide(blk, est_cost=est, now_ts=T0)
            if dec["outcome"] == "admitted":
                spent += est
                assert spent <= limit * margin + 1e-9, (
                    f"trial {trial}: admitted spend {spent} past "
                    f"limit*margin {limit * margin}")


def test_ac1_fully_spent_window_refuses():
    g = pacing.PacingGate(counters={})
    dec = g.decide(_block(limit=10.0, spent=10.0), 0.01, now_ts=T0)
    assert dec["outcome"] == "refused"
    assert dec["remaining_usd"] == 0.0


# ==== AC2 — soft-spacing, explicit counted refusal ===========================

def test_ac2_delay_is_a_fraction_of_time_to_reset():
    """A call costing f of the remaining budget waits f of the time left."""
    assert pacing.pacing_delay_s(5.0, 1.0, T0 + 1000.0, T0) == 200.0
    assert pacing.pacing_delay_s(10.0, 10.0, T0 + 100.0, T0) == 100.0
    # clamps: over-spend of the remainder -> the whole remaining window
    assert pacing.pacing_delay_s(5.0, 99.0, T0 + 100.0, T0) == 100.0
    # no reset time / past reset / free call / exhausted -> no invented wait
    assert pacing.pacing_delay_s(5.0, 1.0, None, T0) == 0.0
    assert pacing.pacing_delay_s(5.0, 1.0, T0 - 1.0, T0) == 0.0
    assert pacing.pacing_delay_s(5.0, 0.0, T0 + 100.0, T0) == 0.0
    assert pacing.pacing_delay_s(0.0, 1.0, T0 + 100.0, T0) == 0.0


def test_ac2_delay_preferred_over_refusal_when_it_fits_the_cap():
    counters = {}
    g = pacing.PacingGate(counters=counters, margin=0.9, wait_s=1.0,
                          wait_cap_s=300.0)
    # limit 10, spent 8.5 -> remaining 1.5: est 1.0 is under the ceiling
    # (9.5 <= 9.0 is false) but the smooth-spend delay, 1.0/1.5 of 300s left,
    # is 200s — inside the cap, so the gate SPACES instead of refusing.
    blk = _block(limit=10.0, spent=8.5, resets_in=300.0)
    dec = g.decide(blk, est_cost=1.0, now_ts=T0)
    assert dec["outcome"] == "waited", dec
    assert dec["delay_s"] == pytest.approx(200.0)
    assert counters["waiting"] == 1 and counters.get("rejected", 0) == 0
    assert "soft-spacing" in dec["reason"]


def test_ac2_wait_over_cap_is_a_refusal_not_a_stall():
    """AC5's bounded wait: a delay past the cap refuses, it does not block."""
    counters = {}
    g = pacing.PacingGate(counters=counters, margin=0.9, wait_s=1.0,
                          wait_cap_s=30.0)
    dec = g.decide(_block(limit=10.0, spent=9.5, resets_in=1000.0), 1.0,
                   now_ts=T0)
    assert dec["outcome"] == "refused"
    assert "exceeds" in dec["reason"] and "cap" in dec["reason"]
    assert counters["rejected"] == 1 and counters.get("waiting", 0) == 0


def test_ac2_default_is_refuse_not_delay():
    """wait_s=0 (the default) never waits — the caller wants a verdict."""
    counters = {}
    g = pacing.PacingGate(counters=counters)
    dec = g.decide(_block(limit=10.0, spent=9.5, resets_in=1000.0), 1.0,
                   now_ts=T0)
    assert dec["outcome"] == "refused"
    assert counters == {"rejected": 1}


def test_ac2_refusal_names_the_window_and_is_counted():
    counters = {}
    g = pacing.PacingGate(counters=counters)
    blk = _block(limit=1.0, spent=1.0, provider="zai-glm",
                 account="acct-2", window="weekly")
    dec = g.decide(blk, 0.5, now_ts=T0)
    assert dec["outcome"] == "refused"
    for frag in ("zai-glm", "acct-2", "weekly"):
        assert frag in dec["reason"], dec["reason"]
    assert counters["rejected"] == 1


# ==== AC3 — cross-account rotation ==========================================

def test_ac3_rotates_only_to_a_sibling_with_its_own_headroom():
    counters = {}
    g = pacing.PacingGate(counters=counters, margin=0.9)
    blocks = {"default": _block(limit=10.0, spent=10.0, account="default"),
              "alt": _block(limit=10.0, spent=1.0, account="alt")}
    blk, dec = g.pick_account("p1", blocks, est_cost=0.5, now_ts=T0)
    assert blk["account"] == "alt"
    assert dec["outcome"] == "admitted" and dec["rotated_from"] == "default"
    # ONE decision, ONE count — probing the preferred block must not inflate
    assert counters == {"accepted": 1}


def test_ac3_no_sibling_headroom_refuses_naming_the_preferred_window():
    counters = {}
    g = pacing.PacingGate(counters=counters, margin=0.9)
    blocks = {"default": _block(limit=10.0, spent=10.0, account="default"),
              "alt": _block(limit=10.0, spent=10.0, account="alt")}
    blk, dec = g.pick_account("p1", blocks, est_cost=0.5, now_ts=T0)
    assert blk is None
    assert dec["outcome"] == "refused"
    assert dec["account"] == "default"          # the PREFERRED window is named
    assert "account default" in dec["reason"]
    assert counters == {"rejected": 1}          # counted once, not per sibling


def test_ac3_preferred_account_named_explicitly():
    g = pacing.PacingGate(counters={}, margin=0.9)
    blocks = {"a": _block(limit=10.0, spent=10.0, account="a"),
              "b": _block(limit=10.0, spent=0.0, account="b")}
    blk, dec = g.pick_account("p1", blocks, est_cost=1.0, preferred="a",
                              now_ts=T0)
    assert blk["account"] == "b" and dec["rotated_from"] == "a"
    # a preferred account with no block is an explicit refusal, not a silent
    # move into another account's budget
    blk2, dec2 = g.pick_account("p1", blocks, est_cost=1.0, preferred="ghost",
                                now_ts=T0)
    assert blk2 is None and "has no L2 block" in dec2["reason"]


def test_ac3_paced_out_preferred_with_unknown_sibling_counts_once():
    """A no-limit sibling is admissible (AC4) and still counted once."""
    counters = {}
    g = pacing.PacingGate(counters=counters, margin=0.9)
    blocks = {"default": _block(limit=10.0, spent=10.0, account="default"),
              "alt": _block(limit=None, account="alt")}
    blk, dec = g.pick_account("p1", blocks, est_cost=0.5, now_ts=T0)
    assert blk["account"] == "alt"
    assert dec["outcome"] == "no_limit"
    assert counters == {"no_limit_contribution": 1}


# ==== AC4 — unknown limits contribute nothing, and say so ===================

def test_ac4_unknown_limit_contributes_no_constraint_and_is_recorded():
    counters = {}
    g = pacing.PacingGate(counters=counters)
    blk = _block(limit=None)
    dec = g.decide(blk, est_cost=999.0, now_ts=T0)      # any spend passes
    assert dec["outcome"] == "no_limit"
    assert dec["no_limit_contribution"] is True
    assert dec["reason"]                              # the fact is NAMED
    assert counters["no_limit_contribution"] == 1
    assert counters.get("rejected", 0) == 0           # never a refusal


def test_ac4_unpriceable_spend_is_unknown_not_headroom():
    """remaining None (spent unpriceable) is an unknown contribution too."""
    counters = {}
    g = pacing.PacingGate(counters=counters)
    blk = _block(limit=10.0)
    blk["remaining_usd"] = None
    blk["remaining_reason"] = "spent could not be priced"
    dec = g.decide(blk, est_cost=5.0, now_ts=T0)
    assert dec["outcome"] == "no_limit"
    assert "could not be priced" in dec["reason"]
    assert counters["no_limit_contribution"] == 1


def test_ac4_unusable_limit_value_is_unknown_not_a_budget():
    counters = {}
    g = pacing.PacingGate(counters=counters)
    blk = _block(limit=10.0, spent=1.0)
    blk["limit_usd"] = "ten"                     # corrupt config, not a limit
    dec = g.decide(blk, est_cost=5.0, now_ts=T0)
    assert dec["outcome"] == "no_limit"
    assert "unusable limit_usd" in dec["reason"]
    assert counters["no_limit_contribution"] == 1


# ==== AC5 — reuse the existing counters; bounded wait; no second store ======

def test_ac5_empty_dict_counters_are_incremented():
    """The natural caller shape `PacingGate(counters={})` counts.

    Regression: a falsy-dict guard made every outcome uncounted, so a caller
    wiring the gate onto the request path saw zero refusals.
    """
    counters = {}
    g = pacing.PacingGate(counters=counters)
    g.decide(_block(limit=10.0, spent=0.0), 1.0, now_ts=T0)
    g.decide(_block(limit=10.0, spent=10.0), 1.0, now_ts=T0)
    assert counters == {"accepted": 1, "rejected": 1}
    assert g.stats() == {"accepted": 1, "rejected": 1}
    assert "lock" not in g.stats()


def test_ac5_admission_counter_lock_is_taken():
    """The _ADMISSION shape: dict + lock, incremented under the lock."""
    lock = _FakeLock()
    counters = {"lock": lock}
    g = pacing.PacingGate(counters=counters)
    g.decide(_block(limit=10.0, spent=0.0), 1.0, now_ts=T0)
    assert lock.entered == 1
    assert counters["accepted"] == 1


def test_ac5_object_shaped_counters_are_reused():
    counters = _ObjectCounters()
    g = pacing.PacingGate(counters=counters)
    g.decide(_block(limit=10.0, spent=0.0), 1.0, now_ts=T0)     # accepted
    g.decide(_block(limit=10.0, spent=10.0), 1.0, now_ts=T0)    # rejected
    assert (counters.accepted, counters.rejected) == (1, 1)
    # an object owns its own publishing — stats() does not pretend to know it
    assert g.stats() is None


def test_ac5_counters_none_is_allowed():
    """No counters object: decisions still work, nothing is invented."""
    g = pacing.PacingGate(counters=None)
    assert g.decide(_block(limit=10.0, spent=1.0), 1.0, now_ts=T0)["outcome"] == \
        "admitted"
    assert g.stats() is None


def test_ac5_invalid_est_cost_is_an_operator_error_not_a_window_refusal():
    counters = {}
    g = pacing.PacingGate(counters=counters)
    for bad in (-1.0, float("nan"), float("inf")):
        dec = g.decide(_block(), bad, now_ts=T0)
        assert dec["outcome"] == "refused" and dec["operator_error"] is True
    assert counters == {}          # the window's counters stay clean


def test_ac5_the_module_never_sleeps(monkeypatch):
    """AC5: the gate COMPUTES a delay; waiting is the caller's call."""
    def _boom(*a, **kw):
        raise AssertionError("router_quota_pacing must never sleep()")

    monkeypatch.setattr(pacing.time, "sleep", _boom)
    src = open(os.path.join(SCRIPTS, "router_quota_pacing.py")).read()
    # no call site at all (the module's own prose may say the word, so match
    # the call form, not the word)
    assert "time.sleep(" not in src
    assert "import sleep" not in src
    g = pacing.PacingGate(counters={}, margin=0.9, wait_s=5.0, wait_cap_s=999.0)
    dec = g.decide(_block(limit=10.0, spent=9.5, resets_in=500.0), 1.0,
                   now_ts=T0)
    assert dec["outcome"] == "waited" and dec["delay_s"] > 0


def test_ac5_l3_writes_nothing_no_second_store(tmp_path):
    """Layer discipline: L3 is a pure decision over the L2 view."""
    led = _ledger(tmp_path, [_row(cost=1.0)])
    lim = _limits_usd(tmp_path, limit=10.0)
    blocks = acc.account_ledger(path=led, limits=acc.load_limits(lim),
                                now_ts=T0)
    before = sorted(os.listdir(tmp_path))
    g = pacing.PacingGate(counters={}, margin=0.9)
    g.decide(blocks[0], 1.0, now_ts=T0)
    g.pick_account("p1", {"default": blocks[0]}, 1.0, now_ts=T0)
    assert sorted(os.listdir(tmp_path)) == before


def test_ac5_gate_over_a_real_l2_block_from_the_ledger(tmp_path):
    """End-to-end: a real ledger -> L2 view -> L3 verdict."""
    led = _ledger(tmp_path, [_row(session="a", cost=8.0)])
    lim = _limits_usd(tmp_path, limit=10.0)
    blocks = acc.account_ledger(path=led, limits=acc.load_limits(lim),
                                now_ts=T0)
    blk = [b for b in blocks if b["provider"] == "p1"][0]
    assert blk["remaining_usd"] == 2.0
    counters = {}
    g = pacing.PacingGate(counters=counters, margin=0.9)
    assert g.decide(blk, 0.5, now_ts=T0)["outcome"] == "admitted"
    assert g.decide(blk, 5.0, now_ts=T0)["outcome"] == "refused"   # 13 > 9
    assert counters == {"accepted": 1, "rejected": 1}


# ==== AC6 — the operator surface: `router quota pacing` =====================

_SCRIPT = os.path.join(SCRIPTS, "router_quota_pacing.py")


def _cli(*args, env_extra=None):
    env = dict(os.environ)
    env.setdefault("TASK_ROUTER_HOME", REPO)
    if env_extra:
        env.update(env_extra)
    return subprocess.run([sys.executable, _SCRIPT, *args],
                          capture_output=True, text=True, env=env, timeout=120)


def test_ac6_cli_json_end_to_end(tmp_path):
    led = _ledger(tmp_path, [_row(cost=1.0)])
    lim = _limits_usd(tmp_path, limit=10.0)
    p = _cli("pacing", "--provider", "p1", "--cost-c", "0.5",
             "--ledger", led, "--limits", lim, "--now", str(T0), "--json")
    assert p.returncode == 0, p.stderr
    doc = json.loads(p.stdout)
    assert doc["as_of"] == _iso(T0)
    assert doc["provider"] == "p1" and doc["margin"] == 0.9
    assert doc["decision"]["outcome"] == "admitted"
    assert doc["gate_stats"] == {"accepted": 1}


def test_ac6_cli_refusal_is_exit_0_with_a_named_window(tmp_path):
    led = _ledger(tmp_path, [_row(cost=9.9)])
    lim = _limits_usd(tmp_path, limit=10.0)
    p = _cli("pacing", "--provider", "p1", "--cost-c", "5",
             "--ledger", led, "--limits", lim, "--now", str(T0), "--json")
    assert p.returncode == 0, p.stderr           # a refusal is a VERDICT
    doc = json.loads(p.stdout)
    assert doc["decision"]["outcome"] == "refused"
    assert "window rolling_5h" in doc["decision"]["reason"]
    assert doc["gate_stats"]["rejected"] == 1


def test_ac6_cli_exit_2_usage_error_writes_nothing(tmp_path):
    led = _ledger(tmp_path, [_row(cost=1.0)])
    lim = _limits_usd(tmp_path)
    before = sorted(os.listdir(tmp_path))
    p = _cli("pacing", "--provider", "p1", "--ledger", led, "--limits", lim,
             "--now", "not-a-date")
    assert p.returncode == 2
    assert "not ISO-8601" in p.stderr
    assert sorted(os.listdir(tmp_path)) == before
    # a provider with no ledger rows and no limit config is an explicit error
    p2 = _cli("pacing", "--provider", "ghost", "--ledger", led,
              "--limits", lim, "--now", str(T0))
    assert p2.returncode == 2 and "no ledger rows" in p2.stderr


def test_ac6_cli_blocks_json_rotates(tmp_path):
    led = _ledger(tmp_path, [_row(cost=1.0)])
    lim = _limits_usd(tmp_path, limit=10.0)
    blocks = {"default": _block(limit=10.0, spent=10.0, account="default"),
              "alt": _block(limit=10.0, spent=1.0, account="alt")}
    bj = tmp_path / "blocks.json"
    bj.write_text(json.dumps(blocks))
    p = _cli("pacing", "--provider", "p1", "--cost-c", "0.5",
             "--blocks-json", str(bj), "--accounts", "default",
             "--ledger", led, "--limits", lim, "--now", str(T0), "--json")
    assert p.returncode == 0, p.stderr
    doc = json.loads(p.stdout)
    assert doc["decision"]["rotated_from"] == "default"
    assert doc["decision"]["account"] == "alt"


def test_ac6_cli_human_output(tmp_path):
    led = _ledger(tmp_path, [_row(cost=1.0)])
    lim = _limits_usd(tmp_path, limit=10.0)
    p = _cli("pacing", "--provider", "p1", "--cost-c", "0.5",
             "--ledger", led, "--limits", lim, "--now", str(T0))
    assert p.returncode == 0, p.stderr
    assert "L3 quota pacing" in p.stdout
    assert "admitted" in p.stdout and "counters" in p.stdout


def test_ac6_reachable_as_router_quota_pacing(tmp_path):
    """The operator surface the fleet calls: `router quota pacing`."""
    led = _ledger(tmp_path, [_row(cost=1.0)])
    lim = _limits_usd(tmp_path, limit=10.0)
    router = os.path.join(REPO, "task_router", "cli.py")
    env = dict(os.environ)
    env["TASK_ROUTER_HOME"] = str(tmp_path)
    env["PYTHONPATH"] = REPO + os.pathsep + env.get("PYTHONPATH", "")
    p = subprocess.run([sys.executable, router, "quota", "pacing",
                        "--provider", "p1", "--cost-c", "0.5",
                        "--ledger", led, "--limits", lim, "--now", str(T0),
                        "--json"],
                       capture_output=True, text=True, env=env, timeout=120)
    assert p.returncode == 0, p.stderr
    doc = json.loads(p.stdout)
    assert doc["decision"]["outcome"] == "admitted"
    # the delegate keeps the layer's exit codes (2 on operator error)
    p2 = subprocess.run([sys.executable, router, "quota", "pacing",
                         "--provider", "p1", "--ledger", led, "--limits", lim,
                         "--now", "nope"],
                        capture_output=True, text=True, env=env, timeout=120)
    assert p2.returncode == 2


def test_ac6_module_is_wired_into_the_runtime_sync_list():
    """A runtime script the scheduler/foremen call must be synced live."""
    sync = open(os.path.join(SCRIPTS, "sync_runtime.sh")).read()
    assert "router_quota_pacing.py" in sync
    assert "router_quota_accounting.py router_quota_pacing.py" in sync
