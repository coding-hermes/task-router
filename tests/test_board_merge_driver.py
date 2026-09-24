"""Contracts for the JSONL board union merge driver.

Context: concurrent `gitreins task complete` runs both append to the same board
and both allocate `max(id)+1`, so the same id lands with different content and
git declares a conflict on a file that has no semantic conflict. Measured
2026-09-23: 4-7 concurrent completions left the task-router board unmerged for
>25 minutes, blocked every commit in the tree, and a later merge/abort wiped
staged work. These tests pin the driver's promises: no row lost, no id
duplicated, newness wins field-by-field, and a failure refuses to write.
"""
import json
import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DRIVER = os.path.join(REPO, 'scripts', 'board-merge-driver.py')


def run_driver(b, o, t, tmp_path, ours_text=None):
    for name, text in (('base', b), ('ours', ours_text if ours_text is not None else o),
                       ('theirs', t)):
        (tmp_path / name).write_text(text)
    r = subprocess.run([sys.executable, DRIVER, str(tmp_path / 'base'),
                        str(tmp_path / 'ours'), str(tmp_path / 'theirs')],
                       capture_output=True, text=True)
    return r, (tmp_path / 'ours')


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
