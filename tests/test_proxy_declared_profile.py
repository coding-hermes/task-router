"""TR-1000518706784133: a proxied call's ledger row must say WHERE its complexity
came from — the caller's declared profile, or the named no-profile degrade.

Board measurement (2026-09-29): a real Hermes agent called the proxy through a
plain OpenAI-compatible provider entry and was SERVED, but every ledger row read
complexity_source='classifier-empty' or 'default' with degrade_reason
'no JSON object in classifier output' and profile_id=P0_FORE — selection was not
complexity-driven. Root cause: a vanilla provider call sends only model+messages,
so there is NO declared profile; the classifier rates the prompt text and, when
that rating fails, the row must say so instead of looking attributable.

The declared-profile precedence already exists (router_server._proxy_requirements
reads 'x-router-profile'; router_outcomes.profile_signature/required_levels
resolve the profile's own levels; _proxy_record stamps 'profile_id' onto the
row). These tests pin the FULL attribution at the LEDGER-ROW level — the row is
really appended to the store (no _proxy_record stub) and read back from disk —
for both arms:

  declared   x-router-profile header -> complexity_source='declared',
             profile_id == the caller's profile, degrade_reason None, the
             classifier never runs (a poison classifier module would explode).
  no profile plain call, no profile header, rating unavailable ->
             complexity_source names the no-profile class ('default' when the
             rating FAILED, 'classifier-empty' when a successful rating pressed
             no category), profile_id is None (the cheap floor, never P0_FORE),
             and degrade_reason carries the cause in one named field.

The /api/ui/ledger surface (the UI panel's ledger search + flow drill-down) is
pinned to return those same fields per row, so the attribution is visible in the
UI, not just in the JSONL.

No network: the classifier module is stubbed/poisoned, the chain resolver is
stubbed, and the breaker subprocess seam (_subprocess_text) is stubbed.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import router_server as rsrv   # noqa: E402

#: the profile a real Hermes caller declares (committed data/tables/task_profiles.jsonl)
REAL_PROFILE = 'P1_CODING'

REAL_CATS = ['agent_tick', 'code_gen', 'creative', 'debug', 'delegation', 'e2e_vision',
             'guard', 'long_doc', 'mock', 'reasoning', 'review', 'schema', 'terminal', 'vision']


class _PoisonClassifier:
    """Any attempt to RATE the prompt explodes — proving the declared arm never
    touches the classifier and the unrated arm degrades instead of crashing."""

    @staticmethod
    def classify(text):
        raise RuntimeError('poison classifier: the prompt must not be rated here')


class _EmptyMatrixClassifier:
    """The classifier ran fine and answered 'this prompt presses no category'."""

    @staticmethod
    def classify(text):
        return {'matrix': {}, 'confidence': 0.5, 'prompt_version': 'v1',
                'model': 'deepseek-flash', 'problems': []}


def _wire_proxy(monkeypatch, tmp_path, classifier=None):
    """Hermetic proxy env: own outcome store, own state dir, stubbed chain +
    breaker subprocess, optional classifier module override.

    _proxy_record is NOT stubbed: the row lands in the real store, which is the
    point — these tests assert the LEDGER ROW, not an internal return value.
    """
    monkeypatch.setenv('ROUTING_OUTCOMES_FILE', str(tmp_path / 'outcomes.jsonl'))
    monkeypatch.setenv('ROUTER_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.delenv('ROUTER_EMPTY_MATRIX_PROFILE', raising=False)
    monkeypatch.delenv('ROUTER_SCORER', raising=False)
    if classifier is not None:
        monkeypatch.setitem(sys.modules, 'router_classify', classifier)
    monkeypatch.setattr(rsrv, '_registry_categories',
                        lambda registry_path=None: list(REAL_CATS))
    monkeypatch.setattr(rsrv, '_proxy_chain', lambda requirements, **k:
                        {'chain': [{'hop': 1, 'provider': 'p1', 'model': 'm1',
                                    'usd_1m': 0.1}]})
    # the circuit breaker records via a subprocess; a unit test must not spawn it
    monkeypatch.setattr(rsrv, '_subprocess_text', lambda *a, **k: '')
    return tmp_path / 'outcomes.jsonl'


def _hermes_body():
    """A Hermes-shaped vanilla provider call: model + messages, nothing else."""
    return {'model': 'whatever-the-client-sent',
            'messages': [{'role': 'user', 'content': 'why is this Go test deadlocking?'}]}


def _ok_upstream():
    def upstream(path, body, headers):
        return 200, {'choices': [{'message': {'content': 'hi'}}], 'model': body.get('model')}
    return upstream


def _last_row(store):
    lines = [ln for ln in store.read_text().splitlines() if ln.strip()]
    assert len(lines) == 1, f'one request -> exactly one ledger row, got {len(lines)}'
    return json.loads(lines[-1])


# ---------------------------------------------------------------- criterion 1+2

def test_declared_profile_lands_on_the_ledger_row(monkeypatch, tmp_path):
    """A Hermes-shaped call that declares a real task profile: the ROW carries
    complexity_source='declared' + the caller's profile_id, and the classifier
    never runs. The header — not the OpenAI 'model' field — picked the lane."""
    store = _wire_proxy(monkeypatch, tmp_path, classifier=_PoisonClassifier)

    status, out = rsrv.proxy_chat(
        '/v1/chat/completions', _hermes_body(),
        {'x-router-profile': REAL_PROFILE}, upstream=_ok_upstream())

    assert status == 200
    row = _last_row(store)
    assert row['complexity_source'] == 'declared', \
        f"the row must read as declared, got {row['complexity_source']!r}"
    assert row['profile_id'] == REAL_PROFILE, \
        (f"selection must be attributable to the caller's profile, "
         f"got profile_id={row['profile_id']!r}")
    assert row['profile_id'] != 'P0_FORE', 'the floor profile must not appear on a declared row'
    assert row['degrade_reason'] is None, 'a declared rating is not a degrade'
    assert row['source_system'] == 'router-proxy'
    # the rating evidence names the declare itself (UI flow panel renders this object)
    assert row['classifier']['parse'] == 'declared'
    assert row['classifier']['source'] == 'declared'
    # the envelope agrees with the row (what a driver reads in the same response)
    env = out['_router']
    assert env['complexity_source'] == 'declared'
    assert env['requirements']['profile_id'] == REAL_PROFILE
    assert env['degrade_reason'] is None
    # the levels the chain was admitted on are the one authority's answer for the
    # declared profile (None hermetically — no generated registry.json in a fresh
    # worktree; on a seeded host both sides resolve to the profile's own levels —
    # the pin is that row and envelope can never disagree).
    assert row['required_categories'] == env['requirements'].get('levels')


# ------------------------------------------------------------------- criterion 4

def test_no_profile_row_names_the_floor_and_the_reason(monkeypatch, tmp_path):
    """The OTHER arm: the same Hermes-shaped call WITHOUT a profile header, the
    rating unavailable (here: the classifier call fails). The row must say so in
    the named fields — complexity_source='default', degrade_reason carrying the
    cause — and record the CHEAP FLOOR requirement set, profile_id None, never
    P0_FORE and never a fake attribution."""
    store = _wire_proxy(monkeypatch, tmp_path, classifier=_PoisonClassifier)

    status, out = rsrv.proxy_chat(
        '/v1/chat/completions', _hermes_body(), {}, upstream=_ok_upstream())

    assert status == 200, 'fail-open: the request is still served'
    row = _last_row(store)
    assert row['complexity_source'] == 'default', \
        f"an unrated row must read 'default', got {row['complexity_source']!r}"
    assert row['profile_id'] is None, \
        f"the floor path records NO profile, got {row['profile_id']!r}"
    assert 'P0_FORE' not in json.dumps(row), 'the priciest profile must not be in play'
    dr = row['degrade_reason']
    assert isinstance(dr, str) and 'unrated -> fail-cheap' in dr, \
        f'degrade_reason must name the fail-cheap floor, got {dr!r}'
    assert 'poison classifier' in dr, 'the rating CAUSE stays visible behind the degrade'
    # the floor requirement set the chain was actually built from
    assert row['required_categories'] == {c: -5 for c in REAL_CATS}
    assert row['classifier']['parse'] == 'no-json'
    # envelope agreement (criterion 4's "explicit reason" is on both surfaces)
    env = out['_router']
    assert env['complexity_source'] == 'default'
    assert env['degrade_reason'] == row['degrade_reason']
    assert env['requirements']['profile_id'] is None


def test_empty_matrix_row_is_the_other_named_no_profile_class(monkeypatch, tmp_path):
    """A SUCCESSFUL rating that presses no category is not a failure: the row
    reads 'classifier-empty' with its own degrade reason, floor set, no profile.
    This is exactly the vocabulary the 2026-09-29 board rows carried."""
    store = _wire_proxy(monkeypatch, tmp_path, classifier=_EmptyMatrixClassifier)

    status, out = rsrv.proxy_chat(
        '/v1/chat/completions', _hermes_body(), {}, upstream=_ok_upstream())

    assert status == 200
    row = _last_row(store)
    assert row['complexity_source'] == 'classifier-empty'
    assert row['profile_id'] is None
    assert row['degrade_reason'] == \
        'empty matrix -> no category pressurised: floor requirements, cheapest eligible lane'
    assert row['required_categories'] == {c: -5 for c in REAL_CATS}
    assert row['classifier']['parse'] == 'empty-matrix'


# ------------------------------------------------------------------- criterion 3

def test_ui_ledger_shows_the_attribution_fields(tmp_path, monkeypatch):
    """The ledger search (the UI panel's /api/ui/ledger route) returns the rows
    verbatim — complexity_source, profile_id, degrade_reason all readable and
    filterable — which is what the flow drill-down renders per row."""
    store = tmp_path / 'outcomes.jsonl'
    declared = {'source_system': 'router-proxy', 'session_id': 's-declared',
                'provider': 'p1', 'model': 'm1', 'ts': 1000.0, 'success': True,
                'complexity_source': 'declared', 'profile_id': REAL_PROFILE,
                'degrade_reason': None}
    unrated = {'source_system': 'router-proxy', 'session_id': 's-unrated',
               'provider': 'p2', 'model': 'm2', 'ts': 2000.0, 'success': True,
               'complexity_source': 'default', 'profile_id': None,
               'degrade_reason': 'unrated -> fail-cheap floor requirements (TR-139) | '
                                 'classifier unavailable: no key'}
    store.write_text(''.join(json.dumps(r) + '\n' for r in (declared, unrated)))
    monkeypatch.setenv('ROUTING_OUTCOMES_FILE', str(store))

    got = rsrv.ui_ledger({'complexity_source': 'declared'})
    assert got['total_matched'] == 1 and got['rows'][0]['profile_id'] == REAL_PROFILE

    got = rsrv.ui_ledger({'complexity_source': 'default'})
    assert got['total_matched'] == 1
    row = got['rows'][0]
    assert row['profile_id'] is None
    assert 'unrated -> fail-cheap' in row['degrade_reason']

    # the fields the UI reads are present on every returned row object
    for r in rsrv.ui_ledger({})['rows']:
        assert {'complexity_source', 'profile_id', 'degrade_reason'} <= set(r)
