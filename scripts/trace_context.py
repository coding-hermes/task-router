#!/usr/bin/env python3
"""trace_context — the search side of the trace graph.

Given a work-unit id (board row, release, CI row), collect every ch:trace marker that
names it, group the references by kind, validate them, and print a context bundle an
agent can read. Read-only.

Usage:
  trace_context.py TR-252
  trace_context.py TR-252 --roots ~/proj-a ~/proj-b        # default: cwd
  trace_context.py --validate-only --roots ~/proj-a        # audit every marker found

Exit: 0 clean, 2 when a marker is invalid (a reference-closure, a missing row, an
unbound verdict), so it can be used as a gate.
"""
import argparse
import os
import re
import sys
import json

# ch:trace row=TR-254 spec=docs/traceability-doctrine.md#one-call-four-surfaces doc=docs/traceability-doctrine.md evidence=reports/xray/task-router-xray.html witness=ledger:router-proxy/rows>0

MARKER = re.compile(r'ch:trace\s+(.*)')
FIELD = re.compile(r'([a-z_]+)=(\S*)')
SKIP_DIRS = {'.git', 'node_modules', '.venv', 'venv', '__pycache__', 'target', '.mypy_cache',
             'dist', 'build', '.pytest_cache', 'worktrees', '.hilo', 'site-packages'}
TEXT_EXT = {'.py', '.go', '.rs', '.ts', '.tsx', '.js', '.jsx', '.md', '.yaml', '.yml', '.toml',
            '.json', '.jsonl', '.sh', '.txt', '.sql', '.c', '.h', '.java', '.rb', '.swift', '.kt'}
REQUIRED = ('row',)
BOUND = ('verdict', 'commit')


def walk(roots, max_bytes=2_000_000):
    for root in roots:
        root = os.path.abspath(os.path.expanduser(root))
        if os.path.isfile(root):
            yield root
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith('.')]
            for fn in filenames:
                if os.path.splitext(fn)[1] not in TEXT_EXT:
                    continue
                p = os.path.join(dirpath, fn)
                try:
                    if os.path.getsize(p) > max_bytes:
                        continue
                except OSError:
                    continue
                yield p


def parse_line(line):
    m = MARKER.search(line)
    if not m:
        return None
    return dict(FIELD.findall(m.group(1)))


PLACEHOLDER = re.compile(r'^<.*>$')


def is_template(marker):
    """A template/example line (row=<ID>) is documentation, not a marker.
    Without this, the validator flags our own docs, and a validator with a known
    false positive is noise."""
    return bool(marker.get('row')) and bool(PLACEHOLDER.match(str(marker.get('row'))))


def validate(marker, where):
    """Return a list of problems. The rules come from docs/traceability-doctrine.md."""
    problems = []
    if is_template(marker):
        return ['TEMPLATE (documentation example, not a claim)']
    for req in REQUIRED:
        if not marker.get(req):
            problems.append('missing required `%s`' % req)
    # rule 2: nothing closes by reference
    ev = marker.get('evidence', '')
    if ev and re.match(r'^(TR-|INT-|RELEASE-|see\s|ref[:=])', ev.strip(), re.I):
        problems.append('evidence closes by reference (%r) - must be an artifact path or a witness' % ev)
    if ev and ev.startswith('witness='):
        problems.append('witness smuggled into evidence=')
    # rule 3: a claim with no witness must say so
    if not marker.get('witness') and 'witness' not in ' '.join(marker.keys()):
        if not ev:
            problems.append('neither witness= nor evidence= present')
    # rule 5: verdict must bind to a commit
    if marker.get('verdict') and not marker.get('commit'):
        problems.append('verdict= without commit=')
    return problems


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('row', nargs='?', help='work-unit id to search for')
    ap.add_argument('--roots', nargs='*', default=['.'], help='roots to scan (default: cwd)')
    ap.add_argument('--validate-only', action='store_true', help='audit all markers, ignore the row filter')
    ap.add_argument('--json', action='store_true')
    args = ap.parse_args()
    if not args.row and not args.validate_only:
        ap.error('give a row id, or --validate-only')

    hits, invalid, scanned = [], [], 0
    for path in walk(args.roots):
        scanned += 1
        try:
            with open(path, encoding='utf-8', errors='replace') as fh:
                for n, line in enumerate(fh, 1):
                    mk = parse_line(line)
                    if not mk:
                        continue
                    probs = validate(mk, path)
                    rec = {'file': path, 'line': n, 'marker': mk, 'problems': probs}
                    if probs and not is_template(mk):
                        invalid.append(rec)
                    if args.validate_only or (args.row and mk.get('row') == args.row):
                        hits.append(rec)
        except OSError:
            continue

    if args.json:
        print(json.dumps({'row': args.row, 'files_scanned': scanned, 'hits': hits, 'invalid': invalid}, indent=2))
        return 2 if invalid else 0

    print('trace_context  row=%s  roots=%s' % (args.row or '(audit)', ', '.join(args.roots)))
    print('files scanned: %d | markers matched: %d | invalid: %d' % (scanned, len(hits), len(invalid)))
    print()
    if not hits and not invalid:
        print('  no markers found. Either the work is untraced, or the roots do not cover it.')
        return 0
    for rec in hits:
        mk = rec['marker']
        print('%s:%d' % (rec['file'], rec['line']))
        for k in ('row', 'spec', 'wave', 'test', 'doc', 'prompt', 'evidence', 'witness', 'verdict', 'commit', 'memory'):
            if k in mk:
                label = {'row': 'state', 'spec': 'contract', 'wave': 'dispatch', 'test': 'proof',
                         'doc': 'doc', 'prompt': 'prompt', 'evidence': 'our artifact',
                         'witness': 'EXTERNAL', 'verdict': 'judgement', 'commit': 'commit',
                         'memory': 'history'}[k]
                print('    %-11s %-11s %s' % (k, '(' + label + ')', mk[k] or '(placeholder)'))
        if rec['problems']:
            for p in rec['problems']:
                print('    !! %s' % p)
        print()
    if invalid:
        print('%d invalid marker(s):' % len(invalid))
        for rec in invalid:
            print('  %s:%d  %s' % (rec['file'], rec['line'], '; '.join(rec['problems'])))
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
