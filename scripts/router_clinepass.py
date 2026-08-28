#!/usr/bin/env python3
"""router_clinepass.py — clinepass catalog + billing sync (Bane 2026-08-27).

clinepass is NOT on models.dev. Its real data comes from its own API:
  GET  https://api.cline.bot/api/v1/models  -> 417 models (id = org/model)
  GET  https://api.cline.bot/api/v1/plans   -> Cline Pass flat $9.99/mo,
       2-5x usage vs API rate, caps $1B/5h $2.5B/7d $5B/30d, included list
       (Kimi K3, GLM 5.2, Kimi K2.6, K2.7 Code, Mimo v2.5/Pro, Minimax M3,
       Qwen3.7 Plus/Max, DeepSeek V4 Pro/Flash), monthly plan carries
       features.discount=0.5 (promo flag — verify)

What it does (idempotent):
  sync  — model_catalog.jsonl (api_id = CALLABLE form 'cline-pass/<bare>';
          the org/model ids in /models are Cline-product-surface ONLY, 403 on
          raw API — verified live) + models.jsonl rows (price NULL ->
          pricing gap; the pricing engine prices the flat-plan included set)
        — plan_terms.jsonl row for clinepass (flat_subscription) if missing
        — temporary_discounts.jsonl rows for the :free lanes (free until
          revoked) if missing
  --dry-run  print, write nothing. Mutually exclusive with --commit/--push
             (TR-028: a dry run must NEVER touch git).
  --commit   git commit ONLY the 4 data files this sync owns (never a
             whole-tree add — TR-028: -A can sweep unrelated concurrent work).
             Uses the repo's real identity + the standard co-author trailer.
  --push     push origin <branch>; implies --commit.

Stdlib only. Repo-relative paths. Key: CLINEPASS_API_KEY in ~/.hermes/.env.
"""
import argparse
import datetime
import json
import os
import subprocess
import sys
import urllib.request

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get('ROUTING_DATA_DIR', os.path.join(_REPO, 'data', 'tables'))
BASE = 'https://api.cline.bot/api/v1'
UA = {'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36'}

# media/embedding/safety ids are not chat-routeable
SKIP = ('embed', 'tts', 'asr', 'audio', 'image', 'video', 'whisper', 'guard',
        'modera', 'rerank', 'speech', 'stt', 'ocr', 'img', 'translate')


def _key():
    p = os.path.expanduser('~/.hermes/.env')
    if os.path.exists(p):
        for line in open(p):
            if 'CLINEPASS_API_KEY' in line and '=' in line:
                k = line.strip().split('=', 1)[1].strip().strip('"\'')
                if k and k.lower() not in ('none', 'null', 'changeme'):
                    return k
    return None


def _get(path):
    key = _key()
    assert key, 'CLINEPASS_API_KEY not found in ~/.hermes/.env'
    req = urllib.request.Request(BASE + path, headers={'Authorization': f'Bearer {key}', **UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def _rows(name):
    path = os.path.join(DATA_DIR, f'{name}.jsonl')
    out = []
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def _write(name, rows):
    path = os.path.join(DATA_DIR, f'{name}.jsonl')
    with open(path + '.tmp', 'w') as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + '\n')
    os.replace(path + '.tmp', path)


def _chat_ok(mid):
    low = mid.lower()
    return not any(s in low for s in SKIP)


def _bare(mid):
    return mid.split('/', 1)[1] if '/' in mid else mid


def sync(dry_run):
    api = _get('/models')
    raw = sorted(d['id'] for d in api.get('data', []))
    print(f'clinepass API: {len(raw)} models')

    models = _rows('models')
    catalog = _rows('model_catalog')
    terms = _rows('plan_terms')
    discounts = _rows('temporary_discounts')
    have = {(m['provider'], m['model']) for m in models}
    have_cat = {(c['provider'], c['model']) for c in catalog}
    have_terms = {t['provider'] for t in terms}
    have_disc = {(d['provider'], d['model']) for d in discounts}
    today = datetime.date.today().isoformat()
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()

    added = skipped_media = skipped_dup = 0
    for mid in raw:
        if not _chat_ok(mid):
            skipped_media += 1
            continue
        name = _bare(mid)
        if ('clinepass', name) in have:
            skipped_dup += 1
            continue
        models.append({'provider': 'clinepass', 'model': name, 'normalized_price': None,
                       'price_evidence': 'clinepass-api', 'data_class': 'zdr', 'plan_tier': None,
                       'token_factor': 1.0, 'archive': False, 'valid_from': today, 'valid_to': None})
        have.add(('clinepass', name))
        added += 1
        if ('clinepass', name) not in have_cat:
            catalog.append({'provider': 'clinepass', 'model': name, 'api_id': f'cline-pass/{name}',
                            'api_note': 'API-callable form (org/model ids are Cline-product-only)',
                            'context_window': None, 'reasoning': None, 'tool_call': None,
                            'vision': None, 'modality': None, 'knowledge_cutoff': None,
                            'cost_input': None, 'cost_output': None, 'family': None,
                            'source': 'clinepass-api', 'fetched_at': now, 'archive': False})
            have_cat.add(('clinepass', name))

    # plan_terms — DO NOT fabricate from code (Bane 2026-08-27): the included
    # list / plan cost / multiplier are provider facts that live in the DATA
    # file plan_terms.jsonl. The plans API list is known STALE (11 vs docs 13)
    # and hardcoding here caused the disable/re-enable flip-flop. If the row
    # is missing, that is a visible gap for the research agent to fill from
    # docs.cline.bot — never a code fallback.
    term_added = 0
    if 'clinepass' not in have_terms:
        print('WARNING: plan_terms row for clinepass MISSING — research agent must fill '
              'from docs.cline.bot/getting-started/clinepass (do NOT hardcode)')

    # temporary discounts for :free lanes
    disc_added = 0
    for mid in raw:
        if not mid.endswith(':free'):
            continue
        name = _bare(mid)
        if ('clinepass', name) in have_disc:
            continue
        discounts.append({'provider': 'clinepass', 'model': name,
                          'discount_type': 'free', 'value': 1.0,
                          'valid_from': today, 'valid_to': None,
                          'source': 'clinepass-api :free lane',
                          'note': 'temporary free lane — verify end date'})
        have_disc.add(('clinepass', name))
        disc_added += 1

    if dry_run:
        print(f'DRY-RUN: would add {added} models, {term_added} plan_terms, {disc_added} discounts; '
              f'{skipped_media} media skipped, {skipped_dup} dup names')
        return 0

    _write('models', models)
    _write('model_catalog', catalog)
    _write('plan_terms', terms)
    _write('temporary_discounts', discounts)
    print(f'wrote: +{added} models, +{term_added} plan_terms, +{disc_added} discounts '
          f'({skipped_media} media skipped, {skipped_dup} dup)')
    return 0


CO_AUTHOR = "Alexis Okuwa <wojonstech@gmail.com>"
# Files this sync owns — the ONLY paths ever staged (TR-028: never a
# whole-tree add, which can sweep unrelated concurrent work).
SYNC_FILES = ['data/tables/models.jsonl', 'data/tables/model_catalog.jsonl',
              'data/tables/plan_terms.jsonl', 'data/tables/temporary_discounts.jsonl']


def _git(*args, check=False):
    p = subprocess.run(['git', '-C', _REPO, *args], capture_output=True, text=True)
    if check and p.returncode != 0:
        raise RuntimeError(f'git {" ".join(args)} failed: {(p.stderr or p.stdout).strip()[:400]}')
    return p


def _branch():
    p = _git('symbolic-ref', '--short', 'HEAD')
    return p.stdout.strip() or 'main'


def commit_and_push(do_push):
    """Stage ONLY the sync-owned files, commit with repo identity + co-author,
    push when requested. Returns 0 on success, non-zero on failure."""
    try:
        # stage the 4 owned paths explicitly — never a whole-tree add
        _git('add', '--', *SYNC_FILES, check=True)
        # only the sync-owned files may be committed — intersect the staged
        # set with SYNC_FILES so a pre-staged unrelated file can never ride along
        staged = _git('diff', '--cached', '--name-only')
        owned = [ln.strip() for ln in staged.stdout.splitlines() if ln.strip()]
        owned = [p for p in owned if p in SYNC_FILES]
        if not owned:
            print('nothing to commit — no changes staged')
            return 0
        # commit with explicit pathspec — even if the user had OTHER files
        # staged beforehand, only the sync-owned paths enter this commit
        msg = f'chore(data): clinepass catalog sync\n\nCo-authored-by: {CO_AUTHOR}'
        p = _git('commit', '-m', msg, '--', *owned)
        if p.returncode != 0:
            print(f'commit failed: {(p.stderr or p.stdout).strip()[:400]}', file=sys.stderr)
            return 1
        first = (p.stdout or '').splitlines()
        print(f'committed: {first[0] if first else "ok"}')
        if do_push:
            _git('push', 'origin', _branch(), check=True)
            print('pushed origin', _branch())
        return 0
    except RuntimeError as e:
        print(f'git error: {e}', file=sys.stderr)
        return 1
    except OSError as e:
        print(f'git error: {e}', file=sys.stderr)
        return 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('action', choices=['sync'])
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--commit', action='store_true')
    ap.add_argument('--push', action='store_true')
    args = ap.parse_args(argv)
    if args.dry_run and (args.commit or args.push):
        ap.error('--dry-run is mutually exclusive with --commit/--push (a dry '
                 'run must never touch git)')
    do_commit = args.commit or args.push  # --push implies --commit
    try:
        rc = sync(args.dry_run)
    except Exception as e:  # noqa: BLE001 — propagate as non-zero, never fake success
        print(f'sync failed: {e}', file=sys.stderr)
        return 1
    if rc != 0:
        return rc
    if do_commit and not args.dry_run:
        return commit_and_push(do_push=args.push)
    return 0


if __name__ == '__main__':
    import sys
    sys.exit(main())
