#!/usr/bin/env python3
"""TR-369 capability census — one vocabulary, three surfaces, zero gaps.

Prints STORE / PUBLISH / ACCEPT coverage per capability and exits non-zero on
any UNEXPLAINED gap. Re-runnable (read-only over the committed data):

  STORE    data/tables/models.jsonl carries all 24 perf_* columns + the six
           cap_<family> columns; every live-lane null is explained (a
           null_reasons.jsonl stamp from the AC2 REASONS vocabulary, or the
           census STRUCTURAL designed-absence registry).
  PUBLISH  GET /v1/models (router_server.openai_models_payload) emits an
           additive `capabilities` block on EVERY routable lane carrying the
           category/family keys, context_limit and price + basis; the
           OpenAI-compatible keys {id, object, created, owned_by} are intact.
  ACCEPT   router_spawn accepts each capability family as `family=1`
           (_validate_adhoc), rejects a wrong level and an unknown capability,
           and _build_chain narrows the chain to lanes whose stored value is
           evidentially True.

Exit code: 0 when every surface holds (families with no evidenced lane are an
honest DATA hole — reported, not failed); 1 with the gap list otherwise.
Stdlib only. Env: ROUTING_DATA_DIR (default <repo>/data/tables).
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any

_HERE = os.path.dirname(os.path.realpath(__file__))
_REPO = os.path.dirname(_HERE)
sys.path.insert(0, _HERE)

import capability_vocab as cv  # noqa: E402
import data_null_census as census  # noqa: E402  (is_live, STRUCTURAL, stamps)

TABLES_DIR = os.environ.get("ROUTING_DATA_DIR", os.path.join(_REPO, "data", "tables"))
FIELDS = list(cv.PERF_COLUMNS) + [cv.family_column(f) for f in cv.CAPABILITY_FAMILIES]

_gaps: list = []


def _gap(surface: Any, detail: Any) -> None:
    _gaps.append(f"{surface}: {detail}")
    print(f"  GAP [{surface}] {detail}")


def _load_jsonl(name: Any) -> list:
    path = os.path.join(TABLES_DIR, name)
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def census_store() -> dict:
    print("=== STORE: models.jsonl capability columns (TR-369) ===")
    rows = _load_jsonl("models.jsonl")
    live = [r for r in rows if census.is_live(r)]
    stamps = census.load_stamps()
    structural = set(census.STRUCTURAL.get("models.jsonl", {}))
    print(f"  lanes={len(rows)} live={len(live)} "
          f"perf_categories={len(cv.PERF_CATEGORIES)} families={len(cv.CAPABILITY_FAMILIES)}")
    if len(cv.PERF_CATEGORIES) != 24:
        _gap("store", f"seed category list is {len(cv.PERF_CATEGORIES)}, want 24")
    coverage = {}
    for field in FIELDS:
        evidence = sum(1 for r in rows if r.get(field) is not None)
        live_null = sum(1 for r in live if r.get(field) is None)
        stamped = sum(1 for r in live if r.get(field) is None
                      and (r.get("provider"), r.get("model"), field) in stamps)
        unexplained = sum(1 for r in live if r.get(field) is None
                          and (r.get("provider"), r.get("model"), field) not in stamps
                          and field not in structural)
        missing_key = sum(1 for r in rows if field not in r)
        coverage[field] = {"evidence": evidence, "live_null": live_null,
                           "stamped": stamped, "unexplained": unexplained,
                           "missing_key": missing_key}
        flag = ""
        if unexplained:
            flag = "  GAP"
            _gap("store", f"{field}: {unexplained} live-lane null(s) unexplained "
                          f"(no stamp, not structural)")
        if missing_key:
            flag = "  GAP"
            _gap("store", f"{field}: missing from {missing_key} row(s) — "
                          "the store does not carry the column everywhere")
        print(f"  {field:<22} evidence={evidence:>5} live_null={live_null:>4} "
              f"stamped={stamped:>5} unexplained={unexplained}{flag}")
    return coverage


def census_publish() -> dict:
    print("=== PUBLISH: GET /v1/models capabilities blocks ===")
    import router_server as srv
    payload = srv.openai_models_payload(data_dir=TABLES_DIR, max_age_s=0)
    lanes = payload.get("data") or []
    if not lanes:
        _gap("publish", "no routable lanes published — cannot verify the block")
        return {}
    required = (["categories", "context_limit", "price", "price_basis",
                 "vision", "thinking", "modality", "tool_call"]
                + list(cv.CAPABILITY_FAMILIES))
    missing_block = 0
    missing_keys: Any = {k: 0 for k in required}
    openai_broken = 0
    ctx_price = 0
    fam_true: Any = {f: 0 for f in cv.CAPABILITY_FAMILIES}
    for lane in lanes:
        caps = lane.get("capabilities")
        if not isinstance(caps, dict):
            missing_block += 1
            continue
        if not (lane.get("id") and lane.get("object") == "model"
                and "created" in lane and "owned_by" in lane):
            openai_broken += 1
        for k in required:
            if k not in caps:
                missing_keys[k] += 1
        if caps.get("context_limit") is not None and caps.get("price"):
            ctx_price += 1
        for f in cv.CAPABILITY_FAMILIES:
            if caps.get(f) is True:
                fam_true[f] += 1
    print(f"  routable lanes={len(lanes)} with_capabilities={len(lanes) - missing_block} "
          f"context_limit+price={ctx_price}")
    if missing_block:
        _gap("publish", f"{missing_block} routable lane(s) carry no capabilities block")
    if openai_broken:
        _gap("publish", f"{openai_broken} lane(s) broke the OpenAI-compatible keys")
    if not ctx_price:
        _gap("publish", "no routable lane exposes context_limit AND price")
    for k in required:
        if missing_keys[k]:
            _gap("publish", f"capabilities key {k!r} missing on "
                            f"{missing_keys[k]} lane(s)")
    for f in cv.CAPABILITY_FAMILIES:
        print(f"  {f:<22} published True on {fam_true[f]} lane(s)")
    return {"lanes": len(lanes), "ctx_price": ctx_price, "fam_true": fam_true}


def census_accept() -> dict:
    print("=== ACCEPT: --profile-req capability requirements ===")
    import router_spawn as spawn
    tables = {}
    for name in ("category_levels", "level_defs", "models", "model_tier"):
        path = os.path.join(TABLES_DIR, f"{name}.jsonl")
        tables[name] = [json.loads(l) for l in open(path, encoding="utf-8")
                        if l.strip()] if os.path.exists(path) else []
    ok = 0
    for fam in cv.CAPABILITY_FAMILIES:
        reqs, err = spawn._validate_adhoc([f"{fam}=1"], tables)
        if err or reqs != [(fam, 1)]:
            _gap("accept", f"{fam}=1 rejected: {err}")
        else:
            ok += 1
            print(f"  {fam:<22} accepted as {fam}=1")
    _, err = spawn._validate_adhoc(["web_search=3"], tables)
    if not err:
        _gap("accept", "web_search=3 accepted — capability families are boolean (=1) only")
    else:
        print(f"  wrong level (web_search=3) rejected: {str(err.get('error'))[:72]}")
    _, err = spawn._validate_adhoc(["definitely_not_a_capability=1"], tables)
    if not err:
        _gap("accept", "unknown capability silently accepted (must be a visible error)")
    else:
        print(f"  unknown capability rejected: {str(err.get('error'))[:72]}")
    # narrowing: only families with evidenced lanes can be probed end-to-end
    narrowed = {}
    base = spawn._build_chain(tables, [])
    for fam in cv.CAPABILITY_FAMILIES:
        chain = spawn._build_chain(tables, [(fam, 1)])
        rows_ok = all(m[5].get(cv.family_column(fam)) is True for m in chain)
        strict = len(chain) < len(base)
        narrowed[fam] = {"base": len(base), "narrowed": len(chain),
                         "all_true": rows_ok, "strict_subset": strict}
        if chain and (not rows_ok or not strict):
            _gap("accept", f"{fam}=1 chain of {len(chain)} is not a strict "
                           f"all-True subset of {len(base)}")
        note = "" if chain else "  (no evidenced lane yet — honest data hole, not a contract gap)"
        print(f"  {fam:<22} chain {len(base)} -> {len(chain)} "
              f"all_true={rows_ok}{note}")
    return {"accepted": ok, "narrowed": narrowed}


def main() -> int:
    store = census_store()
    publish = census_publish()
    accept = census_accept()
    unexplained = sum(v["unexplained"] for v in store.values())
    print("=== SUMMARY ===")
    print(f"  store: {len(cv.PERF_CATEGORIES)} categories + "
          f"{len(cv.CAPABILITY_FAMILIES)} families, live-lane unexplained nulls: "
          f"{unexplained}")
    print(f"  publish: {publish.get('lanes', 0)} lanes, "
          f"{publish.get('ctx_price', 0)} with context_limit+price")
    print(f"  accept: {accept.get('accepted', 0)}/6 families accepted as first-class "
          "requirements")
    if _gaps:
        print(f"FAIL: {len(_gaps)} unexplained capability gap(s)")
        return 1
    print("OK: zero unexplained capability gaps across STORE + PUBLISH + ACCEPT")
    return 0


if __name__ == "__main__":
    sys.exit(main())
