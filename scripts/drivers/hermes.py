"""drivers/hermes.py — Hermes Agent (the reference driver, TR-071).

Hermes is first because it is the cheapest: the telemetry reader ALREADY EXISTS
(router_outcomes.import_hermes, which maps state.db session_model_usage to
TR-049 rows), so this driver's job is the two things that were missing:

  W  make a hermes session a CLIENT of the proxy, and
  A  have the proxy ATTRIBUTE proxied attempts to hermes at ingest time
     rather than reconstructing them from state.db afterwards.

The T surface is not re-implemented here — `rows_from_state_db` delegates to the
existing importer. Duplicating it would create two readers that drift, which is
exactly the failure SPEC-PROXY-DRIVERS §1 exists to prevent.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from base import Driver, register  # noqa: E402

STATE_DB = os.environ.get('HERMES_STATE_DB', '~/.hermes/state.db')


@register
class HermesDriver(Driver):
    id = 'hermes'
    surfaces = ('W', 'A', 'T')
    #: hermes' gateway and providers speak OpenAI-shaped chat completions
    wire_format = 'openai-chat'
    config_path = '~/.hermes/config.yaml'
    base_url_shape = '/v1'

    @classmethod
    def config(cls, proxy_base='http://127.0.0.1:9092'):
        """A `config.yaml` provider entry that routes hermes through the proxy.

        Deliberately shaped like the EXISTING provider entries so it is additive
        (SPEC-INTEGRATION-PATHS §1: no Hermes-internal change, no fork).
        """
        return {
            'providers': {
                'router': {
                    'type': 'openai',
                    'base_url': f'{proxy_base.rstrip("/")}{cls.base_url_shape}',
                    # the proxy is entered with the caller's own key; the chain
                    # walk beyond it uses the router's keys, not this one
                    'api_key_env': 'ROUTER_PROXY_KEY',
                    'headers': {'x-router-caller': cls.caller_id()},
                },
            },
        }

    @classmethod
    def rows_from_state_db(cls, db_path=None):
        """T surface: delegate to the existing importer (never duplicate it)."""
        sys.path.insert(0, os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))
        import router_outcomes as ro
        return ro.import_hermes(db_path or STATE_DB)

    @classmethod
    def row_for(cls, **kw):
        """One outcome row in the TR-049 shape."""
        return {'source_system': cls.id, 'session_id': kw.get('session_id'),
                'task_label': kw.get('task_label'), 'complexity': kw.get('complexity'),
                'profile_id': kw.get('profile_id'),
                'required_categories': kw.get('required_categories'),
                'provider': kw.get('provider'), 'model': kw.get('model'),
                'turns': kw.get('turns'), 'tokens_in': kw.get('tokens_in'),
                'tokens_out': kw.get('tokens_out'),
                'tokens_reasoning': kw.get('tokens_reasoning'),
                'cost_usd': kw.get('cost_usd'), 'wall_time_s': kw.get('wall_time_s'),
                'success': kw.get('success'), 'ts': kw.get('ts')}
