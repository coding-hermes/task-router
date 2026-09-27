#!/usr/bin/env python3
"""router_probe_run.py — run the ranking battery against registry lanes.

The middle of the pipeline: router_rank_audit.py says which lanes carry no rank,
this measures them, and router_probe_ingest.py turns the measurements into
benchmarks rows. Written so probing is repeatable rather than a bespoke script
per model.

Credentials are resolved the way the fleet resolves them — base_url and
api_key_env from ~/.hermes/config.yaml (providers map + custom_providers list),
key VALUES read from ~/.hermes/.env in-process and never printed. Lanes needing
a non-OpenAI transport (minimax anthropic_messages) or a bespoke session header
(opencode-go) are skipped and reported rather than probed wrongly.

Targets default to lanes that are ACTIVE + PRICED but carry no tier rows, on the
providers given — i.e. exactly the population the auditor flags.

Usage:
  ~/.hermes/venvs/board/bin/python3 scripts/router_probe_run.py \
      --providers neuralwatt,commandcode,zai-glm,synthetic [--limit N] [--dry-run]
"""
import argparse
import collections
import concurrent.futures as cf
import datetime
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
DATA_DIR = os.environ.get('ROUTING_DATA_DIR', os.path.join(REPO, 'data', 'tables'))
HOME = os.path.expanduser('~')
CONFIG = os.path.join(HOME, '.hermes', 'config.yaml')
ENV_FILE = os.path.join(HOME, '.hermes', '.env')

SKIP_REASON = {
    'minimax': 'anthropic_messages transport (not OpenAI chat/completions)',
    'opencode-go': 'requires x-opencode-session session header',
    'opencode-go-2': 'requires x-opencode-session session header',
    'muse-code': 'credential is the plugin cache, not an env var',
    'router': 'local router loopback',
    '9router': 'local router loopback',
    'chimera': 'local panel loopback',
    'task-router': 'local router loopback',
    'openai-codex': 'subscription OAuth, no API key',
}


def load_env():
    env = {}
    if not os.path.exists(ENV_FILE):
        return env
    for line in open(ENV_FILE, encoding='utf-8', errors='ignore'):
        if '=' in line and not line.strip().startswith('#'):
            k, v = line.strip().split('=', 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def load_providers():
    """provider name -> (base_url, key_env, extra_headers) from config.yaml."""
    import yaml
    c = yaml.safe_load(open(CONFIG, encoding='utf-8'))
    out = {}
    for name, p in (c.get('providers') or {}).items():
        if isinstance(p, dict):
            out[name] = (p.get('base_url'), p.get('api_key_env'), p.get('extra_headers') or {})
    for p in (c.get('custom_providers') or []):
        if isinstance(p, dict) and p.get('name'):
            out.setdefault(p['name'], (p.get('base_url'), p.get('api_key_env'), p.get('extra_headers') or {}))
    return out


def load(name):
    with open(os.path.join(DATA_DIR, f'{name}.jsonl'), encoding='utf-8') as fh:
        return [json.loads(l) for l in fh if l.strip()]


V5 = [
    ('T1-TOOL', 3, 'You have tools: search(q), calc(expr). Reply with ONLY the JSON tool calls (one per line) '
                    'needed to answer: what is 17*23 plus roughly the population of France? Format exactly: '
                    '{"tool":"calc","args":{"expr":"..."}}',
     lambda o: (1 if ('calc' in o and '"tool"' in o) else 0) + (1 if 'search' in o else 0)
               + (1 if re.search(r'17\s*\*\s*23', o) else 0)),
    ('T2-CODE', 3, 'Write Go code only: func LongestPalindromicSubstring(s string) string and a 3-case table test. No prose.',
     lambda o: (1 if 'func LongestPalindromicSubstring' in o else 0) + (1 if 's[' in o and 'j' in o else 0)
               + (1 if ('tests' in o.lower() or 'table' in o.lower() or 'want' in o.lower()) else 0)),
    ('T3-REASON', 3, 'A train leaves at 08:15, travels 240 km at 80 km/h, with 2 stops of 10 minutes each. '
                     'What time does it arrive? Answer with the time only, then one line of math.',
     lambda o: (1 if re.search(r'1[01]:\d\d', o) else 0) + (1 if ('10:' in o or '11:' in o) else 0)
               + (1 if '3' in o and 'h' in o.lower() else 0)),
    ('T5-DEBUG', 3, 'Find the bug in this Go function and give the fixed version (code only): '
                    'func avg(xs []float64) float64 { s := 0.0; for i := 0; i <= len(xs); i++ { s += xs[i] }; '
                    'return s / float64(len(xs)) }',
     lambda o: (1 if 'i < len' in o else 0) + (1 if ('len(xs) == 0' in o or 'len(xs) > 0' in o or 'if len' in o) else 0)
               + (1 if 'func avg' in o else 0)),
]

AGENTIC = [
    ('agent_tick', 'You are an autonomous coding agent. Output EXACTLY 3 lines, nothing else.\n'
                   'Each line: STEP: <action> | CHECK: <how you verify it worked>\n'
                   'The task: add a /health endpoint to a Go HTTP service.',
     [('three STEP lines', lambda o: len(re.findall(r'(?mi)^\s*STEP:', o)) == 3),
      ('three CHECK lines', lambda o: len(re.findall(r'(?mi)^\s*CHECK:', o)) == 3),
      ('verification is mechanical', lambda o: bool(re.search(r'(?i)(test|build|compile|curl|status|exit)', o))),
      ('no extra prose', lambda o: len([l for l in o.strip().splitlines() if l.strip()]) <= 3)]),
    ('delegation', 'Write the exact brief you would hand a worker agent. Output ONLY these four lines:\n'
                   'TASK: ...\nDONE-WHEN: ...\nEVIDENCE: ...\nDO-NOT: ...',
     [('all four markers', lambda o: all(k in o for k in ('TASK:', 'DONE-WHEN:', 'EVIDENCE:', 'DO-NOT:'))),
      ('DONE-WHEN is measurable', lambda o: bool(re.search(r'(?i)(test|green|exit|pass|curl|byte|count)', o))),
      ('EVIDENCE names an artifact', lambda o: bool(re.search(r'(?i)(log|file|json|diff|output|screenshot|report)', o))),
      ('line count bounded', lambda o: len([l for l in o.strip().splitlines() if l.strip()]) <= 6)]),
    ('schema', 'Output ONLY a JSON Schema (draft-07) object describing a record with fields name (string), '
               'age (integer), email (string). Age and email must be listed in required.',
     [('parses as JSON', lambda o: _j(o) is not None),
      ('declares properties', lambda o: isinstance(_j(o), dict) and 'properties' in _j(o)),
      ('lists required', lambda o: isinstance(_j(o), dict) and 'required' in _j(o)),
      ('types present', lambda o: bool(re.search(r'"(string|integer|number)"', o)))]),
    ('long_doc', 'Summarize this policy in EXACTLY 3 bullet points (each starting with "- ", each 12 words or fewer), '
                 'then one final line starting with "RISK: ".\nPolicy: A failing build must never be merged. Nightly '
                 'builds run at 02:00 and publish artifacts. Any red build blocks the release until it is fixed or '
                 'explicitly waived by the release owner, who records the waiver in the tracking system.',
     [('three bullets', lambda o: len(re.findall(r'(?m)^\s*-\s', o)) == 3),
      ('bullets are short', lambda o: all(len(b.split()) <= 12 for b in re.findall(r'(?m)^\s*-\s(.+)$', o))),
      ('RISK line present', lambda o: bool(re.search(r'(?m)^\s*RISK:\s*\S', o))),
      ('line count bounded', lambda o: len([l for l in o.strip().splitlines() if l.strip()]) <= 6)]),
    ('long_horizon', 'List EXACTLY 4 sequential milestones to migrate a running service to a new datacentre. '
                     'Format each line as: M<n>: <milestone> | EXIT: <measurable exit criterion>',
     [('four milestones', lambda o: len(re.findall(r'(?mi)^\s*M\d:', o)) == 4),
      ('each has EXIT', lambda o: len(re.findall(r'(?mi)EXIT:', o)) == 4),
      ('exit criteria measurable', lambda o: bool(re.search(r'(?i)(test|green|latency|ms|%|count|zero|pass)', o))),
      ('numbered in order', lambda o: bool(re.search(r'(?mi)^\s*M1:', o)) and bool(re.search(r'(?mi)^\s*M4:', o)))]),
    ('test', 'Write Go code only: a function Add(a, b int) int and a table-driven test TestAdd with exactly 3 cases. No prose.',
     [('has TestAdd', lambda o: 'func TestAdd' in o),
      ('table-driven', lambda o: bool(re.search(r'(?i)(\[\]struct|tests\s*:?=|cases\s*:?=)', o))),
      ('three cases', lambda o: len(re.findall(r'(?m)^\s*\{\s*"?', o)) >= 3 or o.count('want') >= 3),
      ('has Add body', lambda o: 'func Add' in o)]),
]


def _j(o):
    m = re.search(r'\{.*\}', o, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except ValueError:
        return None


def call(base, key, model, prompt, extra, max_tokens=3000, retries=3):
    body = {"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": max_tokens}
    headers = {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + key,
               'User-Agent': 'hermes-bench/5.0'}
    headers.update(extra or {})
    for attempt in range(retries):
        req = urllib.request.Request(base.rstrip('/') + '/chat/completions',
                                     data=json.dumps(body).encode(), headers=headers)
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=150) as r:
                d = json.loads(r.read())
            break
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and attempt < retries - 1:
                time.sleep(2 * (attempt + 1))
                continue
            raise
    lat = time.time() - t0
    d = d.get('data', d)
    msg = d['choices'][0]['message']
    content = msg.get('content') or msg.get('reasoning_content') or ''
    return content, lat


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--providers', required=True)
    ap.add_argument('--limit', type=int, default=0, help='max models (0 = all matching)')
    ap.add_argument('--date', default=datetime.date.today().isoformat())
    ap.add_argument('--out-dir', default=os.path.expanduser('~/model_bench'))
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    providers = [p.strip() for p in args.providers.split(',') if p.strip()]
    env, prov_cfg = load_env(), load_providers()
    models, tiers = load('models'), collections.defaultdict(set)
    for r in load('model_tier'):
        tiers[r['model']].add(r['category'])

    targets, skipped = [], {}
    for prov in providers:
        if prov in SKIP_REASON:
            skipped[prov] = SKIP_REASON[prov]
            continue
        base, key_env, extra = prov_cfg.get(prov, (None, None, {}))
        if not base:
            skipped[prov] = 'no base_url in config.yaml'
            continue
        if not env.get(key_env or ''):
            skipped[prov] = f'key env {key_env} absent from .env'
            continue
        for m in models:
            if m['provider'] != prov:
                continue
            if m.get('archive') or m.get('valid_to') or m.get('disabled') or m.get('normalized_price') is None:
                continue
            if tiers.get(m['model']):
                continue                      # already ranked
            targets.append((prov, m['model'], base, env[key_env], extra))

    if args.limit:
        targets = targets[:args.limit]
    print(f'target lanes: {len(targets)}')
    for prov, model, base, _, _ in targets:
        print(f'   {prov:14s} {model[:44]:44s} -> {base}')
    for prov, why in skipped.items():
        print(f'   SKIP {prov}: {why}')
    if args.dry_run or not targets:
        print('\nDRY RUN — nothing probed.' if args.dry_run else '\nnothing to do')
        return 0

    results_v5, results_ag = [], []
    lock = __import__('threading').Lock()

    def one(prov, model, base, key, extra):
        v5 = {'provider': prov, 'model': model}
        ag = {'provider': prov, 'model': model}
        try:
            for tid, mx, prompt, scorer in V5:
                content, lat = call(base, key, model, prompt, extra)
                v5[tid] = scorer(content)
                v5[tid + '_lat'] = round(lat, 1)
            for cat, prompt, checks in AGENTIC:
                content, lat = call(base, key, model, prompt, extra)
                passed = [n for n, fn in checks if fn(content)]
                ag[cat] = {'passed': len(passed), 'total': len(checks), 'lat': round(lat, 1),
                           'failed': [n for n, _ in checks if n not in passed]}
            print(f'done {prov}/{model}', flush=True)
        except Exception as e:  # noqa: BLE001
            v5['error'] = ag['error'] = str(e)[:140]
            print(f'ERR  {prov}/{model}: {str(e)[:90]}', flush=True)
        with lock:
            results_v5.append(v5)
            results_ag.append(ag)

    with cf.ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda t: one(*t), targets))

    for name, data in (('v5', results_v5), ('agentic', results_ag)):
        path = os.path.join(args.out_dir, f'results_lanes_{args.date}_{name}.json')
        json.dump(data, open(path, 'w'), indent=1)
        print('wrote', path)
    return 0


if __name__ == '__main__':
    sys.exit(main())
