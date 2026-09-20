"""drivers/deepseek_harness.py — DeepSeek Harness (dsh), TR-075.

GROUNDED IN THE REPO, NOT THE SURVEY. The row's claim is confirmed against real
source in ~/deepseek-harness:

  * `packages/llm/llm-pi-ai/src/catalog.ts` declares `PiAiCompatProfile` with 34
    classified fields.
  * The archived Agent Note
    (`.agents/notes/archived/feature/2026-08-18-pi-ai-wire-compat-surface.md`)
    states the failure precisely: "For an endpoint its detection does not
    recognize, the answer is 'this is OpenAI itself': `detectCompat` returns
    `supportsDeveloperRole: true`, `maxTokensField: "max_completion_tokens"`,
    `supportsStore: true`. A hand-declared route is by construction an endpoint
    pi-ai does not ship, so every such route received OpenAI's own request shape."
  * `src/config.ts` documents the route-level `compat` field: "pi-ai
    wire-compatibility switches defaulting every model on this route whose
    protocol declares them".

So pointing dsh at the router IS a hand-declared route, and without a compat
block every reasoning model sends `role:developer`, `max_completion_tokens`, and
`store` — the OpenAI-own shape — to a gateway that is none of those things.

LIVE EVIDENCE ON THIS BOX: `~/.dsh/settings.yaml` declares two hand-declared
routes (ninerouter, 262 models total; openrouter) and NEITHER declares compat.
That is the defect this driver's config closes.

A driver states wire facts as DATA; it never implements the workaround
(SPEC-PROXY-DRIVERS §1).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from base import Driver, register  # noqa: E402

SETTINGS_PATH = '~/.dsh/settings.yaml'
SESSIONS_DIR = '~/.dsh/sessions'


@register
class DeepseekHarnessDriver(Driver):
    id = 'deepseek-harness'
    surfaces = ('W', 'T')
    #: the route declares `api: openai-completions` (see config-catalog)
    wire_format = 'openai-completions'
    config_path = SETTINGS_PATH
    base_url_shape = '/v1'
    #: The compat facts a router route must declare. The FIRST one is the row's
    #: CRITICAL wire fact; the other two come from the same archived note, which
    #: names `max_completion_tokens` as carrying "the same defect over a wider
    #: blast radius, since it shapes every request rather than only a reasoning
    #: model's" — so declaring only supportsDeveloperRole would be a half fix.
    compat = {'supports_developer_role': False,
              'max_tokens_field': 'max_tokens',
              'supports_store': False}

    @classmethod
    def config(cls, proxy_base='http://127.0.0.1:9092', route='router',
               model_ids=None):
        """The `llm-pi-ai` route block a user merges into settings.yaml.

        Shaped like the documented route form (README's `acme-gateway` example):
        displayName / apiKeyEnv / api / baseURL / compat / models. `apiKeyEnv` is
        a credential REFERENCE resolved through the harness credential seam, so
        no secret is written into the config (the harness's own rule).
        """
        return {
            'llm-pi-ai': {
                'providers': {
                    route: {
                        'displayName': 'task-router',
                        'apiKeyEnv': 'ROUTER_PROXY_KEY',
                        'api': cls.wire_format,
                        'baseURL': f'{proxy_base.rstrip("/")}{cls.base_url_shape}',
                        # the whole point of this driver: a hand-declared route
                        # otherwise inherits OpenAI's own request shape
                        'compat': {
                            'supportsDeveloperRole':
                                cls.compat['supports_developer_role'],
                            'maxTokensField': cls.compat['max_tokens_field'],
                            'supportsStore': cls.compat['supports_store'],
                        },
                        'defaultInput': ['text'],
                        'models': [{'id': m} for m in (model_ids or ['task-router'])],
                    },
                },
            },
        }

    @classmethod
    def rows_from_sessions(cls, sessions_dir=None):
        """T surface: read the harness session store into TR-049 rows."""
        return import_dsh_sessions(sessions_dir or SESSIONS_DIR)

    @classmethod
    def row_for(cls, **kw):
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


def import_dsh_sessions(sessions_dir='~/.dsh/sessions'):
    """TR-075 T surface: dsh session log -> TR-049 rows.

    The harness writes `<sessions>/<cwd-slug>/<session-id>/session.v<N>.jsonl`
    (this box: `session.v3.jsonl.zstd` — zstd-compressed JSONL, one canonical
    session event per line, per the session-format README). The version is
    renamed rather than overwritten when the format changes, and a reader must
    accept the newest version it understands rather than pinning one, so this
    picks the highest `session.v<N>.jsonl*` per session directory.

    Usage lives inside assistant/message events, which the session-telemetry
    README describes as carrying "its complete embedded compact stream,
    including failed and retried output" — so a session's tokens are read from
    its message events, and the reader counts genuinely-reported fields rather
    than inferring.

    Delegated to from the driver (never duplicated): two readers drift.
    """
    import glob
    import json as _json
    import re

    base = os.path.expanduser(sessions_dir)
    out = []
    # Two layout generations exist on real deployments: an unversioned
    # `session.jsonl[.zstd]` and a versioned `session.v<N>.jsonl[.zstd]`. A
    # reader pinned to one form silently reports zero rows for the other, so
    # both are globbed and the highest version wins per session directory.
    for sdir in sorted(glob.glob(os.path.join(base, '*', '*'))):
        if not os.path.isdir(sdir):
            continue
        cands = (glob.glob(os.path.join(sdir, 'session.v*.jsonl*')) +
                 glob.glob(os.path.join(sdir, 'session.jsonl*')))
        if not cands:
            continue

        def _ver(p):
            m = re.search(r'session\.v(\d+)\.jsonl', os.path.basename(p))
            return int(m.group(1)) if m else 0
        path = max(cands, key=_ver)
        try:
            lines = _read_lines(path)
        except Exception:  # noqa: BLE001 — a corrupt/unreadable store is not a crash
            continue
        if lines is None:
            continue

        sid = os.path.basename(sdir)
        first_ts = last_ts = None
        provider = model = None
        tin = tout = treason = tcread = 0
        cost = None
        turns = 0
        errors = 0
        saw_usage = False
        stop_reasons = []
        for raw in lines:
            raw = raw.strip()
            if not raw:
                continue
            try:
                d = _json.loads(raw)
            except ValueError:
                continue
            t = d.get('type') or ''
            if t == 'session':
                sid = d.get('id') or sid
                continue
            # Timestamps are epoch MILLISECONDS on `time`, not ISO on `timestamp`.
            tm = d.get('time')
            if isinstance(tm, (int, float)):
                e = float(tm) / 1000.0
                first_ts = first_ts if first_ts is not None else e
                last_ts = e
            if t != 'assistant/message':
                continue
            # Everything below is nested under `data` in the canonical log:
            #   data.usage                                   -> token counts
            #   data.message.source.{provider,model}         -> the lane
            #   data.message.source.replayState.response.stopReason
            # Verified against real session files rather than assumed; the
            # earlier guess (flat `message.usage`) read ZERO rows.
            data = d.get('data') or {}
            msg = data.get('message') or {}
            turns += 1
            src = msg.get('source') or {}
            provider = src.get('provider') or provider
            model = src.get('model') or model
            sr = ((src.get('replayState') or {}).get('response') or {}).get('stopReason')
            if isinstance(sr, str):
                stop_reasons.append(sr)
            u = data.get('usage')
            if isinstance(u, dict):
                saw_usage = True
                tin += int(u.get('inputTokens') or u.get('input_tokens') or 0)
                tout += int(u.get('outputTokens') or u.get('output_tokens') or 0)
                treason += int(u.get('reasoningTokens') or u.get('reasoning_tokens') or 0)
                tcread += int(u.get('cacheReadTokens') or u.get('cache_read_tokens') or 0)
                c = u.get('cost')
                if isinstance(c, dict) or isinstance(c, (int, float)):
                    cv = c.get('total') if isinstance(c, dict) else c
                    try:
                        cost = (cost or 0.0) + float(cv or 0)
                    except (TypeError, ValueError):
                        pass
            if msg.get('error') or d.get('error'):
                errors += 1

        if not saw_usage and not turns:
            continue          # nothing reported; never fabricate a row
        success = None
        if stop_reasons:
            success = not any(r == 'error' for r in stop_reasons)
        wall = (last_ts - first_ts) if (first_ts is not None and last_ts is not None) else None
        out.append({'source_system': 'deepseek-harness', 'session_id': sid,
                    'task_label': None, 'complexity': None, 'profile_id': None,
                    'required_categories': None,
                    'provider': provider, 'model': model,
                    'turns': turns, 'tokens_in': tin or None,
                    'tokens_out': tout or None,
                    'tokens_cache_read': tcread or None,
                    'tokens_reasoning': treason or None,
                    'cost_usd': cost,
                    'wall_time_s': wall, 'success': success, 'ts': last_ts})
    return out


def _read_lines(path):
    """Raw text lines from a plain or zstd-compressed session log.

    Prefers the `zstandard` module; falls back to the `zstd` CLI, which is
    present on this fleet. Returns None when the file cannot be decompressed, so
    the caller can SKIP the session rather than report a zero row for a session
    it never read — absence of data is not evidence of no usage.
    """
    if path.endswith('.zstd'):
        try:
            import zstandard
            with open(path, 'rb') as fh:
                dctx = zstandard.ZstdDecompressor()
                with dctx.stream_reader(fh) as r:
                    return r.read().decode('utf-8', errors='replace').splitlines()
        except ImportError:
            pass
        try:
            import subprocess
            r = subprocess.run(['zstd', '-dc', path], capture_output=True, timeout=60)
            if r.returncode != 0:
                return None
            return r.stdout.decode('utf-8', errors='replace').splitlines()
        except (OSError, subprocess.SubprocessError):
            return None
    with open(path, encoding='utf-8', errors='replace') as fh:
        return fh.read().splitlines()


def _iso(v):
    """ISO-8601 -> epoch seconds, or None."""
    import datetime
    if not isinstance(v, str) or not v:
        return None
    try:
        return datetime.datetime.fromisoformat(
            v.strip().replace('Z', '+00:00')).timestamp()
    except ValueError:
        return None
