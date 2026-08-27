#!/usr/bin/env python3
"""router_maintain.py — registry maintenance loop (TR-005).

One entrypoint for the whole maintenance cadence:

  reprice   — OpenRouter spot-check → recompute normalized_price for repricable rows
  seed      — rebuild derived tables via router_seed.py (mandatory; failure ABORTS)
  export    — base tables (providers, models, archetypes, projects, benchmarks)
              routing ns → task-router ns; sync the 6 seed-derived tables as well
  snapshot  — chains/<YYYY-MM-DD>.md from v_task_chain (+ dated copy in docs/)
  commit    — git add + commit in BOTH namespace repos (no push — foreman pushes)
  all       — reprice → seed → export → snapshot → commit

Every subcommand takes --dry-run: prints exactly what would change (price
diffs, exports, files, git commands) and writes NOTHING. Exit 0.

Pricing formulas are Bane's empirical rules (docs/registry-maintenance.md):
  deepseek    normalized_price = OpenRouter in-price of the EXACT matching
              OR id (deepseek/deepseek-*); evidence 'or-spot-<date>'
  zai-glm     STATIC official points/M × $0.03, off-peak rows exactly half —
              NEVER overwritten from OpenRouter (OR glm prices are USD per
              1M tokens, not zai credit points); values are carried in the
              DB rows themselves (evidence 'official formula'):
              glm-5.3-flash 2.3 pts → $0.069, glm-5.3 6.9 pts → $0.207,
              glm-5-turbo 5.7 pts → $0.171, glm-4.7 4.6 pts → $0.138;
              off-peak rows exactly half ($0.0345 / $0.103)
  opencode-go $12/5h ÷ req-per-5h ÷ 31,250 tok/req; budget unknown →
              blended estimate 0.96*in + 0.04*out
Sub-plan / non-OR providers (clinepass, ollama-cloud, kimi-for-coding,
neuralwatt, minimax, stepfun, synthetic, groq, grok-build, crof,
openai-codex, zai-glm) are NEVER overwritten with OR prices. Rows with no
derivable mapping are left unchanged ("skipped (no mapping)").

Fail-open like router_spawn.py: if the spot-check fails (missing key, network
error), reprice warns and skips — never blocks seed/export/commit.
"""
import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys

import duckdb  # noqa: F401  (kept for the seed subprocess's in-memory engine; maintain itself is pure JSON)
import tempfile

# Text registry (Bane 2026-08-27): live store = gitignored JSON in the repo,
# NOT a binary duckdb. ROUTING_REGISTRY override = scratch copies for tests.
REGISTRY = os.environ.get('ROUTING_REGISTRY', '/home/kara/task-router/registry.json')
REGISTRY_DEFAULT = os.environ.get('ROUTING_REGISTRY_DEFAULT', '/home/kara/task-router/registry.json')


def _load_doc():
    """registry.json → full doc; missing/corrupt → None (callers fail-open)."""
    try:
        with open(REGISTRY) as f:
            return json.load(f)
    except Exception:
        return None


def _save_doc(doc):
    """Atomic write of registry.json (tmp + rename — never a torn file)."""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(REGISTRY), suffix='.tmp')
    with os.fdopen(fd, 'w') as f:
        json.dump(doc, f, indent=1)
    os.replace(tmp, REGISTRY)

BOARD_PY = os.path.expanduser('~/.hermes/venvs/board/bin/python3')
SPOT_CHECK = os.path.expanduser(
    '~/.hermes/skills/mlops/model-intelligence/scripts/or-family-spot-check.py')
SEED_SCRIPT = '/home/kara/task-router/scripts/router_seed.py'
ROUTING_NS = '/home/kara/duckbrain/namespaces/routing'
TASKROUTER_NS = '/home/kara/duckbrain/namespaces/task-router'
REPO = '/home/kara/task-router'

# Base tables exported to BOTH namespaces by `export`.
BASE_TABLES = ['archetypes', 'benchmarks', 'models', 'projects', 'providers']
# Derived tables written to the routing ns by router_seed.py and mirrored into
# the task-router ns by `export` (previously a manual copy step).
DERIVED_TABLES = ['category_levels', 'level_defs', 'model_perf', 'model_tier',
                  'task_profile_requirements', 'task_profiles']

# Families passed to or-family-spot-check.py (the repricable OR-backed set).
SPOT_FAMILIES = ['deepseek', 'glm', 'qwen', 'gpt-5.6']

# Providers whose prices must NEVER be overwritten with OR spot-check prices
# (sub plans / non-OpenRouter).
NON_REPRICABLE_PROVIDERS = {
    'clinepass', 'ollama-cloud', 'kimi-for-coding', 'neuralwatt', 'minimax',
    'stepfun', 'synthetic', 'groq', 'grok-build', 'crof', 'openai-codex',
    # zai-glm rows are STATIC official points × 0.03 — OR glm prices are USD
    # per 1M tokens, not credit points, so there is no live OR source for them.
    'zai-glm',
}

OPENCODE_BLENDED_IN = 0.96          # budget unknown → blended estimate weights
OPENCODE_BLENDED_OUT = 0.04


def _iso_now():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M+00:00')


def _today():
    return datetime.date.today().strftime('%Y-%m-%d')


def _parse_spot_output(text):
    """Parse or-family-spot-check.py output lines.

    Line format (see that script):
        <or-id> | in=<f> out=<f> cache=<f> | ctx=<n> | overrides=Y|N
        TOTAL_MODELS: <n>
    Returns {or_id: {'in': float|None, 'out': float|None}}.
    """
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or '|' not in line:
            continue
        mid, rest = line.split('|', 1)
        mid = mid.strip()
        fields = {}
        for part in rest.split('|'):
            for kv in part.split():
                k, _, v = kv.partition('=')
                try:
                    fields[k] = None if v in ('None', '') else float(v)
                except ValueError:
                    pass
        out[mid] = {'in': fields.get('in'), 'out': fields.get('out')}
    return out


def run_spot_check(dry_run=False):
    """Shell out to the OR spot-check tool. Returns ({or_id: prices}, err|None).

    The spot-check is a read-only GET against /v1/models — it runs even under
    --dry-run so the preview shows the REAL price diffs (AC4). Fail-open: on
    any failure returns ({}, reason) — the caller skips reprice without
    aborting the loop.
    """
    cmd = [sys.executable if not os.path.exists(BOARD_PY) else BOARD_PY, SPOT_CHECK] + SPOT_FAMILIES
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {}, f'spot-check launch failed: {e}'
    if p.returncode != 0:
        return {}, (f'spot-check exit {p.returncode}: '
                    f'{(p.stderr or p.stdout).strip()[:300]}')
    prices = _parse_spot_output(p.stdout)
    if not prices:
        return {}, 'spot-check returned no parsable price lines'
    return prices, None


def find_or_id(model_name, prices):
    """Map a registry model name to an OpenRouter id from the spot-check output.

    EXACT leaf match wins first (e.g. 'deepseek-v4-pro' must NOT be priced
    from the longer leaf 'deepseek-v4-pro-0813'); longest-prefix fallback
    only when no exact leaf exists.
    """
    m = (model_name or '').lower()
    for mid in prices:
        if mid.split('/')[1].lower() == m:
            return mid
    # fallback: longest matching family token wins (e.g. glm-5.3-flash over glm-5.3)
    cands = [(len(mid), mid) for mid in prices
             if mid.split('/')[1].lower().startswith(m)]
    if cands:
        return max(cands)[1]
    return None


def compute_price(provider, model, row_evidence, prices):
    """Compute new normalized_price per Bane's provider formulas.

    Returns (new_price, method) or (None, skip_reason).

    zai-glm rows are NEVER touched here: they are STATIC official points/M
    × $0.03 (off-peak half), carried in the DB rows themselves — OR glm
    prices are USD per 1M tokens, not zai credit points.
    """
    ev = (row_evidence.get('price_evidence') or '')
    prov = (provider or '').lower()
    mdl = (model or '').lower()

    if prov == 'deepseek':
        # PAYG tariff == the OR in-price of the EXACT matching deepseek model.
        ent = prices.get(find_or_id(mdl, prices))
        if ent and ent['in'] is not None:
            return ent['in'], 'deepseek: OR in-price'
        return None, 'skipped (no mapping): no OR in-price for deepseek'

    if prov == 'opencode-go':
        # $12/5h ÷ req-per-5h ÷ 31,250 tokens/req; budget unknown → blended:
        # 0.96 × in-price + 0.04 × out-price.
        oid = find_or_id(mdl, prices)
        if oid is None:
            return None, 'skipped (no mapping): no OR id matched'
        ent = prices[oid]
        if ent['in'] is None:
            return None, 'skipped (no mapping): OR in-price missing for %s' % oid
        blended = round(OPENCODE_BLENDED_IN * ent['in'] + OPENCODE_BLENDED_OUT * (ent['out'] or 0.0), 6)
        return blended, 'opencode-go: blended 0.96·in + 0.04·out of %s' % oid

    # openrouter-backed estimate rows: only when evidence says 'estimate'.
    if 'estimate' in ev.lower() and prov not in NON_REPRICABLE_PROVIDERS:
        oid = find_or_id(mdl, prices)
        if oid is None:
            return None, 'skipped (no mapping): no OR id matched'
        ent = prices[oid]
        if ent['in'] is None:
            return None, 'skipped (no mapping): OR in-price missing for %s' % oid
        return ent['in'], 'estimate-row: OR in-price of %s' % oid

    return None, 'skipped (no mapping): provider %s is sub-plan/non-OR' % provider


def load_live_rows(doc):
    """models rows from registry.json: [(provider, model, price, evidence)]. """
    out = []
    for m in (doc.get('tables') or {}).get('models') or []:
        if m.get('valid_to') is None and not m.get('archive'):
            out.append((m.get('provider'), m.get('model'),
                        m.get('normalized_price'), m.get('price_evidence')))
    out.sort(key=lambda r: (r[0], r[1]))
    return out


def collect_reprice_plan(prices, doc):
    """Diff spot-check against live rows. Returns list of update dicts."""
    plan = []
    for prov, mdl, cur, ev in load_live_rows(doc):
        row_ev = {'normalized_price': cur, 'price_evidence': ev}
        new, why = compute_price(prov, mdl, row_ev, prices)
        if new is None:
            plan.append({'provider': prov, 'model': mdl, 'current': cur,
                         'new': None, 'why': why})
            continue
        changed = cur is None or abs(float(new) - float(cur)) > abs(float(cur)) * 0.005 + 1e-9
        plan.append({'provider': prov, 'model': mdl, 'current': cur, 'new': new,
                     'why': why, 'changed': bool(changed)})
    return plan


def apply_reprice(plan, today, doc):
    """Update registry.json models rows in place; returns count applied."""
    n = 0
    for u in plan:
        if u.get('changed') and u.get('new') is not None:
            for m in (doc.get('tables') or {}).get('models') or []:
                if (m.get('provider') == u['provider'] and m.get('model') == u['model']
                        and m.get('valid_to') is None and not m.get('archive')):
                    m['normalized_price'] = u['new']
                    m['price_evidence'] = f'or-spot-{today}'
                    n += 1
                    break
    return n


def report_reprice(plan, dry_run):
    updated = [u for u in plan if u.get('changed')]
    skipped = [u for u in plan if u.get('new') is None]
    print(f"[reprice] {len(updated)} row(s) would change" if dry_run
          else f"[reprice] {len(updated)} row(s) updated")
    for u in updated:
        print(f"  {u['provider']}/{u['model']}: {u['current']} -> "
              f"{round(u['new'], 4)} ({u['why']})")
    print(f"[reprice] {len(skipped)} row(s) skipped/unmapped")
    for u in skipped[:10]:
        print(f"  {u['provider']}/{u['model']}: {u['why']}")
    if len(skipped) > 10:
        print(f"  ... +{len(skipped) - 10} more")


def step_reprice(dry_run):
    doc = _load_doc()
    if doc is None:
        print('[reprice] WARNING: registry.json missing/unreadable — skipping reprice (fail-open)', file=sys.stderr)
        return
    prices, err = run_spot_check(dry_run=dry_run)
    today = _today()
    if err:
        print(f'[reprice] WARNING: skipping reprice — {err}', file=sys.stderr)
        print('[reprice] fail-open: continuing with existing prices')
        return
    try:
        plan = collect_reprice_plan(prices, doc)
        report_reprice(plan, dry_run=dry_run)
    except Exception as e:  # noqa: BLE001 — fail-open, never block the loop
        print(f'[reprice] WARNING: reprice failed ({e}) — fail-open continuing',
              file=sys.stderr)
        return
    if dry_run:
        print(f"[reprice] would update registry.json models: normalized_price + price_evidence='or-spot-{today}'")
        return
    try:
        n = apply_reprice(plan, today, doc)
        if n:
            _save_doc(doc)
    except Exception as e:  # noqa: BLE001 — fail-open, never block the loop
        print(f'[reprice] WARNING: reprice failed ({e}) — fail-open continuing',
              file=sys.stderr)
        return
    print(f"[reprice] applied {n} UPDATE(s) to registry.json")


def step_seed(dry_run):
    env = dict(os.environ)
    if REGISTRY != REGISTRY_DEFAULT:
        env['ROUTING_REGISTRY'] = REGISTRY
    else:
        env.pop('ROUTING_REGISTRY', None)
    if dry_run:
        print('[seed] DRY-RUN: would run:', BOARD_PY, SEED_SCRIPT)
        print('[seed] would rebuild derived tables:',
              ', '.join(DERIVED_TABLES), '+ views v_task_eligible/v_task_chain')
        return
    print('[seed] running router_seed.py ...')
    p = subprocess.run([BOARD_PY, SEED_SCRIPT], cwd=REPO, env=env)
    if p.returncode != 0:
        print(f'[seed] FAILED (exit {p.returncode}) — ABORTING maintenance run',
              file=sys.stderr)
        sys.exit(p.returncode or 1)
    print('[seed] ok')


def _table_rows(doc, table):
    """registry.json table → array-per-line JSONL (ns export format).

    Sorted by the first column — matches the old SQL export's ORDER BY 1 so
    regenerated ns files stay byte-stable (no gratuitous diffs).
    """
    rows = sorted((doc.get('tables') or {}).get(table) or [],
                  key=lambda r: str(r.get(next(iter(r), '')) or ''))
    for r in rows:
        yield json.dumps([r.get(c) for c in r], default=str)


def write_table(path, table):
    doc = _load_doc()
    if doc is None:
        print(f'[export] WARNING: registry.json missing — cannot export {table}', file=sys.stderr)
        return 0
    lines = list(_table_rows(doc, table))
    with open(path, 'w') as f:
        for ln in lines:
            f.write(ln + '\n')
    return len(lines)


def step_export(dry_run):
    copied = []
    for t in BASE_TABLES:
        rpath = f'{ROUTING_NS}/tables/{t}.jsonl'
        tpath = f'{TASKROUTER_NS}/tables/{t}.jsonl'
        if dry_run:
            print(f'[export] DRY-RUN: would export {t} -> {rpath} then copy -> {tpath}')
            continue
        write_table(rpath, t)
        shutil.copyfile(rpath, tpath)
        copied.append(t)
    for t in DERIVED_TABLES:
        rpath = f'{ROUTING_NS}/tables/{t}.jsonl'
        tpath = f'{TASKROUTER_NS}/tables/{t}.jsonl'
        if dry_run:
            print(f'[export] DRY-RUN: would copy {rpath} -> {tpath}')
            continue
        shutil.copyfile(rpath, tpath)
        copied.append(t)
    if not dry_run:
        print(f'[export] synced {len(copied)} table file(s) into both namespaces')


def _fmt_profile_level(cat, lvl):
    sym = '+' * lvl if lvl > 0 else ('-' * -lvl if lvl < 0 else '0')
    return f'{cat}={sym}'


def build_snapshot_text():
    """Render the chains snapshot mirroring docs/chains-2026-08-27.md format."""
    from router_spawn import _load_registry, _build_chain
    tables = _load_registry()
    if not tables:
        return None
    title = f'# Chains snapshot — {_iso_now()}'
    lines = [
        title,
        '',
        'Eligibility: model must clear EVERY category requirement of the profile (tier >= level).',
        'Order: plan_tier ASC, effective $/M ASC, model/provider tie-break. Health/circuit/quota exclusions NOT applied here (see state/).',
    ]
    profs = {r.get('id'): r for r in tables.get('task_profiles') or []}
    reqs = {}
    for r in tables.get('task_profile_requirements') or []:
        reqs.setdefault(r.get('task_id'), []).append((r.get('category'), r.get('level')))
    for pid in sorted(profs):
        title_ = profs[pid].get('title', '')
        lines.append('')
        lines.append(f'## {pid} — {title_}')
        rq = sorted(reqs.get(pid, []), key=lambda x: (-x[1], x[0]))
        rs = ' '.join(_fmt_profile_level(c, l) for c, l in rq)
        lines.append(f'profile: {rs}')
        hops = _build_chain(tables, reqs.get(pid, []), limit=200)
        for h, prov, model, price, _dc in hops:
            lines.append(f'  {h}. $ {price:.3f}/M  {prov}/{model}')
    return '\n'.join(lines) + '\n'


def step_snapshot(dry_run):
    date = _today()
    ns_path = f'{TASKROUTER_NS}/chains/{date}.md'
    doc_path = f'{REPO}/docs/chains-{date}.md'
    if dry_run:
        print(f'[snapshot] DRY-RUN: would write {ns_path}')
        print(f'[snapshot] DRY-RUN: would copy  {doc_path}')
        return
    text = build_snapshot_text()
    if text is None:
        print('[snapshot] WARNING: registry.json missing — snapshot skipped', file=sys.stderr)
        return
    os.makedirs(os.path.dirname(ns_path), exist_ok=True)
    with open(ns_path, 'w') as f:
        f.write(text)
    with open(doc_path, 'w') as f:
        f.write(text)
    hops_total = sum(1 for ln in text.splitlines() if ln.startswith('  ') and '$' in ln)
    print(f'[snapshot] wrote {ns_path} ({hops_total} hops)')
    print(f'[snapshot] copied to {doc_path}')


def _git(repo, *args):
    return subprocess.run(['git', '-C', repo, *args], capture_output=True, text=True)


def step_commit(dry_run):
    cmds = [
        (ROUTING_NS, ['add', 'tables/'],
         f'{ROUTING_NS}/tables/'),
        (ROUTING_NS, ['commit', '-m',
                      f'router-maintain: {_today()} — reprice + seed + export'],
         'commit(reprice+seed+export)'),
        (TASKROUTER_NS, ['add', 'tables/', 'chains/'],
         f'{TASKROUTER_NS}/tables/ chains/'),
        (TASKROUTER_NS, ['commit', '-m',
                         f'router-maintain: {_today()} — tables sync + chains snapshot'],
         'commit(sync+snapshot)'),
    ]
    # detect actual changes first so "nothing changed" exits cleanly
    any_changes = False
    for repo, args, _ in cmds:
        if args[0] == 'add':
            st = _git(repo, 'status', '--porcelain', '--', args[1])
            if st.stdout.strip():
                any_changes = True
    if not any_changes:
        print('[commit] nothing to commit')
        return
    for repo, args, label in cmds:
        full = ['git', '-C', repo] + args
        if dry_run:
            print(f'[commit] DRY-RUN: would run: git -C {repo} {" ".join(args)}')
            continue
        p = _git(repo, *args)
        out = (p.stdout or '').strip()
        err = (p.stderr or '').strip()
        if p.returncode != 0:
            print(f'[commit] git {" ".join(args)} failed in {repo}: {err}',
                  file=sys.stderr)
        elif args[0] == 'commit':
            # git commit reports "nothing to commit" via stderr + rc=1
            if out and out != '':
                first = out.splitlines()[0]
                print(f'[commit] {repo}: {first}')
            elif 'nothing to commit' in err:
                print(f'[commit] {repo}: nothing to commit')
            else:
                print(f'[commit] {repo}: committed')
        else:
            print(f'[commit] staged {label}')


STEPS = ['reprice', 'seed', 'export', 'snapshot', 'commit']


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--dry-run', action='store_true',
                    help='print what would change; write nothing')
    ap.add_argument('step', nargs='+', choices=STEPS + ['all'])
    args = ap.parse_args(argv)

    steps = STEPS if 'all' in args.step else args.step
    for s in steps:
        getattr(sys.modules[__name__], f'step_{s}')(args.dry_run)
    return 0


if __name__ == '__main__':
    sys.exit(main())
