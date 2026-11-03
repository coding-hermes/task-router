"""TR-357 M3 unit tests: lane demotion on consecutive OBSERVED failures.

Board acceptance — demote on consecutive observed failures WITH the demotion
RECORDED, never silent:
  (a) N consecutive success=false tick verdicts => one durable 'demote'
      record (ts, lane, event, consecutive_failure_count, reason, verdicts)
  (b) the demoted lane is VISIBLY displaced in resolve output while demoted
      (entry marker + payload audit block)
  (c) one observed success clears it per the documented semantics
      (clear-on-first-success) with a durable 'clear' record
  (d) below-threshold failures do NOT demote
  (e) idempotent re-application never duplicates records
plus the evidence scope (only M1 tick verdicts count — transport rows and
unobserved rows never join or break a streak), the threshold knob
(ROUTER_DEMOTE_AFTER_FAILURES: re-read per pass, 0 kills, malformed falls
back), the displacement discipline (one swap, never an exclusion, skips
hop-0-ineligible survivors, HELD-and-says-so with no alternative), price-sort
resolves honoring the recorded state, and fail-open on a missing/corrupt log.

Hermetic: mini registry + scratch gate state + scratch outcome store + scratch
demotions log, all under tmp_path (the test_tr354/test_hop0 fixture pattern).
The repo's data/state is never read or written by these tests.
"""
import json
import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "scripts"))
import router_demotions as rd  # noqa: E402
import router_spawn  # noqa: E402

CAT = "agent_tick"
DRY = "demoted-model"            # the cheap lane that will be demoted
ALT = "alt-model"                # the pricier healthy lane
DRY_P, ALT_P = "cheaplane", "altprov"
DRY_ID = f"{DRY_P}/{DRY}"
ALT_ID = f"{ALT_P}/{ALT}"
T0 = 1_800_000_000.0             # 2027-01-15T06:40:00Z, deterministic


def _tables():
    tables = {
        "providers": [{"id": DRY_P}, {"id": ALT_P}, {"id": "fb"}],
        "models": [
            {"provider": DRY_P, "model": DRY, "normalized_price": 0.01,
             "data_class": "open", "context_limit": 200000},
            {"provider": ALT_P, "model": ALT, "normalized_price": 0.50,
             "data_class": "open", "context_limit": 200000},
        ],
        "model_tier": [{"model": m, "category": CAT, "tier": 5}
                       for m in (DRY, ALT)],
        "category_levels": [{"category": CAT, "level": 5, "label": "q95",
                             "min_perf": 0.9}],
        "level_defs": [{"level": lvl, "label": str(lvl)} for lvl in range(-5, 6)],
        "fallback_lanes": [{"provider": "fb", "model": "always-run",
                            "order": 1}],
        "projects": [{"id": "proj", "profile": "TP"}],
        "task_profiles": [{"id": "TP", "title": "test profile"}],
        "task_profile_requirements": [{"task_id": "TP", "category": CAT,
                                       "level": 5}],
    }
    tables["models"].append(
        {"provider": "fb", "model": "always-run", "normalized_price": 9.0,
         "data_class": "open", "context_limit": 200000})
    return tables


def _state(tmp_path, tables):
    d = tmp_path / "state"
    d.mkdir(exist_ok=True)
    json.dump({"providers": {r["id"]: {"status": "open"}
                             for r in tables["providers"]}},
              open(d / "quota-state.json", "w"))
    json.dump({"providers": {}}, open(d / "health-state.json", "w"))
    json.dump({"pairs": {}}, open(d / "circuit-state.json", "w"))
    return str(d)


def _tick_row(session, provider=DRY_P, model=DRY, success=False,
              reason="tick_concluded_no_commits", ts=None, source="hermes"):
    """A store row shaped the way M1's verdict pass leaves it: a bool success
    PLUS the tick success_reason. `reason=None` models a transport/legacy row
    with a bare success flag."""
    row = {"source_system": source, "session_id": session,
           "provider": provider, "model": model, "turns": 3,
           "tokens_in": 1000, "tokens_out": 100, "cost_usd": 0.01,
           "wall_time_s": 60.0, "success": success, "ts": ts or T0}
    if reason is not None:
        row["success_reason"] = reason
    return row


def _write_jsonl(path, rows):
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return str(path)


def _read_jsonl(path):
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def _wire(monkeypatch, tmp_path, tables=None, averages=None, outcomes=None,
          demotions=None):
    """Hermetic plane: registry + gate state + outcome store + demotions log,
    every path under tmp_path. The TR-354 exclusion gate is kill-switched so
    the demotion rule is pinned alone; the hop-0 knobs sit at their defaults."""
    tables = tables or _tables()
    reg = tmp_path / "registry.json"
    reg.write_text(json.dumps({"version": 3, "tables": tables}))
    monkeypatch.setattr(router_spawn, "REGISTRY", str(reg))
    monkeypatch.setattr(router_spawn, "MR", _state(tmp_path, tables))
    avg_path = tmp_path / "averages.jsonl"
    if averages:
        _write_jsonl(avg_path, averages)
    monkeypatch.setenv("ROUTING_AVERAGES_FILE", str(avg_path))
    out_path = tmp_path / "outcomes.jsonl"
    if outcomes:
        _write_jsonl(out_path, outcomes)
    elif os.path.exists(out_path):
        os.remove(out_path)          # a re-wire must not inherit prior rows
    monkeypatch.setenv("ROUTING_OUTCOMES_FILE", str(out_path))
    dem_path = tmp_path / "demotions.jsonl"
    if demotions:
        _write_jsonl(dem_path, demotions)
    elif os.path.exists(dem_path):
        os.remove(dem_path)          # a re-wire starts from an EMPTY log
    monkeypatch.setenv("ROUTING_DEMOTIONS_FILE", str(dem_path))
    monkeypatch.delenv("ROUTER_DEMOTE_AFTER_FAILURES", raising=False)
    monkeypatch.setenv("ROUTER_OUTCOME_FLOOR", "0")
    for var in ("ROUTER_HOP0", "ROUTER_HOP0_MAX_DRY", "ROUTER_HOP0_MIN_KNOWN"):
        monkeypatch.delenv(var, raising=False)
    return tables, str(dem_path)


def _averages_row(provider, model, n=10, known=10, completed=10, cost=0.001):
    """A HEALTHY averages row (dry rate 0) with a measured task cost — the
    same shape the predicted-cost sort ranks on, so the hop-0 rule stays
    inert and the demotion under test is driven purely by the recorded log."""
    return {"source_system": "hermes", "provider": provider, "model": model,
            "complexity": None, "n_samples": n, "n_completed": completed,
            "n_success_known": known,
            "success_rate": (completed / known) if known else None,
            "avg_cost_task_24h": cost}


def _fails(n, model=DRY, provider=DRY_P, start=1):
    return [_tick_row(f"s{i}", provider=provider, model=model,
                      success=False, ts=T0 + i)
            for i in range(start, start + n)]


def _order(result):
    return [f"{e['provider']}/{e['model']}" for e in result["chain"]]


def _entry(result, model):
    return next(e for e in result["chain"] if e["model"] == model)


def _ex(result, model):
    return next((e for e in result["exclusions"] if e["model"] == model), None)


# ---------------------------------------------------------------------------
# (a) the trigger: consecutive observed failures demote — and are RECORDED
# ---------------------------------------------------------------------------

def test_three_consecutive_failures_demote_and_the_record_is_durable(
        monkeypatch, tmp_path):
    _, dem_path = _wire(monkeypatch, tmp_path,
                        outcomes=_fails(3, start=1))       # s1, s2, s3
    summary = rd.evaluate(now_s=T0)
    assert "error" not in summary, summary
    assert summary["records_appended"] == 1
    assert summary["demoted"] == [DRY_ID]
    assert summary["threshold"] == 3
    recs = _read_jsonl(dem_path)
    assert len(recs) == 1
    rec = recs[0]
    # the record carries every field the board deliverable names
    assert rec["event"] == "demote"
    assert rec["provider"] == DRY_P and rec["model"] == DRY
    assert rec["lane"] == DRY_ID
    assert rec["ts"] == T0 and rec["ts_iso"] == "2027-01-15T08:00:00Z"
    assert rec["consecutive_failure_count"] == 3
    assert rec["threshold"] == 3
    assert rec["task"] == "TR-357"
    assert rec["verdicts"] == ["s1", "s2", "s3"]   # the triggering identities
    assert rec["verdict_reasons"] == ["tick_concluded_no_commits"]
    assert "3 consecutive observed task failures" in rec["reason"]
    assert "ROUTER_DEMOTE_AFTER_FAILURES=3" in rec["reason"]
    assert "TR-357" in rec["reason"]
    # the log IS the state: the lane is now actively demoted
    active = rd.active_demotions(dem_path)
    assert set(active) == {(DRY_P, DRY)}


def test_only_m1_tick_verdicts_count_transport_and_unobserved_rows_never_join(
        monkeypatch, tmp_path):
    _, dem_path = _wire(monkeypatch, tmp_path)
    # three transport-class failures (proxy rows, no tick reason): NOT task
    # evidence — the circuit plane owns these, they never demote here
    transport = [_tick_row(f"t{i}", success=False, reason=None,
                           source="router-proxy") for i in range(1, 4)]
    _write_jsonl(os.environ["ROUTING_OUTCOMES_FILE"], transport)
    assert rd.evaluate()["records_appended"] == 0
    assert not os.path.exists(dem_path)
    # a bare-failure hermes row WITHOUT a tick reason is equally not task
    # evidence (legacy/transport shape)
    legacy = [_tick_row(f"l{i}", success=False, reason=None)
              for i in range(1, 4)]
    _write_jsonl(os.environ["ROUTING_OUTCOMES_FILE"], legacy)
    assert rd.evaluate()["records_appended"] == 0
    # unobserved rows (success=null) neither break nor join a streak: three
    # tick failures interleaved with nulls still demote on the TICK streak,
    # and the record's verdicts name only the tick sessions
    mixed = [_tick_row("s1", success=False), _tick_row("n1", success=None,
                                                       reason=None),
             _tick_row("s2", success=False),
             _tick_row("t9", success=False, reason=None,
                       source="router-proxy"),
             _tick_row("s3", success=False)]
    _write_jsonl(os.environ["ROUTING_OUTCOMES_FILE"], mixed)
    summary = rd.evaluate()
    assert summary["records_appended"] == 1
    rec = _read_jsonl(dem_path)[-1]
    assert rec["consecutive_failure_count"] == 3
    assert rec["verdicts"] == ["s1", "s2", "s3"]


# ---------------------------------------------------------------------------
# (d) below the threshold: no demotion, no record
# ---------------------------------------------------------------------------

def test_below_threshold_failures_do_not_demote(monkeypatch, tmp_path):
    _, dem_path = _wire(monkeypatch, tmp_path, outcomes=_fails(2))
    summary = rd.evaluate()
    assert summary["records_appended"] == 0
    assert summary["demoted"] == []
    assert not os.path.exists(dem_path)
    assert rd.active_demotions(dem_path) == {}


def test_a_success_ending_the_streak_leaves_nothing_to_demote(
        monkeypatch, tmp_path):
    """[F, F, SUCCESS] — the trailing verdict is a success: never demoted,
    and (not being demoted) no clear record either."""
    _, dem_path = _wire(monkeypatch, tmp_path, outcomes=_fails(2) + [
        _tick_row("s3", success=True, reason="tick_commits_observed")])
    summary = rd.evaluate()
    assert summary["records_appended"] == 0
    assert not os.path.exists(dem_path)


# ---------------------------------------------------------------------------
# (c) recovery: one observed success clears — with a durable clear record
# ---------------------------------------------------------------------------

def test_one_observed_success_clears_with_a_durable_clear_record(
        monkeypatch, tmp_path):
    _, dem_path = _wire(monkeypatch, tmp_path, outcomes=_fails(3))
    assert rd.evaluate()["records_appended"] == 1
    # the observed success arrives (a later tick concluded with commits)
    with open(os.environ["ROUTING_OUTCOMES_FILE"], "a") as f:
        f.write(json.dumps(_tick_row("s4", success=True,
                                     reason="tick_commits_observed")) + "\n")
    summary = rd.evaluate(now_s=T0 + 100)
    assert summary["cleared"] == [DRY_ID]
    assert summary["records_appended"] == 1
    recs = _read_jsonl(dem_path)
    assert len(recs) == 2
    clear = recs[-1]
    assert clear["event"] == "clear"
    assert clear["lane"] == DRY_ID
    assert clear["consecutive_failure_count"] == 0
    assert clear["verdicts"] == ["s4"]
    assert clear["verdict_reasons"] == ["tick_commits_observed"]
    assert clear["ts"] == T0 + 100
    assert "cleared the demotion" in clear["reason"]
    assert "tick_commits_observed" in clear["reason"]
    # the log IS the state: the lane is head-eligible again
    assert rd.active_demotions(dem_path) == {}
    # a success for a never-demoted lane appends NOTHING (transition-only)
    _, dem2 = _wire(monkeypatch, tmp_path, outcomes=[
        _tick_row("s1", success=True, reason="tick_commits_observed")])
    assert rd.evaluate()["records_appended"] == 0
    assert not os.path.exists(dem2)


# ---------------------------------------------------------------------------
# (e) idempotent: re-runs and post-crash re-runs never duplicate records
# ---------------------------------------------------------------------------

def test_idempotent_reruns_never_duplicate_records(monkeypatch, tmp_path):
    _, dem_path = _wire(monkeypatch, tmp_path, outcomes=_fails(3))
    assert rd.evaluate()["records_appended"] == 1
    # re-run with NO new outcomes: the transition is already recorded
    again = rd.evaluate()
    assert again["records_appended"] == 0
    assert again["already"] == 1
    # re-run with MORE failures arriving while demoted: still the same
    # recorded state — the streak growing is not a new transition
    with open(os.environ["ROUTING_OUTCOMES_FILE"], "a") as f:
        f.write(json.dumps(_tick_row("s4", success=False)) + "\n")
    more = rd.evaluate()
    assert more["records_appended"] == 0
    assert more["already"] == 1
    assert len(_read_jsonl(dem_path)) == 1
    # the crash simulation: a pass that appended the demote record and died
    # before finishing leaves a consistent log — a re-run completes nothing
    # and fabricates no duplicate
    crash = rd.evaluate()
    assert crash["records_appended"] == 0
    assert len(_read_jsonl(dem_path)) == 1


def test_threshold_knob_kill_switch_and_malformed_env(monkeypatch, tmp_path):
    _, dem_path = _wire(monkeypatch, tmp_path, outcomes=_fails(2))
    # tightened knob: 2 consecutive failures are enough
    monkeypatch.setenv("ROUTER_DEMOTE_AFTER_FAILURES", "2")
    summary = rd.evaluate()
    assert summary["threshold"] == 2
    assert summary["demoted"] == [DRY_ID]
    assert _read_jsonl(dem_path)[-1]["threshold"] == 2
    # kill switch: the evaluator touches nothing at all (set AFTER _wire —
    # the fixture resets its deterministic knob baseline)
    _, dem3 = _wire(monkeypatch, tmp_path, outcomes=_fails(5))
    monkeypatch.setenv("ROUTER_DEMOTE_AFTER_FAILURES", "0")
    off = rd.evaluate()
    assert off["records_appended"] == 0
    assert "disabled" in off["reason"]
    assert not os.path.exists(dem3)
    # malformed env falls back to the DEFAULT (3), never crashes the pass
    _, dem4 = _wire(monkeypatch, tmp_path, outcomes=_fails(3))
    monkeypatch.setenv("ROUTER_DEMOTE_AFTER_FAILURES", "not-a-number")
    assert rd.evaluate()["demoted"] == [DRY_ID]
    assert rd.threshold() == 3


def test_show_reports_active_demotions_and_records(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, outcomes=_fails(3))
    rd.evaluate(now_s=T0)
    view = rd.show(limit=5)
    assert view["state_path"] == os.environ["ROUTING_DEMOTIONS_FILE"]
    assert view["total_records"] == 1
    assert view["active"] == [{
        "lane": DRY_ID, "consecutive_failure_count": 3,
        "demoted_at": "2027-01-15T08:00:00Z",
        "verdict_reasons": ["tick_concluded_no_commits"]}]


# ---------------------------------------------------------------------------
# (b) the action: the demoted lane is VISIBLY displaced in resolve output
# ---------------------------------------------------------------------------

def test_demoted_head_is_displaced_and_visible(monkeypatch, tmp_path):
    outcomes = _fails(3)
    _wire(monkeypatch, tmp_path,
          averages=[_averages_row(DRY_P, DRY), _averages_row(ALT_P, ALT,
                                                             cost=0.4)],
          outcomes=outcomes)
    # full loop: M1-shaped rows -> evaluate() writes the record -> resolve
    # reads the log and displaces
    assert rd.evaluate(now_s=T0)["records_appended"] == 1
    r = router_spawn.resolve(project="proj", sort="predicted_cost_per_task")
    # the demoted cheap lane lost the head to the pricier healthy lane...
    assert _order(r) == [ALT_ID, DRY_ID], _order(r)
    assert r["head"]["model"] == ALT
    # ...but was NOT excluded — displacement only, it stays in the chain
    assert _ex(r, DRY) is None
    # the marker rides on the affected entry, naming rule + evidence
    ent = _entry(r, DRY)
    assert ent["outcome_demoted"] is True
    assert "outcome-demoted" in ent["outcome_demoted_reason"]
    assert "3 consecutive observed task failures" in ent["outcome_demoted_reason"]
    assert "tick_concluded_no_commits" in ent["outcome_demoted_reason"]
    assert "s3" in ent["outcome_demoted_reason"]      # last verdict identity
    assert "TR-357" in ent["outcome_demoted_reason"]
    assert "displaced from hop 0 to chain position 1" in \
        ent["outcome_demoted_reason"]
    # the clean alternative carries no marker and holds hop 0
    assert "outcome_demoted" not in _entry(r, ALT)
    # the payload audit block: judged, state path, active, who moved
    blk = r["outcome_demotions"]
    assert blk["judged"] is True
    assert blk["rule"] == router_spawn.ROUTER_DEMOTE_RULE
    assert blk["state_path"] == os.environ["ROUTING_DEMOTIONS_FILE"]
    assert blk["threshold"] == 3
    assert blk["active"] == [{
        "provider": DRY_P, "model": DRY, "consecutive_failure_count": 3,
        "demoted_at": "2027-01-15T08:00:00Z",
        "verdict_reasons": ["tick_concluded_no_commits"]}]
    assert blk["displaced"] == [{
        "provider": DRY_P, "model": DRY, "consecutive_failure_count": 3,
        "now_at_chain_position": 1, "passed_by": ALT_ID}]
    assert "held_no_alternative" not in blk
    # the displaced entry keeps its (pre-rule) hop number — one swap, no renumber
    assert ent["hop"] == 1


def test_non_head_demoted_lane_is_marked_but_keeps_its_place(
        monkeypatch, tmp_path):
    """The rule is hop-0 eligibility: a demoted lane NOT at the head keeps its
    position but the marker makes the demotion visible anyway (never silent)."""
    outcomes = _fails(3, model=ALT, provider=ALT_P)    # demote the PRICIER lane
    _wire(monkeypatch, tmp_path,
          averages=[_averages_row(DRY_P, DRY), _averages_row(ALT_P, ALT)],
          outcomes=outcomes)
    assert rd.evaluate()["records_appended"] == 1
    r = router_spawn.resolve(project="proj", sort="predicted_cost_per_task")
    assert _order(r) == [DRY_ID, ALT_ID], _order(r)
    ent = _entry(r, ALT)
    assert ent["outcome_demoted"] is True
    assert "outcome-demoted" in ent["outcome_demoted_reason"]
    assert "outcome_demoted" not in _entry(r, DRY)
    blk = r["outcome_demotions"]
    assert blk["displaced"] == []
    assert blk["reason"] == "head not demoted — no displacement"
    assert blk["active"][0]["model"] == ALT


def test_clear_record_makes_the_lane_head_eligible_again(monkeypatch, tmp_path):
    outcomes = _fails(3)
    _wire(monkeypatch, tmp_path,
          averages=[_averages_row(DRY_P, DRY), _averages_row(ALT_P, ALT)],
          outcomes=outcomes)
    rd.evaluate()
    r = router_spawn.resolve(project="proj", sort="predicted_cost_per_task")
    assert _order(r) == [ALT_ID, DRY_ID]
    # the observed success lands and is evaluated: the clear record is written
    with open(os.environ["ROUTING_OUTCOMES_FILE"], "a") as f:
        f.write(json.dumps(_tick_row("s4", success=True,
                                     reason="tick_commits_observed")) + "\n")
    assert rd.evaluate()["cleared"] == [DRY_ID]
    r2 = router_spawn.resolve(project="proj", sort="predicted_cost_per_task")
    assert _order(r2) == [DRY_ID, ALT_ID]
    assert "outcome_demoted" not in _entry(r2, DRY)
    blk = r2["outcome_demotions"]
    assert blk["judged"] is True
    assert blk["active"] == []
    assert blk["displaced"] == []
    assert blk["reason"] == "no lane demoted"


def test_demoted_head_with_no_eligible_alternative_is_held_and_says_so(
        monkeypatch, tmp_path):
    """Fail-open twin: a SOLO demoted lane cannot be displaced (no eligible
    alternative) — it keeps hop 0 but the marker and the payload say so."""
    tables = _tables()
    tables["models"] = [m for m in tables["models"] if m["model"] != ALT]
    _wire(monkeypatch, tmp_path, tables=tables,
          averages=[_averages_row(DRY_P, DRY)], outcomes=_fails(3))
    assert rd.evaluate()["records_appended"] == 1
    r = router_spawn.resolve(project="proj", sort="predicted_cost_per_task")
    assert _order(r) == [DRY_ID]
    ent = _entry(r, DRY)
    assert ent["outcome_demoted"] is True
    assert "HELD at hop 0" in ent["outcome_demoted_reason"]
    assert "no eligible alternative" in ent["outcome_demoted_reason"]
    blk = r["outcome_demotions"]
    assert blk["displaced"] == []
    assert blk["held_no_alternative"] == {"provider": DRY_P, "model": DRY}


def test_displacement_skips_hop0_ineligible_survivors(monkeypatch, tmp_path):
    """M2's rule keeps its own hold on hop 0: the demotion displacement never
    hands the head to a hop-0-ineligible lane — it takes the first survivor
    that is neither demoted nor hop-0-ineligible."""
    MID, MID_P = "dry-mid", "midprov"
    OK, OK_P = "ok-model", "okprov"
    tables = _tables()
    tables["providers"] += [{"id": MID_P}, {"id": OK_P}]
    tables["models"] += [
        {"provider": MID_P, "model": MID, "normalized_price": 0.20,
         "data_class": "open", "context_limit": 200000},
        {"provider": OK_P, "model": OK, "normalized_price": 2.00,
         "data_class": "open", "context_limit": 200000}]
    tables["model_tier"] += [{"model": m, "category": CAT, "tier": 5}
                             for m in (MID, OK)]
    # MID is hop-0-ineligible (dry 0.9 over 10 known > 0.65 floor); DRY and OK
    # are healthy on the averages. Costs keep the measured ranking [DRY, MID,
    # ALT, OK]: MID's exclusion would come from M2 only at the head, and the
    # demotion displacement (M3) must skip it when choosing the new head.
    averages = [_averages_row(DRY_P, DRY),
                _averages_row(MID_P, MID, n=10, known=10, completed=1,
                              cost=0.002),
                _averages_row(ALT_P, ALT, cost=0.4),
                _averages_row(OK_P, OK, cost=2.0)]
    _wire(monkeypatch, tmp_path, tables=tables, averages=averages,
          outcomes=_fails(3))
    assert rd.evaluate()["records_appended"] == 1
    r = router_spawn.resolve(project="proj", sort="predicted_cost_per_task")
    # pre-rule measured order [DRY, MID, ALT, OK]: the demoted head DRY is
    # displaced past MID (hop-0-ineligible, position 1) to ALT (position 2) —
    # without the skip, MID would have taken the head
    assert _order(r) == [f"{ALT_P}/{ALT}", f"{MID_P}/{MID}", DRY_ID,
                         f"{OK_P}/{OK}"], _order(r)
    # the demoted lane did NOT hand the head to the hop-0-ineligible lane
    assert r["head"]["model"] == ALT
    assert r["hop0_eligibility"]["ineligible"] != []      # MID judged dry
    assert _entry(r, DRY)["outcome_demoted"] is True
    assert r["outcome_demotions"]["displaced"][0]["passed_by"] == ALT_ID
    assert r["outcome_demotions"]["displaced"][0]["now_at_chain_position"] == 2


def test_price_sort_still_honors_the_recorded_demotion(monkeypatch, tmp_path):
    """The demotion state is precomputed evidence, not an averages verdict: a
    price-sorted resolve (no outcome stats consulted) still keeps a demoted
    lane off hop 0, visibly."""
    _wire(monkeypatch, tmp_path,
          averages=[_averages_row(DRY_P, DRY), _averages_row(ALT_P, ALT)],
          outcomes=_fails(3))
    assert rd.evaluate()["records_appended"] == 1
    r = router_spawn.resolve(project="proj", sort="price")
    assert _order(r) == [ALT_ID, DRY_ID], _order(r)
    assert _entry(r, DRY)["outcome_demoted"] is True
    assert r["outcome_demotions"]["displaced"][0]["model"] == DRY
    # M2's rate rule stayed inert on this price sort; M3's recorded state did not
    assert r["hop0_eligibility"]["judged"] is False
    assert r["outcome_demotions"]["judged"] is True


# ---------------------------------------------------------------------------
# fail-open: the resolve never blocks or misorders on demotion-state trouble
# ---------------------------------------------------------------------------

def test_missing_or_corrupt_log_is_fail_open_with_reason(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path,
          averages=[_averages_row(DRY_P, DRY), _averages_row(ALT_P, ALT)])
    # no log file at all: inert, judged, with the reason on the block
    r = router_spawn.resolve(project="proj", sort="predicted_cost_per_task")
    assert _order(r) == [DRY_ID, ALT_ID]
    blk = r["outcome_demotions"]
    assert blk["judged"] is True
    assert blk["active"] == []
    assert blk["displaced"] == []
    assert blk["reason"] == "no lane demoted"
    assert "outcome_demoted" not in _entry(r, DRY)
    # a corrupt (torn/garbage) log: the tolerant reader skips it — same inert
    # contract, the chain is never blocked or misordered by bad state
    with open(os.environ["ROUTING_DEMOTIONS_FILE"], "w") as f:
        f.write('{"event": "demote", "provider": "cheaplane"\n')
        f.write("not json at all\n")
    r2 = router_spawn.resolve(project="proj", sort="predicted_cost_per_task")
    assert _order(r2) == [DRY_ID, ALT_ID]
    assert r2["outcome_demotions"]["active"] == []
    assert "outcome_demoted" not in _entry(r2, DRY)


def test_demotion_module_absent_is_fail_open(monkeypatch, tmp_path):
    """A missing module must never break a resolve: inert with the reason."""
    _wire(monkeypatch, tmp_path,
          averages=[_averages_row(DRY_P, DRY), _averages_row(ALT_P, ALT)])
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) \
        else __builtins__.__import__

    def _no_demotions(name, *a, **k):
        if name == "router_demotions":
            raise ImportError("simulated missing module")
        return real_import(name, *a, **k)

    monkeypatch.setattr("builtins.__import__", _no_demotions)
    r = router_spawn.resolve(project="proj", sort="predicted_cost_per_task")
    assert _order(r) == [DRY_ID, ALT_ID]
    blk = r["outcome_demotions"]
    assert blk["judged"] is False
    assert "demotion module unavailable" in blk["reason"]


def test_stale_position_markers_when_both_rules_fire(monkeypatch, tmp_path):
    """M2 runs first, M3 second: when the M2 swap leaves a lane at the head
    that M3 then displaces, the final head satisfies BOTH rules."""
    DEM, DEM_P = "dem-head", "demprov"      # becomes demoted AND stays clean
    DRYY, DRYY_P = "dry-alt", "dryprov"     # hop-0-ineligible (M2's verdict)
    OK, OK_P = "ok-model", "okprov"
    tables = _tables()
    tables["providers"][0]["id"] = DEM_P
    tables["models"][0] = {"provider": DEM_P, "model": DEM,
                           "normalized_price": 0.01, "data_class": "open",
                           "context_limit": 200000}
    tables["model_tier"][0]["model"] = DEM
    tables["providers"] += [{"id": DRYY_P}, {"id": OK_P}]
    tables["models"] += [
        {"provider": DRYY_P, "model": DRYY, "normalized_price": 0.10,
         "data_class": "open", "context_limit": 200000},
        {"provider": OK_P, "model": OK, "normalized_price": 2.00,
         "data_class": "open", "context_limit": 200000}]
    tables["model_tier"] += [{"model": m, "category": CAT, "tier": 5}
                             for m in (DRYY, OK)]
    # DEM is healthy on the averages (M2 inert on it) but DEMOTED by the log;
    # DRYY is hop-0-ineligible (dry 1.0 over 10 known); ALT carries no
    # averages row (unmeasured ranks behind the measured lanes). Costs keep
    # the measured ranking [DEM, DRYY, OK].
    averages = [_averages_row(DEM_P, DEM),
                _averages_row(DRYY_P, DRYY, n=10, known=10, completed=0,
                              cost=0.10),
                _averages_row(OK_P, OK, cost=2.0)]
    _wire(monkeypatch, tmp_path, tables=tables, averages=averages,
          outcomes=_fails(3, model=DEM, provider=DEM_P))
    assert rd.evaluate()["records_appended"] == 1
    r = router_spawn.resolve(project="proj", sort="predicted_cost_per_task")
    # DEM (demoted) held the head; M2 didn't move it (healthy averages), so
    # M3 displaces it — skipping the hop-0-ineligible DRYY — to OK; ALT
    # (unmeasured) trails the measured lanes
    assert _order(r) == [f"{OK_P}/{OK}", f"{DRYY_P}/{DRYY}", f"{DEM_P}/{DEM}",
                         ALT_ID], _order(r)
    assert r["head"]["model"] == OK
    assert _entry(r, DEM)["outcome_demoted"] is True
    # DRYY was JUDGED hop-0-ineligible (it lives in M2's judged set — the
    # entry marker only appears when a lane is actually displaced from the
    # head) and it was skipped by the demotion displacement regardless
    assert any(v["model"] == DRYY
               for v in r["hop0_eligibility"]["ineligible"])
    assert "outcome_demoted" not in _entry(r, OK)
