"""TR-292: the board row IS the raw-level contract (complexity-model R6.1-R6.4).

Pins, from the shipped code:
- a row carrying `required_categories` (the router's canonical vocabulary,
  int -5..+5) resolves with NO classifier call — complexity_source=declared-raw;
- the raw levels on the row produce the SAME chain as the identical levels
  passed on the command line (replayability, R6.4);
- the scalar `complexity` cannot drift back to a string: the normaliser maps
  the observed vocabulary and the validator rejects anything non-int.
"""
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'scripts'))
import router_spawn as rs          # noqa: E402
import board_row_levels as brl     # noqa: E402


def _fixture_registry(tmp_path):
    """Same hermetic fixture shape as tests/test_spawn_complexity.py: 3 models
    across 2 providers, tier-eligible for the fixture categories."""
    cats = ("agent_tick", "debug", "code_gen")
    reg = {"version": 3, "generated_at": "fixture",
           "tables": {"models": [], "model_tier": [], "task_profiles": [],
                      "task_profile_requirements": [], "projects": [],
                      "category_levels": [], "level_defs": []}}
    for prov, model, price in (("prov-a", "a1", 1.0), ("prov-a", "a2", 2.0),
                               ("prov-b", "b1", 3.0)):
        reg["tables"]["models"].append({
            "provider": prov, "model": model, "normalized_price": price,
            "token_factor": 1.0, "plan_tier": 0, "data_class": "zdr",
            "valid_to": None, "archive": False})
        for cat in cats:
            reg["tables"]["model_tier"].append(
                {"model": model, "category": cat, "tier": 5})
    for cat in cats:
        for lvl in range(-5, 6):
            reg["tables"]["category_levels"].append(
                {"category": cat, "level": lvl})
    for lvl in range(-5, 6):
        reg["tables"]["level_defs"].append({"level": lvl})
    path = tmp_path / 'registry.json'
    path.write_text(json.dumps(reg))
    return str(path)


def _open_state(tmp_path, providers):
    d = tmp_path / 'state'
    d.mkdir(exist_ok=True)
    (d / 'quota-state.json').write_text(json.dumps(
        {'updated': 'test',
         'providers': {p: {'status': 'open'} for p in providers}}))
    (d / 'health-state.json').write_text(json.dumps(
        {'providers': {p: {'status': 'open'} for p in providers}}))
    (d / 'circuit-state.json').write_text(json.dumps({'pairs': {}}))
    return str(d)


def _write_board(tmp_path, rows):
    p = tmp_path / '.coding-hermes' / 'board'
    p.mkdir(parents=True)
    f = p / 'tasks.jsonl'
    f.write_text('\n'.join(json.dumps(r) for r in rows) + '\n')
    return str(f)


LEVELS = {'code_gen': 4, 'debug': 3}
BOARD_ROW = {'id': 'TR-9001', 'title': 'raw levels row',
             'description': 'declares its own levels',
             'required_categories': LEVELS}


# ---------- the classifier is never called for a declared-raw row ----------

def test_row_levels_resolve_without_the_classifier(monkeypatch, capsys, tmp_path):
    board = _write_board(tmp_path, [BOARD_ROW])
    monkeypatch.setattr(rs, 'REGISTRY', _fixture_registry(tmp_path))
    monkeypatch.setattr(rs, 'MR', _open_state(tmp_path, ('prov-a', 'prov-b')))

    def _boom(*a, **k):
        raise AssertionError('the classifier MUST NOT be called for a '
                             'declared-raw row')
    monkeypatch.setattr(rs, 'complexity_requirements', _boom)
    monkeypatch.setattr(sys, 'argv', ['router_spawn.py', '--profile-from-board',
                                      'TR-9001', '--board', board,
                                      '--format', 'json'])
    rs.main()
    out = json.loads(capsys.readouterr().out)
    assert out['complexity']['source'] == 'declared-raw'
    assert out['complexity']['matrix'] == LEVELS
    assert out['complexity']['degraded'] is False
    assert out['resolved_as'] == 'adhoc', 'the raw levels ARE the requirements'
    assert out.get('chain'), 'the caller still gets a chain'


def test_invalid_levels_degrade_visibly_and_never_block(monkeypatch, capsys,
                                                        tmp_path):
    bad = dict(BOARD_ROW, required_categories={'code_gen': 'huge', 'debug': 12},
               profile='P1_CODING')
    board = _write_board(tmp_path, [bad])
    monkeypatch.setattr(rs, 'REGISTRY', _fixture_registry(tmp_path))
    monkeypatch.setattr(rs, 'MR', _open_state(tmp_path, ('prov-a', 'prov-b')))
    monkeypatch.setattr(sys, 'argv', ['router_spawn.py', '--profile-from-board',
                                      'TR-9001', '--board', board,
                                      '--format', 'json'])
    rs.main()
    out = json.loads(capsys.readouterr().out)
    # no declared-raw resolve: the row's map had no usable numeric level
    assert not (out.get('complexity') or {}).get('source') == 'declared-raw'
    # fail-open: a visible structured answer, exit 0 — never a crash or block
    assert 'error' in out or out.get('chain')


def test_non_numeric_level_is_dropped_others_still_apply(monkeypatch, capsys,
                                                         tmp_path):
    row = dict(BOARD_ROW, required_categories={'code_gen': 4, 'debug': 'n/a'})
    board = _write_board(tmp_path, [row])
    monkeypatch.setattr(rs, 'REGISTRY', _fixture_registry(tmp_path))
    monkeypatch.setattr(rs, 'MR', _open_state(tmp_path, ('prov-a', 'prov-b')))
    monkeypatch.setattr(sys, 'argv', ['router_spawn.py', '--profile-from-board',
                                      'TR-9001', '--board', board,
                                      '--format', 'json'])
    rs.main()
    out = json.loads(capsys.readouterr().out)
    assert out['complexity']['source'] == 'declared-raw'
    assert out['complexity']['matrix'] == {'code_gen': 4}
    assert any('debug' in p for p in out['complexity']['problems'])


# ---------- replayability: row levels == command-line levels (R6.4) ----------

def test_row_levels_replay_the_command_line_chain(monkeypatch, capsys, tmp_path):
    """Given the row (and the registry), the chain equals the one the SAME
    levels produce on --profile-req."""
    board = _write_board(tmp_path, [BOARD_ROW])
    monkeypatch.setattr(rs, 'REGISTRY', _fixture_registry(tmp_path))
    monkeypatch.setattr(rs, 'MR', _open_state(tmp_path, ('prov-a', 'prov-b')))

    def _run(argv):
        monkeypatch.setattr(sys, 'argv', argv)
        rs.main()
        return json.loads(capsys.readouterr().out)

    from_row = _run(['router_spawn.py', '--profile-from-board', 'TR-9001',
                     '--board', board, '--no-health', '--format', 'json'])
    from_cli = _run(['router_spawn.py',
                     '--profile-req', 'code_gen=4', 'debug=3',
                     '--no-health', '--format', 'json'])
    chain = lambda r: [(h['provider'], h['model']) for h in r.get('chain', [])]
    assert chain(from_row) == chain(from_cli)
    assert from_row['resolved_as'] == from_cli['resolved_as'] == 'adhoc'
    assert from_row['complexity']['matrix'] == LEVELS
    # the row's adhoc list is byte-identical to what --profile-req carried
    assert from_row['complexity']['adhoc'] == ['code_gen=4', 'debug=3']


# ---------- the field cannot drift again (normalise + enforce) ----------

def test_scalar_vocabulary_normalises_the_measured_drift(tmp_path):
    board = tmp_path / 'tasks.jsonl'
    board.write_text('\n'.join(json.dumps(r) for r in [
        {'id': 'A', 'complexity': 'moderate', 'title': 'keep me'},
        {'id': 'B', 'complexity': 3, 'title': 'already int'},
        {'id': 'C', 'complexity': 'mechanical', 'title': 'keep me too'},
        {'id': 'D', 'complexity': 'weirdword', 'title': 'no mapping'},
        {'id': 'E', 'complexity': None, 'title': 'null is honest'},
    ]) + '\n')
    rows = brl.latest_by_id(str(board))
    assert rows['A'][1]['complexity'] == 'moderate'
    fixes = {}
    for rid, (n, row) in rows.items():
        f = brl.complexity_drift(row)
        if f is not None:
            fixes[rid] = f[0]
    assert fixes == {'A': 3, 'C': 1}, fixes


def test_validate_flags_strings_but_not_ints(tmp_path, capsys):
    board = tmp_path / 'tasks.jsonl'
    board.write_text('\n'.join(json.dumps(r) for r in [
        {'id': 'A', 'complexity': 'moderate'},
        {'id': 'B', 'complexity': 3},
        {'id': 'C', 'complexity': None,
         'required_categories': {'code_gen': 4}},
        {'id': 'D', 'required_categories': {'code_gen': 9}},
    ]) + '\n')
    assert brl.cmd_validate(type('A', (), {'board': str(board)})()) == 1
    out = capsys.readouterr().out
    assert 'A' in out and 'moderate' in out
    assert 'D' in out and 'code_gen' in out
    # B and C conform: never named as problems
    assert '\n  - B ' not in out and '\n  - C ' not in out


def test_normalize_carries_the_whole_row(tmp_path):
    """The append-only trap: a superseding append that drops fields blanks
    them. The normaliser must copy the WHOLE parsed row."""
    board = tmp_path / 'tasks.jsonl'
    full = {'id': 'A', 'complexity': 'moderate', 'title': 'T',
            'detail': 'long detail that must survive',
            'acceptance_criteria': ['c1', 'c2']}
    board.write_text(json.dumps(full) + '\n')
    rows = brl.latest_by_id(str(board))
    _n, row = rows['A']
    fix = brl.complexity_drift(row)
    out = dict(row)
    out['complexity'] = fix[0]
    out['complexity_note'] = 'TR-292 normalised'
    for k in ('title', 'detail', 'acceptance_criteria'):
        assert out[k] == full[k], f'{k} must survive a superseding append'
    assert out['complexity'] == 3 and out['complexity_note']
