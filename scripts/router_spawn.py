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
import duckdb, json, os, sys, argparse, datetime

DB = os.environ.get('ROUTING_DB', '/home/kara/reports-repo/routing.duckdb')
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


def _effective_caps(con, qdoc, pid):
    """Effective two-knob diversity caps + per-model concurrency default.

    Per-profile task_profiles columns beat the global 'diversity' defaults in
    quota-state.json; NULL/absent everywhere = unbounded (None). Pre-TR-007
    schemas (columns or table missing) degrade to the globals — fail-open.
    """
    diversity = qdoc.get('diversity') or {}
    if not isinstance(diversity, dict):
        diversity = {}
    g_cons = diversity.get('max_consecutive_per_provider')
    g_tot = diversity.get('max_total_per_provider')
    p_cons = p_tot = None
    if pid:
        try:
            row = con.execute(
                'SELECT max_consecutive_per_provider, max_total_per_provider '
                'FROM task_profiles WHERE id=?', [pid]).fetchone()
            if row:
                p_cons, p_tot = row[0], row[1]
        except Exception:
            pass  # old schema / no table: global defaults apply
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


def resolve(project=None, profile_id=None, adhoc=None, use_health=True, limit=15):
    con = duckdb.connect(DB, read_only=True)
    # --- 1. project → profile -------------------------------------------------
    pid = profile_id
    if adhoc:
        pid = None
        con.execute("CREATE TEMP TABLE _adhoc (task_id VARCHAR, category VARCHAR, level INTEGER)")
        for kv in adhoc:
            for part in kv.split():  # tolerate 'a=1 b=2' arriving as one arg
                cat, _, lvl = part.partition('=')
                con.execute("INSERT INTO _adhoc VALUES ('_adhoc',?,?)", [cat.strip(), int(lvl)])
        req_table = '_adhoc'
    elif project:
        row = con.execute("SELECT profile, sensitivity FROM projects WHERE id=?", [project]).fetchone()
        if row is None:
            con.close()
            return {'error': f'project {project} not in registry'}
        pid = row[0] or 'P0_FORE'
        req_table = 'task_profile_requirements'
    else:
        req_table = 'task_profile_requirements'
    if not pid and not adhoc:
        pid = 'P0_FORE'

    # --- 2. chain from the registry -------------------------------------------
    if adhoc:
        # ad-hoc profile: eligibility against model_tier directly (temp req table)
        chain = con.execute("""
            SELECT hop, provider, model, normalized_price, data_class FROM (
              SELECT row_number() OVER (ORDER BY plan_tier ASC, (normalized_price * token_factor) ASC) AS hop,
                     provider, model, normalized_price, data_class
              FROM models m
              WHERE m.valid_to IS NULL AND m.archive = false
                AND normalized_price IS NOT NULL
                AND NOT EXISTS (
                    SELECT 1 FROM _adhoc r
                    WHERE NOT EXISTS (SELECT 1 FROM model_tier t
                                      WHERE t.provider = m.provider AND t.model = m.model
                                        AND t.category = r.category AND t.tier >= r.level)))
            ORDER BY hop LIMIT ?""", [limit]).fetchall()
    else:
        chain = con.execute("""
            SELECT hop, provider, model, normalized_price, data_class
            FROM v_task_chain WHERE task_id=? ORDER BY hop LIMIT ?""", [pid, limit]).fetchall()

    # --- 2.5 settings: diversity caps + per-profile overrides ------------------
    # (resolved while the connection is open; pre-TR-007 DBs degrade to globals)
    qdoc = load_json(f'{MR}/quota-state.json', {})
    if not isinstance(qdoc, dict):
        qdoc = {}
    caps = _effective_caps(con, qdoc, pid)
    con.close()
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
    for hop, prov, model, price, dc in chain:
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
            out_chain.append({'hop': hop, 'provider': prov, 'model': model,
                              'usd_1m': round(float(price), 4) if price is not None else None,
                              'data_class': dc})

    # --- 4. diversity pruning: two-knob caps on the survivor chain -------------
    _prune_diversity(out_chain, exclusions, reasons,
                     caps['max_consecutive_per_provider'],
                     caps['max_total_per_provider'])

    head = out_chain[0] if out_chain else None
    return {'project': project, 'profile': pid, 'resolved_at': now,
            'head': head, 'chain': out_chain, 'exclusions': exclusions,
            'gate_reasons': reasons,
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
        con = duckdb.connect(DB, read_only=True)
        for r in con.execute("SELECT id, title FROM task_profiles ORDER BY 1").fetchall():
            reqs = con.execute("SELECT category, level FROM task_profile_requirements WHERE task_id=? ORDER BY level DESC, category", [r[0]]).fetchall()
            rs = ' '.join(f"{c}={'+'*l if l>0 else ('-'*-l if l<0 else '0')}" for c, l in reqs)
            print(f'{r[0]:<10} {r[1]}')
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
