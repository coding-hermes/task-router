"""drivers/openclaw.py — OpenClaw (openclaw/openclaw), TR-072.

GROUNDED IN THE INSTALLED PACKAGE, NOT THE SURVEY. OpenClaw 2026.9.5 was
installed and its own shipped documentation read directly
(`docs/gateway/config-tools/custom-providers.md`), rather than trusting the
recollection in the row. That document states the row's critical wire fact in its
own words:

  "For self-hosted `/v1/chat/completions` backends such as MLX, vLLM, SGLang, and
   most OpenAI-compatible local servers, use `openai-completions`. A custom
   provider with `baseUrl` but no `api` **defaults to `openai-completions`**; set
   `openai-responses` only when the backend supports `/v1/responses`."

So the base-URL shape decides the dialect and the default is already correct for
the router. The same page lists the request adapters (`openai-completions`,
`openai-responses`, `anthropic-messages`, `google-generative-ai`, ...), so the
dialect IS declareable — which is why this driver declares it explicitly rather
than relying on a default that a future release could change.

TWO FACTS THAT SHAPE THIS DRIVER (both from shipped docs, not invented):
  1. A custom `baseUrl` is also a NETWORK TRUST decision: OpenClaw "allows that
     exact `scheme://host:port` origin through the guarded fetch path". Pointing
     at the proxy therefore also authorises that one origin — no separate option,
     and no other private origin is trusted.
  2. Do NOT copy catalog `compat` flags into config. The docs are explicit:
     "Provider catalogs own `compat`... Do not copy those flags into config...
     `openclaw doctor --fix` removes matching legacy overrides". A `compat` block
     is only for a genuinely custom route, and then only keys verified against
     that endpoint. The router is such a route, so this driver sets the ONE fact
     it can verify — the endpoint accepts `developer` messages, because the proxy
     normalizes them (TR-096) — and nothing it cannot.

INSTALL REALITY: OpenClaw requires Node >=24.16.0 <25 || >=26.1.0 and its
preinstall script HARD-FAILS on anything older. This box ships Node 22.22.3, so a
local Node 24 was fetched to run it. That is an environment prerequisite a user
must satisfy before this driver can be exercised live, and is recorded rather than
worked around silently.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from base import Driver, register  # noqa: E402


@register
class OpenClawDriver(Driver):
    id = 'openclaw'
    surfaces = ('W', 'T')
    #: the default for a custom provider with a baseUrl and no `api` (shipped doc)
    wire_format = 'openai-completions'
    config_path = '~/.openclaw/openclaw.json5'
    base_url_shape = '/v1'
    #: The proxy normalizes developer->system (TR-096), so the router route WILL
    #: accept `developer`. Declared because the docs warn that catalog flags must
    #: not be copied and only endpoint-verified keys belong here.
    compat = {'supports_developer_role': True}

    @classmethod
    def config(cls, proxy_base='http://127.0.0.1:9092',
               provider_name='task-router', model_ids=None):
        """The `models.providers` block that routes OpenClaw through the proxy.

        Shaped for `openclaw config patch` / an additive merge: the doc's own
        example uses `models.mode: "merge"` (the default) and the safe-edit form is
        `config set models.providers.<id> '<json>' --strict-json --merge`, so this
        returns the provider object, not a whole config file, and never replaces
        another provider.

        The model id is a LABEL (the proxy re-targets every hop), and cost is
        deliberately zero so a synthetic config row cannot compete with the
        registry's measured pricing (TR-070).
        """
        base = proxy_base.rstrip('/')
        if not base.endswith(cls.base_url_shape):
            base += cls.base_url_shape
        models = []
        for mid in (model_ids or ['tr-auto']):
            models.append({
                'id': mid,
                'name': f'{mid} (task-router)',
                'reasoning': True,
                'input': ['text'],
                # zero: lane pricing is the router's job (TR-070)
                'cost': {'input': 0, 'output': 0, 'cacheRead': 0, 'cacheWrite': 0},
                'contextWindow': 200000,
                'maxTokens': 32768,
            })
        return {
            'models': {
                'mode': 'merge',
                'providers': {
                    provider_name: {
                        'baseUrl': base,
                        # a reference (the docs prefer SecretRef/env substitution),
                        # never a literal: the proxy walks its own chain
                        'apiKey': 'ROUTER_PROXY_KEY',
                        # declared explicitly — the default is already
                        # openai-completions, but relying on an implicit default is
                        # what this driver exists to avoid
                        'api': cls.wire_format,
                        # attribution. Without this the proxy stamps the row
                        # source_system='router-proxy' (TR-071 degrades an
                        # unattributed caller by design), which is exactly what the
                        # first live run produced. The doc names this field for
                        # "proxy/tenant routing", i.e. this use.
                        'headers': {'x-router-caller': cls.caller_id(),
                                    'x-router-profile': 'P1_CODING'},
                        'models': models,
                    }
                },
            }
        }

    @classmethod
    def rows_from_sessions(cls, state_dir=None):
        """T surface: OpenClaw's own activity records.

        Delegated to the outcome importer so the row shape has one definition.
        """
        sys.path.insert(0, os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))
        import router_outcomes as ro
        return ro.import_openclaw(state_dir or '~/.openclaw')

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
