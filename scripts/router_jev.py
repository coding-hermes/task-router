#!/usr/bin/env python3
"""router_jev.py — JEV as a SECOND, cheaper complexity scorer (Bane 2026-09-20).

The TR-067 classifier asks a chat model for a full per-category matrix (prompt
as a versioned file). JEV is the cheap alternative the owner asked for: ONE
decisions call, input cheap / output free (~$0.000015 measured), answering a
`score` question — "how hard is this input?" — on a 0..2 scale where 1 is the
MIDDLE. The score then selects a band whose LEVELS are the same complexity
matrix the router already consumes, so chain selection is unchanged.

Design constraints (matching the repo's rules):
  * DATA > CODE — the score→matrix bands live in
    `data/classifier/jev-bands.jsonl`, not in this file. Edit the data to
    retune, no code change.
  * The scalar CANNOT see which categories a task stresses: a band carries a
    fixed matrix (coding-oriented by default). That limitation is stated in the
    result (`band_note`) instead of being hidden behind a fake per-category
    answer.
  * FAIL-CLOSED, VISIBLY: no key / transport error / unusable answer →
    matrix=None + `problems`, and the caller degrades exactly like the
    classifier path (source recorded as the degrade, never a silent default).
  * The HTTP call is INJECTABLE (`http=callable(url, payload, headers, timeout)
    -> (status, dict)`) so tests never touch the network.

Scale: `criteria` fixes the scale for JEV, so the same request shape can be
run as 0..2 (default, 1 = middle) or 0..4 etc. by editing BAND data + criteria.
"""
import json
import os
import re
import sys
import urllib.error
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BANDS_PATH = os.path.join(REPO, 'data', 'classifier', 'jev-bands.jsonl')

JEV_URL = 'https://openrouter.ai/api/alpha/decisions'
JEV_MODEL = os.environ.get('JEV_MODEL', 'typesafe/jev-1.13')
#: scale used for the hardness question (0..2 with 1 in the middle — Bane).
HARDNESS_CRITERIA = ['0 = trivial, one obvious edit',
                     '1 = moderate, needs some reasoning',
                     '2 = hard, deep debugging or multi-file reasoning']


# ------------------------------------------------------------------ keys ---

def _env_file_keys(names):
    """Read the NAMED keys from ~/.hermes/.env. Never returns, logs or prints
    anything else from that file."""
    out = []
    path = os.path.expanduser('~/.hermes/.env')
    if not os.path.exists(path):
        return out
    try:
        with open(path, errors='replace') as fh:
            text = fh.read()
    except OSError:
        return out
    for name in names:
        m = re.search(rf'^{re.escape(name)}=(.*)$', text, re.M)
        if m:
            v = m.group(1).strip().strip('"').strip("'")
            if v:
                out.append((name, v))
    return out


def key_candidates():
    """(name, value) pairs in priority order: OR_JEV (the dedicated workspace
    key Bane provisioned 2026-09-20) then the generic OpenRouter keys."""
    names = ('OR_JEV', 'JEV_API_KEY', 'OPENROUTER_API_KEY', 'OR_API_KEY')
    out, seen = [], set()
    for name, val in _env_file_keys(names):
        if val not in seen:
            seen.add(val)
            out.append((name, val))
    for name in names:
        v = os.environ.get(name)
        if v and v not in seen:
            seen.add(v)
            out.insert(0, (name, v))
    return out


# ----------------------------------------------------------------- bands ---

def load_bands(path=None):
    """[{band, score_min, score_max, title, levels, note}] from DATA."""
    rows = []
    with open(path or BANDS_PATH) as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    rows.sort(key=lambda r: r['score_min'])
    return rows


def band_for(score, bands=None):
    """The band whose [score_min, score_max) contains score; the LAST band
    closes its range so a clamped top score still lands."""
    bands = bands if bands is not None else load_bands()
    if not bands or score is None:
        return None
    for i, b in enumerate(bands):
        last = i == len(bands) - 1
        if b['score_min'] <= score and (score < b['score_max'] or (last and score <= b['score_max'])):
            return b
    return bands[-1] if score > bands[-1]['score_max'] else bands[0]


def data_categories(path=None):
    """Category vocabulary from the LEVEL DATA file (data/tables/category_levels.jsonl).

    Fallback used when the registry-derived vocabulary is unavailable (e.g. a
    test or a caller points ROUTING_REGISTRY at a scratch file that has no
    task_profile_requirements). Without this, an empty vocabulary made every
    "unknown" category look KNOWN and invalid band levels passed through
    silently — caught by the suite (2026-09-20)."""
    p = path or os.path.join(REPO, 'data', 'tables', 'category_levels.jsonl')
    cats = set()
    try:
        with open(p) as fh:
            for line in fh:
                line = line.strip()
                if line:
                    c = json.loads(line).get('category')
                    if c:
                        cats.add(c)
    except (OSError, ValueError):
        return []
    return sorted(cats)


def score_to_matrix(score, bands=None, categories=None):
    """(matrix, band, problems) — the SAME matrix shape the classifier returns
    (category -> signed level, -5..+5). Validated through router_classify's own
    validator so both scorers cannot drift apart.

    `categories` injects the vocabulary (tests / callers with a known set).
    Resolution order: explicit param -> registry vocabulary -> level DATA file,
    and an empty vocabulary is REPORTED, never silently accepted."""
    problems = []
    bands = bands if bands is not None else load_bands()
    if score is None:
        return None, None, ['no JEV score']
    if not bands:
        return None, None, ['no bands configured']
    lo, hi = bands[0]['score_min'], bands[-1]['score_max']
    if not (lo <= score <= hi):
        problems.append(f'JEV score {score} outside the configured scale {lo}..{hi}; clamped')
    clamped = max(lo, min(hi, score))
    band = band_for(clamped, bands)
    if band is None:
        return None, None, problems + ['no band matched']
    matrix = dict(band.get('levels') or {})
    sys.path.insert(0, os.path.join(REPO, 'scripts'))
    cats = categories
    try:
        import router_classify as rc
        if cats is None:
            cats = rc.registry_categories()
            if not cats:
                cats = data_categories()
                if cats:
                    problems.append('registry category vocabulary unavailable; validated against '
                                    'data/tables/category_levels.jsonl instead')
        matrix, _conf, vproblems = rc.validate_matrix({'categories': matrix}, cats)
        problems.extend(vproblems)
        if matrix is None:
            problems.append('band levels rejected by the matrix validator')
    except Exception as exc:  # noqa: BLE001 — validation unavailable, still usable
        problems.append(f'matrix validation skipped: {str(exc)[:120]}')
    if cats == []:
        problems.append('category vocabulary unavailable: matrix NOT validated against any '
                        'vocabulary — the levels are whatever the band data says')
    return matrix, band, problems


# ------------------------------------------------------------------- JEV ---

def _http(url, payload, headers, timeout):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers=headers, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()[:400]
    except Exception as exc:  # noqa: BLE001 — transport problems are data
        return 0, str(exc)[:200]


def ask_hardness(state, http=None, timeout=90, criteria=None, model=None):
    """One JEV request → dict(score, confidence, probabilities, legend, cost,
    model, problems). Fail-closed: keys are tried in order, every failure is
    reported, and a malformed answer is a problem — never a default score."""
    out = {'score': None, 'confidence': None, 'probabilities': None, 'legend': None,
           'cost': None, 'model': None, 'key_name': None, 'problems': []}
    keys = key_candidates()
    if not keys:
        out['problems'].append('no JEV key found (OR_JEV absent from env and ~/.hermes/.env)')
        return out
    questions = {'hardness': {'type': 'score',
                              'instructions': 'How hard is this input for a coding agent to complete correctly?',
                              'criteria': criteria or HARDNESS_CRITERIA}}
    call = http or _http
    last = 'no attempt'
    for name, key in keys:
        status, body = call(JEV_URL, {'model': model or JEV_MODEL, 'state': state, 'questions': questions},
                            {'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'}, timeout)
        if status == 200 and isinstance(body, dict) and isinstance(body.get('answers'), dict):
            ans = body['answers'].get('hardness') or {}
            score = ans.get('score')
            if isinstance(score, (int, float)):
                out.update(score=float(score), confidence=ans.get('confidence'),
                           probabilities=ans.get('probabilities'), legend=ans.get('legend'),
                           cost=(body.get('usage') or {}).get('cost'),
                           model=body.get('model'), key_name=name)
                return out
            last = f'malformed answers: {str(ans)[:160]}'
            continue
        last = f'HTTP {status}: {str(body)[:160]}'
    out['problems'].append(f'all {len(keys)} JEV key(s) failed; last: {last}')
    return out


def classify(text, http=None, bands=None, timeout=90, categories=None):
    """Drop-in peer of router_classify.classify: same result keys so the proxy
    can swap scorers without branching downstream."""
    out = {'scorer': 'jev', 'model': JEV_MODEL, 'matrix': None, 'complexity_sig': None,
           'confidence': None, 'score': None, 'band': None, 'band_title': None,
           'band_note': None, 'prompt_version': None, 'cost': None,
           'categories_known': None, 'problems': [], 'raw': None}
    res = ask_hardness(text, http=http, timeout=timeout)
    out['problems'].extend(res['problems'])
    out['score'] = res['score']
    out['confidence'] = res['confidence']
    out['cost'] = res['cost']
    out['model'] = res['model'] or JEV_MODEL
    out['legend'] = res['legend']
    out['key_name'] = res['key_name']
    if res['score'] is None:
        return out
    matrix, band, problems = score_to_matrix(res['score'], bands=bands, categories=categories)
    out['problems'].extend(problems)
    if band:
        out['band'] = band.get('band')
        out['band_title'] = band.get('title')
        out['band_note'] = band.get('note')
    if matrix is None:
        return out
    out['matrix'] = matrix
    try:
        sys.path.insert(0, os.path.join(REPO, 'scripts'))
        import router_outcomes as ro
        out['complexity_sig'] = ro.complexity_sig(matrix)
    except Exception:  # noqa: BLE001 — signature is telemetry, not a contract
        pass
    return out


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    ap.add_argument('--text')
    ap.add_argument('--file')
    ap.add_argument('--bands', help='override the band data file (testing)')
    ap.add_argument('--list-bands', action='store_true')
    args = ap.parse_args(argv)
    if args.list_bands:
        print(json.dumps(load_bands(args.bands), indent=1))
        return 0
    text = args.text or (open(args.file).read() if args.file else None)
    if not text:
        print(json.dumps({'error': 'no --text/--file given'}, indent=1))
        return 2
    print(json.dumps(classify(text, bands=args.bands), indent=1))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
