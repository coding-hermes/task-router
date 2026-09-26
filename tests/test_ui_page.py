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
