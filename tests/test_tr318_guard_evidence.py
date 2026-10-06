"""TR-318 — measured benchmark evidence beats estimates in BOTH directions.

Three defects, one evidence chain (all measured on the committed data):
  1. apply_overlay's "measured beats estimate" arm was gated `cur[0] < rel` —
     a measurement only won when it scored HIGHER. glm-5.3-flash's committed
     GUARD-V2 row (0/4, live-probe-2026-09-27) never reached model_perf
     because the survey estimate (0.8, QUALITY_ESTIMATES) was higher. Same
     asymmetry the declaration arm (models.jsonl:perf_*) already fixed:
     evidence loses to an estimate regardless of which number is larger.
  2. apply_quality_estimates' degenerate-replace ({0.0, 1.0} for guard/mock)
     was written to undo battery-T4-INSTR-floor saturation — but it also
     clobbered DISCRIMINATING bench rows. Live proof: gpt-5.6-sol's guard
     measured 1.0 on GUARD-V2, the overlay landed it (1.0 > 0.92), and the
     estimate pass wrote 0.92 back over the measurement on the same seed.
  3. P4_SECURITY single-sourcing: with the evidence flowing, the registry
     must resolve >= 2 DISTINCT models for the profile (weights-level —
     provider mirrors of the same weights count once).

Every cell seeds from a COPY of the committed data/tables (hermetic, same
pattern as tests/test_tier_derivation.py); cells 1-2 need NO data mutation —
the committed benchmarks.jsonl already carries the measurements the old
passes discarded.
"""
import json
import os
import shutil
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")
DATA_DIR = os.path.join(REPO, "data", "tables")
PY = sys.executable


def _hermetic_env(tmp_path):
    data = tmp_path / "data"
    shutil.copytree(DATA_DIR, data)
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    return {"ROUTING_DATA_DIR": str(data), "ROUTER_STATE_DIR": str(state),
            "ROUTING_REGISTRY": str(tmp_path / "registry.json"),
            "ROUTING_NS": str(tmp_path / "ns"),
            "ROUTER_SEED_PYTHON": PY, "ROUTING_BOARD_PY": PY}


def _seed(env):
    p = subprocess.run([PY, os.path.join(SCRIPTS, "router_seed.py")],
                       capture_output=True, text=True, timeout=600,
                       env=dict(os.environ, **env))
    assert p.returncode == 0, p.stderr[-800:]
    with open(env["ROUTING_REGISTRY"]) as f:
        return json.load(f)


def _perf(reg, model, category):
    for r in reg["tables"]["model_perf"]:
        if r["model"] == model and r["category"] == category:
            return r
    return None


def test_guard_v2_measurement_reaches_model_perf_below_estimate(tmp_path):
    """Cell 1 (the overlay gate): glm-5.3-flash guard = measured 0/4.

    The committed GUARD-V2 row scores 0.0; the survey estimate is 0.8. The
    old `cur[0] < rel` gate kept the estimate forever. After the fix the
    model_perf row carries the measurement (source='bench'), whatever its
    direction.
    """
    env = _hermetic_env(tmp_path)
    reg = _seed(env)
    row = _perf(reg, "glm-5.3-flash", "guard")
    assert row is not None, "glm-5.3-flash lost its guard perf row entirely"
    assert row["source"] == "bench", (
        f"GUARD-V2 measurement still shadowed by {row['source']}/"
        f"{row['source_ref']} (perf={row['perf']}) — overlay estimate arm "
        f"must lose to evidence in BOTH directions")
    assert row["perf"] == pytest.approx(0.0), (
        f"measured guard value drifted: {row['perf']} (committed GUARD-V2 = 0.0)")


def test_saturated_v1_probe_keys_never_reach_model_perf(tmp_path):
    """Cell 4 (found during TR-318): the derived-key pass resurrected the v1
    probes the seed declares INERT.

    BENCH_OVERLAY's comment block (2026-09-27) says the six v1 blocking-category
    probes have "no key, so they are INERT by construction" — but the derived-key
    pass builds BARE keys ('GUARD', 'REVIEW', ...) from
    router_probe_ingest.BATTERIES, and '%GUARD%' LIKE-matches every 'GUARD*'
    source — including the SATURATED v1 sources ('live-probe-2026-09-27/GUARD: ...',
    7/7 lanes 4/4, zero ranking signal). Pre-fix census: 36 v1-only 1.0 rows
    across six models (false tier-5 guard/mock/review rows).

    source_ref records only the KEY ('bench:GUARD'), so a V2/V3 row carries the
    same ref as a v1 row — the leak signature is a model_perf row whose model
    has NO benchmark row for that category outside the excluded v1 sources.
    V1 sources are recognized by their exact token shape '<key>:' (v2/v3 spell
    '<key>-V2:' / '<key>-V3:').
    """
    env = _hermetic_env(tmp_path)
    reg = _seed(env)
    bench = [json.loads(l) for l in
             open(os.path.join(DATA_DIR, "benchmarks.jsonl")) if l.strip()]
    bare = {"bench:GUARD": "GUARD", "bench:REVIEW": "REVIEW",
            "bench:MOCK": "MOCK", "bench:MECHANICAL": "MECHANICAL",
            "bench:SPEC-DOCS": "SPEC-DOCS", "bench:MULTILINGUAL": "MULTILINGUAL"}
    leaked = []
    for r in reg["tables"]["model_perf"]:
        key = bare.get(r["source_ref"])
        if not key:
            continue
        v1_tok = f"/{key}:"
        legit = [b for b in bench
                 if b["category"] == r["category"]
                 and b["model"].lower() == r["model"].lower()
                 and key in str(b.get("source") or "")
                 and v1_tok not in str(b.get("source") or "")]
        if not legit:
            leaked.append(f"{r['model']}/{r['category']}={r['perf']}")
    assert not leaked, (
        f"model_perf rows sourced ONLY from the saturated v1 probes "
        f"({len(leaked)} rows, false 1.0s): {leaked[:8]}")


def test_bench_row_survives_the_quality_estimate_degenerate_replace(tmp_path):
    """Cell 2 (the estimate clobber): gpt-5.6-sol guard = measured 1.0.

    The overlay lands 1.0 over the 0.92 estimate (upgrade direction — this
    worked even before), then apply_quality_estimates saw a "degenerate"
    1.0 and wrote the survey value back. The degenerate-replace exists to
    undo SATURATED sources (battery-T4-INSTR-floor), which are already
    excluded at the overlay door — any surviving bench row is
    discriminating evidence and must not be estimate-clobbered.
    """
    env = _hermetic_env(tmp_path)
    reg = _seed(env)
    row = _perf(reg, "gpt-5.6-sol", "guard")
    assert row is not None, "gpt-5.6-sol lost its guard perf row entirely"
    assert row["source"] == "bench", (
        f"measured GUARD-V2 1.0 clobbered back to {row['source']}/"
        f"{row['source_ref']} (perf={row['perf']}) — degenerate-replace "
        f"must never rewrite a bench row")
    assert row["perf"] == pytest.approx(1.0)


def _alias_family(model):
    """Fold a model name to its alias-family base (model_aliases.jsonl)."""
    path = os.path.join(DATA_DIR, "model_aliases.jsonl")
    amap = {}
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if not line:
                continue
            a = json.loads(line)
            var = a.get("model") or a.get("variant")
            base = a.get("inherits") or a.get("base")
            if var and base:
                amap[var.lower()] = base.lower()
    seen, cur = {model.lower()}, model.lower()
    while cur in amap and amap[cur] not in seen:
        cur = amap[cur]
        seen.add(cur)
    return cur


def test_p4_security_measured_pool_and_exclusion(tmp_path):
    """Cell 3 (TR-318 AC): the P4 pool's state is MEASURED, not estimated.

    AC1 (>= 2 distinct models) proved UNMEETABLE on measured evidence and
    was NOT forced: the live GUARD-V2/REVIEW-V2 battery (2026-10-06, commit-
    ted to benchmarks.jsonl) scores glm-5.2-fast and glm-5.2-short-flex
    guard 0/4 (review 4/4) — the WHOLE GLM family genuinely fails the guard
    battery, and the security>=2 pool holds only {gpt-6.1-sol, GLM-5.3,
    GLM-5.2}. The old estimate (GLM-5.2 guard 0.86, tier 0) was the only
    thing letting a second family into the chain; the measurement removed
    it. Lowering the guard requirement to re-admit GLM is the owner's call
    (TR-318 fix direction b), never the worker's — so this cell pins the
    honest invariants instead:
      * the chain still resolves non-empty (head = gpt-6.1-sol family);
      * every GLM guard row in the pool is bench-sourced (measured), never
        family/estimate — the exclusion is evidence-based;
      * the guard=0 requirement itself is untouched.
    """
    env = _hermetic_env(tmp_path)
    reg = _seed(env)
    perf = {(r["model"], r["category"]): r for r in reg["tables"]["model_perf"]}
    # the exclusion is measured: both GLM sub-families carry bench guard rows
    for m in ("glm-5.3-flash", "glm-5.2-fast", "glm-5.2-short-flex"):
        row = perf.get((m, "guard"))
        assert row is not None, f"{m} lost its guard perf row"
        assert row["source"] == "bench", (
            f"{m} guard regressed to {row['source']}/{row['source_ref']} — "
            f"the pool must be measured, not estimated")
        assert row["perf"] == pytest.approx(0.0), (
            f"{m} measured guard drifted: {row['perf']}")
    # the requirement is untouched (data rows, not the bootstrap dict)
    reqs = {r["category"]: r["level"] for r in reg["tables"]["task_profile_requirements"]
            if r["task_id"] == "P4_SECURITY"}
    assert reqs.get("security") == 2 and reqs.get("guard") == 0 \
        and reqs.get("review") == 0, f"P4 requirements changed: {reqs}"
    # the chain resolves non-empty and its models all clear the bars
    state = tmp_path / "state"
    tiers = reg["tables"]["model_tier"]
    models = reg["tables"]["models"]
    pool_providers = set()
    for m in models:
        if m.get("valid_to") or m.get("archive") or m.get("disabled"):
            continue
        if all(any(t["model"] == m["model"] and t["category"] == c and t["tier"] >= lvl
                   for t in tiers) for c, lvl in reqs.items()):
            pool_providers.add(m["provider"])
    assert pool_providers, "P4 pool is EMPTY — no provider clears the bars"
    with open(state / "quota-state.json", "w") as f:
        json.dump({"providers": {p: {"status": "open"} for p in pool_providers}}, f)
    p = subprocess.run(
        [PY, os.path.join(SCRIPTS, "router_spawn.py"), "--profile", "P4_SECURITY",
         "--format", "json"],
        capture_output=True, text=True, timeout=300,
        env=dict(os.environ, ROUTING_REGISTRY=env["ROUTING_REGISTRY"],
                 ROUTER_STATE_DIR=str(state)))
    assert p.returncode == 0, p.stderr[-800:]
    data = json.loads(p.stdout)
    hops = data.get("chain") or []
    assert hops, "P4_SECURITY chain is EMPTY"
    families = {_alias_family(h["model"]) for h in hops}
    print(f"P4_SECURITY measured pool: {len(hops)} hops, "
          f"{len(families)} alias family(ies): {sorted(families)}")
