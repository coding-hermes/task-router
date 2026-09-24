#!/usr/bin/env python3
"""Git merge driver for the append-only JSONL boards (.coding-hermes/board/*.jsonl).

WHY. Concurrent writers append to the same JSONL and both allocate ids as
`max(id)+1` with no lock, so two sides routinely create the SAME id with
DIFFERENT content. Git then reports a conflict on a file that has no semantic
conflict at all: appends from two workers are both valid. Measured 2026-09-23 in
the task-router tree: 4-7 concurrent `gitreins task complete` processes left the
board unmerged for >25 minutes, blocked commits ("fatal: Exiting because of an
unresolved conflict"), committed nothing, and a later merge/abort wiped a
colleague's staged work. A pre-existing duplicate (event id 102, used 09-19 and
09-21) shows the same failure mode was already accumulating silently.

WHAT. A git merge driver that unions the two sides row by row:
  * row identity = `id` when present, else a hash of the row (non-board rows);
  * a row present on one side only is kept verbatim (appends never conflict);
  * a row present on both sides is field-merged, the side with the newer
    timestamp wins per field (updated_at, then timestamp, then ts);
  * colliding ids (ours and theirs each allocated the same fresh id for
    DIFFERENT rows) are kept as separate rows and the later one is renumbered
    above the file maximum — no row is ever dropped, no id is ever duplicated;
  * ORDER: rows that existed in the base keep base order (so history does not
    shuffle), then this side's new rows, then the other side's.
  * conflict markers from a previously abandoned merge are resolved as the two
    sides (so this driver can also repair an already-conflicted file).

FAIL-SAFE. On any unexpected error the driver exits non-zero and writes nothing,
which leaves git's normal conflict in place — with the markers. A broken driver
must never silently produce a truncated board.

INSTALL (per clone; git config is not cloned):
  scripts/enable-board-merge-driver.sh
  .gitattributes carries: `.coding-hermes/board/*.jsonl merge=boardjsonl`

USAGE (git calls this itself):
  board-merge-driver.py %O %A %B      # base, ours (result written here), theirs
"""
import hashlib
import json
import sys

TIMESTAMP_KEYS = ('updated_at', 'timestamp', 'ts', 'created_at', 'last_updated')


#: Fields that DESCRIBE what a row is. Two rows sharing an id but disagreeing on
#: one of these are DIFFERENT rows that collided on an id (both writers took
#: max+1), not two versions of one row — merging them would silently delete a
#: task or an event. Measured 2026-09-23: two board writers each allocated event
#: id 1789971803 for different events, and a separate pre-existing pair both used
#: id 102 (09-19 and 09-21).
IDENTITY_FIELDS = ('task_id', 'title', 'event', 'event_type', 'project',
                   'provider', 'model', 'name', 'slug')


def row_key(row):
    if isinstance(row, dict) and row.get('id') is not None:
        return ('id', str(row['id']))
    blob = json.dumps(row, sort_keys=True, ensure_ascii=False)
    return ('hash', hashlib.sha256(blob.encode()).hexdigest()[:16])


def same_row(a, b):
    """Could these two rows be two versions of the SAME row (an edit), or are
    they colliding rows that merely share an id?"""
    if row_key(a) != row_key(b):
        return False
    for field in IDENTITY_FIELDS:
        if field in a and field in b and a[field] != b[field]:
            return False
    return True


def _merge_pair(a, b):
    """Field-union two versions of one row; the newer timestamp wins per field."""
    newer, older = (a, b) if stamp(a) >= stamp(b) else (b, a)
    row = dict(older)
    row.update(newer)
    return row


def stamp(row):
    for k in TIMESTAMP_KEYS:
        v = row.get(k) if isinstance(row, dict) else None
        if v:
            return str(v)
    return ''


def parse(text, path='<stdin>'):
    """Rows from a JSONL text. Handles a conflicted file by treating the two
    sides of the markers as rows (a repair path for abandoned merges)."""
    rows, errors = [], []
    mode, head, branch = None, [], []
    for lineno, raw in enumerate(text.split('\n'), 1):
        line = raw.strip()
        if line.startswith('<<<<<<<'):
            mode, head, branch = 'head', [], []
            continue
        if line.startswith('=======') and mode == 'head':
            mode = 'branch'
            continue
        if line.startswith('>>>>>>>'):
            rows.extend(head)
            rows.extend(branch)
            mode, head, branch = None, [], []
            continue
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError as exc:
            errors.append(f'{path}:{lineno}: {exc}')
            continue
        if mode is None:
            rows.append(row)
        elif mode == 'head':
            head.append(row)
        else:
            branch.append(row)
    return rows, errors


def merge_rows(base, ours, theirs):
    """Union three JSONL row lists. Returns (rows, notes)."""
    notes = []
    clusters = []          # [{row, base}] in output order; one entry per ROW
    by_id = {}             # row_key -> [cluster indices]

    def absorb(rows, is_base):
        for row in rows:
            key = row_key(row)
            target = None
            for idx in by_id.get(key, []):
                if same_row(clusters[idx]['row'], row):
                    target = idx
                    break
            if target is None:
                clusters.append({'row': dict(row) if isinstance(row, dict) else row,
                                 'base': is_base})
                by_id.setdefault(key, []).append(len(clusters) - 1)
                continue
            existing = clusters[target]['row']
            if isinstance(existing, dict) and isinstance(row, dict):
                clusters[target]['row'] = _merge_pair(existing, row)
                clusters[target]['base'] = clusters[target]['base'] or is_base

    absorb(base, True)
    absorb(ours, False)
    absorb(theirs, False)
    merged = [c['row'] for c in clusters]

    # id collisions across DIFFERENT rows (both sides took max+1): keep both.
    seen, next_id = {}, None
    for row in merged:
        rid = row.get('id') if isinstance(row, dict) else None
        if rid is None:
            continue
        if rid not in seen:
            seen[rid] = row
            continue
        if next_id is None:
            numeric = [r for r in seen.values()
                       if isinstance(r.get('id'), int)]
            next_id = max((r['id'] for r in numeric), default=0)
        next_id += 1
        row['id'] = next_id
        seen[next_id] = row
        notes.append(f'renumbered a colliding id to {next_id} '
                     f'({row.get("event_type") or row.get("title") or "row"})')
    return merged, notes


def main(argv):
    if len(argv) != 4:
        print('usage: board-merge-driver.py <base> <ours> <theirs>', file=sys.stderr)
        return 2
    base_path, ours_path, theirs_path = argv[1:]
    try:
        base, e1 = parse(open(base_path).read(), base_path)
        ours, e2 = parse(open(ours_path).read(), ours_path)
        theirs_rows, e3 = parse(open(theirs_path).read(), theirs_path)
    except OSError as exc:
        print(f'board-merge-driver: cannot read input: {exc}', file=sys.stderr)
        return 1
    if e3 or e2:  # OUR/THEIR side unparseable: refuse rather than lose rows
        for e in e2 + e3:
            print(f'board-merge-driver: unparseable row: {e}', file=sys.stderr)
        return 1
    if e1:
        print(f'board-merge-driver: base had {len(e1)} unparseable row(s); '
              f'continuing (base is only used for ordering)', file=sys.stderr)

    rows, notes = merge_rows(base, ours, theirs_rows)
    ids = [r.get('id') for r in rows if isinstance(r, dict) and r.get('id') is not None]
    duplicates = {i for i in ids if ids.count(i) > 1}
    if duplicates:  # a bug in this driver must not produce a corrupt board
        print(f'board-merge-driver: REFUSING to write, duplicate ids remain: '
              f'{duplicates}', file=sys.stderr)
        return 1

    with open(ours_path, 'w') as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + '\n')
    for n in notes:
        print(f'board-merge-driver: {n}', file=sys.stderr)
    print(f'board-merge-driver: merged {len(rows)} rows '
          f'({len(ours)} ours + {len(theirs_rows)} theirs, base {len(base)})',
          file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
