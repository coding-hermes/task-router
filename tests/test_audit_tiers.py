"""TR-064 audit-tiers tests: coverage, provenance, evidence-backed vs
default-driven classification, exit codes. Synthetic registries only."""
import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts'))
import router_audit as ra

SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts', 'router_audit.py')


def _tables():
    """3 profiles, 3 lanes: one fully evidenced, one partial (default-driven),
    one with zero tiers. Plus a tier row that carries no provenance."""
    return {
        'models': [
            {'provider': 'p', 'model': 'full', 'normalized_price': 1.0,
             'valid_to': None, 'archive': False, 'disabled': None},
            {'provider': 'p', 'model': 'partial', 'normalized_price': 1.0,
             'valid_to': None, 'archive': False, 'disabled': None},
            {'provider': 'p', 'model': 'silent', 'normalized_price': 1.0,
             'valid_to': None, 'archive': False, 'disabled': None},
            {'provider': 'p', 'model': 'unpriced', 'normalized_price': None,
             'public_price': None, 'valid_to': None, 'archive': False, 'disabled': None},
            {'provider': 'p', 'model': 'off', 'normalized_price': 1.0,
             'valid_to': None, 'archive': False, 'disabled': True},
        ],
        'task_profiles': [{'id': 'P_STRICT'}, {'id': 'P_LENIENT'}],
        'task_profile_requirements': [
            {'task_id': 'P_STRICT', 'category': 'code_gen', 'level': 2},
            {'task_id': 'P_STRICT', 'category': 'test', 'level': 1},
            {'task_id': 'P_LENIENT', 'category': 'code_gen', 'level': -2},
        ],
        'model_tier': [
            {'model': 'full', 'category': 'code_gen', 'perf': 0.9, 'tier': 4,
             'tier_source': 'measured', 'source_ref': 'battery-run-1'},
            {'model': 'full', 'category': 'test', 'perf': 0.8, 'tier': 3,
             'tier_source': 'measured', 'source_ref': 'battery-run-1'},
            {'model': 'partial', 'category': 'code_gen', 'perf': 0.7, 'tier': 3,
             'tier_source': 'family', 'source_ref': 'alias:full'},
            {'model': 'stale', 'category': 'code_gen', 'perf': 0.5, 'tier': 2,
             'tier_source': None, 'source_ref': None},
        ],
    }


def test_universe_is_data_driven_from_profile_requirements():
    t = _tables()
    assert ra.category_universe(t) == ['code_gen', 'test']  # no hardcoded list


def test_evidenced_vs_default_driven():
    rep = ra.audit_tiers(_tables())
    by_model = {r['model']: r for r in rep['lanes_detail']}
    # full: both requirements have real tier rows -> evidenced
    assert by_model['full']['eligible_profiles'] == 2
    assert by_model['full']['eligible_profiles_evidenced'] == 2
    # partial: passes P_LENIENT and P_STRICT? code_gen 3>=2 ok, test missing -> -1 < 1
    # so P_STRICT fails; P_LENIENT passes on a REAL row (code_gen) -> evidenced
    assert by_model['partial']['eligible_profiles'] == 1
    assert by_model['partial']['eligible_profiles_evidenced'] == 1
    # silent: no tiers at all -> P_LENIENT passes only via the -1 default
    assert by_model['silent']['eligible_profiles'] == 1
    assert by_model['silent']['eligible_profiles_evidenced'] == 0
    assert by_model['silent']['covered'] == 0


def test_default_driven_counted_separately():
    rep = ra.audit_tiers(_tables())
    assert rep['routeable'] == 3                    # full, partial, silent
    assert rep['routeable_evidenced'] == 2          # full, partial
    assert rep['routeable_default_driven'] == 1     # silent
    assert 'p/silent' in rep['invisible_lanes']


def test_inactive_and_unpriced_lanes_excluded():
    rep = ra.audit_tiers(_tables())
    models = {r['model'] for r in rep['lanes_detail']}
    assert 'unpriced' not in models  # no price -> never selectable
    assert 'off' not in models       # disabled lane


def test_unprovenanced_tier_row_is_flagged():
    rep = ra.audit_tiers(_tables())
    assert rep['unprovenanced_tier_rows'] == 1


def test_provider_filter():
    rep = ra.audit_tiers(_tables(), provider='nope')
    assert rep['lanes'] == 0
    rep2 = ra.audit_tiers(_tables(), provider='p')
    assert rep2['lanes'] == 3


def test_cli_exit_codes_and_json(tmp_path):
    reg = tmp_path / 'registry.json'
    reg.write_text(json.dumps({'tables': _tables()}))
    env = {**os.environ, 'ROUTING_REGISTRY': str(reg)}

    r = subprocess.run([sys.executable, SCRIPT, 'audit-tiers'], env=env,
                       capture_output=True, text=True)
    assert r.returncode == 2, r.stdout  # invisible lanes + unprovenanced row
    assert 'INVISIBLE' in r.stdout and 'EVIDENCE-BACKED' in r.stdout

    r2 = subprocess.run([sys.executable, SCRIPT, 'audit-tiers', '--no-fail'], env=env,
                        capture_output=True, text=True)
    assert r2.returncode == 0

    r3 = subprocess.run([sys.executable, SCRIPT, 'audit-tiers', '--json'], env=env,
                        capture_output=True, text=True)
    out = json.loads(r3.stdout)
    assert out['lanes'] == 3 and out['routeable_evidenced'] == 2
    assert 'lanes_detail' not in out  # json stays bounded

    r4 = subprocess.run([sys.executable, SCRIPT, 'audit-tiers', '--explain', 'silent'],
                        env=env, capture_output=True, text=True)
    assert r4.returncode == 0 and '"missing"' in r4.stdout
