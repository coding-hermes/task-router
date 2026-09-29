#!/usr/bin/env python3
"""router_release_backfill.py — turn vendor release rosters into release_date.

Companion to router_rank_audit.py. The audit reports which families cannot be
ordered because the launch dates are missing; this consumes the researched
rosters (one JSON per vendor family) and stamps `models.release_date`, then
proposes — never performs — supersession retirements.

Roster shape (one file per vendor, any filename under the roster dir):
  {"models": [{"vendor": "DeepSeek", "model": "deepseek-v4.1-flash",
              "release_date": "2026-08-14", "supersedes": "deepseek-v4-flash",
              "source": "https://api-docs.deepseek.com/...", "confidence": "high"}],
   "gaps": ["..."]}

Rules (from the model-rank-maintenance skill):
  - a launch date is per MODEL, so one roster entry stamps every provider lane
    carrying those weights
  - never guess: an entry without a parseable ISO date is skipped and reported
  - never silently overwrite: an existing release_date that disagrees is a
    CONFLICT and is reported, not replaced (use --force to override)
  - retirement is PROPOSED only, and only where the successor exists on the SAME
    provider and is itself ranked (rank first, retire second)

Usage:
  ~/.hermes/venvs/board/bin/python3 scripts/router_release_backfill.py \
      [--rosters DIR] [--dry-run|--commit] [--force] [--report PATH]
"""
import argparse
import collections
import datetime
import json
import os
import re
import shutil
import sys
import time

_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)
import lifecycle_gate  # noqa: E402  (TR-199: R4 no anonymous dates)

REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
DATA_DIR = os.environ.get('ROUTING_DATA_DIR', os.path.join(REPO, 'data', 'tables'))
DEFAULT_ROSTERS = os.path.expanduser('~/model_bench/rosters')


def load(name):
    with open(os.path.join(DATA_DIR, f'{name}.jsonl'), encoding='utf-8') as fh:
        return [json.loads(l) for l in fh if l.strip()]


def norm(name):
    n = str(name).lower().strip().lstrip('~')
    n = re.sub(r'^accounts/fireworks/(models|routers)/', '', n)
    n = re.sub(r'^[a-z0-9_.\-]+/', '', n)
    return re.sub(r':(free|batch|latest)$', '', n)


def superseded_ids(text):
    """Model ids out of a free-text `supersedes` field.

    Rosters write this field in prose — "deepseek-v4-flash-0731 and
    deepseek-v4-flash-vision-exp (both RETIRED)", "grok-4.3 (as flagship coding
    model)", "Llama 4 Maverick / Scout". Taking the string literally produced a
    key that matched nothing, so a real supersession link was silently dropped
    (measured: deepseek-foreman/deepseek-v4-flash-vision-exp stayed ACTIVE).
    Split on separators, strip parentheticals and trailing prose, drop the
    sentences that carry no id.
    """
    if not text:
        return []
    s = str(text)
    s = re.sub(r'\([^)]*\)', ' ', s)                 # (both RETIRED), (as flagship...)
    parts = re.split(r'\s*(?:,|;|\+|/|\band\b|\bor\b|\bvs\b)\s*', s)
    out = []
    for p in parts:
        p = p.strip().strip('.').strip()
        # keep only tokens that look like a model id, not prose
        if not p or ' ' in p:
            continue
        if re.fullmatch(r'(none|n/?a|nothing|same|unknown|-+)', p, re.I):
            continue
        if not re.search(r'[a-z]', p) or not re.search(r'\d', p):
            continue
        out.append(p)
    return out


def plausible(d):
    try:
        dt = datetime.date.fromisoformat(d)
    except (TypeError, ValueError):
        return False
    return datetime.date(2025, 1, 1) <= dt <= datetime.date.today() + datetime.timedelta(days=7)


def _write_models(models, path=None):
    """Rewrite models.jsonl (path defaults to <DATA_DIR>/models.jsonl).
    TR-199 (spec R4): the write helper itself is gated, so no call site can
    bypass the provenance check."""
    lifecycle_gate.gate_rows('models', models)
    path = path or os.path.join(DATA_DIR, 'models.jsonl')
    with open(path, 'w', encoding='utf-8') as fh:
        for m in models:
            print(json.dumps(m, ensure_ascii=False), file=fh)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--rosters', default=DEFAULT_ROSTERS)
    ap.add_argument('--commit', action='store_true', help='write models.jsonl (default: dry run)')
    ap.add_argument('--force', action='store_true', help='override an existing release_date on conflict')
    ap.add_argument('--retire-strict', action='store_true',
                    help='also retire old lanes whose successor is ranked AT LEAST as well '
                         '(strict upgrade). Regressions are reported, never retired.')
    ap.add_argument('--openrouter-catalog', action='store_true',
                    help='fill STILL-BLANK dates from the live OpenRouter catalog `created` '
                         'field. These are aggregator LISTING dates, not vendor launch dates, '
                         'so they never override a researched date and the provenance is '
                         'written to model_notes.')
    ap.add_argument('--catalog-file', default=None, help='use a cached catalog JSON instead of fetching')
    ap.add_argument('--report', default=os.path.expanduser('~/model_bench/roster_backfill_report.json'))
    args = ap.parse_args()

    if not os.path.isdir(args.rosters):
        print(f'no roster dir at {args.rosters} — nothing to do')
        return 0

    entries, gaps, files = [], [], []
    for fn in sorted(os.listdir(args.rosters)):
        if not fn.endswith('.json'):
            continue
        files.append(fn)
        try:
            doc = json.load(open(os.path.join(args.rosters, fn), encoding='utf-8'))
        except (OSError, ValueError) as exc:
            print(f'WARN unreadable roster {fn}: {exc}')
            continue
        for m in doc.get('models') or []:
            if m.get('model'):
                entries.append(m)
        gaps.extend(doc.get('gaps') or [])

    print(f'rosters read: {len(files)} file(s), {len(entries)} model entries, {len(gaps)} stated gaps')
    if not entries:
        print('no entries — nothing to do')
        return 0

    by_model, bad, supersedes = {}, [], {}
    for e in entries:
        d, name = e.get('release_date'), e['model']
        if not plausible(d):
            bad.append({'model': name, 'release_date': d, 'reason': 'missing or implausible date'})
            continue
        k = norm(name)
        if k in by_model and by_model[k] != d:
            bad.append({'model': name, 'release_date': d, 'reason': f'roster disagrees with {by_model[k]}'})
            continue
        by_model[k] = d
        for old_id in superseded_ids(e.get('supersedes')):
            supersedes.setdefault(norm(old_id), set()).add(k)

    models = load('models')
    tiers = collections.defaultdict(set)
    tier_map = collections.defaultdict(dict)
    for r in load('model_tier'):
        tiers[r['model']].add(r['category'])
        tier_map[r['model']][r['category']] = r['tier']

    # Evidence provenance, so a DECLARATION cannot block a retirement.
    # A `models.jsonl:perf_*` ref is a hand-declared seed value, not a measurement; an
    # `estimate` is QUALITY_ESTIMATES; `family` is inherited from another lane. Only a
    # `bench:` ref is something we actually measured on this model. Comparing a tier
    # derived from a declaration against a tier derived from a measurement is not
    # evidence, and letting it block means the newer model can never displace the older
    # one — measured live: deepseek-v4-flash held the retirement of deepseek-v4.1-flash
    # on agent_tick/debug/delegation with declared 0.75 against measured 0.637.
    perf_src = {}
    for r in load('model_perf'):
        perf_src[(r['model'], r['category'])] = (r.get('source_ref') or '')

    def measured(model, category):
        return perf_src.get((model, category), '').startswith('bench:')

    def active(m):
        return not (m.get('archive') or m.get('valid_to') or m.get('disabled')) and m.get('normalized_price') is not None

    stamped, conflicts, missing = 0, [], collections.Counter()
    for m in models:
        k = norm(m['model'])
        d = by_model.get(k)
        if not d:
            missing[m['provider']] += 1
            continue
        cur = m.get('release_date')
        if cur and cur != d and not args.force:
            conflicts.append({'provider': m['provider'], 'model': m['model'], 'have': cur, 'roster': d})
            continue
        if cur != d:
            stamped += 1
        m['release_date'] = d

    # ---- aggregator LISTING dates for rows still blank (opt-in, never overriding)
    catalog_filled = 0
    if args.openrouter_catalog:
        import urllib.request
        if args.catalog_file:
            doc = json.load(open(args.catalog_file, encoding='utf-8'))
        else:
            with urllib.request.urlopen('https://openrouter.ai/api/v1/models', timeout=60) as resp:
                doc = json.load(resp)
        created = {}
        for row in doc.get('data') or []:
            if row.get('id') and row.get('created'):
                created[row['id']] = datetime.datetime.fromtimestamp(
                    row['created'], datetime.timezone.utc).date().isoformat()
        for m in models:
            if m['provider'] != 'openrouter' or m.get('release_date'):
                continue                       # researched dates always win
            d = created.get(m['model'])
            if d and plausible(d):
                m['release_date'] = d
                catalog_filled += 1
        print(f'OpenRouter LISTING dates applied to blank rows: {catalog_filled}')

    # supersession PROPOSALS (never executed here)
    proposals, blocked, regressions = [], [], []
    seen = set()

    def classify(m, s):
        """The per-category retirement gate. A candidate is not a verdict.

        Compare PER CATEGORY, not by count: counting conflates a successor that
        has not been MEASURED on a category (gpt-6-sol: 10 vs the incumbent's 18)
        with one that was measured and came out WORSE (glm-5.3-flashx, a
        serving-speed variant). Counting made 32 holds look like regressions.
        """
        key = (m['provider'], m['model'], s['model'])
        if key in seen:
            return
        seen.add(key)
        rec = {'provider': m['provider'], 'old': m['model'], 'new': s['model'],
               'new_ranked': len(tiers.get(s['model'], ())), 'old_ranked': len(tiers.get(m['model'], ()))}
        old_t = tier_map.get(m['model'], {})
        new_t = tier_map.get(s['model'], {})
        if len(new_t) < 6:
            rec['reason'] = 'successor not ranked enough yet'
            blocked.append(rec)
            return
        unmeasured = sorted(set(old_t) - set(new_t))
        # A category only blocks when the OLD side's number is a measurement. Otherwise
        # the old model is ahead on paper only (declaration / estimate / inherited) and
        # the gap is not evidence — see the provenance note where perf_src is built.
        weaker = sorted(c for c in (set(old_t) & set(new_t))
                        if new_t[c] < old_t[c] and measured(m['model'], c))
        weaker_unproven = sorted(c for c in (set(old_t) & set(new_t))
                                 if new_t[c] < old_t[c] and not measured(m['model'], c))
        if weaker_unproven:
            rec['weaker_unproven'] = weaker_unproven
        if unmeasured or weaker:
            # Report BOTH when both hold: a successor can be simultaneously
            # unmeasured on some categories and measured WORSE on others
            # (muse-spark-1.3 vs 1.2). Naming only the first hides the stronger
            # reason, and "weaker" is the one that must not be retired past.
            parts = []
            if weaker:
                parts.append('successor weaker on ' + ','.join(weaker[:4]))
            if unmeasured:
                parts.append('unmeasured on ' + ','.join(unmeasured[:4]))
            rec['reason'] = ' + '.join(parts)
            rec['unmeasured'] = unmeasured
            rec['weaker'] = weaker
            regressions.append(rec)
        else:
            proposals.append(rec)

    # CANDIDATE SOURCE 1 — roster `supersedes` links (vendor-declared handoffs)
    for old_key, new_keys in supersedes.items():
        for m in models:
            if norm(m['model']) != old_key or not active(m):
                continue
            for nk in new_keys:
                # every provider carrying the old model, not just the first one
                # found (an early break here silently produced zero proposals)
                sibs = [s for s in models if norm(s['model']) == nk and s['provider'] == m['provider']]
                for s in sibs:
                    if active(s):
                        classify(m, s)

    # CANDIDATE SOURCE 2 — the family matcher in router_rank_audit.py.
    # Roster links only exist where a vendor TOLD us about the handoff; the family
    # matcher derives the same relationship from ids and launch dates. Without
    # this, the audit detected 16 "successor ranked -> retire the old" families
    # for weeks while this executor — reading rosters only — proposed none of
    # them. One matcher, two consumers: the detector and the executor cannot
    # disagree about what a pair is.
    try:
        import sys as _sys
        _sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
        import router_rank_audit as _audit
        _pairs, _ = _audit.family_pairs([m for m in models if active(m)], tiers)
    except Exception as exc:   # noqa: BLE001
        print(f'NOTE: family matcher unavailable ({str(exc)[:60]}); roster links only')
        _pairs = []
    for e in _pairs:
        m = next((x for x in models if x['provider'] == e['provider'] and x['model'] == e['old']['model']), None)
        s = next((x for x in models if x['provider'] == e['provider'] and x['model'] == e['new']['model']), None)
        if m and s and active(m) and active(s):
            classify(m, s)
    print(f'candidate pairs evaluated (roster + family matcher): {len(seen)}')

    retired = []
    if args.retire_strict:
        today = datetime.date.today().isoformat()
        by_pair = {(p['provider'], p['old'], p['new']) for p in proposals}
        for m in models:
            for prov, old, new in by_pair:
                if m['provider'] == prov and m['model'] == old and active(m):
                    m['valid_to'] = today
                    m['replaced_by'] = f'{prov}/{new}'
                    m['lifecycle_source'] = (
                        f'rank-supersession {today}: {new} carries >= the tier coverage of {old} '
                        f'({len(tiers.get(new, ()))}t vs {len(tiers.get(old, ()))}t) and the vendor roster '
                        f'links it as the successor; retired so the fleet stops selecting last generation')
                    m['lifecycle_checked_at'] = today
                    retired.append({'provider': prov, 'old': old, 'new': new})
                    break

    print(f'\nrows stamped: {stamped}   rows already correct/unchanged: '
          f'{sum(1 for m in models if by_model.get(norm(m["model"])))}')
    print(f'conflicts (existing date disagrees): {len(conflicts)}')
    print(f'roster names matched nothing in the registry: '
          f'{len([1 for k in by_model if not any(norm(m["model"]) == k for m in models)])}')
    print(f'strict-upgrade retirements available: {len(proposals)}')
    print(f'HOLD — successor measured weaker (rank it first): {len(regressions)}')
    _unproven = [r for r in regressions if r.get('weaker_unproven')]
    print(f'   of which blocked ONLY by unproven old-side values (declaration/estimate/'
          f'inherited): {len(_unproven)}')
    print(f'HOLD — successor not ranked enough yet: {len(blocked)}')
    for p in proposals[:10]:
        print(f"   {p['provider']:14s} {p['old'][:34]:34s} ({p['old_ranked']}t) -> {p['new'][:30]:30s} ({p['new_ranked']}t)")
    if args.retire_strict:
        print(f'\nRETIRED this run: {len(retired)}')
        for p in retired:
            print(f"   {p['provider']:14s} {p['old'][:34]:34s} -> {p['new'][:34]}")

    report = {'generated_at': datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds'),
              'roster_files': files, 'stamped': stamped, 'conflicts': conflicts,
              'unparseable': bad, 'retire_proposals': proposals, 'retire_hold_regression': regressions,
              'retire_hold_blocked': blocked, 'retired_this_run': retired,
              'roster_gaps': gaps, 'rows_without_roster_date_by_provider': dict(missing.most_common(15))}
    with open(args.report, 'w', encoding='utf-8') as fh:
        json.dump(report, fh, indent=1, ensure_ascii=False)
    print('report:', args.report)

    if not args.commit:
        print('\nDRY RUN — nothing written. Re-run with --commit to stamp models.jsonl.')
        return 0

    path = os.path.join(DATA_DIR, 'models.jsonl')
    # TR-199 (spec R4): no anonymous dates — the gate runs BEFORE the backup
    # so a refused write leaves the committed file untouched.
    lifecycle_gate.gate_rows('models', models)
    shutil.copy(path, f'/tmp/models.jsonl.bak-{time.strftime("%Y%m%d-%H%M%S")}')
    _write_models(models)

    if catalog_filled:
        # Provenance, because release_date must never quietly mean two things.
        note_path = os.path.join(DATA_DIR, 'model_notes.jsonl')
        today = datetime.date.today().isoformat()
        note = {
            'provider': 'openrouter', 'model': '*',
            'note': (f'{catalog_filled} openrouter lanes had NO vendor launch date and were given the '
                     f'OpenRouter catalog `created` value instead — that is the aggregator LISTING date, '
                     f'NOT the maker release date. Rows with a researched date were never touched. '
                     f'Use for ordering/age only; treat as a lower-confidence bound. Source: '
                     f'https://openrouter.ai/api/v1/models'),
            'source': f'catalog-listing-date-{today}', 'valid_from': today,
        }
        with open(note_path, 'a', encoding='utf-8') as fh:
            fh.write(json.dumps(note, ensure_ascii=False) + '\n')
        print(f'provenance note appended to model_notes.jsonl')

    print(f'\nCOMMITTED to working tree: {path} ({len(models)} rows). Re-seed next.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
