"""Commit-time gate for partial model data (TR-model-data-commit-gate).

The property under test is NOT "a function returns a list". It is: a `git commit`
that adds a lane with no capability evidence, or that would raise the TR-369
usability defect count, is REFUSED locally — and an honest commit is not.

Two layers, because they fail differently:
  * pure-function tests over check_capability / check_ratchet — precise rules,
    including the exemptions that keep the gate from blocking pre-existing debt;
  * real `git` end-to-end tests in a throwaway repo — the CLI actually exits
    non-zero, actually names the lane, and actually lets a clean commit through.
"""
import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
GATE = ROOT / "scripts" / "model_data_commit_gate.py"

PERF = ("perf_agent_tick", "perf_debug", "perf_review")


def _gate():
    spec = importlib.util.spec_from_file_location("model_data_commit_gate", GATE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.ROOT = str(ROOT)  # keep the vocab/gate imports pointed at the real repo
    return mod


G = _gate()


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
def _lane(provider, model, price=1.0, **kw):
    row = {"provider": provider, "model": model, "normalized_price": price}
    row.update({f: None for f in PERF})
    row.update(kw)
    return row


def _cap(models_head, models_base=None, tiers=None, perfs=None,
         notes=None, stamps=None, stamps_base=None):
    return G.check_capability(
        models_head, models_base or [], tiers or [], perfs or [],
        notes or [], stamps or [], stamp_rows_base=stamps_base or [],
    )


def _lanes(violations):
    return {(v.get("provider"), v.get("model")) for v in violations}


# ---------------------------------------------------------------------------
# RULE A — capability evidence, unit level
# ---------------------------------------------------------------------------
def test_added_live_lane_without_evidence_is_blocked():
    v = _cap([_lane("acme", "brand-new")])
    assert _lanes(v) == {("acme", "brand-new")}
    assert v[0]["change"] == "added"
    assert "no capability measurement" in v[0]["why"]
    assert set(v[0]["missing_fields"]) >= {"perf_agent_tick", "perf_debug"}


def test_added_lane_with_a_perf_measurement_passes():
    assert _cap([_lane("acme", "measured", perf_debug=0.81)]) == []


def test_added_lane_with_a_tier_measurement_passes():
    """A raw `perf` in model_tier.jsonl IS a measurement, whatever the tier."""
    tiers = [{"model": "tiered", "category": "debug", "perf": 0.4, "tier": -3}]
    assert _cap([_lane("acme", "tiered")], tiers=tiers) == []


def test_added_lane_with_a_model_perf_row_passes():
    perfs = [{"model": "perfed", "category": "review", "perf": 0.66}]
    assert _cap([_lane("acme", "perfed")], perfs=perfs) == []


def test_added_lane_disabled_with_reason_passes():
    row = _lane("acme", "off", disabled=True, disabled_reason="provider retired the endpoint")
    assert _cap([row]) == []


def test_added_lane_disabled_WITHOUT_reason_is_exempt_as_not_routable():
    """Not live = not routable = the router never ranks it. Out of scope by design."""
    assert _cap([_lane("acme", "off", disabled=True, disabled_reason=None)]) == []


def test_added_lane_with_a_model_notes_row_passes():
    notes = [{"provider": "acme", "model": "documented",
              "note": "no public benchmark exists; provider publishes no eval suite"}]
    assert _cap([_lane("acme", "documented")], notes=notes) == []


def test_added_lane_with_a_wildcard_note_passes():
    notes = [{"provider": "*", "model": "documented", "note": "upstream publishes no evals"}]
    assert _cap([_lane("acme", "documented")], notes=notes) == []


def test_empty_note_is_not_a_reason():
    notes = [{"provider": "acme", "model": "x", "note": "   "}]
    assert _lanes(_cap([_lane("acme", "x")], notes=notes)) == {("acme", "x")}


def test_added_lane_with_every_perf_field_declared_unmeasurable_passes():
    """`unknown-by-design` is the honest answer for an unmeasurable category."""
    row = _lane("acme", "stamped")
    stamps = [{"provider": "acme", "model": "stamped", "field": f,
               "reason": "unknown-by-design"} for f in PERF]
    stamps[0]["reason"] = "not-published-by-provider"
    assert _cap([row], stamps=stamps) == []


def test_no_sample_yet_stamps_are_a_deferral_not_a_disposition():
    """The blanket `no-sample-yet` stamp must NOT launder an unmeasured lane."""
    row = _lane("acme", "deferred")
    stamps = [{"provider": "acme", "model": "deferred", "field": f,
               "reason": "no-sample-yet"} for f in PERF]
    v = _cap([row], stamps=stamps)
    assert _lanes(v) == {("acme", "deferred")}
    assert set(v[0]["missing_fields"]) == set(PERF)


def test_partial_stamp_set_is_blocked_and_names_the_missing_fields():
    """The whole point: 'we stamped some of it' is still partial data."""
    row = _lane("acme", "half-stamped")
    stamps = [{"provider": "acme", "model": "half-stamped", "field": PERF[0],
               "reason": "unknown-by-design"}]
    v = _cap([row], stamps=stamps)
    assert _lanes(v) == {("acme", "half-stamped")}
    assert "perf_agent_tick" not in v[0]["missing_fields"]
    assert len(v[0]["missing_fields"]) == len(PERF) - 1


def test_newly_live_lane_is_blocked():
    base = [_lane("acme", "wakes-up", disabled=True, disabled_reason="waiting on quota")]
    head = [_lane("acme", "wakes-up")]
    v = _cap(head, base)
    assert _lanes(v) == {("acme", "wakes-up")}
    assert v[0]["change"] == "newly-live"


def test_evidence_dropped_while_staying_live_is_blocked():
    base = [_lane("acme", "was-measured", perf_debug=0.9)]
    head = [_lane("acme", "was-measured")]
    v = _cap(head, base)
    assert _lanes(v) == {("acme", "was-measured")}
    assert v[0]["change"] == "evidence-dropped"


def test_pre_existing_unmeasured_lane_may_still_be_re_edited():
    """~1900 lanes predate any gate; a price refresh must not be blocked."""
    base = [_lane("acme", "legacy", price=1.0)]
    head = [_lane("acme", "legacy", price=2.0)]
    assert _cap(head, base) == []


def test_untouched_lane_is_never_examined():
    assert _cap([_lane("acme", "old")], [_lane("acme", "old")]) == []


def test_bad_stamp_added_for_a_perf_field_is_named():
    row = _lane("acme", "liar")
    stamps = [{"provider": "acme", "model": "liar", "field": "perf_debug",
               "reason": "we-just-did-not-feel-like-it"}]
    v = _cap([row], stamps=stamps)
    bad = [x for x in v if x.get("change") == "bad-stamp"]
    assert bad, v
    assert "outside the AC2 vocabulary" in bad[0]["why"]


def test_pre_existing_bad_stamp_is_not_this_commits_problem():
    row = _lane("acme", "liar")
    stamps = [{"provider": "acme", "model": "liar", "field": "perf_debug",
               "reason": "we-just-did-not-feel-like-it"}]
    v = _cap([row], stamps=stamps, stamps_base=stamps)
    assert not any(x.get("change") == "bad-stamp" for x in v)


def test_non_perf_stamp_vocabulary_is_out_of_scope():
    """data/tables/null_reasons.jsonl carries plan_entitlement stamps; not our set."""
    row = _lane("acme", "planny")
    stamps = [{"provider": "acme", "model": "planny", "field": "plan_entitlement",
               "reason": "plan-not-entitled"}]
    v = _cap([row], stamps=stamps)
    assert not any(x.get("change") == "bad-stamp" for x in v)


def test_perf_field_set_is_derived_from_the_data_not_hard_coded():
    """A category the rows carry is required; a stale constant would miss it."""
    fields = G.perf_fields([{"perf_code_gen": 0.5}, {"perf_zzz_new_category": None}])
    assert "perf_code_gen" in fields and "perf_zzz_new_category" in fields


# ---------------------------------------------------------------------------
# RULE B — the usability ratchet
# ---------------------------------------------------------------------------
def test_ratchet_passes_when_defects_fall():
    assert G.check_ratchet(330, 340, 345, 345) is None


def test_ratchet_blocks_when_defects_rise_above_baseline():
    v = G.check_ratchet(346, 346, 345, 345)
    assert v and v["limit"] == 345 and v["limit_source"].startswith("baseline")


def test_ratchet_blocks_a_rise_over_the_parent_even_under_the_baseline():
    """Baseline headroom (345 vs 335 today) is NOT a licence to add defects."""
    v = G.check_ratchet(336, 335, 345, 345)
    assert v and v["limit_source"] == "parent commit"


def test_baseline_cannot_be_raised_in_the_same_commit():
    """index says 400, HEAD says 345 -> the lower ceiling wins."""
    v = G.check_ratchet(350, 350, 400, 345)
    assert v and v["limit"] == 345 and v["limit_source"] == "baseline(HEAD)"


def test_ratchet_is_a_no_op_without_any_ceiling():
    assert G.check_ratchet(9999, None, None, None) is None


def test_lower_baseline_in_the_same_commit_is_honoured():
    assert G.check_ratchet(320, 340, 320, 345) is None


# ---------------------------------------------------------------------------
# RULE C — the reasoning-effort axis (TR-398/TR-399)
# ---------------------------------------------------------------------------
def test_effort_missing_is_refused():
    v = G.check_efforts(
        [{"model": "m", "category": "code_gen", "perf": 0.7}], [])
    assert v and v[0]["rule"] == "effort" and "MISSING" in v[0]["why"]


def test_effort_unnormalized_alias_is_refused_never_downgraded():
    """'xhigh' must be stored 'x-high' — mapping it to 'high' would downgrade."""
    v = G.check_efforts(
        [{"model": "m", "category": "code_gen", "perf": 0.7,
          "reasoning_effort": "xhigh"}], [])
    assert v and "un-normalized provider alias" in v[0]["why"]
    assert "'x-high'" in v[0]["why"]


def test_effort_canonical_and_verbatim_names_are_accepted():
    assert G.check_efforts(
        [{"model": "m", "category": "code_gen", "perf": 0.7,
          "reasoning_effort": "x-high"},
         {"model": "m", "category": "code_gen", "perf": 0.7,
          "reasoning_effort": "default"},
         # owner rule: an unmapped provider name is stored verbatim, not
         # guessed down to a lower level
         {"model": "m", "category": "code_gen", "perf": 0.7,
          "reasoning_effort": "ultra"}], []) == []


def test_effort_benchmark_rows_are_gated_too():
    v = G.check_efforts([], [{"model": "m", "category": "code_gen",
                              "score": 0.7, "max_score": 1.0}])
    assert v and v[0]["table"].endswith("benchmarks.jsonl")


# ---------------------------------------------------------------------------
# end-to-end: the CLI against a real git repo
# ---------------------------------------------------------------------------
def _git(repo, *args, check=True):
    env = dict(os.environ)
    env.update({
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_AUTHOR_NAME": "gate-test", "GIT_AUTHOR_EMAIL": "gate@test.invalid",
        "GIT_COMMITTER_NAME": "gate-test", "GIT_COMMITTER_EMAIL": "gate@test.invalid",
    })
    p = subprocess.run(["git"] + list(args), cwd=str(repo), capture_output=True,
                       text=True, env=env)
    if check and p.returncode != 0:
        raise AssertionError("git %s failed: %s%s" % (args, p.stdout, p.stderr))
    return p


def _write(repo, rel, rows):
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(rows, (dict, list)) and not isinstance(rows, str):
        payload = "\n".join(json.dumps(r) for r in rows) + "\n"
    else:
        payload = rows
    path.write_text(payload)


def _make_repo(tmp_path, models, tiers=(), baseline_defects=None, notes=(), stamps=()):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _write(repo, "data/tables/models.jsonl", list(models))
    _write(repo, "data/tables/model_tier.jsonl", list(tiers))
    _write(repo, "data/tables/model_perf.jsonl", [])
    _write(repo, "data/tables/benchmarks.jsonl", [])  # TR-398: ingest target, gated too
    _write(repo, "data/null_reasons.jsonl", list(stamps))
    _write(repo, "data/tables/model_notes.jsonl", list(notes))
    # The baseline must describe the tree it is committed with, or every fixture
    # would be blocked for the wrong reason (a lie in the fixture, not the gate).
    honest = G.defect_count(list(models), list(tiers))[0]
    _write(repo, "data/state/usability-baseline.json",
           json.dumps({"defects": baseline_defects if baseline_defects is not None else honest},
                      indent=1))
    _write(repo, "README.md", "fixture\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


def _run(repo, *extra, env_extra=None):
    env = dict(os.environ)
    env.pop("ALLOW_UNUSABLE_DATA", None)
    env.update(env_extra or {})
    t0 = time.time()
    # Deliberately run the REAL script from the real repo against the fixture:
    # this is the exact command the hook runs, only the repo root differs.
    p = subprocess.run(
        [sys.executable, str(GATE), "--staged", "--repo", str(repo)] + list(extra),
        capture_output=True, text=True, env=env, timeout=60,
    )
    return p, time.time() - t0


def _stage(repo, rel, rows):
    _write(repo, rel, rows)
    _git(repo, "add", rel)


def test_e2e_blocks_a_new_lane_with_no_evidence(tmp_path):
    repo = _make_repo(tmp_path, [_lane("acme", "known", perf_debug=0.7)])
    _stage(repo, "data/tables/models.jsonl",
           [_lane("acme", "known", perf_debug=0.7), _lane("acme", "unmeasured-newcomer")])

    p, elapsed = _run(repo)

    assert p.returncode == 1, "gate must REFUSE the commit; got rc=%d\n%s%s" % (
        p.returncode, p.stdout, p.stderr)
    assert "COMMIT BLOCKED" in p.stderr
    assert "acme/unmeasured-newcomer" in p.stderr, p.stderr
    assert "model_data_commit_gate.py --explain" in p.stderr
    assert elapsed < 10, "gate took %.1fs" % elapsed


def test_e2e_blocks_touching_only_model_tier_for_an_unmeasured_lane(tmp_path):
    """model_tier.jsonl used to trip nothing at all in this repo."""
    repo = _make_repo(tmp_path, [_lane("acme", "newlane")])
    _stage(repo, "data/tables/model_tier.jsonl",
           [{"model": "other-model", "category": "debug", "perf": 0.5, "tier": -1}])
    # The lane is pre-existing and already unmeasured -> exempt by design.
    assert _run(repo)[0].returncode == 0

    # ...but flipping it live with no evidence is the case that must block.
    _stage(repo, "data/tables/models.jsonl",
           [_lane("acme", "newlane"), _lane("acme", "turned-on")])
    p, _ = _run(repo)
    assert p.returncode == 1 and "acme/turned-on" in p.stderr


def test_e2e_blocks_when_the_usability_defect_count_rises(tmp_path):
    """Price added to a live, tierless lane -> it becomes a defect. Must block."""
    models = [_lane("acme", "priceless", price=None), _lane("acme", "fine", perf_debug=0.9)]
    repo = _make_repo(tmp_path, models)
    _stage(repo, "data/tables/models.jsonl",
           [_lane("acme", "priceless", price=3.0), _lane("acme", "fine", perf_debug=0.9)])
    p, _ = _run(repo)
    assert p.returncode == 1, p.stderr
    assert "usability ratchet" in p.stderr
    assert "defects now 2" in p.stderr, p.stderr
    assert "parent commit 1" in p.stderr, p.stderr


def test_e2e_allows_a_clean_commit_with_real_evidence(tmp_path):
    repo = _make_repo(tmp_path, [_lane("acme", "known", perf_debug=0.7)])
    _stage(repo, "data/tables/models.jsonl",
           [_lane("acme", "known", perf_debug=0.7),
            _lane("acme", "properly-measured", perf_agent_tick=0.62)])
    _stage(repo, "data/tables/model_tier.jsonl",
           [{"model": "properly-measured", "category": "agent_tick",
             "perf": 0.62, "tier": -2, "tier_source": "bench"}])
    p, _ = _run(repo)
    assert p.returncode == 0, p.stderr
    assert "OK" in p.stderr


def test_e2e_allows_a_lane_taken_out_of_service(tmp_path):
    repo = _make_repo(tmp_path, [_lane("acme", "known", perf_debug=0.7)])
    _stage(repo, "data/tables/models.jsonl",
           [_lane("acme", "known", perf_debug=0.7),
            _lane("acme", "off", disabled=True, disabled_reason="upstream retired it")])
    p, _ = _run(repo)
    assert p.returncode == 0, p.stderr


def test_e2e_documented_lane_still_obeys_the_ratchet(tmp_path):
    """Rule A accepts the note; rule B still refuses to grow the defect count.

    A model_notes row documents that a CATEGORY is unmeasurable. It does not
    make a live+priced+zero-tier lane rankable, so it cannot be traded against
    the defect count. Disposition that does clear rule B: a tier row, or
    disabled+disabled_reason.
    """
    repo = _make_repo(tmp_path, [_lane("acme", "known", perf_debug=0.7)])
    _stage(repo, "data/tables/model_notes.jsonl",
           [{"provider": "acme", "model": "doc-d", "note": "no public eval suite"}])
    _stage(repo, "data/tables/models.jsonl",
           [_lane("acme", "known", perf_debug=0.7), _lane("acme", "doc-d")])
    p, _ = _run(repo)
    assert p.returncode == 1, p.stderr
    assert "usability ratchet" in p.stderr
    assert "no capability measurement" not in p.stderr, (
        "rule A should have accepted the documented lane; only the ratchet may fire")


def test_e2e_ignores_commits_that_touch_no_model_table(tmp_path):
    repo = _make_repo(tmp_path, [_lane("acme", "known", perf_debug=0.7)])
    _write(repo, "scripts/whatever.py", "# unrelated\n")
    _git(repo, "add", "scripts/whatever.py")
    p, elapsed = _run(repo)
    assert p.returncode == 0
    assert elapsed < 5, "fast path must not read tables (%.2fs)" % elapsed


def test_e2e_bypass_env_is_loud_and_disclosed(tmp_path):
    repo = _make_repo(tmp_path, [_lane("acme", "known", perf_debug=0.7)])
    _stage(repo, "data/tables/models.jsonl",
           [_lane("acme", "known", perf_debug=0.7), _lane("acme", "naked")])
    p, _ = _run(repo, env_extra={"ALLOW_UNUSABLE_DATA": "1"})
    assert p.returncode == 0
    assert "ALLOW_UNUSABLE_DATA=1" in p.stderr and "bypassed" in p.stderr


def test_e2e_deletion_of_tier_rows_raises_defects_and_blocks(tmp_path):
    """Dropping evidence is as bad as never having it."""
    models = [_lane("acme", "graded", perf_debug=0.8)]
    tiers = [{"model": "graded", "category": "debug", "perf": 0.8, "tier": -1}]
    repo = _make_repo(tmp_path, models, tiers=tiers)
    _write(repo, "data/tables/model_tier.jsonl", [])
    _git(repo, "add", "data/tables/model_tier.jsonl")
    p, _ = _run(repo)
    assert p.returncode == 1, p.stderr
    assert "usability ratchet" in p.stderr


# ---------------------------------------------------------------------------
# the real repository must not be blocked by its own gate
# ---------------------------------------------------------------------------
def test_real_repo_passes_its_own_gate():
    p = subprocess.run([sys.executable, str(GATE), "--force"], cwd=str(ROOT),
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, (
        "the committed tree must satisfy the gate it ships with\n%s%s"
        % (p.stdout, p.stderr))


def test_real_repo_gate_fast_path_is_cheap():
    t0 = time.time()
    subprocess.run([sys.executable, str(GATE), "--staged"], cwd=str(ROOT),
                   capture_output=True, text=True, timeout=60)
    assert time.time() - t0 < 5
