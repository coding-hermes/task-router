"""TR-150: the page is one self-contained document, and its search panels cannot lie.

Three things are pinned here, all of them from the row that asked for the page:
  * it is ONE document with NOTHING fetched from the network (no CDN, no external asset), because a
    page that needs the internet is useless exactly when the network is what broke;
  * the first screen is a search box and the keyboard drives it;
  * the board panel reports what it read, like the ledger panel does.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts'))
import router_server as rs  # noqa: E402
import router_ui_page as ui  # noqa: E402


def test_ui_is_served_by_this_service():
    assert '/ui' in (rs.OPENAPI.get('paths') or []), 'the page must be a route of the router itself'


def test_the_page_fetches_nothing_from_the_network():
    page = ui.page_html()
    for token in ('http://', 'https://', '//cdn', 'integrity=', '<script src', '<link rel="stylesheet" href="http'):
        assert token not in page, f'the page must not reference {token!r}'
    # every request it makes is same-origin and rooted
    assert "fetch(url" in page and "'/health'" in page


def test_the_first_screen_is_a_search_and_the_keyboard_drives_it():
    page = ui.page_html()
    assert 'id="q"' in page and 'placeholder="search ledger' in page
    assert "e.key === '/'" in page and 'Escape' in page and 'ArrowDown' in page


def test_the_page_says_it_is_read_only_and_never_renders_a_fake_zero():
    page = ui.page_html()
    assert 'READ-ONLY' in page
    assert 'empty or stale data says so instead of rendering 0' in page
    # the honest empty-state strings the panels use
    assert 'no proxied traffic in this window' in page
    assert 'no ledger row matched' in page
    assert 'no priced sample' in page


def test_board_search_reports_what_it_scanned(tmp_path):
    p = tmp_path / 'tasks.jsonl'
    rows = [
        {'id': 'TR-1', 'title': 'alpha thing', 'status': 'complete', 'updated_at': '2026-09-26T00:00:00Z'},
        {'id': 'TR-2', 'title': 'beta thing', 'status': 'pending', 'foreman_note': 'waiting on a decision'},
    ]
    p.write_text(''.join(json.dumps(r) + '\n' for r in rows))
    r = ui.board_search({'limit': '5'}, str(p))
    assert r['total_rows'] == 2 and r['total_matched'] == 2 and r['returned'] == 2
    assert r['scan_truncated'] is False and r['truncated'] is False
    assert ui.board_search({'q': 'beta', 'limit': '5'}, str(p))['total_matched'] == 1
    assert ui.board_search({'status': 'pending', 'limit': '5'}, str(p))['total_matched'] == 1
    paged = ui.board_search({'limit': '1'}, str(p))
    assert paged['returned'] == 1 and paged['truncated'] is True


def test_board_search_on_a_missing_board_is_an_error_not_an_empty_board(tmp_path):
    r = ui.board_search({}, str(tmp_path / 'nope.jsonl'))
    assert 'error' in r and r['total_rows'] == 0


def test_the_series_buckets_are_honest_about_samples(tmp_path):
    p = tmp_path / 'outcomes.jsonl'
    now = 1_800_000_000.0
    rows = [
        {'source_system': 'router-proxy', 'provider': 'a', 'model': 'm', 'ts': now - 60,
         'success': True, 'cost_usd': 0.5, 'tokens_in': 10, 'tokens_out': 2},
        {'source_system': 'router-proxy', 'provider': 'a', 'model': 'm', 'ts': now - 120,
         'success': False, 'cost_usd': None, 'tokens_in': 5, 'tokens_out': 1},
    ]
    p.write_text(''.join(json.dumps(r) + '\n' for r in rows))
    import router_ui_page as up
    r = up.series({'bucket': 'hour', 'window_h': '6', 'group': 'total'}, str(p), now_s=now)
    live = [b for b in r['buckets'] if b.get('requests')]
    assert len(live) == 1
    b = live[0]
    assert b['requests'] == 2 and b['served'] == 1 and b['failed'] == 1
    assert b['cost_samples'] == 1, 'a bucket must disclose how many samples were priced'
    assert b['cost_usd'] == 0.5 and 'no priced sample' not in (b.get('cost_reason') or '')
    # quiet buckets are present, and say they are quiet rather than reading as zero traffic
    quiet = [x for x in r['buckets'] if not x.get('requests')]
    assert quiet and all(x['note'] == 'no traffic in this bucket' for x in quiet)
    assert r['rows_scanned'] == 2


def test_the_series_can_group_by_band_and_by_lane(tmp_path):
    p = tmp_path / 'outcomes.jsonl'
    now = 1_800_000_000.0
    rows = [
        {'source_system': 'router-proxy', 'provider': 'a', 'model': 'm1', 'ts': now - 60,
         'complexity_sig': 'band-x', 'cost_usd': 0.1},
        {'source_system': 'router-proxy', 'provider': 'b', 'model': 'm2', 'ts': now - 60,
         'complexity_sig': 'band-y', 'cost_usd': 0.2},
    ]
    p.write_text(''.join(json.dumps(r) + '\n' for r in rows))
    import router_ui_page as up
    by_band = {b['key'] for b in up.series({'group': 'band'}, str(p), now_s=now)['buckets'] if b.get('key')}
    by_lane = {b['key'] for b in up.series({'group': 'lane'}, str(p), now_s=now)['buckets'] if b.get('key')}
    assert by_band == {'band-x', 'band-y'}
    assert by_lane == {'a/m1', 'b/m2'}


def test_an_unreadable_store_is_reported_by_the_series(tmp_path):
    import router_ui_page as up
    r = up.series({}, str(tmp_path / 'nope.jsonl'))
    assert 'error' in r and r['buckets'] == []


def test_the_flow_reconciles_what_it_can_and_names_what_it_cannot(tmp_path):
    p = tmp_path / 'outcomes.jsonl'
    rows = [
        {'source_system': 'router-proxy', 'session_id': 's-1', 'provider': 'xkiro',
         'model': 'openai/gpt-6-luna', 'ts': 1000.0, 'success': False, 'cost_usd': None,
         'failure_reason': 'hop-wall-timeout', 'hops_attempted': 2, 'max_hops': 3,
         'complexity_source': 'classifier', 'chain_evidence': {'chain_length': 200, 'truncated': True,
                                                              'excluded': 30}},
        {'source_system': 'router-proxy', 'session_id': 's-1', 'provider': 'stepfun',
         'model': 'step-3.7-flash', 'ts': 1100.0, 'success': True, 'cost_usd': 0.02,
         'price_basis': 'public split', 'steps': 3},
    ]
    p.write_text(''.join(json.dumps(r) + '\n' for r in rows))
    import router_ui_page as up
    r = up.flow({'id': 's-1'}, str(p))
    assert r['found'] is True
    assert r['request']['requests_logged'] == 2 and r['request']['served_lane'] == 'stepfun/step-3.7-flash'
    assert r['rating']['source'] == 'classifier'
    assert r['chain']['considered'] == 200 and r['chain']['truncated'] is True
    assert len(r['timeline']) == 2 and r['timeline'][0]['outcome'] == 'failed'
    assert r['artefacts_available'] == ['envelope', 'ledger_row']
    assert r['artefacts_missing'] == ['gateway_session']
    assert 'no gateway_session_id' in (r['session_error'] or '')


def test_the_flow_reports_a_session_it_cannot_find(tmp_path):
    p = tmp_path / 'outcomes.jsonl'
    p.write_text(json.dumps({'source_system': 'router-proxy', 'session_id': 'other',
                             'provider': 'a', 'model': 'b', 'ts': 1.0}) + '\n')
    import router_ui_page as up
    r = up.flow({'id': 'missing'}, str(p))
    assert r['found'] is False and 'no ledger row' in r['note']


def test_the_flow_requires_an_id(tmp_path):
    import router_ui_page as up
    r = up.flow({}, str(tmp_path / 'x.jsonl'))
    assert 'id=<session_id> is required' in r['error']


def test_the_flow_uses_an_injected_session_fetcher(tmp_path):
    p = tmp_path / 'outcomes.jsonl'
    p.write_text(json.dumps({'source_system': 'router-proxy', 'session_id': 's-9',
                             'provider': 'a', 'model': 'b', 'ts': 1.0,
                             'gateway_session_id': 'gw-1', 'success': True}) + '\n')
    import router_ui_page as up
    r = up.flow({'id': 's-9'}, str(p), session_fetch=lambda gsid: {'id': gsid, 'messages': 12})
    assert r['artefacts_read']['gateway_session'] is True
    assert r['session'] == {'id': 'gw-1', 'messages': 12}


def test_the_chain_view_summarises_exclusions_by_code():
    resolved = {
        'project': 'p', 'profile': 'P1_CODING', 'sort': 'price', 'head': {'provider': 'x', 'model': 'y'},
        'chain': [{'hop': 1, 'provider': 'xkiro', 'model': 'openai/gpt-6-luna', 'usd_1m': 0.116,
                   'context_limit': 1000}],
        'exclusions': [
            {'hop': 2, 'provider': 'a', 'model': 'b', 'codes': ['model-down'], 'why': ['DOWN']},
            {'hop': 3, 'provider': 'c', 'model': 'd', 'codes': ['quota-gated'], 'why': ['gated']},
            {'hop': 4, 'provider': 'e', 'model': 'f', 'codes': ['quota-gated'], 'why': ['gated']},
        ],
    }
    import router_ui_page as up
    v = up.chain_view({}, resolved, price_map={('xkiro', 'openai/gpt-6-luna'): (0.1, 0.5, 0.003867, 0.116)})
    assert v['chain_length'] == 1 and v['excluded_total'] == 3
    assert v['exclusion_summary'][0] == {'code': 'quota-gated', 'lanes': 2}
    assert {s['code'] for s in v['exclusion_summary']} == {'quota-gated', 'model-down'}
    # the AC asks for each lane's PRICE BASIS: plan offset vs list
    assert 'plan-effective 0.003867' in v['chain'][0]['price_basis']
    assert 'list 0.116' in v['chain'][0]['price_basis']
    assert '3 excluded' in v['note'] and '2 code(s)' in v['note']


def test_the_chain_view_says_when_it_is_not_showing_every_exclusion():
    resolved = {'chain': [], 'exclusions': [
        {'hop': i, 'provider': 'p', 'model': 'm' + str(i), 'codes': ['x'], 'why': ['w']} for i in range(80)]}
    import router_ui_page as up
    v = up.chain_view({}, resolved, price_map={}, detail_limit=60)
    assert v['excluded_total'] == 80 and v['excluded_shown'] == 60
    assert v['exclusions_truncated'] is True
    assert 'showing the first 60' in v['note']


def test_a_lane_with_no_declared_price_says_so_rather_than_showing_zero():
    resolved = {'chain': [{'hop': 1, 'provider': 'unknown-prov', 'model': 'm'}], 'exclusions': []}
    import router_ui_page as up
    v = up.chain_view({}, resolved, price_map={})
    assert v['chain'][0]['normalized_price'] is None
    assert v['chain'][0]['price_basis'] == 'no declared price for this lane'


def _board(tmp_path, rows):
    p = tmp_path / 'tasks.jsonl'
    p.write_text(''.join(json.dumps(r) + '\n' for r in rows))
    return str(p)


def test_the_board_census_surfaces_duplicate_ids(tmp_path):
    rows = [{'id': 'TR-1', 'title': 'a', 'status': 'complete'},
            {'id': 'TR-2', 'title': 'b', 'status': 'pending'},
            {'id': 'TR-2', 'title': 'b again', 'status': 'pending'},
            {'id': 'TR-10', 'title': 'c', 'status': 'complete'}]
    import router_ui_page as up
    d = up.board_search({'limit': '10'}, _board(tmp_path, rows))
    assert d['census']['rows'] == 4
    assert d['census']['duplicate_ids'] == [{'id': 'TR-2', 'count': 2}]
    assert d['census']['max_id'] == 'TR-10'
    assert 'DUPLICATE' in d['note']


def test_a_missing_artifact_is_flagged_not_linked(tmp_path):
    repo = tmp_path / 'repo'
    (repo / 'scripts').mkdir(parents=True)
    (repo / 'scripts' / 'there.py').write_text('ok')
    rows = [{'id': 'TR-1', 'title': 'x', 'status': 'complete',
             'files_changed': ['scripts/there.py', 'scripts/gone.py']}]
    import router_ui_page as up
    d = up.board_search({'limit': '5'}, _board(tmp_path, rows), repo_root=str(repo))
    arts = {a['path']: a for a in d['rows'][0]['artifacts']}
    assert arts['scripts/there.py']['exists'] is True
    assert arts['scripts/gone.py']['exists'] is False
    assert 'not found' in arts['scripts/gone.py']['note']
    assert d['artifacts_missing'] == 1


def test_the_board_filters_by_id_status_priority_and_commit(tmp_path):
    rows = [{'id': 'TR-1', 'title': 'a', 'status': 'complete', 'priority': 'P1',
             'commit_hash': 'abc1234'},
            {'id': 'TR-2', 'title': 'b', 'status': 'pending', 'priority': 'P2'},
            {'id': 'TR-15', 'title': 'c', 'status': 'pending', 'priority': 'P1'}]
    import router_ui_page as up
    p = _board(tmp_path, rows)
    assert up.board_search({'id': 'TR-1'}, p)['total_matched'] == 2
    assert up.board_search({'status': 'pending'}, p)['total_matched'] == 2
    assert up.board_search({'priority': 'P1'}, p)['total_matched'] == 2
    assert up.board_search({'commit': 'abc12'}, p)['total_matched'] == 1


def test_the_board_names_the_filter_it_cannot_offer(tmp_path):
    import router_ui_page as up
    d = up.board_search({}, _board(tmp_path, [{'id': 'TR-1', 'title': 'a', 'status': 'x'}]))
    assert 'owner (the board carries no owner field)' in d['filters_absent']
    assert 'status' in d['filters_available']


def test_a_text_blob_in_files_changed_is_not_walked_character_by_character(tmp_path):
    """The false-alarm guard: iterating a string produced single-character 'paths'."""
    rows = [{'id': 'TR-1', 'title': 'x', 'status': 'complete',
             'files_changed': 'scripts/foo.py tests/test_foo.py'}]
    import router_ui_page as up
    d = up.board_search({'limit': '5'}, _board(tmp_path, rows))
    r = d['rows'][0]
    assert 'artifacts' not in r, 'a text blob must not be treated as a list of paths'
    assert r['files_changed_note'].startswith('stored as text')
    assert d['artifacts_missing'] == 0 and d['artifacts_checked'] == 0


def test_a_json_list_stored_as_text_is_parsed(tmp_path):
    rows = [{'id': 'TR-1', 'title': 'x', 'status': 'complete',
             'files_changed': '["scripts/there.py"]'}]
    repo = tmp_path / 'repo'
    (repo / 'scripts').mkdir(parents=True)
    (repo / 'scripts' / 'there.py').write_text('ok')
    import router_ui_page as up
    d = up.board_search({'limit': '5'}, _board(tmp_path, rows), repo_root=str(repo))
    assert d['rows'][0]['artifacts'][0]['exists'] is True


def test_the_row_records_skipped_positions_with_their_gate_codes():
    """TR-158: the resolver never emitted `skipped_hops`, so the row always stored null. The skip
    evidence is in `exclusions` (hop position + machine-readable code)."""
    import router_server as rs
    resolved = {
        'chain': [{'hop': 1, 'provider': 'a', 'model': 'b'}],
        'exclusions': [
            {'hop': 1, 'provider': 'xkiro', 'model': 'minimax/minimax-m3:free',
             'codes': ['model-down'], 'why': ['model DOWN (2026-09-26T08:01:00+00:00)']},
            {'hop': 2, 'provider': 'kimi-for-coding', 'model': 'kimi-k2.7-code',
             'codes': ['health-down', 'model-down'], 'why': ['health DOWN', 'model DOWN']},
        ],
    }
    ce = rs._chain_evidence(resolved, resolved['chain'])
    assert isinstance(ce['skipped_hops'], list) and len(ce['skipped_hops']) == 2
    assert ce['skipped_hops'][0]['hop'] == 1 and ce['skipped_hops'][0]['codes'] == ['model-down']


def test_a_run_with_no_skips_records_an_empty_list_not_null():
    import router_server as rs
    ce = rs._chain_evidence({'chain': [], 'exclusions': []}, [])
    assert ce['skipped_hops'] == [], 'AC: no skips is an EMPTY LIST, never null-with-no-reason'


def test_the_flow_explains_a_served_position_beyond_the_attempt_count(tmp_path):
    """The contradiction TR-158 was filed from: 'served_by_hop=28 with 1 attempt'."""
    p = tmp_path / 'outcomes.jsonl'
    row = {'source_system': 'router-proxy', 'session_id': 's-1', 'provider': 'xkiro',
           'model': 'openai/gpt-6-luna', 'ts': 1.0, 'success': True, 'cost_usd': 0.01,
           'hops_attempted': 1, 'max_hops': 3, 'served_by_hop': 28,
           'chain_evidence': {'chain_length': 68, 'exclusions': [
               {'hop': 1, 'provider': 'xkiro', 'model': 'minimax/minimax-m3:free',
                'codes': ['model-down'], 'why': ['DOWN']}]}}
    p.write_text(json.dumps(row) + '\n')
    import router_ui_page as up
    r = up.flow({'id': 's-1'}, str(p))
    assert len(r['skipped_hops']) == 1
    assert r['hops']['served_position'] == 28
    why = r['skipped_explanation']
    assert 'position 28' in why and '1 attempt' in why
    assert 'SKIPPED BY GATES' in why and 'model-down' in why
    assert 'not a failed attempt' in why


def test_the_flow_says_so_plainly_when_nothing_was_skipped(tmp_path):
    p = tmp_path / 'outcomes.jsonl'
    row = {'source_system': 'router-proxy', 'session_id': 's-2', 'provider': 'a', 'model': 'b',
           'ts': 1.0, 'success': True, 'hops_attempted': 1, 'served_by_hop': 1,
           'chain_evidence': {'chain_length': 5, 'exclusions': []}}
    p.write_text(json.dumps(row) + '\n')
    import router_ui_page as up
    r = up.flow({'id': 's-2'}, str(p))
    assert r['skipped_hops'] == []
    assert 'empty list, not missing data' in r['skipped_explanation']


def test_the_flow_reads_the_flattened_row_shape(tmp_path):
    """The REAL row shape: evidence is flattened into the row, not nested under chain_evidence.
    Measured on a live row: skipped_hops is a top-level list and chain_evidence does not exist."""
    p = tmp_path / 'outcomes.jsonl'
    row = {'source_system': 'router-proxy', 'session_id': 'router-proxy-1', 'provider': 'xkiro',
           'model': 'openai/gpt-6-luna', 'ts': 1790412512.0, 'success': True, 'cost_usd': 0.01,
           'served_by_hop': 2, 'hops_attempted': 1, 'max_hops': 3, 'steps': 1,
           'chain_length': 68, 'chain_truncated': False, 'gate': 'OPEN',
           'exclusions': [{'hop': 1, 'provider': 'xkiro', 'model': 'minimax/minimax-m3:free',
                           'codes': ['model-down'], 'why': ['model DOWN (2026-09-26T08:01:00+00:00)']}],
           'skipped_hops': [{'hop': 1, 'provider': 'xkiro', 'model': 'minimax/minimax-m3:free',
                             'codes': ['model-down'], 'why': ['model DOWN (2026-09-26T08:01:00+00:00)']}]}
    p.write_text(json.dumps(row) + '\n')
    import router_ui_page as up
    r = up.flow({'id': 'router-proxy-1'}, str(p))
    assert r['chain']['considered'] == 68 and r['chain']['gate'] == 'OPEN'
    assert len(r['skipped_hops']) == 1 and r['skipped_hops'][0]['codes'] == ['model-down']
    assert r['hops']['served_position'] == 2 and r['hops']['attempted'] == 1
    assert 'SKIPPED BY GATES' in r['skipped_explanation'] and 'model-down' in r['skipped_explanation']


def test_an_older_row_without_the_field_is_still_explained(tmp_path):
    """Flatten form, written before skipped_hops existed: derive it from the exclusions present."""
    p = tmp_path / 'outcomes.jsonl'
    row = {'source_system': 'router-proxy', 'session_id': 'old-1', 'provider': 'a', 'model': 'b',
           'ts': 1.0, 'success': True, 'served_by_hop': 3, 'hops_attempted': 1,
           'exclusions': [{'hop': 1, 'provider': 'p', 'model': 'm', 'codes': ['quota-gated'],
                           'why': ['gated']}]}
    p.write_text(json.dumps(row) + '\n')
    import router_ui_page as up
    r = up.flow({'id': 'old-1'}, str(p))
    assert len(r['skipped_hops']) == 1
    assert 'quota-gated' in r['skipped_explanation']
