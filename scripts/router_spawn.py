#!/usr/bin/env python3
"""router_spawn.py — THE TASK ROUTER (runtime lookup for the fleet).

Resolves a project (or ad-hoc profile) to the chain of (provider, model) pairs the
scheduler/foreman may use, cross-checked against live gate state:

  quota-state.json   — provider policy gates (GATED = blocked, reason)
  health-state.json  — hourly provider/model ping results (DOWN/SLOW = skip)
  circuit-state.json — circuit breakers (open_until in future = skip)

Chain = eligible models ORDER BY (plan_tier, normalized_price * token_factor).
PAYG (deepseek) is a legitimate fallback hop — it appears where price ranks it;
the gate/health/breaker filters decide admission, not the ordering.

Usage:
  router_spawn.py <project> [--format json|text] [--no-health]
  router_spawn.py --profile 'reasoning=5 debug=3 vision=-2' [--format json]
  router_spawn.py --list-profiles
  router_spawn.py --explain <project>     # show WHY each pair is excluded

Output (json): {project, profile, resolved_at, head, chain[], exclusions[], gate_reasons[]}
Exit 0 always (fail-open: on any error prints {"error": ...} and exits 0) — the
scheduler must NEVER be blocked by the router.
"""
import duckdb, json, os, sys, argparse, datetime

DB = os.environ.get('ROUTING_DB', '/home/kara/reports-repo/routing.duckdb')
MR = os.path.expanduser('~/.hermes/model-router')

def load_json(path, default):
    try:
        return json.load(open(path))
    except Exception:
        return default

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
    con.close()
    if not chain:
        return {'error': 'no chain — profile has no eligible models', 'profile': pid}

    # --- 3. gates: quota + health + circuit ------------------------------------
    qs = load_json(f'{MR}/quota-state.json', {}).get('providers', {})
    hs = load_json(f'{MR}/health-state.json', {}).get('providers', {}) if use_health else {}
    cs = load_json(f'{MR}/circuit-state.json', {}).get('pairs', {})
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')

    out_chain, exclusions, reasons = [], [], []
    for hop, prov, model, price, dc in chain:
        why = []
        q = qs.get(prov, {})
        if q.get('status') != 'open':
            why.append(f'quota GATED: {q.get("reason", "blocked")}')
        h = hs.get(prov, {})
        if h.get('status') == 'DOWN':
            why.append(f'health DOWN ({h.get("ts", "?")})')
        elif h.get('status') == 'SLOW':
            why.append(f'health SLOW ({h.get("latency_ms")}ms)')
        c = cs.get(f'{prov}/{model}')
        if c and c.get('open_until') and c['open_until'] > now:
            why.append(f'circuit OPEN until {c["open_until"]} ({c.get("failures", 0)} failures)')
        if why:
            exclusions.append({'hop': hop, 'provider': prov, 'model': model, 'why': why})
            reasons.append(f'hop {hop} {prov}/{model}: ' + '; '.join(why))
        else:
            out_chain.append({'hop': hop, 'provider': prov, 'model': model,
                              'usd_1m': round(float(price), 4) if price is not None else None,
                              'data_class': dc})

    head = out_chain[0] if out_chain else None
    return {'project': project, 'profile': pid, 'resolved_at': now,
            'head': head, 'chain': out_chain, 'exclusions': exclusions,
            'gate_reasons': reasons,
            'gate': 'OPEN' if head else ('NO-OPEN-HOP' if out_chain or exclusions else 'NO-CHAIN')}

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
