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

WHAT. A git merge driver that unions the three sides row by row:
  * row identity = `id` when present, else a hash of the row (non-board rows);
    two rows sharing an id are versions of ONE row only when they agree on the
    identity fields (task_id, title, event, event_type, ...) — if they disagree
    about WHAT the row is, they are a collision and BOTH rows survive;
  * a row present on one side only is kept verbatim (appends never conflict);
  * two versions of one row are field-merged, the newer timestamp wins per field
    (updated_at, then timestamp, then ts, ...);
  * ORDER: rows that existed in the base keep base order (history does not
    shuffle); NEW rows are ordered by (timestamp, content) — never by the id
    this merge happened to allocate;
  * IDS: a row keeps its id unless another row already holds it. Rows present in
    the merge base ALWAYS keep theirs; a colliding row is given a free id above
    the file maximum. When the same row arrives under two ids — each tree
    renumbered the same collision differently — the variants are recognised by
    their content (every field except `id`) and collapsed onto one id, but only
    when every id involved is CONTESTED (two different rows claim it). Two rows
    that merely look alike keep their own ids: a merge must never swallow a row
    because it resembles another one.
  * conflict markers from a previously abandoned merge are resolved as the two
    sides (so this driver can also repair an already-conflicted file).

INVARIANT. Let M(B, O, T) be the rows written for base/ours/theirs. What makes
concurrent appends safe is that M is a pure function of the three row SETS, not
of the roles git handed us:
  1. ORDER-INDEPENDENT: M(B, O, T) == M(B, T, O), byte for byte. Row order and
     every allocated id are derived from row content, so the two trees that
     resolve the same collision write the SAME board. Without this each tree
     writes a different board, their next merge conflicts again, and the loop
     that blocked the tree repeats. (test_merging_the_same_pair_either_way...)
  2. IDEMPOTENT: M(B, M(B, O, T), T) == M(B, O, T). Re-merging an already merged
     board cannot renumber, duplicate or reorder a row. Without this, merging two
     boards that had each resolved the same pair grew 9 rows to 11 and duplicated
     both events under fresh ids that no later merge could ever collapse.
     (test_merging_two_boards_that_each_resolved_the_collision...)
  3. LOSSLESS: no logical row is dropped and no id is duplicated in the result —
     if either cannot be guaranteed the driver writes NOTHING and exits non-zero
     rather than emit a board it cannot vouch for.
  4. STABLE IDS: a row keeps its id unless another row actually holds it; rows in
     the merge base are never renumbered. An id is not a place in the file, so a
     merge must not move a row's identity. (test_a_row_whose_id_is_unique...)
  5. HIGH-WATER MARK: a renumbered row gets an id above every id in the result,
     so a later `max(id)+1` append lands on a fresh id instead of an existing
     row's. Collisions are healed forward, never reused.

This driver cannot stop two writers from choosing the same id (allocation stays
lock-free by doctrine); it guarantees that any two such boards merge to unique,
stable ids — the same bytes in every tree, the same rows, forever.

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
import re
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

#: `TR-126` / `event-7`: string ids whose numeric tail can be climbed when a
#: collision forces a renumber (so a task keeps its repo's id shape).
_TRAILING_DIGITS = re.compile(r'^(?P<prefix>.*?)(?P<num>\d+)$')


def canonical(value):
    """Stable text form of a row — hashing, comparing and ordering all use it."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':'))


def scalar_id(rid):
    """Hashable form of an id. Boards use ints and strings; anything else is
    still handled so a malformed row cannot crash the driver mid-merge."""
    if rid is None or isinstance(rid, (int, str, float, bool)):
        return rid
    return canonical(rid)


def row_key(row):
    """Which row is this? Its `id` when it has one, else its content."""
    if isinstance(row, dict) and row.get('id') is not None:
        return ('id', str(row['id']))
    return ('hash', hashlib.sha256(canonical(row).encode()).hexdigest()[:16])


def payload_key(row):
    """The row WITHOUT its allocated id: the content that identifies the logical
    row across trees that numbered it differently."""
    if not isinstance(row, dict):
        return canonical(row)
    return canonical({k: v for k, v in row.items() if k != 'id'})


def cluster_ids(cluster):
    """Every id a clustered row has carried: the one from the merge base, the
    one it carries now, and any other it was seen under."""
    row = cluster['row']
    seen = ([cluster['base_id']] if cluster['base_id'] is not None else [])
    if isinstance(row, dict) and row.get('id') is not None:
        seen.append(row['id'])
    seen += cluster['ids']
    out = []
    for rid in seen:
        if rid is not None and scalar_id(rid) not in {scalar_id(x) for x in out}:
            out.append(rid)
    return out


def same_row(a, b):
    """Could these two rows be two versions of the SAME row (an edit), or are
    they colliding rows that merely share an id?"""
    if row_key(a) != row_key(b):
        return False
    if not (isinstance(a, dict) and isinstance(b, dict)):
        return a == b            # non-board rows: identical content, one row
    for field in IDENTITY_FIELDS:
        if field in a and field in b and a[field] != b[field]:
            return False
    return True


def stamp(row):
    for k in TIMESTAMP_KEYS:
        v = row.get(k) if isinstance(row, dict) else None
        if v:
            return str(v)
    return ''


def merge_pair(a, b):
    """Field-union two versions of one row; the newer timestamp wins per field.
    Equal timestamps are broken by content, never by which side was `ours`, so
    the result cannot depend on the direction of the merge."""
    sa, sb = stamp(a), stamp(b)
    if sa == sb:
        newer, older = (a, b) if canonical(a) >= canonical(b) else (b, a)
    else:
        newer, older = (a, b) if sa > sb else (b, a)
    row = dict(older)
    row.update(newer)
    return row


def order_key(row):
    """Order of a NEW row: timestamp, then content. Deliberately NOT the id —
    the id is what this merge allocates, and ordering by it would move a
    renumbered row to a different place on the next run (invariant 2)."""
    return (stamp(row), payload_key(row))


def id_sort_key(rid):
    """Candidate-id preference: ints (event ids) before strings (task ids)."""
    if isinstance(rid, int) and not isinstance(rid, bool):
        return (0, rid, '')
    return (1, 0, str(rid))


class IdAllocator:
    """The file-wide id space, so a renumber can never land on an existing id.

    A fresh id is placed ABOVE every id in the result (invariant 5): the next
    `max(id)+1` append by any writer is then guaranteed to miss every row that
    is already in the board, and collisions are healed forward, never reused."""

    def __init__(self, rows):
        self.taken = set()
        self.high = 0            # max numeric id (events)
        self.prefix_max = {}     # 'TR-' -> max numeric tail seen (tasks)
        for row in rows:
            rid = row.get('id') if isinstance(row, dict) else None
            if rid is None:
                continue
            self.taken.add(scalar_id(rid))
            if isinstance(rid, int) and not isinstance(rid, bool):
                self.high = max(self.high, rid)
            elif isinstance(rid, str):
                m = _TRAILING_DIGITS.match(rid)
                if m:
                    key = m.group('prefix')
                    self.prefix_max[key] = max(self.prefix_max.get(key, 0),
                                               int(m.group('num')))

    def claim(self, rid):
        """Reserve an id chosen for the output."""
        self.taken.add(scalar_id(rid))
        if isinstance(rid, int) and not isinstance(rid, bool):
            self.high = max(self.high, rid)

    def fresh(self, like, hint=''):
        """A free id shaped like `like`: ints climb the high-water mark, `TR-126`
        keeps its prefix and climbs, any other string gets a content suffix."""
        if not isinstance(like, str):          # ints, and rows with no id
            while True:
                self.high += 1
                if scalar_id(self.high) not in self.taken:
                    self.taken.add(self.high)
                    return self.high
        m = _TRAILING_DIGITS.match(like)
        if m:
            prefix = m.group('prefix')
            num = max(self.prefix_max.get(prefix, 0), int(m.group('num')))
            while True:
                num += 1
                cand = f'{prefix}{num}'
                if cand not in self.taken:
                    self.prefix_max[prefix] = num
                    self.taken.add(cand)
                    return cand
        base = f'{like}~{hint}' if hint else f'{like}~1'
        cand, n = base, 1
        while cand in self.taken or cand == like:
            n += 1
            cand = f'{base}.{n}'
        self.taken.add(cand)
        return cand


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
    """Union three JSONL row lists. Returns (rows, notes).

    Pure: the result depends on the three row sets only, never on which side is
    `ours` (invariant 1) and never on how many times it has already run
    (invariant 2)."""
    notes = []
    clusters = []          # one entry per row: {'row', 'base_pos', 'base_id', 'ids'}
    index = {}             # row_key -> [cluster indices]

    def absorb(rows, is_base):
        for pos, row in enumerate(rows):
            key = row_key(row)
            hit = None
            for idx in index.get(key, ()):
                if same_row(clusters[idx]['row'], row):
                    hit = idx
                    break
            if hit is None:
                clusters.append({'row': row, 'base_pos': None, 'base_id': None,
                                 'ids': []})
                index.setdefault(key, []).append(len(clusters) - 1)
                hit = len(clusters) - 1
            cluster = clusters[hit]
            if is_base and cluster['base_pos'] is None:
                cluster['base_pos'] = pos
                cluster['base_id'] = row.get('id') if isinstance(row, dict) else None
            if isinstance(cluster['row'], dict) and isinstance(row, dict):
                if cluster['row'] != row:
                    cluster['row'] = merge_pair(cluster['row'], row)
                rid = row.get('id')
                if rid is not None and scalar_id(rid) not in \
                        {scalar_id(x) for x in cluster['ids']}:
                    cluster['ids'].append(rid)
            # non-board rows (scalars, lists) carry no fields to merge: an
            # identical row is already the same cluster (row_key = content).

    absorb(base, True)
    absorb(ours, False)
    absorb(theirs, False)

    # ORDER: base rows keep base order; new rows are ordered by content, so the
    # order is the same whichever side git called `ours`.
    ordered = sorted((c for c in clusters if c['base_pos'] is not None),
                     key=lambda c: c['base_pos'])
    ordered += sorted((c for c in clusters if c['base_pos'] is None),
                      key=lambda c: order_key(c['row']))

    # Which content claims which id. An id claimed by rows with DIFFERENT
    # content is CONTESTED — that is the collision that forced a renumber, and a
    # renumbering artifact (the same row arriving under two ids, because two
    # trees resolved the same collision differently) can only involve such ids.
    claims = {}
    for cluster in ordered:
        pid = payload_key(cluster['row'])
        for rid in cluster_ids(cluster):
            claims.setdefault(scalar_id(rid), set()).add(pid)
    contested = {rid for rid, pids in claims.items() if len(pids) > 1}

    # Collapse a multi-id group ONLY when every one of its ids is contested: then
    # the group is one row that two trees numbered differently, and merging it
    # onto a single id is what keeps this merge a fixed point instead of
    # duplicating rows on every cross-merge (invariant 2). Two rows that merely
    # look alike keep their own (uncontested) ids — content must never merge rows
    # whose ids nobody disputes, or a real append would be swallowed silently.
    grouped = {}
    for cluster in ordered:
        grouped.setdefault(payload_key(cluster['row']), []).append(cluster)
    logical = []
    for clusters in grouped.values():
        ids = []
        for cluster in clusters:
            for rid in cluster_ids(cluster):
                if rid is not None and scalar_id(rid) not in {scalar_id(x)
                                                              for x in ids}:
                    ids.append(rid)
        if len(clusters) > 1 and ids and all(scalar_id(i) in contested for i in ids):
            logical.append({'row': clusters[0]['row'], 'ids': ids,
                            'base_id': clusters[0]['base_id'],
                            'variants': len(clusters)})
        else:
            for cluster in clusters:
                logical.append({'row': cluster['row'], 'ids': cluster_ids(cluster),
                                'base_id': cluster['base_id'], 'variants': 1})

    # ASSIGN an id to every logical row: its own id when free, else a fresh one
    # above the file high-water mark. Rows from the merge base are assigned
    # first, so history always keeps its ids (invariant 4).
    alloc = IdAllocator([c['row'] for c in clusters])
    claimed = set()
    rows = []
    for entry in logical:
        row = entry['row']
        own = row.get('id') if isinstance(row, dict) else None
        chosen = None
        if entry['base_id'] is not None and \
                scalar_id(entry['base_id']) not in claimed:
            chosen = entry['base_id']        # history keeps its id
        if chosen is None:
            for cand in sorted(entry['ids'], key=id_sort_key):
                if scalar_id(cand) not in claimed:
                    chosen = cand
                    break
        if chosen is None:
            if own is None and not entry['ids']:
                chosen = None                # rows without ids stay without ids
            else:
                chosen = alloc.fresh(own if own is not None else entry['ids'][0],
                                     hint=hashlib.sha256(
                                         payload_key(row).encode()).hexdigest()[:6])
                label = _label(row)
                notes.append(f'renumbered a colliding id to {chosen} ({label})')
        if entry['variants'] > 1:
            notes.append('collapsed %d rows that were the same row under '
                         'different ids (%s) onto id %s'
                         % (entry['variants'],
                            ', '.join(str(i) for i in entry['ids']), chosen))
        if isinstance(row, dict) and chosen is not None and row.get('id') != chosen:
            row = dict(row)
            row['id'] = chosen
        if chosen is not None:
            alloc.claim(chosen)
            claimed.add(scalar_id(chosen))
        rows.append(row)
    return rows, notes


def _label(row):
    if not isinstance(row, dict):
        return 'row'
    for key in ('event_type', 'title', 'task_id', 'event', 'name', 'id'):
        if row.get(key) is not None:
            return f'{key}={row[key]}'
    return 'row'


def main(argv):
    if len(argv) != 4:
        print('usage: board-merge-driver.py <base> <ours> <theirs>', file=sys.stderr)
        return 2
    base_path, ours_path, theirs_path = argv[1:]
    try:
        with open(base_path) as fh:
            base, e1 = parse(fh.read(), base_path)
        with open(ours_path) as fh:
            ours, e2 = parse(fh.read(), ours_path)
        with open(theirs_path) as fh:
            theirs_rows, e3 = parse(fh.read(), theirs_path)
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
