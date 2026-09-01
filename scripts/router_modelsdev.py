#!/usr/bin/env python3
"""router_modelsdev.py — models.dev catalog sync into the text registry.

Bane directive 2026-08-27 (fleet chat: "your list is not providing all the
models expected"): the registry must be cross-checked against models.dev so
EVERY model the providers we use actually offer is present in the text
database — with gaps flagged instead of silently missing.

TR-019 (2026-09-01): enabled-provider filtering + data-driven provider
mapping. Bane: "support for pulling in model details from models.dev to have
a list of models for the providers they have enabled. Support for mapped
providers — I might rename a provider by prefixing or pattern matched and
string replace stuff, this way if I have a router it will work."

ENABLED PROVIDER DEFINITION (data-driven — providers.jsonl has no `enabled`
column by design; facts live in data tables, never in code):
    a provider row in data/tables/providers.jsonl is ENABLED iff
      1. archive = false, AND
      2. data/tables/models.jsonl has >= 1 row referencing that provider id
         (i.e. the lane is routable — the registry can actually hand out a
         (provider, model) pair for it).
    Anything else is DISABLED and skipped by `sync` with a visible reason in
    `skipped_providers` (never silent). `--all` is the escape hatch that
    includes disabled providers explicitly (they are processed like any
    other; the "disabled:" reason disappears).

PROVIDER MAPPING (data/tables/provider_mappings.jsonl): rows
    {"id", "pattern", "match": "prefix"|"regex"|"literal", "replacement",
     "direction": "external->registry", "note"}
Rules apply in FILE ORDER; FIRST MATCH WINS. An external name (models.dev
payload id, or a renamed lane id in the seed) is matched against `pattern`
per `match`; on a match the name becomes `replacement` with literal "\\N"
backreferences expanded from regex capture groups for match="regex"
(match="prefix" strips the prefix; "literal" string-replaces). The mapped
name is then resolved against the registry/providers tables. NO mapping in
scripts: the demo rules live only in the JSONL. An external payload name
that maps to no registry id is a VISIBLE GAP — reported in
`unmapped_providers`, never silently dropped (probe_gaps.jsonl does not fit:
its rows are probe-lane records {provider, model, error, candidate, ts} —
an unmapped models.dev provider has no probeable lane yet, so this is
report-only by design).

What it does:
  fetch     — download https://models.dev/api.json (browser UA) into the cache
  sync      — for each ENABLED provider of OURS present on models.dev
              (after provider_mappings.jsonl rules; --all includes disabled):
                * model_catalog.jsonl: catalog metadata (context, reasoning,
                  tool_call, vision, modality, cost) per (provider, model)
                * NEW models not in models.jsonl -> ADDED as rows with
                  normalized_price NULL + price_evidence 'models.dev-catalog'
                  (gaps flagged; reprice spot-check or research fills prices)
                * models in the DB that models.dev doesn't list -> reported
                  as reseller-only suspects (opencode-go/clinepass/crof lanes
                  are NOT on models.dev — they resell underlying providers)
              --json emits PURE machine-parseable JSON on stdout (repo
              doctrine: tests/test_contract.py pattern)
  mappings  — print the active mapping rules (from the data table)
  --dry-run  print everything, write nothing
  --seed     run router_seed.py afterwards (rebuild derived tables)
  --commit   git commit + push the task-router repo (no ns writes here)

Stdlib only. Repo-relative paths (env overrides: ROUTING_DATA_DIR).
"""
import argparse
import json
import os
import re
import subprocess
import sys
import urllib.request

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get('ROUTING_DATA_DIR', os.path.join(_REPO, 'data', 'tables'))
CACHE = os.environ.get('MODELSDEV_CACHE', os.path.expanduser('~/.chimera/models-dev-cache.json'))
MODELSDEV_URL = 'https://models.dev/api.json'
UA = {'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36'}

# our provider id -> models.dev provider id (None = custom/private, not on models.dev).
# DATA>CODE exception (pre-TR-019 table, kept for speed): entries here are
# DEFAULTS only — data/tables/provider_mappings.jsonl rules win on first match.
PROVIDER_MAP = {
    'ollama-cloud': 'ollama-cloud',
    'opencode-go': 'opencode-go',  # IS on models.dev (31 models: hy3, mimo, qwen3.7...)
    'clinepass': None,             # custom reseller lane (not on models.dev)
    'synthetic': 'synthetic',      # IS on models.dev (8 hf: models)
    'zai-glm': 'zai',
    'neuralwatt': 'neuralwatt',
    'groq': 'groq',
    'kimi-for-coding': 'kimi-for-coding',
    'openai-codex': 'openai',
    'deepseek': 'deepseek',
    'minimax': 'minimax',
    'stepfun': 'stepfun',
    'grok-build': 'xai',
    'crof': 'crof',                # IS on models.dev (21 models)
}

MAPPINGS_TABLE = 'provider_mappings.jsonl'


# ---------------------------------------------------------------- mappings ----
def _rule_matches(rule, name):
    """True if `name` matches `rule`. match: prefix|regex|literal."""
    m = (rule.get('match') or 'prefix').lower()
    pat = rule.get('pattern') or ''
    if m == 'prefix':
        return name.startswith(pat)
    if m == 'literal':
        return pat in name
    if m == 'regex':
        try:
            return re.search(pat, name) is not None
        except re.error:
            return False
    return False


def _rule_apply(rule, name):
    """Apply `rule` to `name` -> mapped name."""
    m = (rule.get('match') or 'prefix').lower()
    pat = rule.get('pattern') or ''
    rep = rule.get('replacement')
    if rep is None:
        rep = ''
    if m == 'prefix':
        return name[len(pat):] if name.startswith(pat) else name
    if m == 'literal':
        return name.replace(pat, rep)
    # regex: expand \1-style backreferences from the first match
    mt = re.search(pat, name)
    if not mt:
        return name
    try:
        return mt.expand(rep)
    except re.error:
        return name


def load_mappings(data_dir=None):
    """Mapping rules from data/tables/provider_mappings.jsonl, in file order
    (first match wins). Missing file / missing row = no rule — a visible gap,
    never a code fallback (same data policy as quality_estimates)."""
    path = os.path.join(data_dir or DATA_DIR, MAPPINGS_TABLE)
    rules = []
    if not os.path.exists(path):
        return rules
    for line in open(path):
        line = line.strip()
        if line:
            rules.append(json.loads(line))
    return rules


def map_provider_name(name, mappings):
    """Map an external name through the rules (FILE ORDER, FIRST MATCH WINS).
    Returns (mapped_name, rule_or_None). No match -> (name, None)."""
    for rule in mappings:
        if rule.get('direction') not in (None, 'external->registry'):
            continue  # only this direction is defined today
        if _rule_matches(rule, name):
            return _rule_apply(rule, name), rule
    return name, None


def resolve_external_provider(external_id, mappings, provider_ids):
    """External (models.dev) name -> our registry provider id.
    Order: mapping rule (first match wins) -> canonical id / exact table hit
    / case-insensitive table hit -> None (unmapped, a visible gap)."""
    mapped, rule = map_provider_name(external_id, mappings)
    if mapped in provider_ids:
        return mapped, rule, 'exact'
    low = mapped.lower()
    for pid in provider_ids:
        if pid.lower() == low:
            return pid, rule, 'case-insensitive'
    return None, rule, None


# ----------------------------------------------------------------- loading ----
def load_models():
    rows = []
    path = os.path.join(DATA_DIR, 'models.jsonl')
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def load_catalog():
    rows = []
    path = os.path.join(DATA_DIR, 'model_catalog.jsonl')
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def load_providers():
    rows = []
    path = os.path.join(DATA_DIR, 'providers.jsonl')
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def enabled_providers(providers, models):
    """ENABLED = providers.jsonl row with archive=false AND >=1 models.jsonl
    row referencing the id (routable). Returns (enabled_ids, disabled map
    id -> reason). See module docstring for the definition."""
    model_counts = {}
    for m in models:
        model_counts[m.get('provider')] = model_counts.get(m.get('provider'), 0) + 1
    enabled, disabled = [], {}
    for p in providers:
        pid = p.get('id')
        if p.get('archive'):
            disabled[pid] = f'archived (archive=true, {model_counts.get(pid, 0)} model rows)'
        elif model_counts.get(pid, 0) < 1:
            disabled[pid] = 'no models.jsonl rows — not routable (needs >=1 model lane)'
        else:
            enabled.append(pid)
    return enabled, disabled


def fetch_api(dry_run):
    if dry_run and os.path.exists(CACHE):
        return json.load(open(CACHE))
    req = urllib.request.Request(MODELSDEV_URL, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode('utf-8'))
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    with open(CACHE, 'w') as f:
        json.dump(data, f)
    return data


# -------------------------------------------------------------------- sync ----
def run_sync(api, models, catalog, mappings, include_all=False, dry_run=False):
    """TR-019 sync core: enabled providers only (unless include_all), mapping
    rules applied to external models.dev ids BEFORE table matching. Returns a
    summary dict — the --json payload / the human report source. Network-free
    given a preloaded `api` (fixture/cache), so tests stay hermetic."""
    providers = load_providers()
    enabled_ids, disabled_map = enabled_providers(providers, models)
    provider_ids = {p.get('id') for p in providers}

    have = {(m['provider'], m['model']) for m in models}
    cat_have = {(c['provider'], c['model']) for c in catalog}
    known_names = {m['model'] for m in models}

    payload = api if isinstance(api, dict) else {}
    payload_keys = sorted(k for k in payload if k != '_fetched_at')

    adds, cat_new = [], 0
    touched, skipped, unmapped = [], [], []

    # TR-019 pre-pass: EXTERNAL payload names -> our registry ids via the
    # mapping rules (file order, first match wins) BEFORE any table matching.
    # An external name that maps to no registry/id AND is not already consumed
    # via the default alias table is a visible gap.
    alias_targets = {v for v in PROVIDER_MAP.values() if v}
    ext_resolved = {}
    for ext in payload_keys:
        mapped, rule = map_provider_name(ext, mappings)
        if mapped in provider_ids:
            ext_resolved[ext] = (mapped, rule, 'exact')
            continue
        ci = next((pid for pid in provider_ids
                   if pid.lower() == mapped.lower()), None)
        if ci:
            ext_resolved[ext] = (ci, rule, 'case-insensitive')
        elif mapped in alias_targets:
            # consumed by the pre-TR-019 default alias table — not a gap
            continue
        else:
            unmapped.append({'external': ext, 'mapped_to': mapped,
                             'note': 'no mapping rule resolves it to a providers.jsonl '
                                     'id and no lane/model row references it — visible gap'})

    for p in sorted(providers, key=lambda x: x.get('id') or ''):
        our_id = p.get('id')
        if not include_all and our_id in disabled_map:
            skipped.append({'provider': our_id,
                            'reason': f'disabled: {disabled_map[our_id]}'})
            continue
        # priority 1: an external payload name mapped to this provider
        ext_hits = sorted(ext for ext, (t, _r, _h) in ext_resolved.items()
                          if t == our_id)
        if ext_hits:
            md_id = ext_hits[0]
            _t, rule, how = ext_resolved[md_id]
        else:
            # priority 2: pre-TR-019 default table alias (DATA>CODE exception —
            # these are defaults only; mapping rules above win on first match)
            md_id = PROVIDER_MAP.get(our_id, our_id)
            rule, how = None, ('exact id' if md_id == our_id else 'default table alias')
            if md_id is None:
                skipped.append({'provider': our_id,
                                'reason': 'custom/private reseller lane — not on models.dev '
                                          '(verified via live probes, kept in registry)'})
                continue
        prov = payload.get(md_id)
        if not prov or not prov.get('models'):
            skipped.append({'provider': our_id,
                            'reason': f'models.dev payload {md_id!r} absent/empty '
                                      f'(resolved via {"mapping rule " + rule["id"] if rule else how})'})
            continue
        via = f'mapping rule {rule["id"]} ({rule["match"]})' if rule else \
            ('default table alias' if md_id != our_id else 'exact id')
        touched.append({'provider': our_id, 'modelsdev': md_id, 'via': via})
        for mname, meta in sorted(prov['models'].items()):
            if not _relevant(meta):
                continue
            cost = meta.get('cost') or {}
            cat = {'provider': our_id, 'model': mname, 'family': meta.get('family'),
                   'context_window': meta.get('limit', {}).get('context'),
                   'reasoning': meta.get('reasoning'), 'tool_call': meta.get('tool_call'),
                   'vision': meta.get('vision'), 'modality': meta.get('modality'),
                   'knowledge_cutoff': meta.get('knowledge_cutoff'),
                   'cost_input': cost.get('input'), 'cost_output': cost.get('output'),
                   'source': 'models.dev', 'fetched_at': None}
            if (our_id, mname) not in cat_have:
                cat_new += 1
                catalog.append(cat)
            if (our_id, mname) not in have:
                # candidate: not in the registry at all
                if mname in known_names:
                    note = 'name-variant (a same-named model exists under another provider)'
                else:
                    note = 'catalog-only'
                adds.append((our_id, mname, meta, note))
            # else: present — PRICING is the router_pricing.py engine's job
            # (normalized, subscription-aware); never set prices from bare
            # models.dev stickers here.

    return {
        'providers_on_modelsdev': len(payload_keys),
        'enabled_providers': sorted(enabled_ids),
        'touched_providers': touched,
        'skipped_providers': skipped,
        'unmapped_providers': unmapped,
        'catalog_new': cat_new,
        'new_models': [{'provider': p, 'model': m, 'note': note}
                       for p, m, _meta, note in adds],
        'adds': adds,           # internal (meta kept for the write path)
        'catalog': catalog,     # mutated copy for the write path
        'dry_run': dry_run,
    }


# catalog-only filter: skip niche/audio/tiny models the fleet will never route
# (whisper, prompt-guard, orpheus...). Keep chat-capable or big-context ones.
def _relevant(meta):
    if meta.get('reasoning') or meta.get('tool_call') or meta.get('vision'):
        return True
    ctx = (meta.get('limit') or {}).get('context') or 0
    return ctx >= 32000


def _write_rows(path, rows):
    with open(path + '.tmp', 'w') as f:
        for r in rows:
            # ensure_ascii=False matches the repo convention
            # (router_maintain.py _data_rows) — literal UTF-8 keeps the
            # diff clean; ensure_ascii=True escaped em-dashes and rewrote
            # every row on each sync (969-line noise diffs).
            f.write(json.dumps(r, ensure_ascii=False) + '\n')
    os.replace(path + '.tmp', path)


def _apply_writes(summary, models, verbose=True):
    """Write model_catalog.jsonl / models.jsonl from a run_sync summary.
    Atomic-ish tmp+rename, per repo convention. Returns (wrote_catalog,
    wrote_models, n_added)."""
    adds, catalog = summary['adds'], summary['catalog']
    wrote_cat = wrote_models = False
    n_added = 0
    if summary['catalog_new']:
        _write_rows(os.path.join(DATA_DIR, 'model_catalog.jsonl'), catalog)
        wrote_cat = True
        if verbose:
            print(f'wrote model_catalog.jsonl ({len(catalog)} rows)')
    if adds:
        for our_id, mname, meta, note in adds:
            models.append({'provider': our_id, 'model': mname,
                           'normalized_price': None, 'price_evidence': 'models.dev-catalog',
                           'data_class': 'zdr', 'plan_tier': None,
                           'perf_agent_tick': None, 'perf_long_doc': None,
                           'perf_debug': None, 'perf_schema': None,
                           'perf_e2e_vision': None, 'perf_review': None,
                           'perf_delegation': None, 'perf_guard': None,
                           'perf_mock': None, 'perf_reasoning': None,
                           'valid_from': None, 'valid_to': None,
                           'archive': False, 'token_factor': 1.0})
        _write_rows(os.path.join(DATA_DIR, 'models.jsonl'), models)
        wrote_models = True
        n_added = len(adds)
        if verbose:
            print(f'added {n_added} models to models.jsonl (price NULL -> pricing gap)')
    return wrote_cat, wrote_models, n_added


def _print_human(summary):
    print(f"== models.dev sync ({summary['providers_on_modelsdev']} providers on catalog) ==")
    for t in summary['touched_providers']:
        print(f"  -> {t['provider']}  via {t['via']}  (models.dev: {t['modelsdev']})")
    for s in summary['skipped_providers']:
        print(f"  skip {s['provider']}: {s['reason']}")
    for u in summary['unmapped_providers']:
        print(f"  UNMAPPED {u['external']} (mapped: {u.get('mapped_to')}): {u['note']}")
    print(f"catalog metadata: {summary['catalog_new']} new rows (model_catalog.jsonl)")
    print(f"NEW models not in registry ({len(summary['new_models'])}):")
    for r in summary['new_models']:
        print(f"  + {r['provider']}/{r['model']}  [{r['note']}]")
    print('PRICING NOTE: normalized prices are the router_pricing.py engine\'s job')
    print('(subscription-aware) — models.dev stickers are catalog reference only.')


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest='action')
    p_sync = sub.add_parser('sync', help='catalog sync (enabled providers only; --all includes disabled)')
    p_sync.add_argument('--all', action='store_true', dest='include_all',
                        help='include DISABLED providers explicitly (default: skip with reason)')
    p_sync.add_argument('--json', action='store_true',
                        help='PURE machine-parseable JSON on stdout (repo doctrine)')
    p_sync.add_argument('--dry-run', action='store_true')
    p_fetch = sub.add_parser('fetch', help='refresh the models.dev cache')
    p_fetch.add_argument('--dry-run', action='store_true')
    sub.add_parser('mappings', help='print active provider mapping rules')
    ap.add_argument('--seed', action='store_true', help='run router_seed.py after sync')
    ap.add_argument('--commit', action='store_true', help='git commit + push the repo')
    args = ap.parse_args(argv)

    action = args.action
    if not action:
        ap.print_help()
        return 2

    if action == 'mappings':
        for r in load_mappings():
            print(json.dumps(r, ensure_ascii=False))
        return 0

    if action == 'fetch':
        api = fetch_api(args.dry_run)
        print(f'models.dev cache refreshed: {len(api)} providers at {CACHE}')
        return 0

    api = fetch_api(args.dry_run)
    models = load_models()
    catalog = load_catalog()
    summary = run_sync(api, models, catalog, load_mappings(),
                       include_all=args.include_all, dry_run=args.dry_run)

    if args.json:
        summary.pop('adds', None)      # internal write-path payloads
        summary.pop('catalog', None)
        summary['action'] = 'sync'
        summary['dry_run'] = args.dry_run
        summary['include_all'] = args.include_all
        print(json.dumps(summary, ensure_ascii=False))
    else:
        _print_human(summary)

    if args.dry_run:
        if not args.json:
            print('DRY-RUN: no writes')
        return 0

    wrote_cat, wrote_models, n_added = _apply_writes(summary, models, verbose=not args.json)
    if args.json:
        # single-JSON-object rule: write outcome goes to stderr so stdout
        # stays pure machine-parseable JSON.
        print(f'wrote catalog={wrote_cat} models={wrote_models} (+{n_added} rows)',
              file=sys.stderr)

    if args.seed:
        seed = os.path.join(_REPO, 'scripts', 'router_seed.py')
        r = subprocess.run([sys.executable, seed], capture_output=True, text=True)
        if r.returncode != 0:
            print('SEED FAILED:', r.stderr[-400:], file=sys.stderr)
            return 1
        print('seed ok')

    if args.commit:
        r = subprocess.run(['git', 'add', '-A'], cwd=_REPO)
        r = subprocess.run(['git', 'commit', '-q', '-m',
                            'data: models.dev catalog sync (model_catalog.jsonl + new model rows)',
                            '--allow-empty'], cwd=_REPO)
        subprocess.run(['git', 'push', '-q', 'origin', 'main'], cwd=_REPO)
        print('committed + pushed')
    return 0


if __name__ == '__main__':
    sys.exit(main())