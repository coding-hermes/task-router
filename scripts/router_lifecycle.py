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
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import router_spawn as rs  # lifecycle_state: the ONE shared rule (TR-069)


def _registry_path():
    return (os.environ.get('ROUTING_REGISTRY')
            or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            'registry.json'))


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

    if not groups['coming_soon'] and not groups['retiring']:
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
                          'states': groups}, indent=1))
        return 0
    print(build_report(rows, today=today, show_retired=args.all))
    return 0


if __name__ == '__main__':
    sys.exit(main())
