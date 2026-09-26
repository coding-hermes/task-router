"""The plan-effective backfill is additive and idempotent - it never rewrites a cost."""
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, 'scripts', 'plan_effective_backfill.py')


def _run(store):
    return subprocess.run([sys.executable, SCRIPT, '--store', store, '--apply'],
                          capture_output=True, text=True, env={**os.environ},
                          cwd=REPO)


def test_it_prices_a_zero_row_and_leaves_a_priced_row_alone(tmp_path):
    store = tmp_path / 'outcomes.jsonl'
    rows = [
        # a driver zero on a lane the registry prices -> gets a real figure
        {'source_system': 'hermes', 'provider': 'ollama-cloud', 'model': 'glm-5.3-flash',
         'tokens_in': 1_000_000, 'tokens_out': 0, 'cost_usd': 0.0, 'session_id': 'a'},
        # a real cost -> untouched, whatever it is
        {'source_system': 'hermes', 'provider': 'ollama-cloud', 'model': 'glm-5.3-flash',
         'tokens_in': 1_000_000, 'tokens_out': 0, 'cost_usd': 9.99, 'session_id': 'b'},
        # a lane with no declared price -> stays null, with the reason recorded
        {'source_system': 'hermes', 'provider': 'custom', 'model': 'kimi-k3-fast',
         'tokens_in': 10, 'tokens_out': 1, 'cost_usd': 0.0, 'session_id': 'c'},
        # a proxy row is not this script's business
        {'source_system': 'router-proxy', 'provider': 'xkiro', 'model': 'openai/gpt-6-luna',
         'tokens_in': 5, 'tokens_out': 1, 'cost_usd': 0.0, 'session_id': 'd'},
    ]
    store.write_text(''.join(json.dumps(r) + '\n' for r in rows))
    r = _run(str(store))
    assert r.returncode == 0, r.stderr
    out = [json.loads(l) for l in store.read_text().splitlines() if l.strip()]
    assert out[0]['cost_usd'] > 0, 'a priced lane must stop reporting a driver zero'
    assert 'driver reported $0' in out[0]['price_basis']
    assert out[1]['cost_usd'] == 9.99, 'a row that already carries a cost is never touched'
    assert out[2]['cost_usd'] is None, 'unpriced is NULL with a reason, never a bare zero'
    assert 'no declared price' in out[2]['price_basis']
    assert out[3]['cost_usd'] in (0.0, 0), 'proxy rows belong to the proxy path'
    first = out[0]['cost_usd']
    _run(str(store))  # idempotent: a second run must not compound the value
    out2 = [json.loads(l) for l in store.read_text().splitlines() if l.strip()]
    assert out2[0]['cost_usd'] == first, 'the backfill must be idempotent'
