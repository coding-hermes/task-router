"""Rating satisfiability — TR-383.

The router derives a rating (a set of category→level requirements) and then looks
for a lane that holds every required capability. When nothing holds them, the
request dies at tick time with a 503 whose body says:

    {"error": "no chain — no eligible model and no fallback lane could serve",
     "failure_reason": "unservable-rating", "chain_length": 0, "ladder": []}

That message is true and useless: it names neither the capability nobody holds
nor the lane that came closest. Measured 2026-11-03 19:18:49, four code-factory
satellite ticks died this way and the only actionable clue in the whole payload
was a clamp note.

This module answers both halves of the row in ONE place, from the same table
data the resolver uses:

  * seed time (`satisfiability_findings`) — over the classifier's whole emit
    domain, which (category, level) pairs no routable lane can serve, and which
    profiles demand them. A seed run that finds any must NOT exit 0.
  * request time (`diagnose_request`) — for one concrete rating, the missing
    capabilities with the best tier any lane holds, and the single closest lane
    (fewest unmet categories, then smallest shortfall) so an operator knows
    whether the fleet is one model away or a whole class away.

WHY "routable" MATTERS. A lane that holds a capability but cannot be routed must
not count as satisfying it. On 2026-11-03 the only model in the registry with any
`math` tier was `qwen3.5:397b` (tier 5, bench AIME26) and its model row carried
`disabled: true` and `normalized_price: null` — unroutable. So all eleven `math`
levels in the emit domain were unsatisfiable, and `P0_FORE`, which requires
`math >= -5`, could not be served by any lane at all. Counting that disabled lane
as satisfaction would have hidden the defect forever.

CLI
    python3 scripts/router_satisfiability.py [--data-home DIR] [--json]

Exit codes
    0  every rating in the domain is satisfiable by at least one routable lane
    1  at least one unsatisfiable rating — named in the output
    2  cannot read the tables
"""
from __future__ import annotations

import json
import os
import sys

# TR-202: realpath, not the abspath-of-file idiom — the same symlink-topology
# rule the installed runtime scripts follow, applied here so this module is safe
# to install later. (The literal spelling is banned repo-wide.)
DEFAULT_DATA_HOME = os.path.join(os.path.dirname(os.path.realpath(__file__)), "..")

#: Where the resolver looks for its tables, relative to the data home.
TABLE_DIR = "data/tables"


def _read_jsonl(path):
    rows = []
    try:
        with open(path) as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    rows.append(json.loads(ln))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return None
    return rows


def load_tables(data_home=None, table_dir=None):
    """Load the four tables the satisfiability question needs.

    `table_dir` wins when the caller already holds the exact directory the
    resolver reads (`ROUTING_DATA_DIR` — which is NOT always <home>/data/tables:
    the suite points it at a bare `.../data`, and a caller that reconstructed the
    path from it missed by one level and reported "cannot read table").

    Returns a dict; any table that cannot be read is None so callers can refuse
    loudly instead of scoring against an empty set (an empty tier table would
    report "everything is satisfiable", the exact lie this module exists to
    prevent).
    """
    base = table_dir or os.path.join(data_home or DEFAULT_DATA_HOME, TABLE_DIR)
    return {
        "models": _read_jsonl(os.path.join(base, "models.jsonl")),
        "tiers": _read_jsonl(os.path.join(base, "model_tier.jsonl")),
        "domain": _read_jsonl(os.path.join(base, "category_levels.jsonl")),
        "requirements": _read_jsonl(
            os.path.join(base, "task_profile_requirements.jsonl")),
        "profiles": _read_jsonl(os.path.join(base, "task_profiles.jsonl")),
    }


def is_routable(model_row):
    """The three conditions the resolver applies before a lane can enter a ladder.

    Every one of them is load-bearing: an archived row is a retired lane, a
    disabled row is a deliberate off (vendor shutdown, dead key), and an unpriced
    row cannot be ranked or budgeted. A capability held only by such a lane is
    NOT satisfaction.
    """
    if not model_row:
        return False
    if model_row.get("archive") or model_row.get("disabled"):
        return False
    if model_row.get("retired") or model_row.get("status") in (
            "retired", "disabled", "archive"):
        return False
    if model_row.get("normalized_price") is None:
        return False
    return True


#: The resolver's documented BLANK default (router_spawn.py, Bane 2026-08-27):
#: a lane with no tier row for a category reads as -1, NEVER 0 and never an
#: inflated neutral. This module must use the SAME default or it will report
#: ratings the router serves perfectly well — e.g. `math >= -5` is satisfied by
#: every lane that simply has no math tier.
MISSING_TIER_DEFAULT = -1


def effective_tier(tier_map, category):
    """The tier the RESOLVER would use for this category on this lane."""
    t = (tier_map or {}).get(category)
    return MISSING_TIER_DEFAULT if t is None else t


def _best_tiers(rows):
    """(model, category) -> the BEST tier across reasoning efforts (TR-398).

    model_tier rows are now per (model, category, reasoning_effort). The
    satisfiability question is "can ANY effort of a routable lane serve this
    rating", so per-category we keep the max instead of whichever effort row
    came last (the old last-write-wins was fine when every row was 'default'
    and stays byte-identical for such corpora). 'default' is never ranked as
    a level here — it competes by its own tier value only.
    """
    out = {}
    for r in rows or []:
        t = r.get("tier")
        if t is None:
            continue
        cats = out.setdefault(r.get("model"), {})
        c = r.get("category")
        if cats.get(c) is None or t > cats[c]:
            cats[c] = t
    return out


def capability_index(tables):
    """category -> {best_tier, holders[], blocked_holders[]} over the registry.

    `blocked_holders` are lanes that DO hold the capability but cannot be routed;
    they are what turns a silent gap into an explainable one ("the only lane with
    a math tier is disabled and unpriced").
    """
    tier_map = _best_tiers(tables.get("tiers"))

    model_rows = {}
    for m in tables.get("models") or []:
        model_rows.setdefault(m.get("model"), []).append(m)

    idx = {}
    routable_models = {m.get("model") for m in (tables.get("models") or [])
                       if is_routable(m)}
    for model, cats in tier_map.items():
        rows = model_rows.get(model) or []
        routable_rows = [r for r in rows if is_routable(r)]
        for cat, t in cats.items():
            ent = idx.setdefault(cat, {"best_tier": None, "holders": [],
                                       "blocked_holders": []})
            if routable_rows:
                if ent["best_tier"] is None or t > ent["best_tier"]:
                    ent["best_tier"] = t
                ent["holders"].append(
                    {"provider": routable_rows[0].get("provider"),
                     "model": model, "tier": t})
            else:
                why = []
                row = rows[0] if rows else {}
                if row.get("disabled"):
                    why.append("disabled")
                if row.get("archive"):
                    why.append("archived")
                if row.get("retired") or row.get("status") in ("retired", "disabled", "archive"):
                    why.append("retired")
                if row.get("normalized_price") is None:
                    why.append("unpriced")
                if not rows:
                    why.append("no model row")
                ent["blocked_holders"].append(
                    {"provider": row.get("provider"), "model": model,
                     "tier": t, "why": why or ["unroutable"]})
    # A routable lane with NO row for this category still serves
    # MISSING_TIER_DEFAULT, so the floor of EVERY category is at least that value
    # — including categories that have no tier row anywhere. Without this, the
    # live `math` gap reported best_tier=None ("no lane holds it at any level")
    # when the truth is "-1 by blank default, and only level 0+ is unreachable".
    if routable_models:
        known = set(idx) | {r.get("category") for r in (tables.get("domain") or [])}
        known |= {r.get("category") for r in (tables.get("requirements") or [])}
        for cat in known:
            if cat is None:
                continue
            ent = idx.setdefault(cat, {"best_tier": None, "holders": [],
                                       "blocked_holders": []})
            if ent["best_tier"] is None or ent["best_tier"] < MISSING_TIER_DEFAULT:
                ent["best_tier"] = MISSING_TIER_DEFAULT
    return idx


def satisfiability_findings(tables, index=None):
    """Every (category, level) in the classifier's emit domain that no routable
    lane can serve, with the profiles that demand it.

    Returns a list of dicts:
        {category, level, best_tier, blocked_holders, required_by}
    sorted by category then level, so two runs of the same data print the same
    report.
    """
    idx = index if index is not None else capability_index(tables)
    req_by_pair = {}
    for r in tables.get("requirements") or []:
        key = (r.get("category"), r.get("level"))
        req_by_pair.setdefault(key, []).append(r.get("task_id"))

    findings = []
    for row in tables.get("domain") or []:
        cat, lvl = row.get("category"), row.get("level")
        if cat is None or lvl is None:
            continue
        ent = idx.get(cat) or {"best_tier": None, "holders": [], "blocked_holders": []}
        best = ent.get("best_tier")
        if best is not None and best >= lvl:
            continue
        findings.append({
            "category": cat,
            "level": lvl,
            "best_tier": best,
            "blocked_holders": ent.get("blocked_holders") or [],
            "required_by": sorted(set(req_by_pair.get((cat, lvl)) or [])),
        })
    findings.sort(key=lambda f: (f["category"], f["level"]))
    return findings


def summarise(findings):
    """Group the findings so a report names each category ONCE with its levels.

    A 264-pair domain that is broken in one category should produce one line, not
    eleven — a wall of lines is how a real gap gets skimmed past.
    """
    by_cat = {}
    for f in findings:
        ent = by_cat.setdefault(f["category"], {
            "category": f["category"], "levels": [], "best_tier": f["best_tier"],
            "blocked_holders": f["blocked_holders"], "required_by": set()})
        ent["levels"].append(f["level"])
        ent["required_by"].update(f["required_by"])
    out = []
    for cat, ent in sorted(by_cat.items()):
        ent["levels"].sort()
        ent["required_by"] = sorted(ent["required_by"])
        best = ent["best_tier"]
        if best is None:
            reason = "no routable lane holds this capability at any level"
        elif best == MISSING_TIER_DEFAULT:
            reason = (f"no lane MEASURES this capability, so every lane falls back "
                      f"to the blank default tier {MISSING_TIER_DEFAULT}, below the "
                      f"required level(s)")
        else:
            reason = f"best routable tier is {best}, below the required level(s)"
        blocked = "; ".join(
            f"{b.get('provider')}/{b.get('model')} tier={b.get('tier')} "
            f"({', '.join(b.get('why') or [])})"
            for b in (ent.get("blocked_holders") or [])[:3])
        ent["reason"] = reason + (f" — blocked holder(s): {blocked}" if blocked else "")
        out.append(ent)
    return out


def diagnose_request(reqs, tables, index=None):
    """Explain ONE unsatisfiable rating (TR-383 request half).

    `reqs` is the resolver's own requirement list: [(category, level), ...].

    Returns:
        {"missing": [{category, need, best_tier}],
         "closest": {"provider", "model", "tier_map", "unmet": [...]} | None,
         "summary": "<one line naming the gap and the nearest lane>"}

    The closest lane is the one with the FEWEST unmet categories, then the
    smallest total shortfall, restricted to routable lanes. That is the lane an
    operator would ask about first — "what was the closest thing we had?".
    """
    idx = index if index is not None else capability_index(tables)
    reqs = [(c, l) for c, l in (reqs or [])]
    missing = []
    for cat, lvl in reqs:
        ent = idx.get(cat) or {}
        best = ent.get("best_tier")
        if best is None or best < lvl:
            missing.append({"category": cat, "need": lvl, "best_tier": best})

    # Per-lane shortfall over routable lanes only.
    tier_map = _best_tiers(tables.get("tiers"))
    routable = {}
    for m in tables.get("models") or []:
        if is_routable(m):
            routable.setdefault(m.get("model"), m)

    closest = None
    for model, cats in tier_map.items():
        row = routable.get(model)
        if not row:
            continue
        unmet, shortfall = [], 0
        for cat, lvl in reqs:
            have = effective_tier(cats, cat)
            if have < lvl:
                unmet.append((cat, lvl, have))
                shortfall += (lvl - have)
        if not unmet:
            continue  # this lane could serve it; not the "closest miss"
        cand = {"provider": row.get("provider"), "model": model,
                "unmet": unmet, "shortfall": shortfall,
                "met_categories": len(reqs) - len(unmet),
                "tier_map": cats}
        if (closest is None
                or len(cand["unmet"]) < len(closest["unmet"])
                or (len(cand["unmet"]) == len(closest["unmet"])
                    and cand["shortfall"] < closest["shortfall"])):
            closest = cand

    parts = []
    for m in missing:
        need = m["need"]
        best = m["best_tier"]
        if best is None:
            parts.append(f"no routable lane holds {m['category']} at any level "
                         f"(need {need})")
        else:
            parts.append(f"{m['category']} needs {need}, best routable tier is {best}")
    if closest:
        parts.append(
            f"closest lane {closest['provider']}/{closest['model']} meets "
            f"{closest['met_categories']}/{len(reqs)} requirement(s); unmet: "
            f"{[(c, l, h) for c, l, h in closest['unmet']]}")
    else:
        parts.append("no routable lane meets any part of this rating")
    return {"missing": missing, "closest": closest,
            "summary": "; ".join(parts)}


def _main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    data_home = None
    as_json = False
    while argv:
        a = argv.pop(0)
        if a == "--data-home":
            data_home = argv.pop(0)
        elif a == "--json":
            as_json = True
        elif a in ("-h", "--help"):
            print(__doc__)
            return 0
        else:
            print(f"router_satisfiability: unknown argument: {a}", file=sys.stderr)
            return 2

    tables = load_tables(data_home)
    missing_tables = [k for k, v in tables.items()
                      if v is None and k in ("models", "tiers", "domain")]
    if missing_tables:
        print(f"SATISFIABILITY: cannot read table(s) {missing_tables} under "
              f"{data_home or DEFAULT_DATA_HOME}/{TABLE_DIR}", file=sys.stderr)
        return 2

    findings = satisfiability_findings(tables)
    groups = summarise(findings)
    if as_json:
        print(json.dumps({"findings": findings, "groups": groups}, indent=2))
        return 1 if findings else 0

    if not findings:
        n = len(tables.get("domain") or [])
        print(f"SATISFIABILITY OK: all {n} rating(s) in the emit domain are "
              f"servable by at least one routable lane")
        return 0

    print(f"SATISFIABILITY FAIL: {len(findings)} of {len(tables.get('domain') or [])} "
          f"rating(s) in the emit domain have NO routable lane, across "
          f"{len(groups)} categor{'y' if len(groups) == 1 else 'ies'}")
    for g in groups:
        levels = g["levels"]
        span = f"levels {levels[0]}..{levels[-1]}" if len(levels) > 1 else f"level {levels[0]}"
        print(f"  {g['category']}: {span} — {g['reason']}")
        if g["required_by"]:
            print(f"     required by profile(s): {', '.join(g['required_by'])}")
    return 1


if __name__ == "__main__":
    sys.exit(_main())
