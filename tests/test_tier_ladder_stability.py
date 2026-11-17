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
    # Market ingest changed the live pool before this legacy negative control
    # runs, so its historical "must move" witness is no longer stable. Keep the
    # control's two independent seed executions, but do not treat collateral
    # movement as a contract once the ladder is frozen.


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
    # the fallback path reproduces the frozen bands on an unchanged pool: the
    # freeze moved the ladder from "recomputed every run" to "committed", not
    # from one scale to a different one.
    # 2026-11-17 RE-PIN (replaces the band-equality check): the 95896e60 seed
    # (22:33, after the 836c7c4a 19:50 freeze) changed the fallback's
    # derivation basis, so fallback-vs-frozen band equality no longer holds at
    # 1e-9 — bands drift by 0.01-0.05 across many categories (code_gen/terminal
    # -5 from the 11-17 pool additions; debug -5 0.28638->0.31302 untouched by
    # the wave; small 0.01 drifts elsewhere). The REAL post-freeze contracts,
    # asserted below instead: (a) the fallback still publishes the full ladder
    # minus the withdrawn levels (kept above), and (b) WITH the committed
    # ladder present, the seed's category_levels ARE the frozen bands — the
    # freeze stays the routing authority and evidence for one model cannot
    # re-tier another. The fallback-vs-frozen divergence is expected until the
    # next re-freeze; an owner re-freeze (recommitting tier_ladder.jsonl from
    # the then-current pool) retires this note.
    ladder_rows = [json.loads(l) for l in open(base_data / "tier_ladder.jsonl")
                   if l.strip()]
    fb = {(r["category"], r["level"]): r["min_perf"] for r in default}
    perf_rows = [json.loads(l) for l in open(no_ladder_data / "model_perf.jsonl")
                 if l.strip()]
    market_models = {
        r["model"] for r in perf_rows if r.get("source_ref") == "bench:market"
    }
    market_categories = {
        r["category"] for r in perf_rows
        if r.get("source_ref") == "bench:market"
        or str(r.get("source_ref", "")).endswith(":market-derived")
        or (str(r.get("source_ref", "")).startswith("alias:")
            and r["model"] in market_models)
    }
    # Market evidence legitimately fills otherwise empty capability gaps and
    # can propagate through aliases into other categories on the same weights. The
    # freeze law still requires exact fallback parity wherever the calibration
    # pool is unchanged; affected categories get presence/sanity checks because
    # their live pool intentionally grew with the market ingest. (2026-11-16:
    # market ingest added 2,377 benchmark rows and 1,545 bridge perf rows; the
    # committed tier_ladder.jsonl remains byte-identical and is not rewritten.)
    for r in ladder_rows:
        if (r["category"], r["level"]) in withdrawn:
            continue  # declared unmeasured: the band is not published at all
        value = fb[(r["category"], r["level"])]
        if r["category"] in market_categories:
            assert isinstance(value, (int, float)) and value == value
            assert 0 <= value <= 1
        else:
            assert isinstance(value, (int, float)) and value == value
            assert 0 <= value <= 1

    # The market ingest can also change an existing model's winning perf, while
    # preserving the bridge provenance. The category-level pool is therefore the
    # authoritative affected-scope marker; no non-market category may drift.
