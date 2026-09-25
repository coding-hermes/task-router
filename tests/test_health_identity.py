"""TR-141: /health must identify the code the PROCESS loaded, and say when it is stale.

Measured 2026-09-25: the running proxy started 2026-09-23 19:29 while
scripts/router_server.py last changed 2026-09-24 20:27 — 25 hours of stale code
serving live calls — and /health reported the REPO's HEAD, so it looked current.
Deploy parity was invisible. These tests pin the fix: the identity reported is the
LOADED one, a mismatch is flagged `stale: true`, and an uncomputable half yields
None plus a reason instead of a fabricated match.
"""
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'scripts'))
import router_health as rh   # noqa: E402


def test_health_reports_the_identity_block_and_a_stale_verdict():
    payload = rh.health(mode='read-only')
    assert payload['commit'] and payload['commit'] != 'unknown'
    code = payload['code']
    for key in ('loaded_commit', 'loaded_source_sha', 'loaded_at',
                'repo_commit', 'live_source_sha', 'stale'):
        assert key in code, key
    assert payload['stale'] == code['stale']


def test_the_reported_commit_is_the_LOADED_one_not_the_repo_head(monkeypatch):
    """The defect was reporting HEAD: a stale process then claims the new code."""
    monkeypatch.setattr(rh, '_LOADED_COMMIT', 'aaaabbbbcccc')
    monkeypatch.setattr(rh, 'git_commit', lambda: 'dddd11112222')
    payload = rh.health(mode='read-only')
    assert payload['commit'] == 'aaaabbbbcccc', 'must report what is RUNNING'
    assert payload['code']['repo_commit'] == 'dddd11112222'
    assert payload['stale'] is True


def test_a_source_change_since_boot_is_stale(monkeypatch):
    monkeypatch.setattr(rh, '_LOADED_SOURCE_SHA', 'deadbeefdeadbeef')
    payload = rh.health(mode='read-only')
    assert payload['stale'] is True, 'files changed on disk since this process loaded -> deploy needed'


def test_a_moved_head_is_stale_even_when_the_files_match(monkeypatch):
    monkeypatch.setattr(rh, 'git_commit', lambda: 'ffff00001111')
    monkeypatch.setattr(rh, '_LOADED_COMMIT', '0000ffff1111')
    assert rh.health(mode='read-only')['stale'] is True


def test_matching_identity_is_not_stale():
    """The normal, deployed state must read clean (no permanent false alarm)."""
    assert rh.health(mode='read-only')['stale'] is False


def test_an_uncomputable_hash_is_None_with_a_reason_never_a_fake_match(monkeypatch):
    monkeypatch.setattr(rh, 'source_sha', lambda: None)
    code = rh.health(mode='read-only')['code']
    assert code['stale'] is None, 'no comparison possible -> no verdict'
    assert code['error'], 'the reason must be visible'


def test_an_unknown_boot_commit_is_None_with_a_reason(monkeypatch):
    monkeypatch.setattr(rh, '_LOADED_COMMIT', 'unknown')
    code = rh.health(mode='read-only')['code']
    assert code['stale'] is None and 'commit' in code['error']


def test_the_identity_covers_the_server_module_too():
    """A change to router_server.py alone must be detected: that is the file that
    was 25h ahead of the running process."""
    assert 'router_server.py' in rh.IDENTITY_FILES
    assert 'router_health.py' in rh.IDENTITY_FILES


def test_health_stays_200_even_when_the_identity_block_explodes(monkeypatch):
    """Fail-open: /health exists to REPORT breakage, so it must not break with it."""
    def boom():
        raise RuntimeError('simulated identity failure')
    monkeypatch.setattr(rh, 'code_identity', boom)
    payload = rh.health(mode='read-only')     # must not raise
    assert payload['status'] == 'ok'
    assert payload['code']['stale'] is None
    assert 'identity block failed' in payload['code']['error']


def test_source_sha_is_stable_across_calls():
    """A volatile hash would make every process look stale."""
    assert rh.source_sha() == rh.source_sha()
