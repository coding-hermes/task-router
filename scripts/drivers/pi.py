"""drivers/pi.py — pi (earendil/pi-mono coding agent), TR-074.

Two things make pi different from hermes (TR-071), and both are grounded in the
installed package's own docs rather than assumed:

  1. WIRE COMPAT. pi's models.md states plainly: "Some OpenAI-compatible servers
     do not understand the `developer` role used for reasoning-capable models.
     For those providers, set `compat.supportsDeveloperRole` to `false` so pi
     sends the system prompt as a `system` message instead." The proxy route is
     such a server from pi's point of view, so the config declares it false —
     pi then rewrites client-side and the request reaches the proxy already
     correct. (The proxy ALSO normalizes developer->system defensively, TR-096,
     so a host that ignores the flag still works. Belt and braces, disclosed.)

  2. NO EXTENSION REQUIRED. pi supports custom providers two ways:
     `pi.registerProvider()` from an extension, and the flat
     `~/.pi/agent/models.json`. The flat file needs no code execution at startup,
     so it is the honest default for a config snippet — with the extension form
     emitted only for callers that need dynamic model discovery.

The driver states wire facts as DATA (`compat`) and never implements the
workaround itself (SPEC-PROXY-DRIVERS §1: one place, not five).
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from base import Driver, register  # noqa: E402

SESSIONS_DIR = '~/.pi/agent/sessions'


@register
class PiDriver(Driver):
    id = 'pi'
    surfaces = ('W', 'T')
    #: pi's default custom-provider dialect (models.md "Supported APIs")
    wire_format = 'openai-completions'
    config_path = '~/.pi/agent/models.json'
    base_url_shape = '/v1'
    #: the wire fact pi's own docs name, declared as data (see module docstring)
    compat = {'supports_developer_role': False,
              'supports_reasoning_effort': False}

    @classmethod
    def config(cls, proxy_base='http://127.0.0.1:9092', model_ids=None):
        """A `~/.pi/agent/models.json` fragment routing pi through the proxy.

        Shaped exactly like pi's documented custom-provider entries so it is
        additive to a user's existing file (merge under `providers`, do not
        replace the file — pi's docs warn that supplying `models` REPLACES that
        provider's models).

        The model `id` is what pi sends as `model` in each request. The proxy
        re-targets every hop anyway (it sets `fwd['model']` per hop), so the id
        is a label here, not a routing decision — lane selection stays in the
        router, per SPEC-PROXY-DRIVERS §5.
        """
        entry = {
            'baseUrl': f'{proxy_base.rstrip("/")}{cls.base_url_shape}',
            'api': cls.wire_format,
            # pi requires an apiKey field; the proxy authenticates the caller
            # with its own key, so this is the caller-side key, not a provider's
            'apiKey': '$ROUTER_PROXY_KEY',
            'compat': {'supportsDeveloperRole': cls.compat['supports_developer_role'],
                       'supportsReasoningEffort': cls.compat['supports_reasoning_effort']},
            'models': [],
        }
        for mid in (model_ids or ['task-router']):
            entry['models'].append({
                'id': mid,
                'name': f'{mid} (task-router)',
                'reasoning': True,
                'input': ['text'],
                # cost is deliberately ZERO: the registry measures spend per
                # candidate model at ingress, so a synthetic config row must not
                # invent a price (TR-070).
                'cost': {'input': 0, 'output': 0, 'cacheRead': 0, 'cacheWrite': 0},
                'contextWindow': 200000,
                'maxTokens': 32768,
            })
        return {'providers': {'task-router': entry}}

    @classmethod
    def extension_source(cls, proxy_base='http://127.0.0.1:9092'):
        """The `pi.registerProvider()` form, for dynamic model discovery.

        Emitted as a string rather than written anywhere: installing an extension
        is the user's act, and a driver must not modify the host's files.
        """
        cfg = cls.config(proxy_base)
        entry = json.dumps(cfg['providers']['task-router'], indent=2)
        return (
            'import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";\n\n'
            f'const ROUTER_PROVIDER = {entry};\n\n'
            'export default function (pi: ExtensionAPI) {\n'
            "  pi.registerProvider('task-router', ROUTER_PROVIDER);\n"
            '}\n'
        )

    @classmethod
    def rows_from_sessions(cls, sessions_dir=None):
        """T surface: delegate to the outcome importer (never duplicate it)."""
        sys.path.insert(0, os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))
        import router_outcomes as ro
        return ro.import_pi(sessions_dir or SESSIONS_DIR)

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
                'cost_usd': kw.get('cost_usd'),
                'wall_time_s': kw.get('wall_time_s'),
                'success': kw.get('success'), 'ts': kw.get('ts')}
