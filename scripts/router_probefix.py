#!/usr/bin/env python3
"""router_probefix.py — resolve 404/400 model ids from probe logs (Bane 2026-08-31).

"something that should be getting the model names for you everyday and updating
you is clearly not wired in to look at the logs of this stuff to find the ones
with 404 to resolve those things" — this is that wiring. The models.dev sync
(router_modelsdev.py) refreshes catalog names daily; THIS step reads the hourly
probe's health.jsonl, finds models the probe marked DOWN with invalid-model-id
signals (HTTP 404 / 400 / ModelError), and resolves them:

  0. CHECK provider_rules.jsonl FIRST (2026-09-18): a provider-level contract
     row may already EXPLAIN the failure (opencode-go's x-opencode-session
     requirement makes the probe's 400 a probe-shape artifact, not a lane
     fault). Such rows carry `explains_probe: ["<regex over the error>"]` —
     DATA drives the explanation, the code only reads it. Explained failures
     are counted, printed, and NOT written as gaps.
  1. SKIP rows already covered by probe_fixes.jsonl / probe_excludes.jsonl.
  2. Fetch the provider's LIVE /models catalog (Bearer key from .env) — ground
     truth; fall back to the models.dev cache (~/.chimera/models-dev-cache.json).
  3. Exact normalized match (prefix / case / hf: variant of the probed id) ->
     ONE candidate -> append {provider, model, fix_to, ...} to
     data/tables/probe_fixes.jsonl. The probe picks it up next hour (no code
     change, no agent needed).
  4. No exact candidate -> version-rename family (same stem, digits differ) ->
     flag as GAP with the candidate suggested (renames can change behavior —
     agent decides). Nothing at all -> GAP row for the daily research agent
     (replace or exclude decision). If the provider's catalog could not be
     fetched at all, the row is flagged CATALOG-N/A (endpoint/credential
     problem — the report must not claim "catalog serves the id").

--sync-providers: also diff config.yaml custom_providers (+ fallback_providers)
against data/tables/probe_providers.jsonl and append any new ones (enabled,
default_model null — the probe probes them via registry rows; visible gap if a
new provider has no registry rows yet). Provider discovery stays data-driven.

DATA > CODE: fix mappings and provider explanations live in data files only.
This script never edits the probe script.

Exit codes: 0 always (pipeline step; findings are data, not failures). Use
--dry-run to see what WOULD be written without writing.
"""
import argparse
import json
import os
import re
import sys
import urllib.request
import urllib.error

MR = os.environ.get('ROUTER_STATE_DIR', os.path.expanduser('~/.hermes/model-router'))
HEALTH_JSONL = f'{MR}/health.jsonl'
DATA_DIR = os.environ.get('ROUTING_DATA_DIR',
                          os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + '/data/tables')
MODELSDEV_CACHE = os.environ.get('MODELSDEV_CACHE', os.path.expanduser('~/.chimera/models-dev-cache.json'))
ENV_FILE = os.environ.get('ROUTER_ENV_FILE', os.path.expanduser('~/.hermes/.env'))
CONFIG = os.environ.get('ROUTER_HERMES_CONFIG', os.path.expanduser('~/.hermes/config.yaml'))
UA = 'hermes-router-probefix/1.0'

# Error strings that mean "the model id is wrong / gone" (actionable) vs
# capacity/auth (not actionable).
ID_ERROR_RE = re.compile(r'404|400|modelerror|model not found|does not exist|'
                         r'not found|invalid model|unknown model|no such model', re.I)


def load_env():
    """Provider keys from the router env file (ROUTER_ENV_FILE override = hermetic tests)."""
    env = {}
    try:
        for line in open(ENV_FILE):
            line = line.strip()
            if '=' in line and not line.startswith('#'):
                k, _, v = line.partition('=')
                env[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:
        pass
    return env


def load_providers():
    provs = {}
    path = os.path.join(DATA_DIR, 'probe_providers.jsonl')
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if row.get('enabled', True) and row.get('id'):
                provs[row['id']] = row
    return provs


def load_rows(fname):
    rows = []
    path = os.path.join(DATA_DIR, fname)
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
    return rows


def disabled_lanes():
    """{(provider, model): reason} of registry lanes already intentionally disabled.

    REGISTRY STATE BEATS A RE-DIAGNOSIS (2026-09-19): the 09-18 xkiro/synthetic
    dead-id rows re-appeared in the very next scan with nothing left to do — the
    lanes had already been disabled. An auto DEAD-ID/GAP row for a lane the
    registry already carries as disabled is stale work, not open work, and it
    buries the rows that ARE open. Same doctrine as provider_rules explanations
    (an explanation beats a re-diagnosis), one level up: the registry's own
    disable IS the resolution record.
    """
    out = {}
    for r in load_rows('models.jsonl'):
        if r.get('disabled'):
            out[(r.get('provider'), r.get('model'))] = r.get('disabled_reason') or ''
    return out


def load_probe_explanations():
    """provider_rules.jsonl rows carrying `explains_probe` -> {provider: [(regex, rule, detail)]}.

    DATA > CODE: the knowledge that "this provider's 400 is our probe's own
    missing session header" belongs in provider_rules.jsonl, not here. A row
    opts in with  "explains_probe": ["400"]  (regex list matched against the
    probe's error text, case-insensitive). Rows with valid_to in the past are
    ignored (an expired contract stops explaining anything).
    """
    out = {}
    today = __import__('datetime').date.today().isoformat()
    for r in load_rows('provider_rules.jsonl'):
        pats = r.get('explains_probe')
        prov = r.get('provider')
        if not pats or not prov:
            continue
        vt = r.get('valid_to')
        if vt and vt < today:
            continue
        for p in (pats if isinstance(pats, list) else [pats]):
            try:
                out.setdefault(prov, []).append((re.compile(str(p), re.I),
                                                 r.get('rule') or 'rule',
                                                 r.get('detail') or ''))
            except re.error:
                continue
    return out


def explain(prov, model, error, explanations):
    """-> (rule, detail) when a provider_rules contract explains this failure."""
    for rx, rule, detail in explanations.get(prov, []):
        if rx.search(error or ''):
            return rule, detail
    return None


def append_row(fname, row):
    path = os.path.join(DATA_DIR, fname)
    with open(path, 'a') as f:
        f.write(json.dumps(row) + '\n')


def scan_health(runs):
    """health.jsonl -> {(provider, model): {'error':..., 'ts':...}} for DOWN
    rows whose error matches the invalid-model-id pattern (latest wins)."""
    fails = {}
    if not os.path.exists(HEALTH_JSONL):
        return fails
    lines = [l for l in open(HEALTH_JSONL) if l.strip()][-runs:]
    for line in lines:
        try:
            run = json.loads(line)
        except Exception:
            continue
        for prov, r in (run.get('providers') or {}).items():
            for model, mm in (r.get('models') or {}).items():
                if mm.get('status') != 'DOWN':
                    continue
                err = mm.get('error') or ''
                if ID_ERROR_RE.search(err):
                    fails[(prov, model)] = {'error': err, 'ts': run.get('ts')}
    return fails


def normalize(s):
    """Lowercase, strip hf:/provider prefixes, keep alnum + slash + dot + colon."""
    s = (s or '').strip().lower()
    s = re.sub(r'^hf:', '', s)
    s = re.sub(r'^[a-z0-9_.-]+/', '', s, count=1)  # vendor prefix (deepseek/x, xiaomi/m)
    s = re.sub(r'^[a-z0-9_.-]+:', '', s)           # custom prefix (syn:)
    return re.sub(r'[^a-z0-9/._-]', '', s)


def fetch_live_catalog(prov, row, env):
    """GET {base}/models with the provider key. Returns list of ids or None."""
    base, key_env = row.get('base_url'), row.get('key_env')
    if not base or not key_env:
        return None
    key = env.get(key_env, '')
    if not key:
        return None
    req = urllib.request.Request(base.rstrip('/') + '/models',
                                 headers={'Authorization': f'Bearer {key}',
                                          'User-Agent': UA})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = json.loads(r.read().decode(errors='replace'))
        if isinstance(raw, list):
            return [m if isinstance(m, str) else m.get('id') for m in raw if m]
        for keyname in ('data', 'models'):
            arr = raw.get(keyname)
            if isinstance(arr, list) and arr:
                return [m if isinstance(m, str) else (m.get('id') or m.get('name'))
                        for m in arr if m]
        return None
    except Exception:
        return None


def fetch_modelsdev_ids(prov):
    try:
        cache = json.load(open(MODELSDEV_CACHE))
    except Exception:
        return None
    p = cache.get(prov)
    if not p:
        return None
    out = []
    for m in p.get('models', []):
        out.append(m if isinstance(m, str) else m.get('id'))
    return out or None


def version_family(a, b):
    """True when a and b share a stem and differ only in version digits
    (qwen3.6-27b vs qwen3.8-27b)."""
    return re.sub(r'\d+', '#', a) == re.sub(r'\d+', '#', b) and a != b


def live_serves(live, model):
    """Exact id or normalized-equal id present in the provider's live catalog."""
    if not live:
        return False
    n = normalize(model)
    for cid in live:
        if not cid:
            continue
        if cid.lower() == (model or '').lower() or normalize(cid) == n:
            return True
    return False


def resolve(prov, model, error, providers, env):
    """-> ('skip', reason, None) | ('fix', fix_to, via) | ('stuck', None, None)
    | ('gap', candidate, None) | ('unexplained', None, None)."""
    fixes = {(r['provider'], r['model']) for r in load_rows('probe_fixes.jsonl')}
    excludes = {(r['provider'], r['model']) for r in load_rows('probe_excludes.jsonl')}
    key = (prov, model)
    if key in excludes:
        return ('skip', 'already excluded', None)
    if key in fixes:
        # probe retries the corrected id every hour and STILL fails -> real
        # problem for the agent, not something a rename fixes.
        return ('stuck', None, None)

    row = providers.get(prov)
    if not row:
        return ('gap', None, None)
    n = normalize(model)
    live = fetch_live_catalog(prov, row, env)

    # exact-normalized candidates (never the probed id itself — self-match is
    # NOT a fix; catalog serves it yet inference 404s = unexplained)
    cands_live, cands_mdev = set(), set()
    if live is not None:
        for cid in live:
            if cid and normalize(cid) == n and cid.lower() != model.lower():
                cands_live.add(cid)
    if not cands_live:
        mdev = fetch_modelsdev_ids(prov)
        if mdev:
            for cid in mdev:
                if cid and normalize(cid) == n and cid.lower() != model.lower():
                    cands_mdev.add(cid)
    total = cands_live | cands_mdev
    if len(total) == 1:
        c = next(iter(total))
        via = 'live /models' if c in cands_live else 'models.dev cache'
        return ('fix', c, via)
    if len(total) > 1:
        return ('gap', sorted(total)[0], None)  # ambiguous — agent picks

    # version-rename family (qwen3.6-27b -> qwen3.8-27b): AUTO-FIX only when
    # the provider's OWN live catalog proves the rename (authoritative — the
    # provider renamed it); a models.dev-only candidate is stale-cache evidence
    # -> gap, agent decides.
    fam_live, fam_mdev = set(), set()
    for src, target in ((live, fam_live), (fetch_modelsdev_ids(prov), fam_mdev)):
        if not src:
            continue
        for cid in src:
            if cid and version_family(normalize(cid), n) and cid.lower() != model.lower():
                target.add(cid)
    if len(fam_live) == 1:
        return ('fix', fam_live.pop(), 'live /models (version rename)')
    fam_all = fam_live | fam_mdev
    if len(fam_all) == 1:
        return ('gap', fam_all.pop(), None)
    # no candidate anywhere. Classify HONESTLY, three ways:
    #   no live catalog        -> we cannot say anything (endpoint problem)
    #   live catalog, id gone  -> the id is DEAD upstream (disable/replace)
    #   live catalog has the id-> inference fails anyway (transient/auth-shaped)
    if live is None:
        return ('nocatalog', None, None)
    if not live_serves(live, model):
        return ('dead', None, None)
    return ('unexplained', None, None)


def sync_providers(env, dry_run):
    """config.yaml custom_providers + fallback_providers -> probe_providers.jsonl
    additions (never removes/edits existing rows). Regex parse — stdlib only."""
    try:
        text = open(CONFIG).read()
    except Exception as e:
        print(f'⚠️  cannot read {CONFIG}: {e}')
        return 0
    found = {}
    for m in re.finditer(r'-\s*name:\s*([A-Za-z0-9_.-]+).*?api_key_env:\s*([A-Za-z0-9_.-]+).*?base_url:\s*(\S+)',
                         text, re.S):
        name, key_env, base = m.group(1), m.group(2), m.group(3).strip('"\'')
        if name in ('groq', 'ollama-cloud', 'minimax', 'kimi', 'zai-glm', 'opencode-go',
                    'stepfun', 'synthetic', 'neuralwatt', 'crof', 'nvidia',
                    'deepseek-duckbrain-sync'):
            found[name] = (base, key_env)
    existing = load_providers()
    added = 0
    for name, (base, key_env) in sorted(found.items()):
        if name in existing:
            continue
        row = {'id': name, 'base_url': base, 'key_env': key_env,
               'default_model': None, 'enabled': True,
               'note': 'auto-discovered from config.yaml custom_providers'}
        if dry_run:
            print(f'  [dry] would add provider {name} ({base})')
        else:
            append_row('probe_providers.jsonl', row)
            print(f'  added provider {name} ({base})')
        added += 1
    return added


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--runs', type=int, default=48, help='health.jsonl runs to scan (default 48)')
    ap.add_argument('--dry-run', action='store_true', help='print findings, write nothing')
    ap.add_argument('--sync-providers', action='store_true',
                    help='diff config.yaml custom_providers -> probe_providers.jsonl')
    args = ap.parse_args(argv)

    env = load_env()
    providers = load_providers()
    if args.sync_providers:
        print('== provider discovery (config.yaml custom_providers) ==')
        sync_providers(env, args.dry_run)
        providers = load_providers()

    fails = scan_health(args.runs)
    if not fails:
        print('probe 404 scan: no invalid-model-id failures in last '
              f'{args.runs} runs — nothing to resolve')
        return 0

    # latest run is ground truth: a model that was failing historically but is
    # OK in the newest health.jsonl entry is already resolved — skip it.
    latest = None
    try:
        for line in open(HEALTH_JSONL):
            if line.strip():
                latest = json.loads(line)
    except Exception:
        latest = None

    print(f'probe 404 scan: {len(fails)} invalid-model-id failure(s) in last {args.runs} runs')
    explanations = load_probe_explanations()
    already_disabled = disabled_lanes()
    n_fix = n_gap = n_skip = n_unexplained = n_explained = n_nocatalog = n_dead = n_disabled = 0
    for (prov, model), info in sorted(fails.items()):
        if latest:
            mm = ((latest.get('providers') or {}).get(prov) or {}).get('models', {}).get(model)
            if mm and mm.get('status') in ('OK', 'SLOW', 'OVERLOADED', 'TIMEOUT'):
                n_skip += 1
                continue  # resolved since the failing run
        # provider contract beats a re-diagnosis (provider_rules.jsonl)
        exp = explain(prov, model, info['error'], explanations)
        if exp:
            n_explained += 1
            print(f'  EXPLAINED {prov} {model}: {info["error"]} — {exp[0]} '
                  f'({exp[1][:90]})')
            continue
        # registry state beats a re-diagnosis too: a lane the registry already
        # carries as disabled has nothing left to do (the disable IS the record).
        if (prov, model) in already_disabled:
            n_disabled += 1
            print(f'  HANDLED {prov} {model}: lane already disabled in the registry '
                  f'({already_disabled[(prov, model)][:70]})')
            continue
        outcome, extra, via = resolve(prov, model, info['error'], providers, env)
        ts = info['ts']
        if outcome == 'fix':
            row = {'provider': prov, 'model': model, 'fix_to': extra,
                   'reason': f'auto-fixed on {info["error"]} — {extra} matched in {via}',
                   'ts': ts, 'source': 'auto'}
            if args.dry_run:
                print(f'  [dry] FIX {prov} {model} -> {extra} ({via})')
            else:
                # skip if identical row already present
                existing = load_rows('probe_fixes.jsonl')
                if any(r.get('provider') == prov and r.get('model') == model and r.get('fix_to') == extra for r in existing):
                    print(f'  FIX {prov} {model} -> {extra} (already present)')
                else:
                    append_row('probe_fixes.jsonl', row)
                    print(f'  FIX {prov} {model} -> {extra} ({via})')
            n_fix += 1
        elif outcome in ('gap', 'stuck'):
            action = 'fix-not-working (agent)' if outcome == 'stuck' else 'replace-or-exclude (agent)'
            row = {'provider': prov, 'model': model, 'error': info['error'],
                   'candidate': extra, 'ts': ts, 'source': 'auto', 'action': action}
            if args.dry_run:
                print(f'  [dry] {"STUCK" if outcome == "stuck" else "GAP"} {prov} {model} (candidate: {extra or "none"})')
            else:
                existing = load_rows('probe_gaps.jsonl')
                if any(r.get('provider') == prov and r.get('model') == model and r.get('action') == action for r in existing):
                    print(f'  {"STUCK" if outcome == "stuck" else "GAP"} {prov} {model} (already flagged)')
                else:
                    append_row('probe_gaps.jsonl', row)
                    print(f'  {"STUCK" if outcome == "stuck" else "GAP"} {prov} {model} (candidate: {extra or "none"})')
            n_gap += 1
        elif outcome == 'skip':
            n_skip += 1
        elif outcome == 'nocatalog':
            # provider catalog unreachable — we cannot claim the id is served.
            n_nocatalog += 1
            row = {'provider': prov, 'model': model, 'error': info['error'],
                   'candidate': None, 'ts': ts, 'source': 'auto',
                   'action': 'provider-catalog-unavailable (endpoint/credential — check probe_providers base_url/key)'}
            if args.dry_run:
                print(f'  [dry] CATALOG-N/A {prov} {model}: {info["error"]} '
                      f'(provider /models unreachable — cannot verify id)')
            else:
                existing = load_rows('probe_gaps.jsonl')
                if not any(r.get('provider') == prov and r.get('model') == model
                           and r.get('action', '').startswith('provider-catalog-unavailable')
                           for r in existing):
                    append_row('probe_gaps.jsonl', row)
                    print(f'  CATALOG-N/A {prov} {model}: {info["error"]} '
                          f'(provider /models unreachable — cannot verify id)')
        elif outcome == 'dead':
            # live catalog fetched, id absent from it -> dead upstream id.
            n_dead += 1
            row = {'provider': prov, 'model': model, 'error': info['error'],
                   'candidate': None, 'ts': ts, 'source': 'auto',
                   'action': 'id-absent-from-provider-catalog (disable or replace — agent)'}
            if args.dry_run:
                print(f'  [dry] DEAD-ID {prov} {model}: {info["error"]} '
                      f'(absent from the live catalog)')
            else:
                existing = load_rows('probe_gaps.jsonl')
                if not any(r.get('provider') == prov and r.get('model') == model
                           and r.get('action', '').startswith('id-absent-from-provider-catalog')
                           for r in existing):
                    append_row('probe_gaps.jsonl', row)
                    print(f'  DEAD-ID {prov} {model}: {info["error"]} '
                          f'(absent from the live catalog)')
        else:
            # unexplained: probe says 404 but provider's own catalog serves the id —
            # transient or auth-shaped; surface it once for the agent.
            if args.dry_run:
                print(f'  [dry] UNEXPLAINED {prov} {model}: {info["error"]} (catalog serves the id)')
            else:
                existing = load_rows('probe_gaps.jsonl')
                row = {'provider': prov, 'model': model, 'error': info['error'],
                       'candidate': None, 'ts': ts, 'source': 'auto',
                       'action': 'verify (catalog serves id — likely transient/auth)'}
                if not any(r.get('provider') == prov and r.get('model') == model and r.get('action', '').startswith('verify') for r in existing):
                    append_row('probe_gaps.jsonl', row)
                    print(f'  UNEXPLAINED {prov} {model}: {info["error"]} (catalog serves the id)')
            n_unexplained += 1
    print(f'probe 404 scan done: {n_fix} auto-fix(es), {n_gap} gap(s) flagged, '
          f'{n_disabled} already-disabled lane(s) (registry state — not open work), '
          f'{n_skip} already handled, {n_unexplained} unexplained, '
          f'{n_explained} explained by provider_rules, {n_dead} dead-id(s), '
          f'{n_nocatalog} catalog-unavailable')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
