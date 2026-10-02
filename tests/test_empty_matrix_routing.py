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
puts the cheapest capable lane first.

TR-139 (was deliberately left untouched by TR-161, landed separately): a genuine
FAILURE of the rating step (classifier call failed, JEV failed, or the parse failed)
also fails CHEAP now — floor requirements like the empty-matrix path, or the
ROUTER_EMPTY_MATRIX_PROFILE override when it names a real registry profile — never
P0_FORE. The degrade stays visible (problems[0] carries 'unrated -> fail-cheap', which
is what degrade_reason records, so the unrated rate is countable straight from
data/state/outcomes.jsonl:

    jq 'select(.degrade_reason != null and
               (.degrade_reason | contains("unrated -> fail-cheap")))' \
        data/state/outcomes.jsonl | wc -l
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
    # The walk unpacks `status, payload = hop_call(...)` — two values. (The old
    # fixture returned a third dict, which crashed every hop with
    # "too many values to unpack"; the TR-161 assertions never read the served
    # response, so the broken shape went unnoticed until TR-139 needed a SERVED
    # request for its ledger row.)
    def upstream(path, body, headers):
        return 200, (payload or {'choices': [{'message': {'content': 'ok'}}]})
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


def test_empty_matrix_profile_override_validates_against_the_registry(tmp_path, monkeypatch):
    """TR-139: ROUTER_EMPTY_MATRIX_PROFILE is honoured ONLY when the registry
    declares the profile; anything else means the cheap floor, with the ignore
    named in problems[]."""
    reg = tmp_path / "registry.json"
    reg.write_text(json.dumps({"tables": {
        "task_profiles": [{"id": "P9_CHEAP"}, {"id": "P0_FORE"}],
        "task_profile_requirements": []}}))
    monkeypatch.delenv('ROUTER_EMPTY_MATRIX_PROFILE', raising=False)
    assert router_server._empty_matrix_profile(registry_path=reg) == (None, []), \
        'default (env unset) is the cheap floor: no profile, no problems'
    monkeypatch.setenv('ROUTER_EMPTY_MATRIX_PROFILE', 'P9_CHEAP')
    assert router_server._empty_matrix_profile(registry_path=reg) == ('P9_CHEAP', [])
    monkeypatch.setenv('ROUTER_EMPTY_MATRIX_PROFILE', 'NOT_A_PROFILE')
    pid, problems = router_server._empty_matrix_profile(registry_path=reg)
    assert pid is None and 'override ignored' in problems[0] and 'NOT_A_PROFILE' in problems[0]
    # an unreadable registry: the override cannot be validated -> floor, visibly
    monkeypatch.setenv('ROUTER_EMPTY_MATRIX_PROFILE', 'P9_CHEAP')
    pid, problems = router_server._empty_matrix_profile(registry_path=tmp_path / "nope.json")
    assert pid is None, 'an unvalidatable override must not be honoured'
    assert any('override ignored' in p for p in problems)


REAL_CATS = ['agent_tick', 'code_gen', 'creative', 'debug', 'delegation', 'e2e_vision',
             'guard', 'long_doc', 'mock', 'reasoning', 'review', 'schema', 'terminal', 'vision']


def _boom_classifier(monkeypatch):
    """The rating step FAILED (the classifier call could not be rated at all)."""
    class _Boom:
        @staticmethod
        def classify(text):
            raise RuntimeError('classifier unavailable')
    monkeypatch.setitem(sys.modules, 'router_classify', _Boom)


def _capture_chain(monkeypatch):
    seen = {}

    def fake_chain(requirements, sort_spec=None, window_h=None):
        seen['requirements'] = requirements
        return {'chain': [{'provider': 'free', 'model': 'lane', 'usd_1m': 0.0}]}

    monkeypatch.setattr(router_server, '_proxy_chain', fake_chain)
    return seen


def test_a_real_rating_failure_fails_cheap_not_into_the_priciest_profile(monkeypatch):
    """TR-139: a genuine rating FAILURE takes the cheap floor path, visibly.

    (Supersedes the old pin `profile_id == 'P0_FORE'` — that encoded the defective
    contract: an unrated prompt was billed like the priciest profile in the fleet.)
    """
    _boom_classifier(monkeypatch)
    monkeypatch.delenv('ROUTER_EMPTY_MATRIX_PROFILE', raising=False)
    monkeypatch.setattr(router_server, '_registry_categories',
                        lambda registry_path=None: list(REAL_CATS))
    seen = _capture_chain(monkeypatch)
    monkeypatch.setattr(router_server, '_proxy_record', lambda *a, **k: None)
    status, out = router_server.proxy_chat(
        '/v1/chat/completions', {'messages': [{'role': 'user', 'content': 'hi'}]},
        {}, upstream=_ok_upstream())
    req = seen['requirements']
    r = out['_router']
    assert status == 200, 'fail-open: the request must still be served'
    assert r['complexity_source'] == 'default', 'the row must still read as unrated'
    assert req.get('profile_id') is None, \
        f'unrated failure must not substitute a profile, got {req.get("profile_id")!r}'
    matrix = req.get('matrix') or {}
    assert matrix, 'the floor requirement set must be passed to the resolver'
    assert set(matrix.values()) == {-5}, f'every category must sit at the floor, got {matrix}'
    assert set(matrix) == set(REAL_CATS), 'the floor set must cover the registry categories'
    assert 'P0_FORE' not in json.dumps(req), 'the priciest profile must not be in play'
    problems = req.get('problems') or []
    assert problems and 'unrated -> fail-cheap' in problems[0], \
        f'the degrade must be the FIRST problem (degrade_reason), got {problems}'
    assert any('classifier unavailable' in str(p) for p in problems), \
        'the rating cause must stay visible behind the degrade'


def test_router_empty_matrix_profile_override_selects_that_profile_on_failure(monkeypatch):
    """TR-139: the operator override names the degrade profile for unrated prompts."""
    _boom_classifier(monkeypatch)
    monkeypatch.setenv('ROUTER_EMPTY_MATRIX_PROFILE', 'P9_CHEAP')
    monkeypatch.setattr(router_server, '_registry_categories',
                        lambda registry_path=None: list(REAL_CATS))
    monkeypatch.setattr(router_server, '_registry_profile_ids',
                        lambda registry_path=None: {'P9_CHEAP'})
    seen = _capture_chain(monkeypatch)
    monkeypatch.setattr(router_server, '_proxy_record', lambda *a, **k: None)
    status, out = router_server.proxy_chat(
        '/v1/chat/completions', {'messages': [{'role': 'user', 'content': 'hi'}]},
        {}, upstream=_ok_upstream())
    req = seen['requirements']
    assert out['_router']['complexity_source'] == 'default'
    assert req.get('profile_id') == 'P9_CHEAP', 'the honoured override must drive the chain'
    assert 'P0_FORE' not in json.dumps(req)
    problems = req.get('problems') or []
    assert problems and 'unrated -> fail-cheap' in problems[0]
    assert any('override profile P9_CHEAP' in str(p) for p in problems), \
        f'the override must be named in the envelope, got {problems}'


def test_an_unknown_override_is_ignored_visibly_and_the_floor_applies(monkeypatch):
    """TR-139: an override the registry cannot honour is dropped LOUDLY, not silently,
    and the cheap floor still applies."""
    _boom_classifier(monkeypatch)
    monkeypatch.setenv('ROUTER_EMPTY_MATRIX_PROFILE', 'NOT_A_PROFILE')
    monkeypatch.setattr(router_server, '_registry_categories',
                        lambda registry_path=None: list(REAL_CATS))
    monkeypatch.setattr(router_server, '_registry_profile_ids',
                        lambda registry_path=None: {'P9_CHEAP'})
    seen = _capture_chain(monkeypatch)
    monkeypatch.setattr(router_server, '_proxy_record', lambda *a, **k: None)
    status, out = router_server.proxy_chat(
        '/v1/chat/completions', {'messages': [{'role': 'user', 'content': 'hi'}]},
        {}, upstream=_ok_upstream())
    req = seen['requirements']
    assert req.get('profile_id') is None, 'an unhonoured override must not substitute a profile'
    assert set((req.get('matrix') or {}).values()) == {-5}, 'the cheap floor must apply'
    problems = req.get('problems') or []
    assert any('NOT_A_PROFILE' in str(p) and 'override ignored' in str(p) for p in problems), \
        f'the ignored override must be visible in problems, got {problems}'


def test_the_last_resort_degrade_is_named_when_no_floor_set_can_be_built(monkeypatch):
    """TR-139: with the registry unreadable (no floor categories) and no override,
    P0_FORE survives ONLY as the explicitly-named last resort."""
    _boom_classifier(monkeypatch)
    monkeypatch.delenv('ROUTER_EMPTY_MATRIX_PROFILE', raising=False)
    monkeypatch.setattr(router_server, '_registry_categories',
                        lambda registry_path=None: [])
    seen = _capture_chain(monkeypatch)
    monkeypatch.setattr(router_server, '_proxy_record', lambda *a, **k: None)
    status, out = router_server.proxy_chat(
        '/v1/chat/completions', {'messages': [{'role': 'user', 'content': 'hi'}]},
        {}, upstream=_ok_upstream())
    req = seen['requirements']
    assert out['_router']['complexity_source'] == 'default'
    assert req.get('profile_id') == 'P0_FORE', \
        'the named last resort is the one place P0_FORE may still appear'
    assert not (req.get('matrix') or {}), 'no floor set could be built: no matrix'
    problems = req.get('problems') or []
    assert any('floor set unavailable' in str(p) and 'last resort' in str(p) for p in problems), \
        f'the last-resort degrade must be named in the envelope, got {problems}'
    assert any('unrated -> fail-cheap' in str(p) for p in problems)


def test_the_last_resort_honours_a_valid_override_even_without_categories(monkeypatch):
    """TR-139: registry categories unreadable but the profile list still readable —
    the stamped override survives (it was honoured at the rating step) instead of
    the P0_FORE last resort."""
    _boom_classifier(monkeypatch)
    monkeypatch.setenv('ROUTER_EMPTY_MATRIX_PROFILE', 'P9_CHEAP')
    monkeypatch.setattr(router_server, '_registry_categories',
                        lambda registry_path=None: [])
    monkeypatch.setattr(router_server, '_registry_profile_ids',
                        lambda registry_path=None: {'P9_CHEAP'})
    seen = _capture_chain(monkeypatch)
    monkeypatch.setattr(router_server, '_proxy_record', lambda *a, **k: None)
    status, out = router_server.proxy_chat(
        '/v1/chat/completions', {'messages': [{'role': 'user', 'content': 'hi'}]},
        {}, upstream=_ok_upstream())
    req = seen['requirements']
    assert req.get('profile_id') == 'P9_CHEAP', 'the override beats the P0_FORE last resort'
    assert any('override profile P9_CHEAP' in str(p) for p in req.get('problems') or [])


def test_an_unrated_failure_is_countable_from_the_ledger(tmp_path, monkeypatch):
    """TR-139 acceptance: the unrated rate is countable from the ledger alone.

    The row's degrade_reason is problems[0], which carries 'unrated -> fail-cheap';
    count occurrences with:
        jq 'select(.degrade_reason != null and
                   (.degrade_reason | contains("unrated -> fail-cheap")))' \
            data/state/outcomes.jsonl | wc -l
    """
    import router_outcomes as ro
    ledger = tmp_path / 'outcomes.jsonl'
    ledger.write_text('')
    monkeypatch.setattr(ro, 'outcomes_path', lambda *a, **k: str(ledger))
    monkeypatch.setenv('ROUTING_OUTCOMES_FILE', str(ledger))
    monkeypatch.setenv('ROUTER_STATE_DIR', str(tmp_path / 'state'))
    _boom_classifier(monkeypatch)
    monkeypatch.delenv('ROUTER_EMPTY_MATRIX_PROFILE', raising=False)
    monkeypatch.setattr(router_server, '_registry_categories',
                        lambda registry_path=None: list(REAL_CATS))
    seen = _capture_chain(monkeypatch)
    status, out = router_server.proxy_chat(
        '/v1/chat/completions', {'messages': [{'role': 'user', 'content': 'hi'}]},
        {'x-router-session': 'tr139-count'}, upstream=_ok_upstream())
    assert status == 200
    rows = [json.loads(l) for l in ledger.open() if l.strip()]
    unrated = [r for r in rows
               if r.get('degrade_reason') and 'unrated -> fail-cheap' in r['degrade_reason']]
    assert unrated, f'no countable unrated row in {rows}'
    row = unrated[0]
    assert row['complexity_source'] == 'default', 'the unrated stamp must reach the row'
    assert row['profile_id'] is None, 'the row must not read as a P0_FORE call'
    # the row's rating evidence lives under 'classifier' (see _proxy_record)
    assert (row.get('classifier') or {}).get('parse') == 'no-json'
    # the documented jq one-liner, in-process: exactly the unrated rows count out
    counted = [r for r in rows
               if (r.get('degrade_reason') or '').find('unrated -> fail-cheap') >= 0]
    assert len(counted) == len(unrated) == 1
