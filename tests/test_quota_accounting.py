"""TR-209 regression battery — quota L2 accounting: a VIEW over the ledger.

The quota plane had limits (L0, data/tables/provider_quota.jsonl) and provider
readbacks (L1, TR-207/TR-208) but no answer to "how much have WE spent in the
current window". TR-209 adds that as a QUERY over data/state/outcomes.jsonl —
NO second store (docs/quota-layers-spec.md §1/§4).

Covered (the ACs):
  AC1  no second store: nothing is written anywhere; deleting every ledger row
       empties the view; appending a row moves it; mutating a row moves it.
       (Mutate-the-ledger -> results change is the derivation proof.)
  AC2  window sums at boundaries: rolling window rolling OFF (a row exactly at
       the start edge is out; at `now` it counts); weekly window; calendar
       month window; out-of-order rows sum identically (order independence).
  AC3  unit conversion reuses the price table (plan_effective_cost, verified
       against its exact arithmetic); unknown price -> spent NULL, never 0,
       while the row still counts in the token sums.
  AC4  remaining is NULL whenever limit is NULL and the reason travels with it.
  AC5  per-provider keying: many models/lanes under one limit = ONE budget
       (row count and model count never multiply it).
  AC6  the limit-config source exists, is read at query time (edit mid-process
       takes effect), is validated, and fails open on a corrupt file with a
       warning instead of silently pretending "no limits".
  AC7  the CLI prints per-provider JSON with spent/limit/remaining/resets_at/
       headroom, honors --provider/--ledger/--limits/--now/--json, exits
       0/2/1 like router_quota.py, and is reachable as `router quota accounting`.

Hermetic: every test writes its own temp ledger + temp limit config and points
ROUTER_QUOTA_LIMITS_FILE / --ledger at them. The live 366k-row ledger is NEVER
read (ROUTING_OUTCOMES_FILE is pinned to a temp file defensively, since
account_ledger's default resolves it).
"""
import datetime
import json
import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")

if REPO not in sys.path:
    sys.path.insert(0, REPO)
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import router_outcomes  # noqa: E402
import router_quota_accounting as acc  # noqa: E402

UTC = datetime.timezone.utc
T0 = 1780000000.0            # 2026-05-29T07:46:40Z — fixed clock for all tests


# ------------------------------------------------------------------ helpers --

def _iso(epoch):
    return datetime.datetime.fromtimestamp(epoch, UTC).isoformat(timespec="seconds")


def _row(provider="p1", model="m1", ts=T0, tin=0, tout=0, treason=0, cost=None,
         session="s", **extra):
    r = {"source_system": "test", "session_id": session, "provider": provider,
         "model": model, "tokens_in": tin, "tokens_out": tout,
         "tokens_reasoning": treason, "cost_usd": cost, "success": None,
         "ts": ts}
    r.update(extra)
    return r


def _ledger(tmp_path, rows):
    p = tmp_path / "outcomes.jsonl"
    with open(p, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return str(p)


def _limits(tmp_path, providers=None, default_window=None):
    doc = {}
    if default_window:
        doc["default_window"] = default_window
    if providers is not None:
        doc["providers"] = providers
    p = tmp_path / "quota_limits.json"
    p.write_text(json.dumps(doc))
    return str(p)


def _limits_usd(tmp_path, limit=10.0, window="rolling_5h", provider="p1",
                **extra):
    cfg = {"window": window, "limit_usd": limit}
    cfg.update(extra)
    return _limits(tmp_path, providers={provider: cfg})


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, tmp_path):
    """No test may touch the live ledger or the live limit config.

    The price-map reset is monkeypatched (save/restore): a bare assignment
    would poison router_outcomes' module cache for every later test in the
    process (measured: test_verified_outcomes' real-registry pricing died
    of exactly that)."""
    monkeypatch.setenv("ROUTING_OUTCOMES_FILE", str(tmp_path / "unused.jsonl"))
    monkeypatch.setenv("ROUTER_QUOTA_LIMITS_FILE",
                       str(tmp_path / "unused-limits.json"))
    monkeypatch.setattr(router_outcomes, "_PRICE_MAP", {})


def _run(now=T0):
    """account() over a fixed clock with a fresh limits fixture each time."""
    return acc.account(_LEDGER_ROWS, acc.load_limits(_LIMITS), now)


def _block(blocks, provider="p1"):
    hits = [b for b in blocks if b["provider"] == provider]
    assert hits, f"no block for {provider}: {[b['provider'] for b in blocks]}"
    return hits[0]


# ==== AC1 — NO second store ==================================================

def test_ac1_no_state_file_created(tmp_path, monkeypatch):
    """The view writes NOTHING: pre/post dir listings identical, no new file."""
    led = _ledger(tmp_path, [_row(tin=1000, tout=500, cost=0.5)])
    lim = _limits_usd(tmp_path)
    before = sorted(os.listdir(tmp_path))
    doc = acc.account_ledger(path=led, limits=acc.load_limits(lim), now_ts=T0)
    after = sorted(os.listdir(tmp_path))
    assert before == after, "accounting created a file — that is a second store"
    assert acc.load_limits(lim)["providers"]["p1"]["limit_usd"] == 10.0
    assert _block(doc)["spent_usd"] == 0.5


def test_ac1_derives_from_ledger_mutations(tmp_path):
    """Derivation proof: delete / append / mutate ledger rows -> view moves."""
    lim = _limits_usd(tmp_path)
    rows = [_row(session="a", tin=100, cost=1.0),
            _row(session="b", tin=200, cost=2.0)]
    led = _ledger(tmp_path, rows)

    blocks = acc.account_ledger(path=led, limits=acc.load_limits(lim), now_ts=T0)
    assert _block(blocks)["spent_usd"] == 3.0

    # delete a row -> spent drops
    with open(led, "w") as f:
        f.write(json.dumps(rows[0]) + "\n")
    blocks = acc.account_ledger(path=led, limits=acc.load_limits(lim),
                                now_ts=T0)
    assert _block(blocks)["spent_usd"] == 1.0

    # append a row -> spent rises
    with open(led, "a") as f:
        f.write(json.dumps(_row(session="c", tin=300, cost=4.0)) + "\n")
    blocks = acc.account_ledger(path=led, limits=acc.load_limits(lim), now_ts=T0)
    assert _block(blocks)["spent_usd"] == 5.0

    # mutate a row in place -> spent follows the ledger, not a cached counter
    mutated = [_row(session="a", tin=100, cost=99.0),
               _row(session="c", tin=300, cost=4.0)]
    _ledger(tmp_path, mutated)
    blocks = acc.account_ledger(path=led, limits=acc.load_limits(lim), now_ts=T0)
    assert _block(blocks)["spent_usd"] == 103.0

    # delete EVERY row -> the view is empty of spend and rows
    open(led, "w").close()
    blocks = acc.account_ledger(path=led, limits=acc.load_limits(lim), now_ts=T0)
    b = _block(blocks)
    assert b["spent_usd"] == 0.0 and b["n_rows"] == 0


def test_ac1_no_lock_file_no_sidecar(tmp_path):
    """Even the lock files router_quota.py needs are absent: read-only paths."""
    led = _ledger(tmp_path, [_row(cost=1.0)])
    lim = _limits_usd(tmp_path)
    acc.account_ledger(path=led, limits=acc.load_limits(lim), now_ts=T0)
    acc.account_ledger(path=led, limits=acc.load_limits(lim), now_ts=T0)
    assert sorted(os.listdir(tmp_path)) == sorted(
        [os.path.basename(led), os.path.basename(lim)])


# ==== AC2 — window boundaries ================================================

def test_ac2_rolling_edge_row_rolls_off(tmp_path):
    """A row exactly at the rolling start edge has rolled OFF; a row exactly
    at `now` still counts; one microsecond newer than the edge counts."""
    led = _ledger(tmp_path, [
        _row(session="out", ts=T0 - 5 * 3600, tin=1, cost=1.0),        # == start
        _row(session="in_now", ts=T0, tin=2, cost=2.0),                # == now
        _row(session="in_edge", ts=T0 - 5 * 3600 + 0.000001, tin=4,
             cost=4.0),                                                # just in
    ])
    blocks = acc.account_ledger(path=led, limits=_limits_usd(tmp_path),
                                now_ts=T0)
    b = _block(blocks)
    assert b["n_rows"] == 2
    assert b["spent_usd"] == 6.0
    assert b["n_rows_outside_window"] == 1
    assert b["resets_at"] == _iso(T0 - 5 * 3600 + 0.000001 + 5 * 3600)


def test_ac2_rolling_edge_rows_in_full_ledger(tmp_path):
    """Same boundary inside a ledger that also holds old and future rows."""
    led = _ledger(tmp_path, [
        _row(session="old", ts=T0 - 5 * 3600 - 1, cost=100.0),   # rolled off
        _row(session="edge", ts=T0 - 5 * 3600, cost=100.0),      # == edge: out
        _row(session="future", ts=T0 + 3600, cost=100.0),        # future: out
        _row(session="now", ts=T0, cost=2.0),                    # in
    ])
    blocks = acc.account_ledger(path=led, limits=_limits_usd(tmp_path),
                                now_ts=T0)
    b = _block(blocks)
    assert b["spent_usd"] == 2.0 and b["n_rows"] == 1
    assert b["n_rows_outside_window"] == 3


def test_ac2_weekly_window(tmp_path):
    """2026-05-29 is a Friday: the week started Mon 2026-05-25T00:00Z and
    resets Mon 2026-06-01T00:00Z. Monday's row is IN; the prior Sunday's is
    OUT; a row exactly at the reset instant is OUT."""
    t0 = datetime.datetime(2026, 5, 29, 12, 0, tzinfo=UTC).timestamp()
    week_start = datetime.datetime(2026, 5, 25, tzinfo=UTC).timestamp()
    next_week = datetime.datetime(2026, 6, 1, tzinfo=UTC).timestamp()
    led = _ledger(tmp_path, [
        _row(session="mon", ts=week_start, tin=1, cost=1.0),       # == start
        _row(session="sun", ts=week_start - 1, tin=2, cost=2.0),   # prior week
        _row(session="fri", ts=t0, tin=4, cost=4.0),
        _row(session="reset", ts=next_week, tin=8, cost=8.0),      # == reset
    ])
    blocks = acc.account_ledger(
        path=led,
        limits=_limits_usd(tmp_path, window="weekly"),
        now_ts=t0)
    b = _block(blocks)
    assert b["window"] == "weekly"
    assert b["n_rows"] == 2 and b["spent_usd"] == 5.0
    assert b["window_start"] == _iso(week_start)
    assert b["resets_at"] == _iso(next_week)


def test_ac2_monthly_window(tmp_path):
    """Calendar month: the 1st 00:00Z counts, the previous month's last second
    does not, and a row at the next month's 1st is OUT."""
    t0 = datetime.datetime(2026, 5, 15, 8, 0, tzinfo=UTC).timestamp()
    may1 = datetime.datetime(2026, 5, 1, tzinfo=UTC).timestamp()
    jun1 = datetime.datetime(2026, 6, 1, tzinfo=UTC).timestamp()
    led = _ledger(tmp_path, [
        _row(session="first", ts=may1, tin=1, cost=1.0),           # == start
        _row(session="apr30", ts=may1 - 1, tin=2, cost=2.0),       # April
        _row(session="mid", ts=t0, tin=4, cost=4.0),
        _row(session="jun", ts=jun1, tin=8, cost=8.0),             # == reset
    ])
    blocks = acc.account_ledger(
        path=led,
        limits=_limits_usd(tmp_path, window="monthly"),
        now_ts=t0)
    b = _block(blocks)
    assert b["window"] == "monthly"
    assert b["n_rows"] == 2 and b["spent_usd"] == 5.0
    assert b["window_start"] == _iso(may1)
    assert b["resets_at"] == _iso(jun1)


def test_ac2_monthly_december_wraps_year(tmp_path):
    """December's reset is NEXT YEAR's January (a classic off-by-one)."""
    t0 = datetime.datetime(2026, 12, 15, 8, 0, tzinfo=UTC).timestamp()
    jan1 = datetime.datetime(2027, 1, 1, tzinfo=UTC).timestamp()
    led = _ledger(tmp_path, [_row(ts=t0, cost=1.0)])
    blocks = acc.account_ledger(
        path=led,
        limits=_limits_usd(tmp_path, window="monthly"),
        now_ts=t0)
    b = _block(blocks)
    assert b["resets_at"] == _iso(jan1)


def test_ac2_out_of_order_rows_sums_are_order_independent(tmp_path):
    """Sums must not depend on file order: reverse + shuffle to the same view."""
    rows = [_row(session=f"s{i}", ts=T0 - i * 600, tin=100 + i, cost=1.0 + i)
            for i in range(10)]
    led_a = _ledger(tmp_path, rows)
    led_b = _ledger(tmp_path, list(reversed(rows)))
    led_c = _ledger(tmp_path, [rows[i] for i in (3, 7, 1, 9, 0, 4, 8, 2, 6, 5)])
    lim = _limits_usd(tmp_path)
    ref = None
    for led in (led_a, led_b, led_c):
        b = _block(acc.account_ledger(path=led, limits=acc.load_limits(lim),
                                      now_ts=T0))
        doc = (b["n_rows"], b["spent_usd"], b["tokens_in"], b["resets_at"],
               b["window_start"])
        if ref is None:
            ref = doc
        assert doc == ref


# ==== AC3 — unit conversion ==================================================

def test_ac3_conversion_reuses_price_table(tmp_path, monkeypatch):
    """A cost-less row is priced by plan_effective_cost — the router's own
    single conversion function (TR-070). Check the EXACT arithmetic of the
    public-split path: tin/1e6*pin + tout/1e6*pout, and a plan-ratio lane."""
    led = _ledger(tmp_path, [
        _row(provider="pub", model="mpub", session="a", tin=1_000_000,
             tout=2_000_000, cost=None),
        _row(provider="plan", model="mplan", session="b", tin=1_000_000,
             tout=1_000_000, cost=None),
    ])
    monkeypatch.setitem(router_outcomes._PRICE_MAP, ("pub", "mpub"),
                        (3.0, 1.5, None, None))       # public split only
    # plan lane: public 6.0, normalized 0.5 -> ratio 1/12
    monkeypatch.setitem(router_outcomes._PRICE_MAP, ("plan", "mplan"),
                        (3.0, 3.0, 0.5, 6.0))
    blocks = acc.account_ledger(path=led, limits=_limits_usd(tmp_path),
                                now_ts=T0)
    b = _block(blocks, "pub")
    assert b["spent_usd"] == pytest.approx(3.0 * 1 + 1.5 * 2)     # 6.0
    assert b["n_rows"] == 1
    plan = _block(blocks, "plan")
    assert plan["spent_usd"] == pytest.approx(6.0 / 12.0)         # plan ratio


def test_ac3_row_cost_wins_over_price_table(tmp_path, monkeypatch):
    """A measured cost_usd is ground truth — the price table must NOT
    re-price a row that carries its own cost."""
    monkeypatch.setitem(router_outcomes._PRICE_MAP, ("p1", "m1"),
                        (999.0, 999.0, None, None))
    led = _ledger(tmp_path, [_row(tin=10, tout=10, cost=0.25)])
    b = _block(acc.account_ledger(path=led, limits=_limits_usd(tmp_path),
                                  now_ts=T0))
    assert b["spent_usd"] == 0.25


def test_ac3_unknown_price_yields_null_not_zero(tmp_path):
    """No declared price: spent stays UNKNOWN (None) — never a fabricated 0 —
    while the row STILL counts in the token sums (AC3 literal)."""
    led = _ledger(tmp_path, [
        _row(session="free", tin=500, tout=250, cost=None),   # no price entry
        _row(session="priced", tin=100, tout=50, cost=0.5),
    ])
    b = _block(acc.account_ledger(path=led, limits=_limits_usd(tmp_path),
                                  now_ts=T0))
    assert b["spent_usd"] is None
    assert b["tokens_in"] == 600 and b["tokens_out"] == 300
    assert b["tokens_total"] == 900
    assert b["unpriced_rows"] == 1 and b["unpriced_reason"]
    assert "no declared price" in b["unpriced_reason"]
    # remaining must NOT be limit-0; it is NULL too, with the reason
    assert b["remaining_usd"] is None
    assert "spent unknown" in b["remaining_reason"]


def test_ac3_zero_usage_row_is_neutral_not_unpriced(tmp_path):
    """A row with no tokens and no cost contributes nothing and is not
    reported as an unpriced debt."""
    led = _ledger(tmp_path, [_row(session="e", tin=0, tout=0, cost=None)])
    b = _block(acc.account_ledger(path=led, limits=_limits_usd(tmp_path),
                                  now_ts=T0))
    assert b["spent_usd"] == 0.0 and b["unpriced_rows"] == 0
    assert b["remaining_usd"] == 10.0


def test_ac3_unpriced_mix_reported(tmp_path):
    """Priced + unpriced + no-usage rows: an unknown-priced term makes the
    SUM null (an understated budget would fake headroom for L3 pacing); the
    reason names it; the no-usage row is a genuine zero, not a price debt."""
    led = _ledger(tmp_path, [
        _row(session="p", tin=10, cost=1.0),
        _row(session="u", tin=10, cost=None),      # unknown price
        _row(session="n", tin=0, tout=0, cost=None),  # no usage
    ])
    b = _block(acc.account_ledger(path=led, limits=_limits_usd(tmp_path),
                                  now_ts=T0))
    assert b["spent_usd"] is None
    assert b["unpriced_rows"] == 1              # only the unknown-PRICE row
    assert "no declared price" in b["unpriced_reason"]
    assert b["remaining_usd"] is None
    assert "spent unknown" in b["remaining_reason"]


# ==== AC4 — remaining NULL with reason =======================================

def test_ac4_no_limit_null_with_reason(tmp_path):
    """AC4 literal: remaining is NULL whenever limit is NULL, and the reason
    names the provider."""
    led = _ledger(tmp_path, [_row(cost=1.0)])
    lim = _limits(tmp_path, providers={})            # nothing configured
    b = _block(acc.account_ledger(path=led, limits=acc.load_limits(lim),
                                  now_ts=T0))
    assert b["limit_usd"] is None and b["remaining_usd"] is None
    assert b["remaining_reason"] == "no limit configured for provider p1"
    # spend is still visible
    assert b["spent_usd"] == 1.0


def test_ac4_remaining_follows_limit_across_providers(tmp_path):
    """A configured provider gets remaining; an unconfigured sibling stays
    NULL-with-reason in the SAME view."""
    led = _ledger(tmp_path, [
        _row(provider="p1", session="a", cost=4.0),
        _row(provider="p2", session="b", cost=6.0),
    ])
    lim = _limits(tmp_path, providers={"p1": {"limit_usd": 10.0}})
    blocks = acc.account_ledger(path=led, limits=acc.load_limits(lim),
                                now_ts=T0)
    p1 = _block(blocks, "p1")
    p2 = _block(blocks, "p2")
    assert p1["remaining_usd"] == 6.0 and p1["remaining_reason"] is None
    assert p2["remaining_usd"] is None
    assert p2["remaining_reason"] == "no limit configured for provider p2"


def test_ac4_headroom_only_when_remaining_and_reset_known(tmp_path):
    """headroom = remaining / seconds_to_reset (spec §4); None while either
    side is unknown; exact for a rolling window whose reset = oldest + 5h."""
    t0 = T0
    led = _ledger(tmp_path, [_row(ts=t0 - 600, cost=4.0)])
    b = _block(acc.account_ledger(path=led, limits=_limits_usd(tmp_path),
                                  now_ts=t0))
    resets = (t0 - 600) + 5 * 3600
    assert b["headroom_usd_per_s"] == pytest.approx(6.0 / (resets - t0))


def test_ac4_bad_limit_config_is_named_not_guessed(tmp_path):
    """A non-numeric limit is not silently 'no limit': the reason says the
    config itself is unusable (null-with-reason law)."""
    led = _ledger(tmp_path, [_row(cost=1.0)])
    lim = _limits(tmp_path, providers={"p1": {"limit_usd": "ten"}})
    b = _block(acc.account_ledger(path=led, limits=acc.load_limits(lim),
                                  now_ts=T0))
    assert b["remaining_usd"] is None
    assert "unusable limit config" in b["remaining_reason"]


def test_ac4_bad_window_config_is_named_not_guessed(tmp_path):
    led = _ledger(tmp_path, [_row(cost=1.0)])
    lim = _limits(tmp_path, providers={"p1": {"window": "fortnightly",
                                              "limit_usd": 10.0}})
    b = _block(acc.account_ledger(path=led, limits=acc.load_limits(lim),
                                  now_ts=T0))
    assert b["remaining_usd"] is None
    assert "unusable limit config" in b["remaining_reason"]
    assert "fortnightly" in b["remaining_reason"]


def test_ac4_rolling_without_window_hours_is_named(tmp_path):
    led = _ledger(tmp_path, [_row(cost=1.0)])
    lim = _limits(tmp_path, providers={"p1": {"window": "rolling",
                                              "limit_usd": 10.0}})
    b = _block(acc.account_ledger(path=led, limits=acc.load_limits(lim),
                                  now_ts=T0))
    assert b["remaining_usd"] is None
    assert "window_hours" in b["remaining_reason"]


# ==== AC5 — per-provider keying ==============================================

def test_ac5_lane_count_never_multiplies_budget(tmp_path):
    """498 lanes under one $10 window is ONE budget: every model of the
    provider sums into the SAME block; total spend is the SUM, not sum*lanes;
    model/lane count appears nowhere in the output."""
    rows = [_row(provider="clinepass", model=f"model-{i}",
                 session=f"lane-{i}", tin=10, tout=5, cost=0.01)
            for i in range(498)]
    led = _ledger(tmp_path, rows)
    blocks = acc.account_ledger(
        path=led,
        limits=_limits_usd(tmp_path, limit=10.0, provider="clinepass"),
        now_ts=T0)
    assert len(blocks) == 1                      # ONE provider block
    b = blocks[0]
    assert b["provider"] == "clinepass"
    assert b["spent_usd"] == pytest.approx(0.01 * 498)     # 4.98 — the SUM
    assert b["n_rows"] == 498
    assert b["remaining_usd"] == pytest.approx(10.0 - 4.98)
    blob = json.dumps(b)
    assert "model" not in blob and "lane" not in blob   # not lane-keyed


def test_ac5_rows_without_provider_are_dropped_not_attributed(tmp_path):
    """A row with no provider cannot sit in any budget — dropped from the
    per-provider view rather than attributed to a fake key."""
    led = _ledger(tmp_path, [_row(provider=None, session="x", cost=1.0),
                             _row(provider="p1", session="y", cost=2.0)])
    blocks = acc.account_ledger(path=led, limits=_limits_usd(tmp_path),
                                now_ts=T0)
    assert [b["provider"] for b in blocks] == ["p1"]


# ==== AC6 — limit configuration source =======================================

def test_ac6_missing_config_means_no_limits(tmp_path):
    """Missing file is NORMAL (nothing configured), not an error."""
    lim = os.path.join(str(tmp_path), "absent.json")
    doc = acc.load_limits(lim)
    assert doc == {"default_window": "rolling_5h", "providers": {}}
    led = _ledger(tmp_path, [_row(cost=1.0)])
    b = _block(acc.account_ledger(path=led, limits=acc.load_limits(lim),
                                  now_ts=T0))
    assert b["limit_usd"] is None
    assert "no limit configured for provider p1" in b["remaining_reason"]


def test_ac6_corrupt_config_warns_and_fails_open(tmp_path, capsys):
    """A corrupt config warns on stderr and behaves like 'no limits' — a
    diagnostic must never break, but it must not fail SILENTLY either."""
    p = tmp_path / "broken.json"
    p.write_text("{not json")
    doc = acc.load_limits(str(p))
    assert doc["providers"] == {}
    err = capsys.readouterr().err
    assert "unreadable limit config" in err and str(p) in err
    led = _ledger(tmp_path, [_row(cost=1.0)])
    b = _block(acc.account_ledger(path=led, limits=doc, now_ts=T0))
    assert b["limit_usd"] is None


def test_ac6_config_read_at_query_time(tmp_path):
    """Edit the config between queries -> the view moves: read at query time,
    not cached into a store (AC6 literal)."""
    led = _ledger(tmp_path, [_row(cost=1.0)])
    lim = _limits_usd(tmp_path, limit=10.0)
    b = _block(acc.account_ledger(path=led, limits=acc.load_limits(lim),
                                  now_ts=T0))
    assert b["limit_usd"] == 10.0
    with open(lim, "w") as f:
        json.dump({"providers": {"p1": {"window": "rolling_5h",
                                        "limit_usd": 50.0}}}, f)
    b = _block(acc.account_ledger(path=led, limits=acc.load_limits(lim),
                                  now_ts=T0))
    assert b["limit_usd"] == 50.0 and b["remaining_usd"] == 49.0


def test_ac6_env_override_path(tmp_path, monkeypatch):
    env_file = tmp_path / "env-limits.json"
    env_file.write_text(json.dumps(
        {"providers": {"p1": {"window": "weekly", "limit_usd": 7.0}}}))
    monkeypatch.setenv("ROUTER_QUOTA_LIMITS_FILE", str(env_file))
    assert acc.limits_path() == str(env_file)
    led = _ledger(tmp_path, [_row(cost=3.0)])
    b = _block(acc.account_ledger(now_ts=T0))
    assert b["limit_usd"] == 7.0 and b["window"] == "weekly"


def test_ac6_default_window_applies_to_unconfigured_providers(tmp_path):
    """default_window covers providers absent from the config — their window
    is derived, their limit still NULL with reason."""
    lim = _limits(tmp_path, default_window="monthly", providers={})
    led = _ledger(tmp_path, [_row(cost=1.0)])
    b = _block(acc.account_ledger(path=led, limits=acc.load_limits(lim),
                                  now_ts=T0))
    assert b["window"] == "monthly"


# ==== AC7 — CLI ==============================================================

_SCRIPT = os.path.join(SCRIPTS, "router_quota_accounting.py")


def _cli(*args, env_extra=None):
    env = dict(os.environ)
    env.setdefault("TASK_ROUTER_HOME", REPO)      # canonical install: no state-dir warn
    if env_extra:
        env.update(env_extra)
    return subprocess.run([sys.executable, _SCRIPT, *args],
                          capture_output=True, text=True, env=env,
                          timeout=120)


def test_ac7_cli_json_end_to_end(tmp_path):
    led = _ledger(tmp_path, [
        _row(provider="p1", session="a", tin=1_000_000, tout=500_000,
             cost=None),
        _row(provider="p2", session="b", cost=2.0),
    ])
    lim = _limits(tmp_path, providers={
        "p1": {"window": "rolling_5h", "limit_usd": 10.0},
    })
    p = _cli("accounting", "--ledger", led, "--limits", lim,
             "--now", str(T0), "--json",
             env_extra={"ROUTER_QUOTA_LIMITS_FILE": os.path.join(
                 str(tmp_path), "unused.json")})
    assert p.returncode == 0, p.stderr
    doc = json.loads(p.stdout)
    assert doc["as_of"] == _iso(T0)
    assert doc["ledger"] == led
    assert doc["limits_present"] is True
    provs = {b["provider"]: b for b in doc["providers"]}
    assert provs["p1"]["remaining_usd"] is None        # unpriced -> unknown
    assert "no declared price" in provs["p1"]["unpriced_reason"]
    assert provs["p2"]["spent_usd"] == 2.0
    assert provs["p2"]["remaining_usd"] is None
    assert provs["p2"]["remaining_reason"] == \
        "no limit configured for provider p2"
    assert set(provs["p1"]) >= {"provider", "window", "spent_usd", "tokens_in",
                                "tokens_out", "limit_usd", "remaining_usd",
                                "remaining_reason", "resets_at",
                                "headroom_usd_per_s"}


def test_ac7_cli_human_output(tmp_path):
    led = _ledger(tmp_path, [_row(cost=1.0)])
    lim = _limits_usd(tmp_path)
    p = _cli("accounting", "--ledger", led, "--limits", lim, "--now", str(T0),
             env_extra={"ROUTER_QUOTA_LIMITS_FILE": os.path.join(
                 str(tmp_path), "unused.json")})
    assert p.returncode == 0
    assert "p1" in p.stdout and "resets" in p.stdout.lower()


def test_ac7_cli_via_router_wrapper(tmp_path):
    """Reachable as `router quota accounting` (task_router.cli dispatch) —
    the AC7 integration the fleet actually calls."""
    led = _ledger(tmp_path, [_row(provider="p1", cost=1.0)])
    lim = _limits_usd(tmp_path)
    router = os.path.join(REPO, "task_router", "cli.py")
    env = dict(os.environ)
    # TASK_ROUTER_HOME only selects the DATA home (bootstrap sample state is
    # written there); pointing it at the repo would drop quota-state.json into
    # the checkout on every test run. Script resolution is __file__-derived.
    env["TASK_ROUTER_HOME"] = str(tmp_path)
    p = subprocess.run([sys.executable, router, "quota", "accounting",
                        "--ledger", led, "--limits", lim, "--now", str(T0),
                        "--json"],
                       capture_output=True, text=True, env=env, timeout=120)
    assert p.returncode == 0, p.stderr
    doc = json.loads(p.stdout)
    assert [b["provider"] for b in doc["providers"]] == ["p1"]
    assert doc["providers"][0]["spent_usd"] == 1.0


def test_ac7_cli_provider_filter_and_exit_codes(tmp_path):
    led = _ledger(tmp_path, [_row(provider="p1", cost=1.0),
                             _row(provider="p2", cost=9.0)])
    lim = _limits(tmp_path, providers={"p1": {"limit_usd": 10.0}})
    p = _cli("accounting", "--ledger", led, "--limits", lim, "--now", str(T0),
             "--provider", "p2", "--json",
             env_extra={"ROUTER_QUOTA_LIMITS_FILE": os.path.join(
                 str(tmp_path), "unused.json")})
    assert p.returncode == 0
    doc = json.loads(p.stdout)
    assert [b["provider"] for b in doc["providers"]] == ["p2"]
    assert doc["providers"][0]["limit_usd"] is None
    assert doc["providers"][0]["remaining_reason"] == \
        "no limit configured for provider p2"


def test_ac7_cli_exit_2_usage_error_no_write(tmp_path):
    led = _ledger(tmp_path, [_row(cost=1.0)])
    lim = _limits_usd(tmp_path)
    before = sorted(os.listdir(tmp_path))
    p = _cli("accounting", "--ledger", led, "--limits", lim,
             "--now", "not-a-date",
             env_extra={"ROUTER_QUOTA_LIMITS_FILE": os.path.join(
                 str(tmp_path), "unused.json")})
    assert p.returncode == 2
    assert "not ISO-8601" in p.stderr
    assert sorted(os.listdir(tmp_path)) == before       # a bad --now wrote nothing


def test_ac7_cli_exit_1_unexpected_failure(monkeypatch, capsys):
    """An unexpected error prints ONE clean stderr line and exits 1 — never a
    traceback (router_quota.py style). Proven in-process: main() catches the
    exception itself (a subprocess would also see argparse's SystemExit(2)
    for the same missing file? no — load_outcome_rows returns [] for absent
    paths, which is exactly the fail-open doctrine; the exception path needs
    an injected failure)."""
    led = os.path.join(str(_hermetic_tmp()), "ledger.jsonl")
    monkeypatch.setattr(acc.router_outcomes, "load_outcome_rows",
                        lambda p: (_ for _ in ()).throw(RuntimeError("disk gone")))
    code = acc.main(["accounting", "--ledger", led, "--json"])
    assert code == 1
    err = capsys.readouterr().err
    assert err.startswith("router_quota_accounting error:")
    assert "disk gone" in err and "Traceback" not in err


def _hermetic_tmp():
    import tempfile
    d = tempfile.mkdtemp(prefix="tr209-exit1-")
    return d


def test_ac7_cli_now_accepts_iso(tmp_path):
    led = _ledger(tmp_path, [_row(ts=_iso(T0), cost=1.0)])
    lim = _limits_usd(tmp_path)
    p = _cli("accounting", "--ledger", led, "--limits", lim,
             "--now", _iso(T0), "--json",
             env_extra={"ROUTER_QUOTA_LIMITS_FILE": os.path.join(
                 str(tmp_path), "unused.json")})
    assert p.returncode == 0, p.stderr
    doc = json.loads(p.stdout)
    assert doc["providers"][0]["spent_usd"] == 1.0
    assert doc["as_of"] == _iso(T0)


def test_ac7_cli_ignores_live_limit_config_env(tmp_path):
    """--limits wins over $ROUTER_QUOTA_LIMITS_FILE (explicit args win — the
    same precedence rule as router_quota.py --state-file over --state-dir)."""
    led = _ledger(tmp_path, [_row(cost=1.0)])
    lim = _limits_usd(tmp_path, limit=10.0)
    other = tmp_path / "other.json"
    other.write_text(json.dumps(
        {"providers": {"p1": {"window": "rolling_5h", "limit_usd": 99.0}}}))
    p = _cli("accounting", "--ledger", led, "--limits", lim, "--now", str(T0),
             "--json", env_extra={"ROUTER_QUOTA_LIMITS_FILE": str(other)})
    doc = json.loads(p.stdout)
    assert doc["providers"][0]["limit_usd"] == 10.0


# ==== time parsing / misc ====================================================

def test_parse_ts_shapes():
    assert acc.parse_ts(T0) == T0
    assert acc.parse_ts(_iso(T0)) == T0
    assert acc.parse_ts(_iso(T0).replace("+00:00", "Z")) == T0
    assert acc.parse_ts("  ") is None
    assert acc.parse_ts(None) is None
    assert acc.parse_ts(True) is None
    assert acc.parse_ts("garbage") is None
    assert acc.parse_ts(float("nan")) is None
    assert acc.parse_ts(float("inf")) is None
    assert acc.parse_ts("12345.5") == 12345.5
    # naive ISO is read as UTC (contract); derive the literal instead of
    # trusting a hand-computed date comment
    naive = datetime.datetime.fromtimestamp(T0, UTC) \
        .replace(tzinfo=None).isoformat()
    assert acc.parse_ts(naive) == T0


def test_window_bounds_rolling_hours():
    s, r, sec = acc.window_bounds("rolling", T0, window_hours=24)
    assert (s, r, sec) == (T0 - 86400.0, None, 86400.0)
    assert acc.window_bounds("rolling", T0, window_hours=0) is None
    assert acc.window_bounds("rolling", T0, window_hours=-2) is None
    assert acc.window_bounds("rolling", T0, window_hours="x") is None
    assert acc.window_bounds("nope", T0) is None
