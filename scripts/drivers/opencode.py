"""drivers/opencode.py — opencode (anomalyco/opencode, via sst/opencode), TR-073.

EVERY FACT BELOW WAS MEASURED, NOT TAKEN FROM THE SURVEY SKETCH. The TR-051
sketch carried opencode as `surfaces {wire:[openai-chat, anthropic-messages,
gemini], launch, acp, telemetry}` and TR-097 existed to test whether the
anthropic-messages part was real. It is not:

  * Ran the real opencode 1.18.29 binary against the real proxy: 119 requests,
    100% to `/v1/chat/completions`, with ZERO anthropic markers — no top-level
    `system` field, no `stop_sequences`, no content blocks, and `max_tokens`
    rather than `max_completion_tokens`. The system prompt arrives as a message.
  * The dialect is chosen by the bundled package, not the config: for a
    hand-declared provider the binary falls back to
    `@ai-sdk/openai-compatible` (22 occurrences of that literal against 2 of
    `anthropic-messages`). `api.id` is an open string in the published config
    schema, so the dialect MUST be declared — omit-vs-omit is not a safe default.
  * NO proxy-scope work was needed. TR-097 closed as not-required, evidence in
    docs/evidence/tr097-anthropic-messages-verdict.md.

TWO CONFIG FACTS THAT COST REAL TIME (both verified live):
  1. opencode IGNORES a project opencode.jsonc unless `OPENCODE_CONFIG` points at
     it. Without it, opencode bootstraps from the global config dir and resolves
     the model against the models.dev catalog, failing with
     `ProviderModelNotFoundError: ... Did you mean: ai-router, unorouter,
     fastrouter?`.
  2. Attribution needs a header, and opencode sends it from
     `provider.<name>.options.headers`. Verified arriving at a recording mock:
     `x-router-caller: opencode`, `x-router-profile: P1_CODING`. The proxy STRIPS
     these before forwarding upstream, which is why they cannot be seen in an
     upstream-side log — only in a mock the host talks to directly.

The T surface is opencode's SQLite store (`~/.local/share/opencode/opencode.db`),
NOT a set of JSON session files. Reading real data rather than assuming the shape:
3,711 assistant messages across 131 sessions, every one carrying a `tokens` dict
and a real `cost`, with the lane at `providerID`/`modelID` and cache accounting
nested under `tokens.cache.read` / `tokens.cache.write` (94,146,162 cache reads
honoured, never folded into `input`).
"""
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from base import Driver, register  # noqa: E402

DEFAULT_DB = '~/.local/share/opencode/opencode.db'


@register
class OpenCodeDriver(Driver):
    id = 'opencode'
    #: W for the proxy path (the verified one). A (ACP, JSON-RPC over stdio) exists
    #: for fleet-spawned runs and is deliberately NOT wired here — the proxy path
    #: is what this row's acceptance tests.
    surfaces = ('W', 'T')
    #: VERIFIED: opencode sends openai-chat for a hand-declared provider.
    wire_format = 'openai-chat'
    #: opencode reads config from OPENCODE_CONFIG (see trap 1 in the docstring).
    config_path = 'opencode.jsonc'
    base_url_shape = '/v1'
    compat = {}

    @classmethod
    def config(cls, proxy_base='http://127.0.0.1:9092',
               provider_name='router', model_ids=None):
        """The `opencode.jsonc` fragment that routes opencode through the proxy.

        Shaped for an ADDITIVE merge into a user's existing file.

        `npm` and `api` are BOTH required: `api.id` is an open string in the
        published schema and opencode otherwise resolves the model against the
        models.dev catalog (trap 1). `baseURL` is repeated inside `options`
        because that is where ai-sdk's openai-compatible provider reads it.

        The model id is a LABEL: the proxy re-targets every hop
        (`fwd['model']` per hop), so lane selection stays in the router per
        SPEC-PROXY-DRIVERS §5. Cost is deliberately zero for the same reason
        TR-070 gives — a synthetic config row must not invent a price.
        """
        base = proxy_base.rstrip('/')
        if not base.endswith(cls.base_url_shape):
            base += cls.base_url_shape
        models = {}
        for mid in (model_ids or ['tr-auto']):
            models[mid] = {'name': f'{mid} (task-router)',
                           'limit': {'context': 200000, 'output': 32768}}
        return {
            '$schema': 'https://opencode.ai/config.json',
            'provider': {
                provider_name: {
                    'name': 'Task Router',
                    'npm': '@ai-sdk/openai-compatible',
                    'api': base,
                    'options': {
                        'baseURL': base,
                        # the caller-side key; the proxy walks its own chain with
                        # its own credentials (SPEC-PROXY-DRIVERS §2.2)
                        'apiKey': '{env:ROUTER_PROXY_KEY}',
                        # VERIFIED: this is how opencode emits x-router-caller
                        'headers': {'x-router-caller': cls.caller_id(),
                                    'x-router-profile': 'P1_CODING'},
                    },
                    'models': models,
                }
            },
            'model': f'{provider_name}/{(model_ids or ["tr-auto"])[0]}',
        }

    @classmethod
    def rows_from_sessions(cls, db_path=None):
        """T surface: delegate to the outcome importer (never duplicate it)."""
        sys.path.insert(0, os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))
        import router_outcomes as ro
        return ro.import_opencode(db_path or DEFAULT_DB)

    @classmethod
    def row_for(cls, **kw):
        """One outcome row in the TR-049 shape."""
        return {'source_system': cls.id, 'session_id': kw.get('session_id'),
                'task_label': kw.get('task_label'),
                'complexity': kw.get('complexity'),
                'profile_id': kw.get('profile_id'),
                'required_categories': kw.get('required_categories'),
                'provider': kw.get('provider'), 'model': kw.get('model'),
                'turns': kw.get('turns'), 'tokens_in': kw.get('tokens_in'),
                'tokens_out': kw.get('tokens_out'),
                'tokens_reasoning': kw.get('tokens_reasoning'),
                'tokens_cache_read': kw.get('tokens_cache_read'),
                'tokens_cache_write': kw.get('tokens_cache_write'),
                'cost_usd': kw.get('cost_usd'),
                'wall_time_s': kw.get('wall_time_s'),
                'success': kw.get('success'), 'ts': kw.get('ts')}
