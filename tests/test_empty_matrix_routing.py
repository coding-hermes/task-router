"""TR-161: "this prompt needs nothing special" must not route to the priciest lane.

Measured live, 2026-09-26, on proxied rows with a rating verdict (my own declared probes
excluded):
    RATED    (classifier/jev)   8 rows   mean $0.0278   median $0.0204
    DEGRADED (fail-open)       22 rows   mean $0.0654   median $0.0654   mean 10 out-tokens

While reading why, the cause turned out not to be a failure at all. router_classify's
validate_matrix deliberately ALLOWS an empty matrix ("a task may require nothing"), and
router_server then substituted profile P0_FORE - the priciest profile in the registry - so
"needs nothing" was billed as "needs the best". Probed directly:
    --profile P0_FORE            -> 16 lanes,  head stepfun/step-3.5-flash  $0.108/M
    (no profile, no requirements)-> 16 lanes,  head stepfun/step-3.5-flash  $0.108/M
    one lenient requirement      -> 166 lanes, head qwen3-coder-plus:free   $0.000/M

So an unpressured prompt should build its chain from a FLOOR requirement set (every
category at the scale minimum): the full eligible pool, sorted by effective price, which
puts the cheapest capable lane first. The fall-back for a genuine FAILURE of the rating
step is a separate question (TR-139, the operator's) and is deliberately untouched here.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import router_server  # noqa: E402


def _empty_matrix_classifier(monkeypatch):
    """The classifier ran fine and answered 'nothing required'."""
    class _Mod:
        @staticmethod
        def classify(text):
            return {'matrix': {}, 'confidence': 0.5, 'prompt_version': 'v1',
                    'model': 'deepseek-flash', 'problems': []}
    monkeypatch.setitem(sys.modules, 'router_classify', _Mod)


def _ok_upstream(payload=None):
    def upstream(path, body, headers):
        return 200, (payload or {'choices': [{'message': {'content': 'ok'}}]}), {'provider': 'p', 'model': 'm'}
    return upstream


def test_empty_matrix_builds_a_floor_chain_not_the_priciest_profile(monkeypatch):
    _empty_matrix_classifier(monkeypatch)
    # the registry's real category set (14), as the profiles declare it
    real_cats = ['agent_tick', 'code_gen', 'creative', 'debug', 'delegation', 'e2e_vision',
                 'guard', 'long_doc', 'mock', 'reasoning', 'review', 'schema', 'terminal', 'vision']
    monkeypatch.setattr(router_server, '_registry_categories',
                        lambda registry_path=None: list(real_cats))
    seen = {}

    def fake_chain(requirements, sort_spec=None, window_h=None):
        seen['requirements'] = requirements
        return {'chain': [{'provider': 'free', 'model': 'lane', 'usd_1m': 0.0}]}

    monkeypatch.setattr(router_server, '_proxy_chain', fake_chain)
    monkeypatch.setattr(router_server, '_proxy_record', lambda *a, **k: None)
    status, out = router_server.proxy_chat(
        '/v1/chat/completions', {'messages': [{'role': 'user', 'content': 'Reply with: ok'}]},
        {}, upstream=_ok_upstream())
    req = seen['requirements']
    r = out['_router']
    assert r['complexity_source'] == 'classifier-empty'
    assert req.get('profile_id') in (None, ''), 'must not substitute a named profile'
    matrix = req.get('matrix') or {}
    assert matrix, 'the floor requirement set must be passed to the resolver'
    assert set(matrix.values()) == {-5}, f'every category must sit at the floor, got {matrix}'
    assert len(matrix) >= 10, 'the floor set should cover the registry categories'
    assert 'P0_FORE' not in json.dumps(req), 'the priciest profile must not be in play'


def test_the_floor_set_is_derived_from_the_registry(tmp_path):
    """Data-driven, against a fixture registry (the real one is a 2.8 MB generated file
    that is gitignored, so a test must not depend on it being present)."""
    reg = tmp_path / "registry.json"
    reg.write_text(json.dumps({"tables": {"task_profile_requirements": [
        {"task_id": "P0_FORE", "category": "code_gen", "level": 2},
        {"task_id": "P0_FORE", "category": "debug", "level": 1},
        {"task_id": "P0_FORE", "category": "code_gen", "level": 0},
    ]}}))
    cats = router_server._registry_categories(registry_path=reg)
    assert cats == ["code_gen", "debug"], f"categories must be unique and ordered, got {cats}"
    missing = router_server._registry_categories(registry_path=tmp_path / "nope.json")
    assert missing == [], "an unreadable registry must return [] so the caller can fall back"


def test_a_real_rating_failure_still_degrades_visibly(monkeypatch):
    """The empty-matrix fix must NOT quietly change the failure path (TR-139 is the operator's)."""
    class _Boom:
        @staticmethod
        def classify(text):
            raise RuntimeError('classifier unavailable')
    monkeypatch.setitem(sys.modules, 'router_classify', _Boom)
    seen = {}
    monkeypatch.setattr(router_server, '_proxy_chain',
                        lambda requirements, sort_spec=None, window_h=None:
                        (seen.setdefault('r', requirements), {'chain': []})[1])
    monkeypatch.setattr(router_server, '_proxy_record', lambda *a, **k: None)
    status, out = router_server.proxy_chat(
        '/v1/chat/completions', {'messages': [{'role': 'user', 'content': 'hi'}]},
        {}, upstream=_ok_upstream())
    assert out['_router']['complexity_source'] == 'default'
    assert seen['r'].get('profile_id') == 'P0_FORE', 'the failure fallback is unchanged'
    problems = (out['_router'].get('requirements') or {}).get('problems') or []
    assert any('classifier unavailable' in str(p) for p in problems), 'the reason must be visible'
