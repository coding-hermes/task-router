"""The tier ladder is ABSOLUTE: evidence for one model cannot re-tier another.

Regression pin for the measured defect (2026-11-15, twice in one tick):
  * landing the morph guard evidence moved neuralwatt/glm-5.2-short-fast-flex
    guard -1 -> -2 and flipped the P4_SECURITY head off the $0.725 lane onto
    openrouter/z-ai/glm-5.3 at $1.52;
  * 43 ollama-cloud rows moved qwen3.8-max debug 1 -> 0 and dropped both
    must_include lanes out of adhoc/senior-coding-band.
Both were pure pool-quantile artefacts: the ladder was a function of the live
model_perf pool, so ANY new row re-drew every percentile in its category.

The card's acceptance test is the polarity below: seed the committed tables,
seed them again with ONE extra benchmark row for one model, and diff every tier
row belonging to every OTHER model. Before the ladder was frozen that diff was
non-empty; now it must be empty.
"""
import json
import os
import shutil
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")

SEED_TIMEOUT = 600

#: How far the pool-quantile fallback may sit from the committed (frozen)
#: ladder before it stops being "the same scale". The freeze happened at a
#: fixed point in the pool's history, so every later evidence row moves the
#: recomputed quantiles — measured 2026-11-16: 258 comparable bands, max drift
#: 0.1000 at long_horizon L-2 (0.89 -> 0.99). That drift is legitimate and
#: re-freezing is a reviewed act (router_tier_calibrate.py --write re-tiers
#: hundreds of rows), never something a test forces. So this bound is NOT a
#: drift budget: it is a SCALE check. A ladder that moved to a different scale
#: (percentiles 0..100, a 0..1 vs 0..5 mix, a quantile index instead of a perf
#: floor) lands far outside it; ordinary pool movement does not.
MAX_POOL_DRIFT = 0.25


def _seed(tmp_path, tag, mutate=None, ignore_ladder=False):
    """Seed a private copy of the committed tables; optionally mutate one input."""
    base = tmp_path / tag
    data = base / "data" / "tables"
    data.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(os.path.join(REPO, "data", "tables"), data)
    if mutate:
        mutate(data)
    env = dict(os.environ,
               ROUTING_REGISTRY=str(base / "registry.json"),
               ROUTING_DATA_DIR=str(data),
               ROUTING_NS=str(base / "ns"),
               ROUTING_ALLOW_UNSATISFIABLE="1")
    if ignore_ladder:
        env["ROUTING_TIER_LADDER_IGNORE"] = "1"
    p = subprocess.run([sys.executable, os.path.join(SCRIPTS, "router_seed.py")],
                       capture_output=True, text=True, env=env, timeout=SEED_TIMEOUT)
    if p.returncode != 0:
        pytest.fail(f"seed {tag} failed: {p.stdout[-1500:]}\n{p.stderr[-1500:]}")
    return data, p.stdout


def _tiers(data):
    rows = [json.loads(l) for l in open(data / "model_tier.jsonl") if l.strip()]
    return {(r["model"], r["category"], r["reasoning_effort"]): r["tier"] for r in rows}


def test_ladder_file_is_what_the_seed_reads():
    """The committed ladder exists, covers every category at default effort, and
    the seed's reader agrees with it line for line (a silent rename/typo in the
    file name would otherwise degrade the whole registry to pool quantiles)."""
    ladder = os.path.join(REPO, "data", "tables", "tier_ladder.jsonl")
    assert os.path.exists(ladder), (
        "data/tables/tier_ladder.jsonl is the frozen ladder — without it every "
        "category falls back to pool quantiles and tiers move whenever evidence lands")
    rows = [json.loads(l) for l in open(ladder) if l.strip()]
    cats = {r["category"] for r in rows}
    levels = {r["level"] for r in rows}
    assert levels == set(range(-5, 6)), f"ladder levels {sorted(levels)} != -5..+5"
    assert all((r.get("reasoning_effort") or "default") == "default" for r in rows)
    assert len(cats) == 24, f"ladder covers {len(cats)} categories, expected 24"
    # the seed must actually READ it — a renamed constant or a dropped read would
    # silently degrade the whole registry to pool quantiles (the behaviour the
    # seed prints "NO frozen ladder" for). Checked as SOURCE TEXT on purpose:
    # importing router_seed would EXECUTE the seed in-process, which is exactly
    # the kind of module-import side effect that poisons a later test.
    seed_src = open(os.path.join(SCRIPTS, "router_seed.py"), encoding="utf-8").read()
    assert "'tier_ladder.jsonl'" in seed_src, (
        "router_seed no longer points at the committed ladder file")
    assert "FROM tier_ladder" in seed_src, (
        "router_seed no longer derives category_levels from the frozen ladder")


def _pick_target(data):
    """A live model with no perf row in `creative` — a NEW measurement landing.

    `creative` is the measured teeth: its pool has 2 distinct perf values over 40
    families, so a single 1.0 row re-drew its quantiles enough to move 40
    unrelated model rows on the quantile path (longcat-2.0 creative 5 -> 3).
    """
    perf = {(r["model"], r["category"])
            for r in (json.loads(l) for l in open(data / "model_perf.jsonl") if l.strip())}
    models = [json.loads(l) for l in open(data / "models.jsonl") if l.strip()]
    live = [m["model"] for m in models
            if not m.get("archive") and m.get("valid_to") is None and not m.get("disabled")]
    return next(m for m in live if (m, "creative") not in perf)


def _append_row(data, target):
    with open(data / "benchmarks.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "model": target, "category": "creative", "score": 1.0,
            "max_score": 1.0, "source": "tier-stability-invariance-probe",
            "valid_from": "2026-11-15", "reasoning_effort": "default"}) + "\n")


def test_evidence_for_one_model_never_moves_another_models_tier(tmp_path):
    """THE ACCEPTANCE TEST: +1 benchmark row for model A leaves model B alone."""
    base_data, _ = _seed(tmp_path, "base")
    target_model = _pick_target(base_data)

    plus_data, out = _seed(tmp_path, "plus", lambda d: _append_row(d, target_model))
    assert "tier ladder: 24 frozen absolute pair(s)" in out, (
        "the seed did not read the frozen ladder — this test would be vacuous")

    before, after = _tiers(base_data), _tiers(plus_data)
    assert (target_model, "creative", "default") not in before, (
        f"{target_model}/creative already had a perf row; the probe row "
        "would not be a NEW measurement")
    assert (target_model, "creative", "default") in after, (
        "the synthetic benchmark row did not reach model_tier — the invariance "
        "assertion below would pass for the wrong reason")

    collateral = {k: (before.get(k), after.get(k))
                  for k in set(before) | set(after)
                  if k[0] != target_model and before.get(k) != after.get(k)}
    assert not collateral, (
        f"one benchmark row for {target_model} re-tiered {len(collateral)} "
        f"other model row(s): {dict(list(collateral.items())[:5])}")


def test_quantile_path_would_move_other_models_so_the_pin_has_teeth(tmp_path):
    """Negative control for the test above.

    The same single synthetic row, seeded with the ladder IGNORED (the seed's
    pre-ladder behaviour), re-tiers other models — measured 40 rows, longcat-2.0
    creative 5 -> 3. If this ever stops failing, the invariance test above has
    gone vacuous (the probe row no longer bites) and must be re-aimed.
    """
    base_data, _ = _seed(tmp_path, "qbase", ignore_ladder=True)
    target_model = _pick_target(base_data)
    plus_data, out = _seed(tmp_path, "qplus",
                           lambda d: _append_row(d, target_model), ignore_ladder=True)
    assert "IGNORED" in out

    before, after = _tiers(base_data), _tiers(plus_data)
    collateral = [k for k in set(before) | set(after)
                  if k[0] != target_model and before.get(k) != after.get(k)]
    assert collateral, (
        "the synthetic row no longer shifts the pool quantiles — the invariance "
        "pin above now passes vacuously; pick a target category with a smaller "
        "distinct-perf population")


def test_quantile_fallback_still_exists_when_the_ladder_is_absent(tmp_path):
    """The ladder is a PREFERENCE, not a hard requirement: with no ladder file the
    seed must still produce a full 11-level ladder per category from the pool
    (otherwise a fresh clone could not seed at all)."""
    base_data, _ = _seed(tmp_path, "nl")
    ladder = base_data / "tier_ladder.jsonl"
    assert ladder.exists()

    def drop(data):
        ladder = data / "tier_ladder.jsonl"
        if ladder.exists():
            ladder.unlink()

    no_ladder_data, out = _seed(tmp_path, "noladder", drop)
    assert "NO frozen ladder" in out
    assert "pool quantiles" in out

    rows = [json.loads(l) for l in open(no_ladder_data / "category_levels.jsonl")
            if l.strip()]
    default = [r for r in rows if (r.get("reasoning_effort") or "default") == "default"]
    # A DECLARED-unmeasured range is withdrawn from the domain on purpose
    # (data/tables/unmeasured_categories.jsonl, applied by the seed), so the
    # full 11-level ladder is expected MINUS exactly those levels — and never
    # minus anything else. Without this the assertion would demand the seed
    # publish a rating no routable lane can serve.
    decl_path = no_ladder_data / "unmeasured_categories.jsonl"
    decl = [json.loads(l) for l in open(decl_path) if l.strip()] if decl_path.exists() else []
    withdrawn = set()
    for d in decl:
        lo = int(d.get("min_level") if d.get("min_level") is not None else 0)
        hi = d.get("max_level")
        hi = int(hi) if hi is not None else 5
        withdrawn |= {(d["category"], lvl) for lvl in range(lo, hi + 1)}
    assert len(default) == 24 * 11 - len(withdrawn)
    assert not [r for r in default if (r["category"], r["level"]) in withdrawn]
    # The fallback path is the SAME SCALE as the frozen ladder, not a different
    # one: the freeze moved the ladder from "recomputed every run" to
    # "committed", it did not move the scale. Exact band equality is only true
    # at freeze time — the committed ladder was derived from the pool as it
    # stood then, and every evidence row that has landed since shifts the
    # quantiles (measured 2026-11-16: 21 bands moved, max 0.1000 at
    # long_horizon L-2). Re-freezing is a DELIBERATE act with a blast radius
    # (scripts/router_tier_calibrate.py --write printed 370 tier rows moving),
    # so this test must not demand it: it pins coverage, scale and ordering,
    # and prints the drift so a maintainer sees it growing.
    ladder_rows = [json.loads(l) for l in open(base_data / "tier_ladder.jsonl")
                   if l.strip()]
    fb = {(r["category"], r["level"]): r["min_perf"] for r in default}
    drift = []
    for r in ladder_rows:
        key = (r["category"], r["level"])
        if key in withdrawn:
            continue  # declared unmeasured: the band is not published at all
        assert key in fb, f"the fallback published no band for {key}"
        assert 0.0 <= fb[key] <= 1.0, f"{key}: fallback band {fb[key]} off-scale"
        drift.append((abs(fb[key] - r["min_perf"]), key, r["min_perf"], fb[key]))
    assert drift, "no comparable bands — the fallback published nothing"
    worst = max(drift)
    print(f"fallback vs frozen ladder: {len(drift)} band(s), max drift "
          f"{worst[0]:.4f} at {worst[1]} ({worst[2]} -> {worst[3]})")
    assert worst[0] <= MAX_POOL_DRIFT, (
        f"the pool-quantile fallback has drifted {worst[0]:.4f} from the frozen "
        f"ladder at {worst[1]} ({worst[2]} -> {worst[3]}), beyond the "
        f"{MAX_POOL_DRIFT} bound: either the pool moved a lot (re-freeze with "
        f"scripts/router_tier_calibrate.py --write, a reviewed act) or the "
        f"fallback is no longer on the frozen scale")
    # Ordering is the part that must never drift: a higher level can never be
    # easier to reach than a lower one, on either path.
    by_cat = {}
    for (cat, lvl), perf in fb.items():
        by_cat.setdefault(cat, []).append((lvl, perf))
    for cat, pairs in by_cat.items():
        pairs.sort()
        for (lo_l, lo_p), (hi_l, hi_p) in zip(pairs, pairs[1:]):
            assert hi_p >= lo_p - 1e-12, (
                f"{cat}: fallback ladder is not monotone — level {hi_l} "
                f"({hi_p}) is easier than level {lo_l} ({lo_p})")
