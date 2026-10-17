"""TR-258: the models browser + provider rollups.

The acceptance contract, pinned:
  * the endpoint shape — every registry lane, joined, as a served route;
  * every missing value carries a STATED reason, never null/blank-as-content;
  * latency cells name their SOURCE LEVEL (model vs provider) so a provider
    number can never pose as a per-model measurement;
  * provider rollups RECONCILE with the per-lane sums — proven, not asserted;
  * the page renders the panel and its filters (same self-contained contract
    as every other panel: local data only, no provider request at render).
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts'))
import router_models_browser as mb  # noqa: E402
import router_server as rs  # noqa: E402
import router_ui_page as ui  # noqa: E402

REGISTRY = [
    # up (model-level probe), priced, sampled
    {'provider': 'p-up', 'model': 'm-probed', 'public_price': 0.5,
     'context_limit': 200000},
    # provider-level fallback: no per-model probe row
    {'provider': 'p-up', 'model': 'm-provider-level', 'public_price': None},
    # down at the model level
    {'provider': 'p-down', 'model': 'm-down', 'public_price': 2.0},
    # never probed at all -> health unknown
    {'provider': 'p-ghost', 'model': 'm-ghost', 'public_price': None},
]
HEALTH = {'updated': '2026-10-17T01:00:54+00:00', 'providers': {
    'p-up': {'status': 'OK', 'latency_ms': 100,
             'models': {'m-probed': {'status': 'OK', 'latency_ms': 300,
                                     'ts': '2026-10-17T01:00:54+00:00'}}},
    'p-down': {'status': 'OK', 'latency_ms': 100,
               'models': {'m-down': {'status': 'DOWN'}}},
    # EXCLUDED is neither up nor down: it must read unknown WITH the reason,
    # never as a fabricated gate
    'p-excluded': {'status': 'EXCLUDED', 'models': {}},
}}
AVERAGES = [
    # two buckets for the same lane (per backend x signature): weighted join
    {'provider': 'p-up', 'model': 'm-probed', 'n_samples': 3,
     'avg_tokens_total_168h': 100.0, 'avg_wall_time_168h': 1.5},
    {'provider': 'p-up', 'model': 'm-probed', 'n_samples': 1,
     'avg_tokens_total_168h': 400.0, 'avg_wall_time_168h': 2.0},
    # a bucket that never recorded tokens: samples count, tokens stay missing
    {'provider': 'p-down', 'model': 'm-down', 'n_samples': 4,
     'avg_tokens_total_168h': None, 'avg_wall_time_168h': 0.5},
]


def _view():
    return mb.models_view(REGISTRY, HEALTH, AVERAGES)


def _lane(view, provider, model):
    return next(l for l in view['lanes']
                if l['provider'] == provider and l['model'] == model)


# ---------- the endpoint is a served route with the accepted shape ----------

def test_both_routes_are_served_and_in_the_contract():
    assert '/api/models' in (rs.OPENAPI.get('paths') or [])
    assert '/api/provider-rollups' in (rs.OPENAPI.get('paths') or [])
    app = rs.RouterApplication('read-only', '')
    status, payload = app.dispatch('GET', '/api/models')
    assert status == 200
    assert {'ts', 'counts', 'sources', 'providers', 'lanes'} <= set(payload)
    status, payload = app.dispatch('GET', '/api/provider-rollups')
    assert status == 200
    assert {'providers', 'totals'} <= set(payload)


def test_every_registry_lane_is_present_with_the_accepted_columns():
    v = _view()
    assert v['counts']['lanes'] == len(REGISTRY)
    lane = _lane(v, 'p-up', 'm-probed')
    for key in ('provider', 'model', 'status', 'latency_ms', 'tokens_168h',
                'sample_count', 'avg_wall_ms', 'public_price_usd_1m'):
        assert key in lane, key
    assert lane['latency_ms'] == 300 and lane['latency_level'] == 'model'
    # sample-count-weighted join across the lane's complexity buckets: (3*100+1*400)/4
    assert lane['tokens_168h'] == 175.0
    assert lane['sample_count'] == 4
    # wall time: only the 3 buckets that carry it, weighted (3*1500+1*2000)/4
    assert abs(lane['avg_wall_ms'] - 1625.0) < 1e-9
    assert lane['public_price_usd_1m'] == 0.5
    assert lane['bucket_rows_joined'] == 2
    # the served wrapper produces the same join over real loads (shape only)
    served = mb.served_models_view(REGISTRY, mr_dir_override='/nonexistent-mr')
    assert served['counts']['lanes'] == len(REGISTRY)
    assert all(l['status'] == 'unknown' for l in served['lanes']), \
        'a missing probe file renders health unknown, never a fabricated down'


def test_provider_dropdown_ids_are_offered_by_the_endpoint():
    v = _view()
    assert sorted(v['providers']) == ['p-down', 'p-ghost', 'p-up']


# ---------- missing values carry reasons, never blank/zero ------------------

def test_missing_values_carry_the_stated_reasons():
    v = _view()
    sampled = _lane(v, 'p-up', 'm-probed')
    assert sampled['tokens_reason'] is None and sampled['samples_reason'] is None
    unprobed = _lane(v, 'p-ghost', 'm-ghost')
    assert unprobed['status'] == 'unknown'
    assert unprobed['status_reason'] == mb.REASON_HEALTH_UNKNOWN
    assert unprobed['latency_ms'] is None
    assert unprobed['latency_reason'] == mb.REASON_NO_SAMPLE
    assert unprobed['tokens_168h'] is None
    assert unprobed['tokens_reason'] == mb.REASON_NO_SAMPLE
    assert unprobed['sample_count'] is None
    assert unprobed['samples_reason'] == mb.REASON_NO_SAMPLE
    assert unprobed['avg_wall_ms'] is None
    assert unprobed['wall_reason'] == mb.REASON_NO_SAMPLE
    assert unprobed['public_price_usd_1m'] is None
    assert unprobed['price_reason'] == mb.REASON_NOT_PRICED
    # a lane with samples but no recorded tokens: n stays, tokens say why
    down = _lane(v, 'p-down', 'm-down')
    assert down['sample_count'] == 4
    assert down['tokens_168h'] is None
    assert down['tokens_reason'] == mb.REASON_NO_SAMPLE
    # and every lane in the view satisfies the law: missing <=> reason present
    for l in v['lanes']:
        for val_key, reason_key in (('latency_ms', 'latency_reason'),
                                    ('tokens_168h', 'tokens_reason'),
                                    ('sample_count', 'samples_reason'),
                                    ('avg_wall_ms', 'wall_reason'),
                                    ('public_price_usd_1m', 'price_reason')):
            if l[val_key] is None:
                assert l[reason_key], (l['provider'], l['model'], val_key)
            else:
                assert l[reason_key] is None, (l['provider'], l['model'], val_key)


def test_probe_statuses_map_to_up_down_unknown_not_to_a_silent_gate():
    v = _view()
    assert _lane(v, 'p-up', 'm-probed')['status'] == 'up'
    assert _lane(v, 'p-up', 'm-provider-level')['status'] == 'up'
    assert _lane(v, 'p-down', 'm-down')['status'] == 'down'
    assert _lane(v, 'p-ghost', 'm-ghost')['status'] == 'unknown'
    assert v['counts'] == {'lanes': 4, 'up': 2, 'down': 1, 'unknown': 1}


def test_latency_cells_name_their_source_level():
    v = _view()
    model_level = _lane(v, 'p-up', 'm-probed')
    assert model_level['latency_level'] == 'model'
    assert model_level['latency_ms'] == 300, 'the per-model probe wins'
    provider_level = _lane(v, 'p-up', 'm-provider-level')
    assert provider_level['latency_ms'] == 100
    assert provider_level['latency_level'] == 'provider'
    # and the page renders the tag next to the number, for BOTH levels
    page = ui.page_html()
    assert "(provider)" in page and "(model)" in page
    # the route refuses to serve a bare number without a level
    for l in v['lanes']:
        if l['latency_ms'] is not None:
            assert l['latency_level'] in ('model', 'provider')


# ---------- rollups reconcile with the per-lane sums ------------------------

def test_provider_rollups_reconcile_with_per_lane_totals():
    v = _view()
    r = mb.provider_rollups(v['lanes'])
    t = r['totals']
    assert t['reconciles'] is True
    assert t['lanes'] == len(v['lanes'])
    assert t['up'] == v['counts']['up'] and t['down'] == v['counts']['down']
    assert t['unknown'] == v['counts']['unknown']
    lane_tokens = sum(l['tokens_168h'] for l in v['lanes']
                      if isinstance(l['tokens_168h'], (int, float)))
    assert t['tokens_168h'] == t['lanes_tokens_168h'] == lane_tokens
    assert t['sample_count'] == sum(l['sample_count'] for l in v['lanes']
                                    if isinstance(l['sample_count'], (int, float)))
    p_up = next(p for p in r['providers'] if p['provider'] == 'p-up')
    assert p_up['lanes_count'] == 2 and p_up['up_count'] == 2
    # the provider's mean latency spans BOTH of its measured lanes
    assert abs(p_up['avg_latency_ms'] - 200.0) < 1e-9
    assert p_up['latency_lanes'] == 2
    assert p_up['tokens_168h'] == 175.0 and p_up['sample_count'] == 4
    p_ghost = next(p for p in r['providers'] if p['provider'] == 'p-ghost')
    assert p_ghost['avg_latency_ms'] is None and p_ghost['tokens_168h'] == 0.0
    assert p_ghost['latency_lanes'] == 0


def test_rollups_of_the_served_surface_reconcile_against_the_served_lanes():
    """The rollup endpoint must never drift from the models endpoint: same
    loads, same lanes. Proven through the SERVED functions."""
    v = mb.served_models_view(REGISTRY, mr_dir_override='/nonexistent-mr')
    r = mb.served_provider_rollups(REGISTRY, mr_dir_override='/nonexistent-mr')
    assert r['totals']['reconciles'] is True
    assert r['totals']['lanes'] == v['counts']['lanes']
    assert sorted(p['provider'] for p in r['providers']) \
        == sorted(set(l['provider'] for l in v['lanes']))


# ---------- the page renders the panels --------------------------------------

def test_the_page_carries_the_models_panels_and_their_contract():
    page = ui.page_html()
    # panels exist
    assert 'id="models"' in page and 'id="provider_rollups"' in page
    # the accepted reason strings are rendered verbatim — the page takes the
    # reason from the endpoint's cell payload (single source of truth) and
    # shows it via the shared missing-cell renderer
    assert mb.REASON_NO_SAMPLE in page
    assert 'M_MISSING(r.price_reason)' in page \
        and 'M_MISSING(r.latency_reason' in page \
        and 'M_MISSING(r.tokens_reason)' in page \
        and 'M_MISSING(r.samples_reason)' in page \
        and 'M_MISSING(r.wall_reason)' in page
    # filters: provider dropdown + status up/down/all
    assert 'id="m_provider"' in page
    assert 'id="m_status"' in page and 'any health' in page
    assert "'up'" in page and "'down'" in page and "'unknown'" in page
    # sortable: headers carry the sort dataset + toggle handler
    assert 'data-mk' in page and 'sort_dir' in page
    # the panel fetches only same-origin local endpoints
    assert "j('/api/models')" in page and "j('/api/provider-rollups')" in page


def test_the_openapi_contract_lists_both_new_gets():
    assert rs.OPENAPI['paths']['/api/models']['get']['operationId'] == 'getApiModels'
    assert rs.OPENAPI['paths']['/api/provider-rollups']['get']['operationId'] \
        == 'getApiProviderRollups'
