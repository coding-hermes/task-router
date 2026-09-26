"""TR-155: the registry reference, and the guarded edit path's guarantees."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts'))
import router_ui_page as ui  # noqa: E402

TABLES = {
    'models': [
        {'provider': 'xkiro', 'model': 'openai/gpt-6-luna', 'public_price': 0.116,
         'normalized_price': 0.003867, 'public_in_per_m': 0.1, 'public_out_per_m': 0.5,
         'plan_tier': 0, 'context_limit': 1050000, 'valid_from': '2026-09-18',
         'price_evidence': 'xkiro $200 plan'},
        {'provider': 'custom', 'model': 'unknown-lane', 'public_price': None,
         'normalized_price': None},
    ],
    'model_tier': [
        {'model': 'openai/gpt-6-luna', 'category': 'code_gen', 'tier': 4, 'perf': 0.85,
         'tier_source': 'family'},
    ],
    'category_levels': [
        {'category': 'code_gen', 'level': -5}, {'category': 'code_gen', 'level': 5},
        {'category': 'debug', 'level': 0}, {'category': 'debug', 'level': 4},
    ],
    'quality_estimates': [{'model': 'openai/gpt-6-luna', 'guard': 0.9}],
    'providers': [{'id': 'xkiro', 'plan': '$200 coding plan', 'data_class': 'zdr'}],
}
KEY = 'SECRET'


def _edit(tmp_path, body, key=KEY, expected=KEY):
    return ui.registry_edit(body, key, expected, TABLES,
                            str(tmp_path / 'overlay.jsonl'), str(tmp_path / 'audit.jsonl'),
                            now_s=1.0)


def test_the_lane_detail_is_a_join_that_names_its_files():
    b = ui.registry_browse({'q': 'gpt-6-luna'}, TABLES)
    assert b['matched'] == 1
    lane = b['lanes'][0]
    assert lane['levels'][0]['category'] == 'code_gen' and lane['levels'][0]['tier'] == 4
    assert lane['level_source_file'] == 'data/tables/model_tier.jsonl'
    assert lane['provider_plan'] == '$200 coding plan'
    assert lane['provider_source_file'] == 'data/tables/providers.jsonl'
    assert lane['public_price'] == 0.116 and lane['normalized_price'] == 0.003867


def test_an_unknown_category_is_refused_rather_than_silently_empty():
    b = ui.registry_browse({'category': 'not_a_category'}, TABLES)
    assert 'error' in b and 'unknown category' in b['error']


def test_the_browser_filters_by_category_and_level():
    assert ui.registry_browse({'category': 'code_gen', 'min_level': 4}, TABLES)['matched'] == 1
    assert ui.registry_browse({'category': 'code_gen', 'min_level': 5}, TABLES)['matched'] == 0


def test_a_write_without_the_key_is_refused_and_audited(tmp_path):
    status, payload, _ = _edit(tmp_path, {'provider': 'xkiro', 'model': 'openai/gpt-6-luna',
                                          'public_price': 0.12}, key='wrong')
    assert status == 403
    audit = [json.loads(l) for l in open(tmp_path / 'audit.jsonl') if l.strip()]
    assert audit and audit[-1]['outcome'] == 'refused'
    assert not os.path.exists(tmp_path / 'overlay.jsonl')


def test_a_server_with_no_edit_key_refuses_everything(tmp_path):
    status, payload, check = _edit(tmp_path, {'provider': 'xkiro', 'model': 'openai/gpt-6-luna'},
                                   key=KEY, expected='')
    assert status == 403 and 'read-only' in payload['error']


def test_validation_refuses_nonsense_rather_than_clamping(tmp_path):
    base = {'provider': 'xkiro', 'model': 'openai/gpt-6-luna', 'lifecycle_source': 'x'}
    for extra, why in (
            ({'category': 'nope', 'level': 2}, 'unknown category'),
            ({'category': 'code_gen', 'level': 99}, 'outside the ladder'),
            ({'public_price': 12.0}, 'order of magnitude'),
            ({'valid_to': 'not-a-date'}, 'ISO date'),
            ({}, 'provenance') if False else ({'public_price': 0.12}, None)):
        body = dict(base, **extra)
        status, payload, _ = _edit(tmp_path, body)
        if why is None:
            continue
        assert status == 422, why
    # and a missing provenance is refused too
    status, payload, _ = _edit(tmp_path, {'provider': 'xkiro', 'model': 'openai/gpt-6-luna',
                                          'public_price': 0.12})
    assert status == 422
    assert any('provenance' in p for p in payload['problems']), payload['problems']


def test_a_valid_edit_appends_to_the_overlay_and_never_a_generated_table(tmp_path):
    status, payload, _ = _edit(tmp_path, {'provider': 'xkiro', 'model': 'openai/gpt-6-luna',
                                          'public_price': 0.13,
                                          'lifecycle_source': 'Bane 2026-09-26'})
    assert status == 200 and payload['overlay_appended'] is True
    assert payload['generated_tables_touched'] is False
    overlay = [json.loads(l) for l in open(tmp_path / 'overlay.jsonl') if l.strip()]
    assert overlay == [{'provider': 'xkiro', 'model': 'openai/gpt-6-luna',
                        'public_price': 0.13, 'lifecycle_source': 'Bane 2026-09-26'}]
    audit = [json.loads(l) for l in open(tmp_path / 'audit.jsonl') if l.strip()]
    assert audit[-1]['outcome'] == 'accepted'
    assert audit[-1]['before']['public_price'] == 0.116 and audit[-1]['after']['public_price'] == 0.13


def test_an_edit_cannot_silently_move_a_plan_lanes_reported_cost(tmp_path):
    """public vs normalized stay DISTINCT: setting one never rewrites the other."""
    status, payload, _ = _edit(tmp_path, {'provider': 'xkiro', 'model': 'openai/gpt-6-luna',
                                          'normalized_price': 0.004,
                                          'lifecycle_source': 'Bane 2026-09-26'})
    assert status == 200
    overlay = [json.loads(l) for l in open(tmp_path / 'overlay.jsonl') if l.strip()]
    assert 'normalized_price' in overlay[-1] and 'public_price' not in overlay[-1]
    assert overlay[-1]['normalized_price'] == 0.004


def test_a_ledger_has_an_advertised_revert(tmp_path):
    status, payload, _ = _edit(tmp_path, {'provider': 'xkiro', 'model': 'openai/gpt-6-luna',
                                          'public_price': 0.13, 'lifecycle_source': 'x'})
    assert status == 200 and payload['revert']['revert_of'] is not None
    status2, payload2, _ = _edit(tmp_path, dict(payload['revert'], public_price=0.116))
    assert status2 == 200, payload2
    overlay = [json.loads(l) for l in open(tmp_path / 'overlay.jsonl') if l.strip()]
    assert overlay[-1].get('revert_of') is not None
