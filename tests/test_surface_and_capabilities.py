"""TR-140: an unknown path must read as a typo, and the surface must be discoverable.

Measured 2026-09-25: `POST :9391/v1/messages` (an Anthropic-shaped client) answered
"read-only mode" — a 403 that answers a question nobody asked. The caller had a
wrong path, not a permission problem, and the error hid both the real problem and
the surface that would have told them what to call instead.

Second half: /v1/capabilities exists on the gateway (the proxy even probes it at
startup and shows it on "/") but was NOT served through the proxy, so a caller
could not discover the upstream surface without guessing.
"""
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'scripts'))
import router_server as rsrv   # noqa: E402


@pytest.fixture()
def ro_server():
    """A read-only control plane — the mode that produced the wrong 403."""
    return rsrv.RouterApplication(mode='read-only', edit_key='k')


# ---------- the 404-not-403 rule ----------

def test_an_unknown_post_path_is_a_404_that_names_the_surface(ro_server):
    status, payload = ro_server.dispatch('POST', '/v1/messages', body={}, headers={})
    assert status == 404, 'a typo is not a permission problem'
    assert payload['path'] == '/v1/messages'
    assert payload['hint']
    assert '/v1/chat/completions' in payload['surface']
    assert '/v1/responses' in payload['surface']


def test_a_KNOWN_mutation_still_gets_the_read_only_403(ro_server):
    """The auth contract is preserved: read-only means read-only."""
    status, payload = ro_server.dispatch('POST', '/circuit/record',
                                         body={'provider': 'p', 'model': 'm', 'outcome': 'success'},
                                         headers={})
    assert status == 403 and payload['error'] == 'read-only mode'


def test_the_surface_lists_the_real_endpoints():
    paths = rsrv.known_post_paths()
    for expected in ('/v1/chat/completions', '/v1/responses'):
        assert expected in paths
    assert any(p.startswith('/listings') for p in paths), 'the dynamic paths count'
    assert len(paths) >= 5


def test_dynamic_listing_paths_are_known():
    assert rsrv._is_known_post_path('/listings/provider')
    assert rsrv._is_known_post_path('/listings/model')
    assert not rsrv._is_known_post_path('/listings/')
    assert not rsrv._is_known_post_path('/definitely-not-a-path')


def test_the_helper_fails_OPEN(monkeypatch):
    """A broken contract must never turn a real endpoint into a 404."""
    monkeypatch.setattr(rsrv, 'build_openapi', lambda: (_ for _ in ()).throw(RuntimeError('boom')))
    assert rsrv._is_known_post_path('/anything') is True


# ---------- /v1/capabilities ----------

def test_capabilities_is_served_from_a_LIVE_probe(ro_server, monkeypatch):
    monkeypatch.setattr(rsrv, '_hermes_capabilities_metadata',
                        lambda base, _opener=None: {'object': 'hermes.api_server.capabilities',
                                                    'is_hermes_gateway': True,
                                                    'session_key_header': 'X-Hermes-Session-Key'})
    status, payload = ro_server.dispatch('GET', '/v1/capabilities')
    assert status == 200
    assert payload['source'] == 'live'
    assert payload['upstream']['is_hermes_gateway'] is True
    assert 'stale' not in payload


def test_an_unreachable_upstream_falls_back_to_the_startup_probe_marked_STALE(ro_server, monkeypatch):
    ro_server.hermes_capabilities = {'object': 'hermes.api_server.capabilities', 'responses_api': True}
    monkeypatch.setattr(rsrv, '_hermes_capabilities_metadata',
                        lambda base, _opener=None: {'error': 'capabilities probe failed: refused'})
    status, payload = ro_server.dispatch('GET', '/v1/capabilities')
    assert status == 200
    assert payload['source'] == 'startup-probe' and payload['stale'] is True
    assert 'refused' in payload['live_error']


def test_no_capability_information_yields_an_honest_error_not_an_invention(ro_server, monkeypatch):
    ro_server.hermes_capabilities = {}
    monkeypatch.setattr(rsrv, '_hermes_capabilities_metadata',
                        lambda base, _opener=None: {'error': 'probe failed'})
    status, payload = ro_server.dispatch('GET', '/v1/capabilities')
    assert status == 200
    assert payload['upstream'] is None and payload['source'] == 'unavailable'
    assert payload['error']


def test_capabilities_is_in_the_published_contract():
    assert '/v1/capabilities' in rsrv.build_openapi()['paths']
