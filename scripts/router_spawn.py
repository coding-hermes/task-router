#!/usr/bin/env python3
"""router_spawn.py — THE TASK ROUTER (runtime lookup for the fleet).

Resolves a project (or ad-hoc profile) to the chain of (provider, model) pairs the
scheduler/foreman may use, cross-checked against live gate state:

  quota-state.json   — provider policy gates (GATED = blocked, reason)
                       + optional "diversity" knobs (TR-007):
                         {"max_consecutive_per_provider": N|null,
                          "max_total_per_provider":       N|null,
                          "model_concurrency_limit":      N|null}
                       + optional per-model limits (TR-007):
                         "models": {"<provider>/<model>": {"concurrency_limit": N}}
  health-state.json  — hourly provider/model ping results (DOWN/SLOW = skip)
  circuit-state.json — circuit breakers (open_until in future = skip)

Diversity (TR-007, Bane design): two knobs applied as PRUNING on the price-ordered
eligible chain — walk the survivors, drop violators with a reported reason,
preserve price order among survivors. NEVER a provider-wide pre-filter. A
null/absent knob is unbounded (output identical to pre-TR-007).

Concurrency is per-MODEL: a model at its concurrency limit is skipped individually
(like a circuit exclusion); the provider's other models stay eligible — a busy
model NEVER removes the whole provider. In-flight counts derive from the spawn
ledger (~/.hermes/model-router/ledger.jsonl, wired via scripts/router_ledger.py):
a trace whose LAST row is outcome='started' is in flight; 'started' rows older
than 30 minutes are stale (crash without `end`) and do not count.

Chain = eligible models ORDER BY (plan_tier, normalized_price * token_factor).
PAYG (deepseek) is a legitimate fallback hop — it appears where price ranks it;
the gate/health/breaker/busy filters decide admission, not the ordering.

Usage:
  router_spawn.py <project> [--format json|text] [--no-health]
  router_spawn.py --profile 'reasoning=5 debug=3 vision=-2' [--format json]
  router_spawn.py --list-profiles
  router_spawn.py --explain <project>     # show WHY each pair is excluded

Output (json): {project, profile, resolved_at, head, chain[], exclusions[],
gate_reasons[], gate, settings{max_consecutive_per_provider,
max_total_per_provider, model_concurrency_limit, overrides}}
Exit 0 always (fail-open: on any error prints {"error": ...} and exits 0) — the
scheduler must NEVER be blocked by the router.
"""
import json, os, sys, argparse, datetime

# Text registry (Bane 2026-08-27): the live store is a gitignored JSON file in
# the task-router repo — NOT a binary duckdb. Env-overridable for hermetic
# tests. registry.json is produced by router_seed.py (version 3: {"version",
# "generated_at", "tables": {name: [row...]}}).
# Repo-relative defaults: the project is self-contained (clone → use).
_HERE = os.path.dirname(os.path.realpath(__file__))
_REPO = os.path.dirname(_HERE)
REGISTRY = os.environ.get('ROUTING_REGISTRY', os.path.join(_REPO, 'registry.json'))
DATA_DIR = os.environ.get('ROUTING_DATA_DIR', os.path.join(_REPO, 'data', 'tables'))
# State dir (quota/health/circuit/ledger). Env-overridable so tests are hermetic
# and ops can point at a scratch dir; default identical to the historical path.
MR = os.environ.get('ROUTER_STATE_DIR', os.path.expanduser('~/.hermes/model-router'))

# A 'started' ledger row older than this is stale (crashed without `end`) and
# does not count as in-flight.
STALE_MS = 30 * 60 * 1000


def load_json(path, default):
    try:
        return json.load(open(path))
    except Exception:
        return default


def _parse_utc(ts):
    """Best-effort ISO-8601 → aware UTC datetime; None on any failure."""
    try:
        dt = datetime.datetime.fromisoformat(str(ts))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(datetime.timezone.utc)
    except Exception:
        return None


def ledger_in_flight(state_dir):
    """{(provider, model): in_flight_count} derived from the spawn ledger.

    Traces are reconstructed by trace_id across ALL rows (terminal rows carry
    no provider/model): a trace's LAST row decides its outcome, while its pair
    comes from whichever row carries one ('start'). A trace whose final outcome
    is 'started' is in flight; 'started' rows older than STALE_MS are stale
    (crash without `end`) and do not count. Any read/parse error degrades to {}
    — fail-open, the router must never raise on state reads.
    """
    try:
        last = {}
        with open(os.path.join(state_dir, 'ledger.jsonl')) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if not isinstance(row, dict):
                    continue
                tid = row.get('trace_id')
                if not tid:
                    continue
                rec = last.setdefault(tid, [None, None, None, None])  # prov, mdl, outcome, ts
                if row.get('provider'):
                    rec[0] = row['provider']
                if row.get('model'):
                    rec[1] = row['model']
                if row.get('outcome') is not None:
                    rec[2] = row['outcome']
                if row.get('ts') is not None:
                    rec[3] = row['ts']
        now = datetime.datetime.now(datetime.timezone.utc)
        counts = {}
        for prov, mdl, outcome, ts in last.values():
            if outcome != 'started' or not prov or not mdl:
                continue
            dt = _parse_utc(ts)
            if dt is None or (now - dt).total_seconds() * 1000 > STALE_MS:
                continue
            counts[(prov, mdl)] = counts.get((prov, mdl), 0) + 1
        return counts
    except Exception:
        return {}


def _effective_caps(profiles, qdoc, pid):
    """Effective two-knob diversity caps + per-model concurrency default.

    Per-profile task_profiles columns beat the global 'diversity' defaults in
    quota-state.json; NULL/absent everywhere = unbounded (None). Pre-TR-007
    schemas degrade to the globals — fail-open.
    """
    diversity = qdoc.get('diversity') or {}
    if not isinstance(diversity, dict):
        diversity = {}
    g_cons = diversity.get('max_consecutive_per_provider')
    g_tot = diversity.get('max_total_per_provider')
    p_cons = p_tot = None
    if pid:
        row = profiles.get(pid)
        if row:
            p_cons = row.get('max_consecutive_per_provider')
            p_tot = row.get('max_total_per_provider')
    cons = p_cons if p_cons is not None else g_cons
    tot = p_tot if p_tot is not None else g_tot
    return {
        'max_consecutive_per_provider': cons,
        'max_total_per_provider': tot,
        'model_concurrency_limit': diversity.get('model_concurrency_limit'),
        'overrides': {
            'profile': p_cons is not None or p_tot is not None,
            'consecutive': p_cons is not None,
            'total': p_tot is not None,
        },
    }


def _model_limit(models_cfg, diversity, prov, model):
    """Effective per-model concurrency limit for a pair.

    Precedence: explicit quota-state 'models' entry ('<provider>/<model>' →
    'concurrency_limit') beats the global diversity.model_concurrency_limit.
    No entry anywhere → None = never busy (unbounded).
    """
    entry = models_cfg.get(f'{prov}/{model}')
    if isinstance(entry, dict):
        lim = entry.get('concurrency_limit')
        if lim is not None:
            return lim
    lim = diversity.get('model_concurrency_limit')
    return lim if lim is not None else None


def _prune_diversity(out_chain, exclusions, reasons, cons_cap, tot_cap):
    """Walk the price-ordered survivor chain; drop diversity violators.

    Per provider: consecutive_run counts hops IN A ROW (resets when the
    provider changes); total counts hops across the whole chain. Over-cap hops
    move to exclusions with an explicit reason ('consecutive cap N' /
    'chain cap N'); survivors keep their relative price order. Both caps unset
    → no-op (identical output to pre-TR-007).
    """
    if cons_cap is None and tot_cap is None:
        return
    survivors = []
    run_prov, run_len = None, 0
    totals = {}
    for ent in out_chain:
        prov = ent['provider']
        if prov != run_prov:
            run_prov, run_len = prov, 0
        run_len += 1
        totals[prov] = totals.get(prov, 0) + 1
        why = []
        if cons_cap is not None and run_len > cons_cap:
            why.append(f'consecutive cap {cons_cap}')
        if tot_cap is not None and totals[prov] > tot_cap:
            why.append(f'chain cap {tot_cap}')
        if why:
            exclusions.append({'hop': ent['hop'], 'provider': prov,
                               'model': ent['model'], 'why': why})
            reasons.append(f"hop {ent['hop']} {prov}/{ent['model']}: "
                           + '; '.join(why))
        else:
            survivors.append(ent)
    out_chain[:] = survivors


def _load_registry():
    """registry.json → {tables: {name: [row...]}}; any error → {} (fail-open).

    Fallback (fresh clone, no registry.json yet): read the committed
    data/tables/*.jsonl (same keyed-record format) directly — the project is
    usable with stdlib python only, no seed run required.
    """
    try:
        with open(REGISTRY) as f:
            doc = json.load(f)
        return doc.get('tables') or {}
    except Exception:
        pass
    try:
        tables = {}
        for fn in sorted(os.listdir(DATA_DIR)):
            if fn.endswith('.jsonl'):
                name = fn[:-len('.jsonl')]
                rows = []
                with open(os.path.join(DATA_DIR, fn)) as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            rows.append(json.loads(line))
                tables[name] = rows
        return tables if tables else {}
    except Exception:
        return {}


def _build_chain(tables, reqs, limit=30):
    """Replicates v_task_chain exactly, in pure python.

    reqs = [(category, level), ...] (profile requirements or ad-hoc).
    Eligible = active models (valid_to null, not archived, priced) with a
    tier >= level for EVERY requirement. Order: plan_tier ASC,
    normalized_price * token_factor ASC, model ASC, provider ASC (the SQL
    view's tie-breaks). Returns rows [(hop, provider, model, price, dclass)].
    """
    models = tables.get('models') or []
    # Evidence is per MODEL (Bane 2026-08-27): tiers keyed by model name only;
    # every provider lane of the same weights inherits the same tier. A lane
    # with a bad deployment is disabled EXPLICITLY via models.disabled.
    tiers = {}
    for r in tables.get('model_tier') or []:
        tiers.setdefault(r.get('model'), {})[r.get('category')] = r.get('tier')
    eligible = []
    for m in models:
        if m.get('archive') or m.get('valid_to') is not None:
            continue
        if m.get('disabled'):
            continue  # explicit per-provider lane disable (bad deployment)
        price = m.get('normalized_price')
        if price is None:
            continue
        prov, model = m.get('provider'), m.get('model')
        mt = tiers.get(model) or {}
        # BLANK default (Bane 2026-08-27): a missing tier = -1 (no data = slightly
        # below median — clears lenient bars, fails 0 and up). NEVER 0, never an
        # inflated neutral.
        if any((mt.get(cat) if mt.get(cat) is not None else -1) < lvl
               for cat, lvl in reqs):
            continue
        eligible.append(m)
    eligible.sort(key=lambda m: (
        m.get('plan_tier') if m.get('plan_tier') is not None else 1 << 30,
        (m.get('normalized_price') or 0.0) * (m.get('token_factor') or 1.0),
        m.get('model') or '',
        m.get('provider') or '',
    ))
    # 6th element = the full model row, so callers can expose PUBLIC prices
    # (usd_1m/in_per_m/out_per_m) without a second lookup.
    return [(i + 1, m.get('provider'), m.get('model'),
             m.get('normalized_price'), m.get('data_class'), m)
            for i, m in enumerate(eligible[:limit])]


def _pub_prices(m):
    """Public-price triplet for a model row: (usd_1m, in_per_m, out_per_m).

    Bane 2026-08-27: cost reporting ("what did it cost to build feature X")
    quotes the provider's PUBLIC list price, not the internal normalized rate.
    usd_1m = public blended price when known, else the normalized effective
    rate (fail-open — a priced lane never reports None). in/out are the
    public per-1M split; None when only a blended price is known. Chain
    ORDERING still uses normalized_price — public prices are for reporting.
    """
    pub = m.get('public_price')
    if pub is None:
        pub = m.get('normalized_price')
    return pub, m.get('public_in_per_m'), m.get('public_out_per_m')


def _resolve_fallback(tables, qs, hs, cs, reqs, limit=30, profile_id=None):
    """FALLBACK LANES (Bane 2026-08-27): when the primary chain is fully
    gated/down, resolve the always-run lanes from data/tables/fallback_lanes.jsonl
    (registry table `fallback_lanes`: {provider, model, order, key_env, profiles?}).

    This is a DEGRADED path, not a normal chain (gpt-5.6-sol review 2026-08-27):
    - fallback lanes may serve a profile they don't fully clear — but that is
      REPORTED, never silent: each hop carries `requirements_unmet` and the
      resolve response sets `degraded_fallback=true` when it fires.
    - the same gates as the primary chain apply: quota GATED, health DOWN/SLOW,
      circuit OPEN, model-level health, and the lane must exist/be priced.
    - `profiles` field (optional) restricts a lane to specific profiles (e.g.
      the vision-exp lane serves only P5_VISION_E2E so a text model never
      handles vision E2E); absent = generic lane for all profiles.
    - profile-specific matching lanes resolve BEFORE generic lanes."""
    lanes = sorted(tables.get('fallback_lanes') or [],
                   key=lambda r: (r.get('order') or 1 << 30))
    by_lane = {}
    for m in tables.get('models') or []:
        by_lane[(m.get('provider'), m.get('model'))] = m
    tiers = {}
    for r in tables.get('model_tier') or []:
        tiers.setdefault(r.get('model'), {})[r.get('category')] = r.get('tier')
    generic, specific = [], []
    for f in lanes:
        profs = f.get('profiles') or []
        if profs:
            if profile_id and profile_id in profs:
                specific.append(f)  # curated for THIS profile
        else:
            generic.append(f)  # default lane, all profiles
    ordered = specific + generic  # curated-for-this-profile first, then default
    out = []
    for f in ordered:
        key = (f.get('provider'), f.get('model'))
        m = by_lane.get(key)
        if m is None:
            continue  # lane doesn't exist in registry — gap, not a fabrication
        if m.get('archive') or m.get('valid_to') is not None or m.get('disabled'):
            continue
        if m.get('normalized_price') is None:
            continue
        # ---- gates, same as the primary chain ----
        q = qs.get(f.get('provider')) or {}
        if q.get('status') != 'open':
            continue
        h = hs.get(f.get('provider')) or {}
        if h.get('status') in ('DOWN', 'SLOW'):
            continue
        mm = (h.get('models') or {}).get(f.get('model')) or {}
        if mm.get('status') in ('DOWN', 'SLOW'):
            continue
        if (cs.get((f.get('provider'), f.get('model'))) or
                cs.get(f'{f.get("provider")}/{f.get("model")}')):
            continue  # circuit OPEN for this exact pair
        mt = tiers.get(f.get('model')) or {}
        unmet = [(c, lvl, mt.get(c) if mt.get(c) is not None else -1)
                 for c, lvl in reqs
                 if (mt.get(c) if mt.get(c) is not None else -1) < lvl]
        out.append({'hop': len(out) + 1, 'provider': f.get('provider'),
                    'model': f.get('model'),
                    'usd_1m': round(float(_pub_prices(m)[0]), 4),
                    'in_per_m': _pub_prices(m)[1], 'out_per_m': _pub_prices(m)[2],
                    'data_class': m.get('data_class'),
                    'fallback': True, 'key_env': f.get('key_env'),
                    'requirements_unmet': unmet})
        if len(out) >= limit:
            break
    return out


def resolve(project=None, profile_id=None, adhoc=None, use_health=True, limit=30):
    tables = _load_registry()
    projects = {r.get('id'): r for r in tables.get('projects') or []}
    profiles = {r.get('id'): r for r in tables.get('task_profiles') or []}
    reqs_rows = tables.get('task_profile_requirements') or []
    reqs_by_profile = {}
    for r in reqs_rows:
        reqs_by_profile.setdefault(r.get('task_id'), []).append(
            (r.get('category'), r.get('level')))
    # --- 1. project → profile -------------------------------------------------
    pid = profile_id
    if adhoc:
        pid = None
        reqs = []
        for kv in adhoc:
            for part in kv.split():  # tolerate 'a=1 b=2' arriving as one arg
                cat, _, lvl = part.partition('=')
                reqs.append((cat.strip(), int(lvl)))
    elif project:
        row = projects.get(project)
        if row is None:
            return {'error': f'project {project} not in registry'}
        pid = row.get('profile') or 'P0_FORE'
        reqs = reqs_by_profile.get(pid, [])
    else:
        reqs = reqs_by_profile.get(pid or 'P0_FORE', [])
    if not pid and not adhoc:
        pid = 'P0_FORE'

    # --- 2. chain from the registry -------------------------------------------
    # Profiles with NO requirement rows resolve to an empty chain — identical
    # to v_task_eligible (its task list comes from DISTINCT requirements).
    chain = _build_chain(tables, reqs, limit=limit) if reqs else []

    # --- 2.5 settings: diversity caps + per-profile overrides ------------------
    qdoc = load_json(f'{MR}/quota-state.json', {})
    if not isinstance(qdoc, dict):
        qdoc = {}
    caps = _effective_caps(profiles, qdoc, pid)
    if not chain:
        return {'error': 'no chain — profile has no eligible models', 'profile': pid}

    # --- 3. gates: quota + health + circuit + per-model busy --------------------
    qs = qdoc.get('providers') or {}
    if not isinstance(qs, dict):
        qs = {}
    diversity = qdoc.get('diversity') or {}
    if not isinstance(diversity, dict):
        diversity = {}
    models_cfg = qdoc.get('models') or {}
    if not isinstance(models_cfg, dict):
        models_cfg = {}
    hs = load_json(f'{MR}/health-state.json', {}).get('providers', {}) if use_health else {}
    cs = load_json(f'{MR}/circuit-state.json', {}).get('pairs', {})
    inflight = ledger_in_flight(MR)  # fail-open: {} on any error
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')

    out_chain, exclusions, reasons = [], [], []
    for hop, prov, model, price, dc, mrow in chain:
        why = []
        q = qs.get(prov, {})
        if not isinstance(q, dict):
            q = {}
        if q.get('status') != 'open':
            why.append(f'quota GATED: {q.get("reason", "blocked")}')
        h = hs.get(prov, {})
        if not isinstance(h, dict):
            h = {}
        if h.get('status') == 'DOWN':
            why.append(f'health DOWN ({h.get("ts", "?")})')
        elif h.get('status') == 'SLOW':
            why.append(f'health SLOW ({h.get("latency_ms")}ms)')
        # model-level health (probe v2 writes providers.<p>.models.<m>.status;
        # gpt-5.6-sol review 2026-08-27: the router previously ignored it and
        # routed onto 22 DOWN pairs)
        hm = (h.get('models') or {}).get(model) or {}
        if hm.get('status') == 'DOWN':
            why.append(f'model DOWN ({hm.get("ts", "?")})')
        elif hm.get('status') == 'SLOW':
            why.append(f'model SLOW ({hm.get("latency_ms")}ms)')
        c = cs.get(f'{prov}/{model}')
        if c and c.get('open_until') and c['open_until'] > now:
            why.append(f'circuit OPEN until {c["open_until"]} ({c.get("failures", 0)} failures)')
        mlim = _model_limit(models_cfg, diversity, prov, model)
        if mlim is not None:
            nf = inflight.get((prov, model), 0)
            if nf >= mlim:
                why.append(f'model busy ({nf} in-flight >= limit {mlim})')
        if why:
            exclusions.append({'hop': hop, 'provider': prov, 'model': model, 'why': why})
            reasons.append(f'hop {hop} {prov}/{model}: ' + '; '.join(why))
        else:
            pub_usd, pub_in, pub_out = _pub_prices(mrow)
            out_chain.append({'hop': hop, 'provider': prov, 'model': model,
                              'usd_1m': round(float(pub_usd), 4) if pub_usd is not None else None,
                              'in_per_m': pub_in, 'out_per_m': pub_out,
                              'data_class': dc})

    # --- 4. diversity pruning: two-knob caps on the survivor chain -------------
    _prune_diversity(out_chain, exclusions, reasons,
                     caps['max_consecutive_per_provider'],
                     caps['max_total_per_provider'])

    head = out_chain[0] if out_chain else None

    # --- 4.5 FALLBACK LANES (Bane 2026-08-27): crons must ALWAYS run ---------
    # When every eligible hop is gated/down, fall back to the designated
    # always-available lanes (deepseek-v4 + cron key). Cheap subs first,
    # deepseek as the guaranteed last hop — never a None chain for a cron.
    fb_used = []
    if not head:
        fb = _resolve_fallback(tables, qs, hs, cs, reqs, limit=limit,
                               profile_id=pid)
        if fb:
            fb_used = fb
            head = fb[0]
            out_chain = fb
            reasons.append(
                f'FALLBACK: all {len(exclusions)} eligible hops gated — using '
                f'{head["provider"]}/{head["model"]} (always-run lane; '
                f'DEGRADED — requirements_unmet: '
                f'{[(c, lvl, have) for c, lvl, have in head.get("requirements_unmet", [])]})')

    return {'project': project, 'profile': pid, 'resolved_at': now,
            'head': head, 'chain': out_chain, 'exclusions': exclusions,
            'gate_reasons': reasons,
            'degraded_fallback': bool(fb_used),
            'gate': 'OPEN' if head else ('NO-OPEN-HOP' if out_chain or exclusions else 'NO-CHAIN'),
            'settings': caps}

def main():
    ap = argparse.ArgumentParser(description='Task router — resolve chain for project/profile')
    ap.add_argument('project', nargs='?')
    ap.add_argument('--profile', dest='profile_id')
    ap.add_argument('--profile-req', dest='adhoc', nargs='+', help="ad-hoc 'cat=level' list")
    ap.add_argument('--list-profiles', action='store_true')
    ap.add_argument('--explain', action='store_true')
    ap.add_argument('--format', choices=['json', 'text'], default='json')
    ap.add_argument('--no-health', action='store_true')
    args = ap.parse_args()

    if args.list_profiles:
        tables = _load_registry()
        profs = {r.get('id'): r for r in tables.get('task_profiles') or []}
        reqs = {}
        for r in tables.get('task_profile_requirements') or []:
            reqs.setdefault(r.get('task_id'), []).append(
                (r.get('category'), r.get('level')))
        for pid in sorted(profs):
            rq = sorted(reqs.get(pid, []), key=lambda x: (-x[1], x[0]))
            rs = ' '.join(f"{c}={'+'*l if l>0 else ('-'*-l if l<0 else '0')}" for c, l in rq)
            print(f'{pid:<10} {profs[pid].get("title", "")}')
            print(f'           {rs}')
        return

    if not args.project and not args.profile_id and not args.adhoc:
        ap.print_usage()
        return

    r = resolve(project=args.project, profile_id=args.profile_id,
                adhoc=args.adhoc, use_health=not args.no_health)
    if args.format == 'json':
        print(json.dumps(r, indent=1))
        return
    print(f'▶ {r.get("project", r.get("profile"))}  profile={r.get("profile")}  gate={r.get("gate")}')
    h = r.get('head')
    if h:
        print(f'  HEAD: {h["provider"]}/{h["model"]}  ${h["usd_1m"]}/M')
    for c in r.get('chain', [])[1:6]:
        print(f'  hop {c["hop"]}: {c["provider"]}/{c["model"]}  ${c["usd_1m"]}/M')
    for g in r.get('gate_reasons', []):
        print(f'  EXCLUDED: {g}')

if __name__ == '__main__':
    main()
