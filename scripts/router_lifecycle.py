#!/usr/bin/env python3
"""router_lifecycle.py — TR-069 digest: the lifecycle listing by state.

Bane's rule: "nothing vanishes silently." The resolver hides retired and
pre-release lanes from chains but COUNTS them; this command is the human
view of the same data — which lanes are coming soon, which are retiring
(with the date and the successor), which are gone.

Reads the same registry view as the resolver (ROUTING_REGISTRY hook, or the
repo registry.json). Informational only: exit 0 always, fail-open display.
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import router_spawn as rs  # lifecycle_state: the ONE shared rule (TR-069)


def _registry_path():
    return (os.environ.get('ROUTING_REGISTRY')
            or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            'registry.json'))


def _cache_path():
    return os.environ.get('ROUTER_MODELSDEV_CACHE') or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'data', 'state', 'modelsdev-cache.json')


def _norm(s):
    return re.sub(r'[^a-z0-9]', '', str(s).lower())


def _pdot(s):
    """fireworks writes '.' inside a segment as 'p' (glm-5p2 == glm-5.2).
    Only a 'p' between an alphanumeric and a digit converts, so 'preview',
    'plus', 'pro' and 'compound' are never mangled."""
    return re.sub(r'(?<=[a-z0-9])p(?=[0-9])', '.', str(s).lower())


def _split_tag(mid):
    """Split a model id into (path segments, name, tag).

    ':' is a TAG separator only when it appears after the last '/' — otherwise
    it is a namespace separator (`hf:moonshotai/Kimi-K3`). Splitting blindly on
    the first ':' collapsed that id to the useless key 'hf' (TR-076).
    """
    segs = str(mid).split('/')
    last = segs[-1]
    if ':' in last:
        name, tag = last.split(':', 1)
        return segs, name, tag
    return segs, last, None


def _keys(mid, with_tag=True):
    """Comparison keys for one id.

    Keys shorter than 3 chars are dropped — they match too much to be evidence
    of anything. The threshold is 3, not 4: `hy3` is a real registry lane that
    IS in the catalog, and a length-4 floor silently reported it as absent.
    """
    segs, name, tag = _split_tag(mid)
    use = f'{name}:{tag}' if (tag and with_tag) else name
    full = '/'.join(segs[:-1] + [use])
    out = set()
    for v in (full, use):
        for t in (v, _pdot(v)):
            k = _norm(t)
            if len(k) >= 3:
                out.add(k)
    return out


PLAN_TAGS = {'free', 'paid', 'beta', 'preview', 'exp', 'experimental', 'latest',
             'fast', 'flex', 'short', 'highspeed', 'thinking'}
# TR-076: lanes that are absent from the public catalog BY DESIGN — they are
# provider-side dynamic routes / meta-models with no fixed model id to list.
# Whitelisting the pattern (not the individual lane) keeps `router lifecycle`
# honest: these are excluded from "possibly gone" because they can never be in
# the catalog, not because someone silenced them.
BY_DESIGN = {
    'openrouter': (
        # OpenRouter's own routing/meta endpoints: the router picks the backing
        # model at request time, so there is no catalog entry to match.
        r'^openrouter/(auto|bodybuilder|fusion|pareto-code)$',
    ),
}


def _by_design(pid, mid):
    import re as _re
    return any(_re.match(p, mid) for p in BY_DESIGN.get(pid, ()))


def _token_key(mid):
    """Order-insensitive key: the LAST segment's tokens, sorted.

    fireworks publishes `nemotron-3-5-lightning-30b-a3b` while the catalog has
    `nemotron-lightning-3p5-30b-a3b` — the same model with the tokens reordered
    and the dot written as 'p'. Only the last path segment is used (the
    namespace prefix is a naming convention, not part of the model identity) and
    `_pdot` runs BEFORE splitting so `3p5` becomes `3.5` and yields the tokens
    `3`,`5` on both sides. Reported as `token-shape` — never auto-remapped,
    because reordering is weaker evidence than an id match.
    """
    segs, name, tag = _split_tag(mid)
    v = f'{name}:{tag}' if tag else name
    toks = []
    for t in _pdot(v).replace(':', '-').split('-'):
        n = _norm(t)
        if not n:
            continue
        # A version written '3.5', '3-5' or '3p5' must yield the same token set,
        # so a run of digits becomes its individual digits (30b stays intact —
        # it carries a unit suffix and is part of the model's size).
        if n.isdigit() and len(n) > 1:
            toks.extend(list(n))
        else:
            toks.append(n)
    return '|'.join(sorted(toks)) if toks else None


def _catalog_index(cache):
    """provider -> (exact index, tag-stripped index).

    Two indexes so a tag difference (`x:free` vs `x`) can never masquerade as a
    plain id-shape remap: `gpt-oss:120b` and `gpt-oss:20b` are different models,
    so a tag-only match is reported for human decision, never auto-remapped.
    """
    out = {}
    for pid, models in cache.items():
        if not isinstance(models, dict):
            continue
        exact, base = {}, {}
        toks = set()
        for cid in models:
            for k in _keys(cid, with_tag=True):
                exact.setdefault(k, set()).add(cid)
            for k in _keys(cid, with_tag=False):
                base.setdefault(k, set()).add(cid)
            tk = _token_key(cid)
            if tk:
                toks.add((tk, cid))
        out[pid] = (exact, base, dict(toks))
    return out


def _match(pid_index, mid):
    """Return (kind, catalog_id). Strongest tier first."""
    exact, base, toks = pid_index
    for k in _keys(mid, with_tag=True):
        hit = exact.get(k)
        if hit:
            return ('remap', sorted(hit)[0]) if len(hit) == 1 else ('ambiguous', None)
    segs, name, tag = _split_tag(mid)
    # A tag-stripped fallback is always attempted: the base id is the identity,
    # and the ':120b' vs ':20b' collision safety comes from the ambiguous/exact
    # tiers above, not from refusing to look.
    for k in _keys(mid, with_tag=False):
        hit = base.get(k)
        if hit:
            return ('tag-shape', sorted(hit)[0]) if len(hit) == 1 else ('ambiguous', None)
    # Last tier: same tokens, different order/spelling (weak — never auto-remap).
    tk = _token_key(mid)
    hit = toks.get(tk) if tk else None
    if hit:
        return ('token-shape', hit)
    return ('absent', None)


def catalog_drift(rows, cache=None):
    """TR-069 intake assist (Bane: automate detection of silent retirements).

    An ACTIVE lane whose provider exists in the models.dev cache but whose
    model id does not (under any canonical comparison key) is a CANDIDATE silent
    retirement. Absence is NOT a date — this reports, it never stamps; verify
    with the provider, then stamp via lifecycle.jsonl.

    TR-076: results are classified so only the ones needing provider work stay
    in that bucket —
      `remap`     the lane IS in the catalog under a different spelling
                  (namespace prefix, case, or the fireworks p-for-dot form);
      `tag-shape` ids agree once a ':tag' is dropped — NOT auto-remapped,
                  because `x:free` vs `x` and `120b` vs `20b` are different
                  lanes, so a human decides;
      `ambiguous` several catalog ids match — needs a human, never a stamp;
      `absent`    nothing matches: the real candidate retirement, verify it.
    """
    if cache is None:
        try:
            cache = json.load(open(_cache_path()))
        except Exception:
            return []
    idx = _catalog_index(cache)
    out = []
    for m in rows:
        if m.get('archive') or m.get('disabled') or rs.row_is_retired(m):
            continue
        if m.get('available_from') and str(m['available_from'])[:10] > rs._today():
            continue
        pid, mid = m.get('provider'), m.get('model') or ''
        if pid not in idx:
            continue  # provider not in the public catalog (proxies/plans) — skip
        if _by_design(pid, mid):
            out.append({'provider': pid, 'model': mid, 'kind': 'by-design',
                        'note': 'dynamic route / meta-model — can never appear in '
                                'the catalog; excluded from "possibly gone" by '
                                'pattern, not silenced'})
            continue
        kind, cid = _match(idx[pid], mid)
        if kind == 'remap':
            if cid != mid:
                out.append({'provider': pid, 'model': mid, 'kind': 'remap',
                            'catalog_id': cid,
                            'note': 'id-shape only — the lane IS in the catalog '
                                    'under a different spelling; remap or ignore, '
                                    'do NOT stamp a retirement'})
            continue
        if kind == 'tag-shape':
            out.append({'provider': pid, 'model': mid, 'kind': 'tag-shape',
                        'catalog_id': cid,
                        'note': 'ids agree only once the :tag is dropped — a tag '
                                'difference can be a real lane difference '
                                '(:free vs paid, 120b vs 20b), so verify with '
                                'the provider before remapping or stamping'})
            continue
        if kind == 'token-shape':
            out.append({'provider': pid, 'model': mid, 'kind': 'token-shape',
                        'catalog_id': cid,
                        'note': 'same tokens in a different order/spelling — the '
                                'listing almost certainly drifted; confirm with '
                                'the provider before remapping'})
            continue
        if kind == 'ambiguous':
            out.append({'provider': pid, 'model': mid, 'kind': 'ambiguous',
                        'note': 'several catalog ids match this lane — needs a '
                                'human decision; never stamp from this'})
            continue
        out.append({'provider': pid, 'model': mid, 'kind': 'absent',
                    'note': 'active lane absent from the models.dev catalog — '
                            'verify with the provider, then stamp valid_to'})
    return out


def _fmt_date(d):
    return str(d)[:10] if d else '-'


def build_report(rows, today=None, show_retired=False):
    """Grouped report: counts by state, then the lanes that need a decision."""
    groups = {'coming_soon': [], 'live': [], 'retiring': [], 'retired': []}
    for m in rows:
        groups.setdefault(rs.lifecycle_state(m, today=today), []).append(m)

    lines = [f'LIFECYCLE DIGEST — {today if today else rs._today()}']
    c = {k: len(v) for k, v in groups.items()}
    lines.append(f"counts: live {c.get('live', 0)} | coming_soon {c.get('coming_soon', 0)}"
                 f" | retiring {c.get('retiring', 0)} | retired {c.get('retired', 0)}")

    if groups['coming_soon']:
        lines.append('\nCOMING SOON (not yet routable):')
        for m in sorted(groups['coming_soon'], key=lambda r: str(r.get('available_from'))):
            lines.append(f"  {m.get('provider')}/{m.get('model')}  "
                         f"available {_fmt_date(m.get('available_from'))}"
                         f"{'  src: ' + m['lifecycle_source'] if m.get('lifecycle_source') else ''}")

    if groups['retiring']:
        lines.append('\nRETIRING (routable, deadline visible):')
        for m in sorted(groups['retiring'], key=lambda r: str(r.get('valid_to'))):
            left = rs._days_until(m.get('valid_to'), today or rs._today())
            lines.append(f"  {m.get('provider')}/{m.get('model')}  "
                         f"retires {_fmt_date(m.get('valid_to'))} (in {left}d)"
                         f"{'  -> ' + m['replaced_by'] if m.get('replaced_by') else ''}"
                         f"{'  [DISABLED: ' + str(m.get('disabled_reason')) + ']' if m.get('disabled') else ''}"
                         f"{'  src: ' + m['lifecycle_source'] if m.get('lifecycle_source') else ''}")

    if show_retired and groups['retired']:
        lines.append('\nRETIRED (hidden from chains, counted here):')
        for m in sorted(groups['retired'], key=lambda r: str(r.get('valid_to')))[-20:]:
            lines.append(f"  {m.get('provider')}/{m.get('model')}  "
                         f"retired {_fmt_date(m.get('valid_to'))}"
                         f"{'  -> ' + m['replaced_by'] if m.get('replaced_by') else ''}")
        if len(groups['retired']) > 20:
            lines.append(f'  ... and {len(groups["retired"]) - 20} more (use --json for all)')

    drift = catalog_drift(rows)
    remaps = [d for d in drift if d.get('kind') == 'remap']
    tagshape = [d for d in drift if d.get('kind') == 'tag-shape']
    tokenshape = [d for d in drift if d.get('kind') == 'token-shape']
    ambiguous = [d for d in drift if d.get('kind') == 'ambiguous']
    bydesign = [d for d in drift if d.get('kind') == 'by-design']
    absent = [d for d in drift if d.get('kind') == 'absent']
    if remaps:
        lines.append('\nID-SHAPE ONLY (in the catalog under a different spelling — '
                     'remap or ignore, do NOT stamp a retirement):')
        for d in remaps[:25]:
            lines.append(f"  {d['provider']}/{d['model']}  ->  {d['catalog_id']}")
        if len(remaps) > 25:
            lines.append(f'  ... and {len(remaps) - 25} more (use --json for all)')
    if tokenshape:
        lines.append('\nLISTING DRIFT (same tokens, different order/spelling — '
                     'confirm with the provider):')
        for d in tokenshape[:25]:
            lines.append(f"  {d['provider']}/{d['model']}  ->  {d['catalog_id']}")
    if tagshape:
        lines.append('\nTAG-SHAPE (agree only once the :tag is dropped — a tag '
                     'difference can be a real lane difference):')
        for d in tagshape[:25]:
            lines.append(f"  {d['provider']}/{d['model']}  ->  {d['catalog_id']}")
    if ambiguous:
        lines.append('\nAMBIGUOUS (several catalog ids match — human decision, '
                     'never stamp from this):')
        for d in ambiguous[:25]:
            lines.append(f"  {d['provider']}/{d['model']}")
    if absent:
        lines.append('\nPOSSIBLY GONE (active lane, absent from the current models.dev '
                     'catalog — verify with the provider before stamping):')
        for d in absent[:25]:
            lines.append(f"  {d['provider']}/{d['model']}")
        if len(absent) > 25:
            lines.append(f'  ... and {len(absent) - 25} more (use --json for all)')
    if bydesign:
        lines.append(f'\nBY DESIGN ({len(bydesign)} dynamic routes / meta-models — '
                     'can never be in the catalog; excluded by pattern):')
        for d in bydesign[:25]:
            lines.append(f"  {d['provider']}/{d['model']}")

    if not groups['coming_soon'] and not groups['retiring'] and not drift:
        lines.append('\nno upcoming arrivals or retirements on record')
    return '\n'.join(lines)


def _load_rows_from(candidates):
    """Core loader over an explicit candidate list (unit-testable)."""
    for path in candidates:
        try:
            reg = json.load(open(path))
            # registry shape: {"version", "generated_at", "source",
            #                  "tables": {table_name: [rows]}}
            rows = (reg.get('tables') or {}).get('models') or []
            if path != candidates[0]:
                print(f'lifecycle: registry at {candidates[0]} unreadable — '
                      f'using repo fallback {path}', file=sys.stderr)
            return rows
        except Exception:
            continue
    print(f'lifecycle: no readable registry ({candidates})', file=sys.stderr)
    return []


def load_rows():
    """Same resilience as the resolver: exported/registry path first, then the
    repo registry (fail-open display, never hard-fail)."""
    return _load_rows_from([
        _registry_path(),
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     'registry.json')])


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description='TR-069 model lifecycle digest')
    ap.add_argument('--json', action='store_true', help='machine-readable output')
    ap.add_argument('--all', action='store_true', help='also list retired lanes (last 20)')
    ap.add_argument('--today', default=None, help='frozen clock (YYYY-MM-DD) for checks')
    args = ap.parse_args(argv)

    rows = load_rows()
    today = args.today or rs._today()
    if args.json:
        groups = {}
        for m in rows:
            groups.setdefault(rs.lifecycle_state(m, today=today), []).append(
                {k: m.get(k) for k in ('provider', 'model', 'available_from', 'valid_to',
                                       'replaced_by', 'lifecycle_source', 'disabled')})
        print(json.dumps({'today': today,
                          'counts': {k: len(v) for k, v in groups.items()},
                          'states': groups,
                          'catalog_drift': catalog_drift(rows)}, indent=1))
        return 0
    print(build_report(rows, today=today, show_retired=args.all))
    return 0


if __name__ == '__main__':
    sys.exit(main())
