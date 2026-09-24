"""Contracts for the JSONL board union merge driver.

Context: concurrent `gitreins task complete` runs both append to the same board
and both allocate `max(id)+1`, so the same id lands with different content and
git declares a conflict on a file that has no semantic conflict. Measured
2026-09-23: 4-7 concurrent completions left the task-router board unmerged for
>25 minutes, blocked every commit in the tree, and a later merge/abort wiped
staged work. These tests pin the driver's promises: no row lost, no id
duplicated, newness wins field-by-field, a failure refuses to write — and the
invariant that makes a lock-free board survive concurrency at all:

  * ORDER-INDEPENDENT — merging the same pair with the sides swapped writes the
    same bytes, so two trees that resolve the same collision agree instead of
    re-conflicting forever. Without it each tree wrote a different board and the
    next cross-merge duplicated the colliding rows again (9 -> 11 -> 15 -> 23 ->
    39 -> 71 -> 135 rows, measured against the previous driver).
  * IDEMPOTENT — re-merging the driver's own output is a no-op.
  * STABLE IDS — a row whose id nobody else holds keeps it.

To reproduce a failure against another copy of the driver (e.g. the previous
revision) set BOARD_MERGE_DRIVER=/path/to/board-merge-driver.py.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DRIVER = os.environ.get('BOARD_MERGE_DRIVER') or os.path.join(
    REPO, 'scripts', 'board-merge-driver.py')
BOARD = '.coding-hermes/board/events.jsonl'


def run_driver(b, o, t, tmp_path, ours_text=None):
    for name, text in (('base', b), ('ours', ours_text if ours_text is not None else o),
                       ('theirs', t)):
        (tmp_path / name).write_text(text)
    r = subprocess.run([sys.executable, DRIVER, str(tmp_path / 'base'),
                        str(tmp_path / 'ours'), str(tmp_path / 'theirs')],
                       capture_output=True, text=True)
    return r, (tmp_path / 'ours')


def run_once(b, o, t):
    """One driver run in a throwaway dir: (returncode, stderr, merged text)."""
    work = tempfile.mkdtemp(prefix='board-merge-test-')
    return merge_in(work, b, o, t)


def merge_in(work, b, o, t):
    for name, text in (('base', b), ('ours', o), ('theirs', t)):
        with open(os.path.join(work, name), 'w') as fh:
            fh.write(text)
    r = subprocess.run([sys.executable, DRIVER, os.path.join(work, 'base'),
                        os.path.join(work, 'ours'), os.path.join(work, 'theirs')],
                       capture_output=True, text=True)
    with open(os.path.join(work, 'ours')) as fh:
        return r.returncode, r.stderr, fh.read()


def rows_of(path):
    return [json.loads(l) for l in path.read_text().split('\n') if l.strip()]


def jl(*rows):
    return ''.join(json.dumps(r) + '\n' for r in rows)


def test_independent_appends_merge_without_a_conflict(tmp_path):
    """The common case: both sides appended. Nothing conflicts, keep both."""
    base = jl({'id': 1, 'task_id': 'A'})
    ours = jl({'id': 1, 'task_id': 'A'}, {'id': 2, 'task_id': 'B'})
    theirs = jl({'id': 1, 'task_id': 'A'}, {'id': 3, 'task_id': 'C'})
    r, out = run_driver(base, ours, theirs, tmp_path)
    assert r.returncode == 0, r.stderr
    rows = rows_of(out)
    assert [x['id'] for x in rows] == [1, 2, 3]
    assert [x['task_id'] for x in rows] == ['A', 'B', 'C']


def test_the_same_id_taken_by_both_sides_keeps_both_rows(tmp_path):
    """The failure mode that deadlocked the tree: both took max+1 for DIFFERENT
    rows. Both must survive, and the id must end up unique."""
    base = jl({'id': 7, 'task_id': 'A'})
    ours = jl({'id': 7, 'task_id': 'A'}, {'id': 8, 'event_type': 'ours_new', 'task_id': 'B'})
    theirs = jl({'id': 7, 'task_id': 'A'}, {'id': 8, 'event_type': 'theirs_new', 'task_id': 'C'})
    r, out = run_driver(base, ours, theirs, tmp_path)
    assert r.returncode == 0, r.stderr
    rows = rows_of(out)
    assert len(rows) == 3, 'one row would have been lost'
    ids = [x['id'] for x in rows]
    assert len(ids) == len(set(ids)), 'ids must stay unique'
    assert {x.get('event_type') for x in rows} == {'ours_new', 'theirs_new', None}
    assert 'renumbered' in r.stderr


def test_same_row_edited_on_both_sides_newest_timestamp_wins(tmp_path):
    base = jl({'id': 'TR-9', 'status': 'open', 'updated_at': '2026-09-01T00:00:00Z'})
    ours = jl({'id': 'TR-9', 'status': 'open', 'updated_at': '2026-09-01T00:00:00Z',
               'owner': 'ours'})
    theirs = jl({'id': 'TR-9', 'status': 'complete', 'updated_at': '2026-09-02T00:00:00Z'})
    r, out = run_driver(base, ours, theirs, tmp_path)
    assert r.returncode == 0, r.stderr
    row = rows_of(out)[0]
    assert row['status'] == 'complete', 'the newer edit must win'
    assert row['owner'] == 'ours', 'fields only one side has are kept'


def test_an_unparseable_side_refuses_to_write(tmp_path):
    """A driver bug must never silently truncate the board: leaving the conflict
    with markers in place is strictly better than losing rows."""
    good = jl({'id': 1})
    broken = '{"id": 1}\nnot json at all\n'
    r, out = run_driver(good, broken, good, tmp_path)
    assert r.returncode != 0
    assert out.read_text() == broken, 'the input must be left untouched'


def test_a_previously_conflicted_file_is_repaired_by_union(tmp_path):
    conflicted = ('{"id": 1, "task_id": "A"}\n'
                  '<<<<<<< HEAD\n{"id": 2, "task_id": "B"}\n'
                  '=======\n{"id": 2, "task_id": "C"}\n'
                  '>>>>>>> wt/other\n')
    r, out = run_driver(jl({'id': 1, 'task_id': 'A'}), conflicted, '', tmp_path)
    assert r.returncode == 0, r.stderr
    text = out.read_text()
    assert '<<<<<<<' not in text and '>>>>>>>' not in text
    rows = rows_of(out)
    assert sorted(x['task_id'] for x in rows) == ['A', 'B', 'C']
    ids = [x['id'] for x in rows]
    assert len(ids) == len(set(ids))


def test_a_pre_existing_duplicate_id_is_repaired_not_propagated(tmp_path):
    """event id 102 was used twice (09-19 and 09-21) by the same lock-free
    allocation. Merging must not propagate the duplicate."""
    dup = jl({'id': 102, 'event_type': 'first'}, {'id': 102, 'event_type': 'second'})
    r, out = run_driver('', dup, jl({'id': 103, 'event_type': 'other'}), tmp_path)
    assert r.returncode == 0, r.stderr
    ids = [x['id'] for x in rows_of(out)]
    assert len(ids) == len(set(ids))
    assert len(rows_of(out)) == 3, 'no row lost while de-duplicating'


def test_blank_lines_and_missing_trailing_newline_are_tolerated(tmp_path):
    r, out = run_driver('', '{"id": 1}\n\n', '\n{"id": 2}', tmp_path)
    assert r.returncode == 0, r.stderr
    assert [x['id'] for x in rows_of(out)] == [1, 2]


def test_gitattributes_selects_the_driver():
    """Without this line git never calls the driver and merges conflict again."""
    path = os.path.join(REPO, '.gitattributes')
    assert os.path.exists(path), '.gitattributes is required for the driver to run'
    text = open(path).read()
    assert '.coding-hermes/board/*.jsonl merge=boardjsonl' in text


def test_the_setup_script_wires_the_driver_command():
    path = os.path.join(REPO, 'scripts', 'enable-board-merge-driver.sh')
    text = open(path).read()
    assert 'merge.boardjsonl.driver' in text
    assert 'board-merge-driver.py' in text


# --- the concurrency invariant ---------------------------------------------
#
# A board is appended by many writers with no lock, so two of them WILL allocate
# the same id for different rows. Everything below is about what must be true for
# that to stay survivable: the merge has to be a function of the ROWS, never of
# which side git happened to call `ours`, and running it twice has to change
# nothing.


def rows(text):
    return [json.loads(l) for l in text.split('\n') if l.strip()]


def concurrent_pair():
    """Two writers, one fresh id, different rows — and the two boards the two
    trees produced when each resolved that collision first (roles swapped)."""
    base = jl(*[{'id': i, 'timestamp': '2026-09-23T00:0%d:00Z' % i,
                 'event_type': 'seed', 'task_id': 'T%d' % i}
                for i in range(1, 8)])
    x = {'id': 8, 'timestamp': '2026-09-23T01:00:00Z',
         'event_type': 'writer_x', 'task_id': 'TR-X'}
    y = {'id': 8, 'timestamp': '2026-09-23T01:00:01Z',
         'event_type': 'writer_y', 'task_id': 'TR-Y'}
    rc, err, ours = run_once(base, base + jl(x), base + jl(y))
    assert rc == 0, err
    rc, err, theirs = run_once(base, base + jl(y), base + jl(x))
    assert rc == 0, err
    return base, ours, theirs


def test_merging_the_same_pair_either_way_writes_the_same_bytes():
    """ORDER-INDEPENDENCE — the root cause of the deadlock. Both trees merge the
    SAME two rows and only the roles differ; if the answer depends on the roles,
    the two boards disagree and their next merge conflicts again."""
    base, ours, theirs = concurrent_pair()
    _, _, one_way = run_once(base, ours, theirs)
    _, _, other_way = run_once(base, theirs, ours)
    assert one_way == other_way, \
        'the merge depends on which side was ours — two trees diverge, and the ' \
        'next merge of the two conflicts on the same file again'
    assert one_way == ours, 'keeping BOTH appends is the answer, not picking one'


def test_repeated_cross_merges_do_not_duplicate_rows():
    """THE DEADLOCK, at the driver level. Two trees that resolved the same
    collision keep merging each other's result; every round must be a no-op.
    The previous driver grew the board 9 -> 11 -> 15 -> 23 -> 39 -> 71 -> 135
    rows and never settled, because it renumbered rows by merge order, so no two
    trees ever agreed on which row owned which id."""
    base, ours, theirs = concurrent_pair()
    for rnd in range(6):
        _, _, ours = run_once(base, ours, theirs)
        _, _, theirs = run_once(base, theirs, ours)
        assert ours == theirs, \
            'round %d left two different boards: the trees can never converge' % rnd
        assert len(rows(ours)) == 9, \
            'round %d: 9 rows in, %d out — appends are being duplicated' \
            % (rnd, len(rows(ours)))


def test_a_row_two_trees_numbered_differently_collapses_onto_one_id():
    """X is id 8 in one tree and id 9 in the other (each tree renumbered the
    same collision the other way). Ids 8 and 9 are CONTESTED — Y claims each of
    them too — so the variants are one row, not two."""
    base, ours, theirs = concurrent_pair()
    _, _, merged = run_once(base, ours, theirs)
    tail = [(r['id'], r['event_type']) for r in rows(merged)[7:]]
    assert tail == [(8, 'writer_x'), (9, 'writer_y')], tail


def test_rows_that_only_differ_by_id_are_kept_apart_when_nothing_is_contested():
    """The collapse rule stays conservative: content may only fold rows whose ids
    nobody else disputes. Two rows that merely look alike keep both ids — a merge
    must never swallow a row because it resembles another."""
    rc, err, merged = run_once('', '{"id": 1}\n', '{"id": 2}\n')
    assert rc == 0, err
    assert [r['id'] for r in rows(merged)] == [1, 2]


def test_a_row_whose_id_is_unique_is_never_renumbered():
    """STABLE IDS. The previous driver handed the next integer to whatever row
    followed a collision, so id 9 below — a real, uncontested row — moved to 10.
    A row whose id changes on every merge can never converge."""
    dup = jl({'id': 8, 'event_type': 'a'}, {'id': 8, 'event_type': 'b'})
    other = jl({'id': 9, 'event_type': 'real_row', 'task_id': 'TR-9'})
    rc, err, merged = run_once('', dup, other)
    assert rc == 0, err
    by_event = {r['event_type']: r['id'] for r in rows(merged)}
    assert by_event['real_row'] == 9, 'a row nobody disputes keeps its id'
    assert sorted(by_event.values()) == [8, 9, 10]
    assert len(rows(merged)) == 3, 'no row lost while de-duplicating'


def test_rerunning_the_driver_on_its_own_output_changes_nothing():
    """IDEMPOTENCE. A merge that has already been applied must be a no-op when it
    is applied again — rewinds, `git merge --abort`, a repeated pull."""
    base, ours, _ = concurrent_pair()
    _, _, merged = run_once(base, ours, '')
    _, _, again = run_once('', merged, merged)
    assert again == merged
    _, _, with_base = run_once(base, merged, merged)
    assert with_base == merged


def test_a_legacy_row_without_an_id_is_left_alone():
    """Older events carry `ts`/`event` and no id at all. The driver must not
    invent one, and must not let them take part in id allocation."""
    legacy = {'ts': '2026-09-19T10:00:00Z', 'event': 'legacy_row'}
    rc, err, merged = run_once('', jl(legacy, {'id': 5, 'event_type': 'new'}),
                               jl({'id': 6, 'event_type': 'theirs'}))
    assert rc == 0, err
    assert legacy in rows(merged), 'the legacy row must survive verbatim'
    assert sorted(r['id'] for r in rows(merged) if 'id' in r) == [5, 6]


def test_a_colliding_task_id_keeps_the_repos_id_shape():
    """Two task rows both allocated TR-126. The loser keeps the TR-<n> shape and
    climbs above the highest TR id instead of becoming something no board
    consumer recognises."""
    rc, err, merged = run_once('', jl({'id': 'TR-126', 'title': 'A'},
                                      {'id': 'TR-126', 'title': 'B'}),
                               jl({'id': 'TR-128', 'title': 'C'}))
    assert rc == 0, err
    assert sorted(r['id'] for r in rows(merged)) == ['TR-126', 'TR-128', 'TR-129']


def test_the_module_docstring_states_the_invariant():
    """The invariant is the contract a later maintainer must not break by
    accident; it lives in the driver's own docstring, not only in this file."""
    head = open(DRIVER).read().split('"""')[1]
    for marker in ('ORDER-INDEPENDENT', 'IDEMPOTENT', 'LOSSLESS', 'STABLE IDS',
                   'HIGH-WATER MARK'):
        assert marker in head, marker


# --- the same deadlock on a real merge -------------------------------------


def _git(repo, *args):
    return subprocess.run(('git',) + args, cwd=str(repo), capture_output=True,
                          text=True, env={**os.environ, 'GIT_CONFIG_NOSYSTEM': '1',
                                          'GIT_TERMINAL_PROMPT': '0'})


def _git_ok(repo, *args):
    r = _git(repo, *args)
    assert r.returncode == 0, 'git %s failed:\n%s%s' % (' '.join(args), r.stdout, r.stderr)
    return r


def _append(path, row):
    with open(path, 'a') as fh:
        fh.write(json.dumps(row) + '\n')


@pytest.mark.skipif(shutil.which('git') is None, reason='git is required')
def test_two_trees_that_merge_each_other_end_up_with_the_same_board(tmp_path):
    """THE DEADLOCK, on a real merge (not only in-process).

    Two trees each append a row and allocate the SAME fresh id, then each merges
    the other's PRE-MERGE commit — the shape of two workers pulling each other.
    With a side-order-dependent driver the two trees wrote different boards, so
    every later merge conflicted again: that loop is what left the board unmerged
    for >25 minutes and refused every commit in the tree."""
    repo = tmp_path / 'clone'
    repo.mkdir()
    _git_ok(repo, 'init', '-q', '-b', 'main')
    for key, value in (('user.email', 'board@example.invalid'),
                       ('user.name', 'board test'),
                       ('commit.gpgsign', 'false'),
                       ('merge.boardjsonl.name', 'union JSONL board merge'),
                       ('merge.boardjsonl.driver',
                        '%s %s %%O %%A %%B' % (sys.executable, DRIVER))):
        _git_ok(repo, 'config', key, value)
    (repo / '.gitattributes').write_text(BOARD + ' merge=boardjsonl\n')
    board = repo / BOARD
    board.parent.mkdir(parents=True)
    board.write_text(jl(*[{'id': i, 'timestamp': '2026-09-23T00:0%d:00Z' % i,
                           'event_type': 'seed'} for i in range(1, 8)]))
    _git_ok(repo, 'add', '-A')
    _git_ok(repo, 'commit', '-qm', 'base board (ids 1..7)')
    _git_ok(repo, 'branch', 'other')

    _append(board, {'id': 8, 'timestamp': '2026-09-23T01:00:00Z',
                    'event_type': 'writer_x'})
    _git_ok(repo, 'add', '-A')
    _git_ok(repo, 'commit', '-qm', 'writer x appends id 8')
    x_rev = _git_ok(repo, 'rev-parse', 'HEAD').stdout.strip()

    _git_ok(repo, 'checkout', '-q', 'other')
    _append(board, {'id': 8, 'timestamp': '2026-09-23T01:00:01Z',
                    'event_type': 'writer_y'})
    _git_ok(repo, 'add', '-A')
    _git_ok(repo, 'commit', '-qm', 'writer y appends id 8')
    y_rev = _git_ok(repo, 'rev-parse', 'HEAD').stdout.strip()

    for who, peer in (('main', y_rev), ('other', x_rev)):
        _git_ok(repo, 'checkout', '-q', who)
        _git_ok(repo, 'merge', '--no-edit', peer)

    main_board = _git_ok(repo, 'show', 'main:' + BOARD).stdout
    other_board = _git_ok(repo, 'show', 'other:' + BOARD).stdout
    assert main_board == other_board, (
        'the two trees wrote different boards for the same collision: their next '
        'merge conflicts on the same file again — that is the deadlock')

    merged = rows(main_board)
    assert len(merged) == 9, 'both appends survived, nothing duplicated'
    assert len({r['id'] for r in merged}) == 9, 'ids stay unique'
    assert {r['event_type']: r['id'] for r in merged if r['event_type'] != 'seed'} \
        == {'writer_x': 8, 'writer_y': 9}, 'each append keeps one stable id'
    assert '<<<<<<<' not in main_board

    # and the loop is broken: further cross-merges never leave the two trees
    # disagreeing or add a row
    for who, peer in (('main', 'other'), ('other', 'main')):
        _git_ok(repo, 'checkout', '-q', who)
        r = _git(repo, 'merge', '--no-edit', peer)
        assert r.returncode == 0, 'a merge of two settled boards must not conflict'
    assert _git_ok(repo, 'show', 'main:' + BOARD).stdout == main_board
    assert _git_ok(repo, 'show', 'other:' + BOARD).stdout == main_board
