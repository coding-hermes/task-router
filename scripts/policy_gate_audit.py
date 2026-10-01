#!/usr/bin/env python3
"""TR-203 AC4 — policy-gate coverage audit: registry vs quota-state.json.

Prints one row per provider in data/tables/providers.jsonl:
  provider | in quota-state | status | archive | tos_class | verdict

Verdicts:
  ok                        providers.<id>.status == open  (routable)
  GATED (<status>)          row present, status != open    (quota-gated)
  policy-gate-missing-row   NO row, not exempt             (excluded, GAP)
  intentionally-ungated     NO row, exempt via the file's top-level
                            intentionally_ungated list     (documented gap)

Exit code: 0 when no GAP rows remain, 1 otherwise (cron-diffable), 2 on a
hard error (files missing/malformed). Read-only — never writes state.

Usage:
  policy_gate_audit.py [--quota-state <path>] [--repo <repo root>]
"""
import argparse
import json
import os
import sys

# TR-202: realpath, never abspath — this script is exec'd through the
# ~/.hermes/scripts symlink; an abspath idiom on __file__ would derive the
# repo root from ~/.hermes and read the wrong providers.jsonl.
DEFAULT_REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))


def load_jsonl(path):
    rows = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--repo', default=DEFAULT_REPO,
                    help='task-router repo root (default: script parent)')
    ap.add_argument('--quota-state',
                    default=os.path.join(
                        os.environ.get('ROUTER_STATE_DIR',
                                       os.path.expanduser(
                                           '~/.hermes/model-router')),
                        'quota-state.json'),
                    help='quota-state.json path (default: the deployment '
                         'state the resolver reads; ROUTER_STATE_DIR wins)')
    args = ap.parse_args(argv)

    prov_path = os.path.join(args.repo, 'data', 'tables', 'providers.jsonl')
    try:
        provs = load_jsonl(prov_path)
    except OSError as exc:
        print(f'FATAL: cannot read {prov_path}: {exc}', file=sys.stderr)
        return 2
    try:
        with open(args.quota_state) as fh:
            qdoc = json.load(fh)
    except OSError as exc:
        print(f'FATAL: cannot read {args.quota_state}: {exc}', file=sys.stderr)
        return 2
    except json.JSONDecodeError as exc:
        print(f'FATAL: {args.quota_state} is not valid JSON: {exc}',
              file=sys.stderr)
        return 2
    if not isinstance(qdoc, dict):
        print(f'FATAL: {args.quota_state} is not a JSON object',
              file=sys.stderr)
        return 2

    qs = qdoc.get('providers') or {}
    if not isinstance(qs, dict):
        qs = {}
    raw = qdoc.get('intentionally_ungated') or []
    ungated = set(raw) if isinstance(raw, list) else set()

    meta = {r.get('id'): r for r in provs if r.get('id')}
    print(f'quota-state.json: {args.quota_state} '
          f'(updated={qdoc.get("updated")})')
    print(f'registry: {len(meta)} providers in providers.jsonl; '
          f'{len(qs)} rows in quota-state.json; '
          f'{len(ungated)} intentionally_ungated')
    print()
    hdr = (f'{"provider":<24} {"in quota-state":<15} {"status":<10} '
           f'{"arch":<5} {"tos_class":<16} verdict')
    print(hdr)
    print('-' * len(hdr))
    gaps = []
    for pid in sorted(meta):
        row = meta[pid]
        ent = qs.get(pid)
        arch = 'yes' if row.get('archive') else 'no'
        tos = str(row.get('tos_class') or '-')
        if isinstance(ent, dict):
            status = str(ent.get('status'))
            verdict = 'ok' if status == 'open' else f'GATED ({status})'
        elif pid in ungated:
            status = '-'
            verdict = 'intentionally-ungated'
        else:
            status = '-'
            verdict = 'policy-gate-missing-row'
            gaps.append(pid)
        print(f'{pid:<24} {"yes" if isinstance(ent, dict) else "NO":<15} '
              f'{status:<10} {arch:<5} {tos:<16} {verdict}')

    extra = sorted(set(qs) - set(meta))
    print()
    print(f'covered: {len(meta) - len(gaps)}/{len(meta)}; '
          f'GAPS: {len(gaps)}{gaps if gaps else ""}')
    if extra:
        print(f'quota-state rows not in providers.jsonl (informational): '
              f'{extra}')
    return 1 if gaps else 0


if __name__ == '__main__':
    sys.exit(main())
