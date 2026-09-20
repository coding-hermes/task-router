"""TR-075 — the deepseek-harness (dsh) driver (SPEC-PROXY-DRIVERS §3, §6).

The row's CRITICAL wire fact is confirmed against real source in
~/deepseek-harness, not assumed:

  * `packages/llm/llm-pi-ai/src/catalog.ts` declares `PiAiCompatProfile` with 34
    classified fields, `supportsDeveloperRole` among them.
  * The archived Agent Note
    (`.agents/notes/archived/feature/2026-08-18-pi-ai-wire-compat-surface.md`)
    states: "For an endpoint its detection does not recognize, the answer is
    'this is OpenAI itself': detectCompat returns supportsDeveloperRole: true,
    maxTokensField: 'max_completion_tokens', supportsStore: true. A hand-declared
    route is by construction an endpoint pi-ai does not ship, so every such route
    received OpenAI's own request shape."
  * The same note names maxTokensField as carrying "the same defect over a wider
    blast radius, since it shapes every request rather than only a reasoning
    model's" — so declaring only supportsDeveloperRole would be a half fix.

Pointing dsh at the router IS a hand-declared route. Hence three compat facts,
not one.
"""
import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'scripts'))
sys.path.insert(0, os.path.join(REPO, 'scripts', 'drivers'))

import drivers          # noqa: E402


def _d():
    return drivers.get_driver('deepseek-harness')


# ------------------------------------------------------------------ W: config
def test_driver_registered():
    d = _d()
    assert d is not None and d.id == 'deepseek-harness'
    assert 'W' in d.surfaces and 'T' in d.surfaces
    assert d.wire_format == 'openai-completions'
    assert d.config_path == '~/.dsh/settings.yaml'


def test_config_declares_all_three_compat_facts():
    """supportsDeveloperRole alone is the half fix the note explicitly rejects."""
    entry = _d().config('http://127.0.0.1:9092')['llm-pi-ai']['providers']['router']
    c = entry['compat']
    assert c['supportsDeveloperRole'] is False, (
        'a hand-declared route otherwise sends role:developer')
    assert c['maxTokensField'] == 'max_tokens', (
        'default max_completion_tokens shapes EVERY request, not just reasoning')
    assert c['supportsStore'] is False


def test_config_shape_matches_the_documented_route_form():
    entry = _d().config('http://127.0.0.1:9092')['llm-pi-ai']['providers']['router']
    assert entry['api'] == 'openai-completions'
    assert entry['baseURL'] == 'http://127.0.0.1:9092/v1'
    assert entry['displayName'] == 'task-router'
    assert isinstance(entry['models'], list) and entry['models']


def test_credential_is_a_reference_not_a_secret():
    """The harness resolves apiKeyEnv through its credential seam, so no secret
    may be written into the config — its own documented rule."""
    entry = _d().config('http://127.0.0.1:9092')['llm-pi-ai']['providers']['router']
    # the credential is an env-var NAME, not a value
    assert re.fullmatch(r'[A-Z][A-Z0-9_]*', entry['apiKeyEnv']), (
        'apiKeyEnv must name an environment variable, never carry a value')
    # and no field anywhere holds a literal key
    assert 'apiKey' not in entry
    assert 'token' not in json.dumps(entry).lower().split('"')


def test_route_is_named_router_not_a_provider_name():
    """SPEC §5: a driver never teaches the router about host model names, and
    the config must not impersonate a provider."""
    cfg = _d().config('http://127.0.0.1:9092')
    assert 'router' in cfg['llm-pi-ai']['providers']


# ------------------------------------------------------------------- T: reader
def _mk(tmp, name, lines):
    """The real layout is <base>/<cwd-slug>/<session-id>/session.v<N>.jsonl, so a
    fixture needs both levels or the reader (correctly) finds nothing."""
    d = tmp / 'slug' / name
    d.mkdir(parents=True)
    p = d / 'session.v3.jsonl'
    p.write_text('\n'.join(json.dumps(x) for x in lines) + '\n')
    return d


def _assistant(prov, model, tin, tout, stop='stop', ts=1789087747549, cread=0):
    return {'type': 'assistant/message', 'seq': 1, 'time': ts,
            'data': {'turn': 1, 'step': 1,
                     'message': {'role': 'assistant', 'content': [],
                                 'source': {'kind': 'model', 'provider': prov,
                                            'model': model,
                                            'replayState': {'response': {
                                                'stopReason': stop}}}},
                     'usage': {'inputTokens': tin, 'outputTokens': tout,
                               'totalTokens': tin + tout,
                               'cacheReadTokens': cread}}}


def test_reader_reads_the_real_nested_shape(tmp_path):
    """usage lives at data.usage with camelCase keys, and the lane at
    data.message.source.* — the flat shape reads ZERO rows (verified live)."""
    import deepseek_harness as dh
    _mk(tmp_path, 'session-aaa', [
        {'type': 'session', 'version': 3, 'id': 'session-aaa',
         'createdAt': 1789080240036},
        _assistant('ninerouter', 'glm/glm-4.6', 2041, 44)])
    rows = dh.import_dsh_sessions(str(tmp_path))
    assert len(rows) == 1
    r = rows[0]
    assert r['source_system'] == 'deepseek-harness'
    assert r['provider'] == 'ninerouter' and r['model'] == 'glm/glm-4.6'
    assert r['tokens_in'] == 2041 and r['tokens_out'] == 44
    assert r['success'] is True


def test_reader_sums_across_turns(tmp_path):
    import deepseek_harness as dh
    _mk(tmp_path, 'session-bbb', [
        {'type': 'session', 'id': 'session-bbb'},
        _assistant('p', 'm', 100, 10, ts=1789087747000),
        _assistant('p', 'm', 200, 20, ts=1789087748000)])
    r = dh.import_dsh_sessions(str(tmp_path))[0]
    assert r['turns'] == 2
    assert r['tokens_in'] == 300 and r['tokens_out'] == 30


def test_reader_preserves_cache_read_tokens(tmp_path):
    import deepseek_harness as dh
    _mk(tmp_path, 'session-ccc', [
        {'type': 'session', 'id': 'session-ccc'},
        _assistant('p', 'm', 1181, 126, cread=7936)])
    r = dh.import_dsh_sessions(str(tmp_path))[0]
    assert r['tokens_cache_read'] == 7936


def test_timestamps_are_milliseconds_converted_to_seconds(tmp_path):
    """`time` is epoch ms; treating it as seconds would put every session in
    the year 58,000."""
    import deepseek_harness as dh
    _mk(tmp_path, 'session-ddd', [
        {'type': 'session', 'id': 'session-ddd'},
        _assistant('p', 'm', 1, 1, ts=1789087747549)])
    r = dh.import_dsh_sessions(str(tmp_path))[0]
    assert 1_700_000_000 < r['ts'] < 2_000_000_000, 'epoch SECONDS expected'


def test_reader_accepts_the_unversioned_layout(tmp_path):
    """Real deployments carry both `session.jsonl.zstd` and
    `session.v3.jsonl.zstd`; a reader pinned to one form reports zero rows."""
    import deepseek_harness as dh
    d = tmp_path / 'slug' / 'session-eee'
    d.mkdir(parents=True)
    (d / 'session.jsonl').write_text(
        json.dumps({'type': 'session', 'id': 'session-eee'}) + '\n'
        + json.dumps(_assistant('p', 'm', 5, 5)) + '\n')
    rows = dh.import_dsh_sessions(str(tmp_path))
    assert len(rows) == 1


def test_highest_version_wins_when_both_exist(tmp_path):
    import deepseek_harness as dh
    d = tmp_path / 'slug' / 'session-fff'
    d.mkdir(parents=True)
    (d / 'session.jsonl').write_text(
        json.dumps({'type': 'session', 'id': 'old'}) + '\n'
        + json.dumps(_assistant('p', 'm', 1, 1)) + '\n')
    (d / 'session.v3.jsonl').write_text(
        json.dumps({'type': 'session', 'id': 'new'}) + '\n'
        + json.dumps(_assistant('p', 'm', 9, 9)) + '\n')
    r = dh.import_dsh_sessions(str(tmp_path))[0]
    assert r['tokens_in'] == 9, 'the newest format generation must be read'


def test_reader_ignores_a_session_with_no_assistant_turns(tmp_path):
    """A real session here has only lifecycle events; it must not become a row."""
    import deepseek_harness as dh
    _mk(tmp_path, 'session-ggg', [
        {'type': 'session', 'id': 'session-ggg'},
        {'type': 'permission/preset'}, {'type': 'sandbox/mode'}])
    assert dh.import_dsh_sessions(str(tmp_path)) == []


def test_reader_reports_error_stop_reason_honestly(tmp_path):
    import deepseek_harness as dh
    _mk(tmp_path, 'session-hhh', [
        {'type': 'session', 'id': 'session-hhh'},
        _assistant('p', 'm', 1, 1, stop='error')])
    assert dh.import_dsh_sessions(str(tmp_path))[0]['success'] is False


def test_reader_survives_corrupt_and_missing_input(tmp_path):
    import deepseek_harness as dh
    d = tmp_path / 'slug' / 'session-iii'
    d.mkdir(parents=True)
    (d / 'session.v3.jsonl').write_text(
        json.dumps({'type': 'session', 'id': 's'}) + '\nnot json\n'
        + json.dumps(_assistant('p', 'm', 2, 2)) + '\n')
    assert len(dh.import_dsh_sessions(str(tmp_path))) == 1
    assert dh.import_dsh_sessions('/nonexistent/dsh/sessions') == []


def test_undecodable_compressed_session_is_skipped_not_zeroed(tmp_path, monkeypatch):
    """Absence of data is not evidence of no usage: a store we cannot read must
    be SKIPPED, never reported as a zero row."""
    import deepseek_harness as dh
    d = tmp_path / 'slug' / 'session-jjj'
    d.mkdir(parents=True)
    (d / 'session.v3.jsonl.zstd').write_bytes(b'\x28\xb5\x2f\xfdnot-really-zstd')
    monkeypatch.setattr(dh, '_read_lines', lambda p: None)
    assert dh.import_dsh_sessions(str(tmp_path)) == []


def test_reader_delegates_from_the_driver():
    import inspect
    import deepseek_harness as dh
    src = inspect.getsource(dh.DeepseekHarnessDriver.rows_from_sessions)
    assert 'import_dsh_sessions' in src
    assert dh.DeepseekHarnessDriver.row_for(session_id='s')['source_system'] == \
        'deepseek-harness'


def test_real_sessions_on_this_box_map_without_error():
    """Live check; skips when no dsh sessions exist."""
    rows = _d().rows_from_sessions()
    if not rows:
        return
    assert all(r['source_system'] == 'deepseek-harness' for r in rows)
    assert all(r['session_id'] for r in rows)
    assert all(r['ts'] is None or r['ts'] < 2_000_000_000 for r in rows), (
        'timestamps must be epoch seconds, not milliseconds')
