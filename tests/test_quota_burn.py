"""TR-211 regression battery — quota L3: the burn-surplus finder.

The quota plane could SEE a window (L2 accounting view, TR-209) and DECIDE
per call (L3 pacing gate, TR-210), but nothing answered the owner's actual
question: "which provider quota is about to EXPIRE UNUSED, so I can pick a
project to burn it on?" scripts/router_quota_burn.py is that projection,
per window:

    expected_unused = remaining - (current_rate * time_to_reset)

    current_rate  = spent_in_window / seconds_elapsed   (smooth-spend rate;
                   ~0 on a window that just started — reported, not invented)
    time_to_reset = resets_at - now

Positive expected_unused => surface provider / window / surplus in its unit
/ reset time / marginal cash. Negative => no burn recommendation. Unknown
inputs => NULL + reason, never 0. It NEVER auto-spends: a report and a
recommendation artifact only, and it WRITES NOTHING (docs/quota-layers-spec
.md section 5, consumer 3).

Covered (the ACs):
  AC1  expected_unused is exactly remaining - rate * time_to_reset per
       window (rolling AND calendar), with elapsed derived from the SAME
       window arithmetic the L2 view uses; unknown projections carry the
       reason (rolling window with no rows, unpriceable spend) — NULL, not 0.
  AC2  every entry names provider, window, surplus in its unit, reset time
       and the marginal cash cost; the CLI JSON is a report document with a
       summary census.
  AC3  never auto-spends: no network, no spawn, no state file — the report
       goes to stdout only; tolerant CLI exit codes 0/2/1.
  AC4  synthetic windows: expiring-with-surplus burns, plenty-left-with-time
       does not; negative surplus => no-burn (surplus clamped to 0, the
       negative expected_unused kept); no-limit => remaining NULL with the
       reason, never 0.
  AC5  a subscription window is never reported as free money without naming
       it: `cost_basis` / `subscription_covered` on EVERY entry, sourced
       from the limit config plan_kind or an explicit --plan-kind override;
       undeclared => marginal cash NULL with the reason, never guessed $0.
  AC6  reachable as `router quota burn` (task_router.cli -> router_quota.py
       delegate), the module is in the runtime sync list, and --provider
       filters the report.

Hermetic: every test builds its own blocks/ledgers/limits under tmp_path and
pins ROUTING_OUTCOMES_FILE / ROUTER_QUOTA_LIMITS_FILE there. The live ledger
is NEVER read.
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
import router_quota_burn as burn  # noqa: E402

UTC = datetime.timezone.utc
T0 = 1780000000.0            # 2026-05-29T07:46:40Z — fixed clock for all tests
SPAN_5H = 5 * 3600.0


# ------------------------------------------------------------------ helpers --

def _iso(epoch):
    return datetime.datetime.fromtimestamp(epoch, UTC).isoformat(timespec="seconds")


def _block(limit=10.0, spent=0.0, resets_in=1000.0, provider="p1",
           window="rolling_5h", limit_usd=True):
    """A synthetic L2 block with the REAL L2 shape (see account_provider)."""
    blk = {"provider": provider, "window": window, "as_of": _iso(T0),
           "n_rows": 3, "spent_usd": spent,
           "limit_usd": limit if limit_usd else None,
           "remaining_usd": (None if limit is None else max(0.0, limit - spent)),
           "remaining_reason": None, "resets_at": None,
           "headroom_usd_per_s": 0.0}
    if limit is None:
        blk["remaining_usd"] = None
        blk["remaining_reason"] = f"no limit configured for provider {provider}"
    if resets_in is not None:
        blk["resets_at"] = _iso(T0 + resets_in)
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


def _limits_usd(tmp_path, limit=10.0, window="rolling_5h", provider="p1",
                **extra):
    entry = {"window": window, "limit_usd": limit}
    entry.update(extra)
    return _limits(tmp_path, {provider: entry})


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, tmp_path):
    """No test may touch the live ledger, limits or price map."""
    monkeypatch.setenv("ROUTING_OUTCOMES_FILE", str(tmp_path / "unused.jsonl"))
    monkeypatch.setenv("ROUTER_QUOTA_LIMITS_FILE",
                       str(tmp_path / "unused-limits.json"))
    monkeypatch.setattr(router_outcomes, "_PRICE_MAP", {})


def _ledger_block(tmp_path, rows, limit=10.0, window="rolling_5h",
                  provider="p1"):
    """The real L2 block for one provider from a synthetic ledger."""
    led = _ledger(tmp_path, rows)
    lim = _limits_usd(tmp_path, limit=limit, window=window, provider=provider)
    blocks = acc.account_ledger(path=led, limits=acc.load_limits(lim),
                                now_ts=T0)
    return [b for b in blocks if b["provider"] == provider][0]


# ==== AC1 — the projection ===================================================

def test_ac1_rolling_math_is_exact(tmp_path):
    """expected_unused = remaining - (spent/elapsed) * time_to_reset, with
    elapsed = window_span - time_to_reset (rolling: the window opens on the
    first in-window spend)."""
    blk = _ledger_block(tmp_path, [_row(ts=T0 - 9000, cost=4.0)], limit=10.0)
    # L2 view: spent 4, remaining 6, resets_at = oldest + 5h = T0 + 9000.
    assert blk["remaining_usd"] == 6.0
    e = burn.project_window(blk, T0)
    assert e["verdict"] == "burn"
    assert e["time_to_reset_s"] == pytest.approx(9000.0)
    assert e["seconds_elapsed"] == pytest.approx(9000.0)
    assert e["current_rate_usd_per_s"] == pytest.approx(4.0 / 9000.0, abs=1e-12)
    assert e["projected_spend_usd"] == pytest.approx(4.0, abs=1e-6)
    assert e["expected_unused_usd"] == pytest.approx(2.0, abs=1e-6)
    assert e["surplus_usd"] == pytest.approx(2.0, abs=1e-6)
    assert e["resets_at"] == _iso(T0 + 9000)
    assert e["unit"] == "usd"


def test_ac1_calendar_monthly_math(tmp_path):
    """Calendar windows: elapsed = now - window_start, time_to_reset from the
    next anchor — derived here independently from the calendar, not from the
    module's window arithmetic."""
    now_dt = datetime.datetime.fromtimestamp(T0, UTC)
    start = now_dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if now_dt.month == 12:
        resets = start.replace(year=now_dt.year + 1, month=1)
    else:
        resets = start.replace(month=now_dt.month + 1)
    elapsed = (now_dt - start).total_seconds()
    t2r = (resets - now_dt).total_seconds()
    blk = _ledger_block(tmp_path, [_row(ts=T0 - 600, cost=25.0)],
                        limit=100.0, window="monthly")
    e = burn.project_window(blk, T0)
    expected = (100.0 - 25.0) - (25.0 / elapsed) * t2r
    assert e["seconds_elapsed"] == pytest.approx(elapsed)
    assert e["time_to_reset_s"] == pytest.approx(t2r)
    assert e["expected_unused_usd"] == pytest.approx(expected, abs=1e-6)
    assert e["verdict"] == "burn"


def test_ac1_missing_resets_at_falls_back_to_the_calendar(tmp_path):
    """A synthetic block without resets_at still projects on a calendar
    window (the anchor is derivable); an entry field stays None-free."""
    blk = _block(limit=100.0, spent=25.0, resets_in=None, window="monthly")
    e = burn.project_window(blk, T0)
    assert e["resets_at"] is not None
    assert e["verdict"] in ("burn", "no-burn")


def test_ac1_window_just_started_reports_the_near_zero_rate(tmp_path):
    """A window that just started has rate ~ 0 — reported, not invented
    (spec section 5.3: 'rate may be ~0 — that is fine, report it')."""
    blk = _ledger_block(tmp_path, [_row(ts=T0 - 100, cost=0.001)],
                        limit=100.0)
    e = burn.project_window(blk, T0)
    assert e["seconds_elapsed"] == pytest.approx(100.0)
    assert e["current_rate_usd_per_s"] == pytest.approx(0.001 / 100.0)
    assert e["verdict"] == "burn"
    assert e["reason"]


def test_ac1_rolling_window_with_no_rows_is_unknown_never_a_rate(tmp_path):
    """No in-window rows => no reset instant => the projection is UNKNOWN
    with that reason — never a fabricated rate or a $0 surplus."""
    led = _ledger(tmp_path, [])
    lim = _limits_usd(tmp_path, limit=10.0)
    blocks = acc.account_ledger(path=led, limits=acc.load_limits(lim),
                                now_ts=T0)
    e = burn.project_window(blocks[0], T0)
    assert e["verdict"] == "unknown"
    assert e["burnable"] is False
    assert e["remaining_usd"] == 10.0          # the L2 measured zero stays
    assert "resets_at unknown" in e["reason"]
    assert e["expected_unused_usd"] is None and e["surplus_usd"] is None


def test_ac1_unpriceable_spend_is_unknown_not_headroom(tmp_path):
    """L2 could not price the spend => remaining NULL with the reason =>
    the projection is unknown; the L2 reason travels verbatim."""
    blk = _ledger_block(
        tmp_path, [_row(cost=None, tokens_in=1000, tokens_out=500)],
        limit=10.0)
    assert blk["spent_usd"] is None
    e = burn.project_window(blk, T0)
    assert e["verdict"] == "unknown"
    assert "spent unknown" in e["reason"]
    assert e["surplus_usd"] is None


# ==== AC2 — the entry names the things =======================================

def test_ac2_entry_names_provider_window_surplus_reset_and_cash(tmp_path):
    blk = _ledger_block(tmp_path, [_row(ts=T0 - 9000, cost=4.0)], limit=10.0)
    e = burn.project_window(blk, T0)
    for key in ("provider", "window", "surplus_usd", "unit", "resets_at",
                "marginal_cash_usd", "cost_basis", "subscription_covered"):
        assert key in e, key
    assert e["provider"] == "p1" and e["window"] == "rolling_5h"
    assert e["resets_at"] == _iso(T0 + 9000)


def test_ac2_cli_json_is_a_report_document(tmp_path):
    led = _ledger(tmp_path, [_row(ts=T0 - 9000, cost=4.0)])
    lim = _limits_usd(tmp_path, limit=10.0)
    p = _cli("burn", "--ledger", led, "--limits", lim, "--now", str(T0),
             "--json")
    assert p.returncode == 0, p.stderr
    doc = json.loads(p.stdout)
    for key in ("as_of", "ledger", "limits_file", "limits_present",
                "summary", "windows"):
        assert key in doc, key
    assert doc["as_of"] == _iso(T0)
    assert doc["summary"] == {"burn": 1, "no_burn": 0, "unknown": 0,
                              "total_surplus_usd": pytest.approx(2.0, abs=1e-6)}
    assert doc["windows"][0]["verdict"] == "burn"


# ==== AC3 — never auto-spends, writes nothing ================================

_SCRIPT = os.path.join(SCRIPTS, "router_quota_burn.py")


def _cli(*args, env_extra=None):
    env = dict(os.environ)
    env.setdefault("TASK_ROUTER_HOME", REPO)
    if env_extra:
        env.update(env_extra)
    return subprocess.run([sys.executable, _SCRIPT, *args],
                          capture_output=True, text=True, env=env, timeout=120)


def test_ac3_the_module_never_spends_or_touches_the_world(tmp_path):
    """No network, no spawn, no sleep, no open() of its own, no state file:
    a real report run leaves the filesystem exactly as it found it and the
    source carries none of the machinery that could do otherwise."""
    src = open(_SCRIPT).read()
    for forbidden in ("import socket", "import urllib", "import requests",
                      "import http", "import subprocess", "Popen",
                      "os.system", "os.mkdir", "os.makedirs", "shutil",
                      "sleep(", "open("):
        assert forbidden not in src, forbidden
    led = _ledger(tmp_path, [_row(ts=T0 - 9000, cost=4.0)])
    lim = _limits_usd(tmp_path, limit=10.0)
    before = sorted(os.listdir(tmp_path))
    p = _cli("burn", "--ledger", led, "--limits", lim, "--now", str(T0),
             "--json")
    assert p.returncode == 0, p.stderr
    assert sorted(os.listdir(tmp_path)) == before
    json.loads(p.stdout)          # the report is stdout, not a file


def test_ac3_find_surplus_is_pure_over_blocks():
    """The finder reads its arguments and returns entries — no counters, no
    global state, identical output on a repeat call."""
    blocks = [_block(provider="pa", limit=10.0, spent=4.0, resets_in=9000),
              _block(provider="pb", limit=10.0, spent=10.0, resets_in=1000)]
    a = burn.find_surplus(blocks, T0)
    b = burn.find_surplus(blocks, T0)
    assert a == b
    assert [e["verdict"] for e in a] == ["burn", "no-burn"]


def test_ac3_cli_human_output(tmp_path):
    led = _ledger(tmp_path, [_row(ts=T0 - 9000, cost=4.0)])
    lim = _limits_usd(tmp_path, limit=10.0)
    p = _cli("burn", "--ledger", led, "--limits", lim, "--now", str(T0))
    assert p.returncode == 0, p.stderr
    assert "L3 quota burn-surplus" in p.stdout
    assert "burn" in p.stdout and "cost basis" in p.stdout


def test_ac3_exit_2_usage_errors_never_masked(tmp_path):
    led = _ledger(tmp_path, [_row(cost=1.0)])
    lim = _limits_usd(tmp_path)
    # burn is the one verb
    p = _cli("--json")
    assert p.returncode == 2
    # bad clock
    p = _cli("burn", "--ledger", led, "--limits", lim, "--now", "not-a-date")
    assert p.returncode == 2 and "not ISO-8601" in p.stderr
    # malformed --plan-kind
    p = _cli("burn", "--ledger", led, "--limits", lim, "--plan-kind", "p1")
    assert p.returncode == 2 and "not PROVIDER=KIND" in p.stderr
    # empty provider
    p = _cli("burn", "--ledger", led, "--limits", lim, "--provider", "  ")
    assert p.returncode == 2 and "provider is empty" in p.stderr


def test_ac3_exit_1_unexpected_failure_is_clean(tmp_path, monkeypatch, capsys):
    """In-process main(): an unexpected failure inside the view -> exit 1
    with a clean stderr message, never a traceback. (A subprocess would not
    see the injected fault — the fault is injected here, the contract checked
    is main()'s.)"""
    led = _ledger(tmp_path, [_row(cost=1.0)])
    lim = _limits_usd(tmp_path)

    def _boom(**kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(burn, "find_surplus_ledger", _boom)
    rc = burn.main(["burn", "--ledger", led, "--limits", lim,
                    "--now", str(T0), "--json"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "router_quota_burn error" in err
    assert "Traceback" not in err


def test_ac3_empty_ledger_and_empty_config_is_an_honest_empty_report(tmp_path):
    lim = _limits(tmp_path)
    p = _cli("burn", "--ledger", str(tmp_path / "absent.jsonl"),
             "--limits", lim, "--now", str(T0), "--json")
    assert p.returncode == 0, p.stderr
    doc = json.loads(p.stdout)
    assert doc["windows"] == []
    assert doc["summary"]["burn"] == 0


# ==== AC4 — expiring vs plenty, negatives, no-limit ==========================

def test_ac4_expiring_window_surfaces_a_burn(tmp_path):
    """Most of the window gone, most of the budget left => it WILL expire
    unused => burn."""
    blk = _ledger_block(tmp_path, [_row(ts=T0 - 9000, cost=4.0)], limit=10.0)
    e = burn.project_window(blk, T0)
    assert e["verdict"] == "burn" and e["burnable"] is True
    assert e["estimated"] is True and e["basis"] == "derived-from-ledger"


def test_ac4_plenty_left_with_time_is_no_burn(tmp_path):
    """Early window, modest spend: the current rate projects the window to
    spend itself => no burn recommendation, negative expected_unused KEPT
    (never clamped into a fake zero), surplus clamped at 0."""
    blk = _ledger_block(tmp_path, [_row(ts=T0 - 1000, cost=2.0)], limit=10.0)
    e = burn.project_window(blk, T0)
    assert e["verdict"] == "no-burn"
    assert e["burnable"] is False
    assert e["expected_unused_usd"] == pytest.approx(
        8.0 - (2.0 / 1000.0) * 17000.0, abs=1e-6)
    assert e["expected_unused_usd"] < 0
    assert e["surplus_usd"] == 0.0
    assert "no burn" in e["reason"]


def test_ac4_no_limit_window_is_null_with_reason_never_zero(tmp_path):
    led = _ledger(tmp_path, [_row(ts=T0 - 9000, cost=4.0)])
    lim = _limits(tmp_path, {})          # provider absent => no limit at all
    blocks = acc.account_ledger(path=led, limits=acc.load_limits(lim),
                                now_ts=T0)
    e = burn.project_window(blocks[0], T0)
    assert e["verdict"] == "unknown"
    assert e["remaining_usd"] is None
    assert "no limit configured" in e["reason"]
    assert e["surplus_usd"] is None and e["expected_unused_usd"] is None


def test_ac4_find_surplus_sorts_burn_first_by_surplus():
    """Surplus arithmetic under the rolling elapsed rule
    (elapsed = span - time_to_reset): spent 1 with 9000s left projects 1.0
    spent => ~8 unused (big burn); spent 4 => ~2 (small burn); spent 9 with
    2000s left projects 1.125 against 1 remaining => negative => no-burn;
    limit None => unknown. Burn class sorts by surplus descending."""
    entries = burn.find_surplus(
        [_block(provider="z-no-burn", limit=10.0, spent=9.0, resets_in=2000),
         _block(provider="z-unknown", limit=None, spent=1.0, resets_in=1000),
         _block(provider="a-burn-big", limit=10.0, spent=1.0, resets_in=9000),
         _block(provider="m-burn-small", limit=10.0, spent=4.0,
                resets_in=9000)],
        T0, provider_cfgs={})
    assert [e["verdict"] for e in entries] == ["burn", "burn", "no-burn",
                                               "unknown"]
    assert entries[0]["provider"] == "a-burn-big"
    assert entries[0]["surplus_usd"] == pytest.approx(8.0, abs=1e-6)
    assert entries[1]["provider"] == "m-burn-small"
    assert entries[1]["surplus_usd"] == pytest.approx(2.0, abs=1e-6)


def test_ac4_summarize_counts_only_burnable_surplus():
    entries = [{"verdict": "burn", "surplus_usd": 2.0, "provider": "a"},
               {"verdict": "burn", "surplus_usd": 3.0, "provider": "b"},
               {"verdict": "no-burn", "surplus_usd": 0.0, "provider": "c"},
               {"verdict": "unknown", "surplus_usd": None, "provider": "d"}]
    s = burn.summarize(entries)
    assert s == {"burn": 2, "no_burn": 1, "unknown": 1,
                 "total_surplus_usd": 5.0}


# ==== AC5 — cost basis: never free money without naming it ===================

def test_ac5_subscription_burn_is_zero_marginal_and_named(tmp_path):
    led = _ledger(tmp_path, [_row(ts=T0 - 9000, cost=4.0)])
    lim = _limits_usd(tmp_path, limit=10.0, plan_kind="subscription")
    blocks = acc.account_ledger(path=led, limits=acc.load_limits(lim),
                                now_ts=T0)
    e = burn.project_window(blocks[0], T0, cfg={"plan_kind": "subscription"})
    assert e["verdict"] == "burn"
    assert e["cost_basis"] == "subscription-covered"
    assert e["subscription_covered"] is True
    assert e["marginal_cash_usd"] == 0.0 and e["marginal_cash_reason"] is None


def test_ac5_metered_burn_costs_the_surplus_itself(tmp_path):
    blk = _ledger_block(tmp_path, [_row(ts=T0 - 9000, cost=4.0)], limit=10.0)
    e = burn.project_window(blk, T0, plan_kind="payg")
    assert e["cost_basis"] == "metered"
    assert e["subscription_covered"] is False
    assert e["marginal_cash_usd"] == pytest.approx(e["surplus_usd"], abs=1e-9)


def test_ac5_undeclared_plan_is_never_reported_as_free_money(tmp_path):
    """THE AC: a subscription window with surplus must never read as $0
    without saying it is subscription-covered. Undeclared => marginal cash
    NULL + a reason naming the fix — never a guessed 0."""
    blk = _ledger_block(tmp_path, [_row(ts=T0 - 9000, cost=4.0)], limit=10.0)
    e = burn.project_window(blk, T0)
    assert e["verdict"] == "burn"
    assert e["cost_basis"] == "unknown"
    assert e["subscription_covered"] is None
    assert e["marginal_cash_usd"] is None
    assert "plan_kind" in e["marginal_cash_reason"]
    assert "--plan-kind" in e["marginal_cash_reason"]


def test_ac5_override_beats_the_config(tmp_path):
    blk = _ledger_block(tmp_path, [_row(ts=T0 - 9000, cost=4.0)], limit=10.0)
    e = burn.project_window(blk, T0, cfg={"plan_kind": "metered"},
                            plan_kind="subscription")
    assert e["cost_basis"] == "subscription-covered"
    assert e["marginal_cash_usd"] == 0.0


def test_ac5_unrecognized_kind_stays_unknown_and_names_the_declaration():
    assert burn.classify_plan("quantum") == "unknown"
    e = burn.project_window(_block(limit=10.0, spent=4.0, resets_in=9000),
                            T0, plan_kind="quantum")
    assert e["plan_kind"] == "quantum"
    assert e["cost_basis"] == "unknown"
    assert e["marginal_cash_usd"] is None


def test_ac5_classify_plan_vocabulary():
    assert burn.classify_plan(None) is None
    assert burn.classify_plan("") is None
    for k in ("subscription", "Subscription-Covered", "sub", "plan",
              "included"):
        assert burn.classify_plan(k) == "subscription-covered", k
    for k in ("metered", "payg", "pay-as-you-go", "on-demand", "api",
              "credit"):
        assert burn.classify_plan(k) == "metered", k


def test_ac5_parse_plan_kinds_unit():
    assert burn.parse_plan_kinds(None) == {}
    assert burn.parse_plan_kinds("") == {}
    assert burn.parse_plan_kinds("p1=sub, p2 = metered") == {
        "p1": "sub", "p2": "metered"}
    with pytest.raises(ValueError):
        burn.parse_plan_kinds("p1")
    with pytest.raises(ValueError):
        burn.parse_plan_kinds("=sub")
    with pytest.raises(ValueError):
        burn.parse_plan_kinds("p1=")


# ==== AC6 — the operator surface: `router quota burn` ========================

def test_ac6_cli_plan_kind_override_reaches_the_report(tmp_path):
    led = _ledger(tmp_path, [_row(ts=T0 - 9000, cost=4.0)])
    lim = _limits_usd(tmp_path, limit=10.0)
    p = _cli("burn", "--ledger", led, "--limits", lim, "--now", str(T0),
             "--plan-kind", "p1=subscription", "--json")
    assert p.returncode == 0, p.stderr
    doc = json.loads(p.stdout)
    e = doc["windows"][0]
    assert e["cost_basis"] == "subscription-covered"
    assert e["marginal_cash_usd"] == 0.0


def test_ac6_provider_filter(tmp_path):
    rows = [_row(provider="p1", ts=T0 - 9000, cost=4.0, session="s1"),
            _row(provider="p2", ts=T0 - 1000, cost=2.0, session="s2")]
    led = _ledger(tmp_path, rows)
    lim = _limits(tmp_path, {"p1": {"window": "rolling_5h",
                                    "limit_usd": 10.0},
                             "p2": {"window": "rolling_5h",
                                    "limit_usd": 10.0}})
    p = _cli("burn", "--ledger", led, "--limits", lim, "--now", str(T0),
             "--provider", "p2", "--json")
    assert p.returncode == 0, p.stderr
    doc = json.loads(p.stdout)
    assert [w["provider"] for w in doc["windows"]] == ["p2"]
    assert doc["windows"][0]["verdict"] == "no-burn"


def test_ac6_reachable_via_router_quota_delegate(tmp_path):
    led = _ledger(tmp_path, [_row(ts=T0 - 9000, cost=4.0)])
    lim = _limits_usd(tmp_path, limit=10.0)
    router = os.path.join(SCRIPTS, "router_quota.py")
    env = dict(os.environ)
    env["TASK_ROUTER_HOME"] = str(tmp_path)
    p = subprocess.run([sys.executable, router, "burn", "--ledger", led,
                        "--limits", lim, "--now", str(T0), "--json"],
                       capture_output=True, text=True, env=env, timeout=120)
    assert p.returncode == 0, p.stderr
    doc = json.loads(p.stdout)
    assert doc["windows"][0]["verdict"] == "burn"
    # the delegate keeps the layer's exit codes (2 on operator error)
    p2 = subprocess.run([sys.executable, router, "burn", "--ledger", led,
                         "--limits", lim, "--now", "nope"],
                        capture_output=True, text=True, env=env, timeout=120)
    assert p2.returncode == 2


def test_ac6_reachable_as_router_cli_quota_burn(tmp_path):
    """The operator surface the fleet calls: `router quota burn`."""
    led = _ledger(tmp_path, [_row(ts=T0 - 9000, cost=4.0)])
    lim = _limits_usd(tmp_path, limit=10.0)
    router = os.path.join(REPO, "task_router", "cli.py")
    env = dict(os.environ)
    env["TASK_ROUTER_HOME"] = str(tmp_path)
    env["PYTHONPATH"] = REPO + os.pathsep + env.get("PYTHONPATH", "")
    p = subprocess.run([sys.executable, router, "quota", "burn",
                        "--ledger", led, "--limits", lim, "--now", str(T0),
                        "--json"],
                       capture_output=True, text=True, env=env, timeout=120)
    assert p.returncode == 0, p.stderr
    doc = json.loads(p.stdout)
    assert doc["windows"][0]["verdict"] == "burn"
    assert doc["summary"]["burn"] == 1


def test_ac6_module_is_wired_into_the_runtime_sync_list():
    """A runtime script the scheduler/foremen call must be synced live."""
    sync = open(os.path.join(SCRIPTS, "sync_runtime.sh")).read()
    assert "router_quota_burn.py" in sync
    assert ("router_quota_accounting.py router_quota_pacing.py "
            "router_quota_burn.py") in sync
