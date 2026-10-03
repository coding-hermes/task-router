"""TR-243 — last-mile upstream selection and URL joining.

Two defects found live on the 2026-10-03 TR-160 flip (scheduler -> router proxy
-> Hermes gateway), both in the per-provider "last mile" path:

1. URL JOIN. `_provider_upstream_factory` did ``base.rstrip('/') + path`` while
   a provider's `api_base_url` is the OpenAI-compatible BASE and already carries
   the version segment. Every last-mile hop therefore asked for a doubled path:
       https://api.commandcode.ai/provider/v1 + /v1/responses
         -> https://api.commandcode.ai/provider/v1/v1/responses   (404)
   Measured: 12 scheduler ticks died on that 404 and the resulting api_down
   failures opened 4 commandcode-2 breakers.

2. SHAPE. /v1/responses is the Hermes Responses API — it exists on a Hermes
   gateway upstream, not on a raw provider carrier. Routing it to a provider
   last mile can only 404, and a caller-supplied upstream must never be
   overridden by last-mile routing.

These are pure-function contracts: the join, and the "may this hop use the
provider's own upstream?" decision.
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'scripts'))

import router_server as rs   # noqa: E402


# --------------------------------------------------------------------- join
def test_version_segment_is_not_doubled():
    assert rs._join_upstream_url(
        'https://api.meta.ai/v1', '/v1/chat/completions'
    ) == 'https://api.meta.ai/v1/chat/completions'
    assert rs._join_upstream_url(
        'https://api.commandcode.ai/provider/v1', '/v1/responses'
    ) == 'https://api.commandcode.ai/provider/v1/responses'


def test_base_without_a_version_is_joined_unchanged():
    """The operator's configured base is never rewritten, only de-duplicated."""
    assert rs._join_upstream_url(
        'https://example.test/openai', '/v1/chat/completions'
    ) == 'https://example.test/openai/v1/chat/completions'


def test_non_matching_version_is_left_alone():
    """A base ending in v2 with a /v1 route is a real path, not a duplicate."""
    assert rs._join_upstream_url(
        'https://example.test/v2', '/v1/chat/completions'
    ) == 'https://example.test/v2/v1/chat/completions'


def test_trailing_slash_and_missing_leading_slash_are_tolerated():
    assert rs._join_upstream_url(
        'https://api.meta.ai/v1/', 'v1/chat/completions'
    ) == 'https://api.meta.ai/v1/chat/completions'


# ------------------------------------------------------- last-mile decision
def test_chat_shape_may_use_the_provider_last_mile():
    assert rs._hop_uses_provider_last_mile('/v1/chat/completions', False) is True


def test_responses_shape_never_uses_a_provider_last_mile():
    """A raw provider carrier does not implement the Hermes Responses API."""
    assert rs._hop_uses_provider_last_mile('/v1/responses', False) is False
    assert rs._hop_uses_provider_last_mile('/v1/responses/', False) is False


def test_a_caller_supplied_upstream_is_authoritative():
    assert rs._hop_uses_provider_last_mile('/v1/chat/completions', True) is False
    assert rs._hop_uses_provider_last_mile('/v1/responses', True) is False


# ------------------------------------------------- end-to-end of the factory
def test_factory_builds_the_correct_last_mile_url(monkeypatch):
    """Drive the factory with a captured urlopen: the URL is what goes on the wire."""
    seen = {}

    class _Resp:
        status = 200

        def read(self, n=-1):
            return b'{}'

        def readline(self):
            return b''

        def close(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        seen['url'] = req.full_url
        return _Resp()

    monkeypatch.setattr(rs.urllib.request, 'urlopen', fake_urlopen)
    call = rs._provider_upstream_factory(
        'commandcode-2',
        {'commandcode-2': {
            'api_base_url': 'https://api.commandcode.ai/provider/v1',
            'api_key_env': '',
        }})
    assert call is not None
    call('/v1/chat/completions', {'model': 'x'}, {})
    assert seen['url'] == (
        'https://api.commandcode.ai/provider/v1/chat/completions'), seen['url']
    assert '/v1/v1/' not in seen['url']
