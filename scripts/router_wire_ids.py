#!/usr/bin/env python3
"""router_wire_ids.py — TR-148: registry id -> the id the upstream actually serves.

The registry's ids stay BARE everywhere the router dedupes, ranks, records and
reports (TR-233 doctrine: probers keep the bare id as the state key). But the
id that goes out on the WIRE is per-provider data, not a guess:

  clinepass in-plan lanes serve 'modelType/model' ('cline-pass/glm-5.3') and
  answer every bare id with HTTP 400 'invalid model format. Expected format:
  modelType/model' (live control 2026-09-28, TR-233; model_catalog.api_id has
  carried the same form since 2026-08-27).

  clinepass ':free' lanes answer ONLY at the GET /models vendor-org id
  ('google/gemma-4-31b-it:free'); bare and cline-pass/<bare>:free forms answer
  400/404 (probe battery 2026-09-09: 18/18 listed ids probed 200).

  Individual lanes drift — 'deepseek-v4-flash' moved vendor -> cline-pass on
  2026-09-05 and back to the vendor form on 2026-09-27 (probe batteries).

So the transform is DATA-FIRST: data/tables/probe_fixes.jsonl carries a
verified fix per (provider, model); the LAST row in file order wins (the table
is append-only; some legacy rows carry date-only stamps, so never re-sort by
ts). Only when a lane has no verified fix does the provider default below
apply — itself table-derived, never hardcoded vendor guesses.

Slash-carrying registry ids are already wire-shaped and pass through
untouched; the transform is idempotent (an already-prefixed id never
double-prefixes). Consumers: the proxy dispatch hop (router_server.py) today;
the probers carry their own copy of the same rule via provider-specific
tables (TR-233) and may adopt this module later.

Stdlib only.
"""
import json
import os

# Provider default: the table-verified callable form for lanes with no
# verified probe_fixes row of their own (model_catalog.api_id since 08-27;
# probe_providers.jsonl note: 'ids served vendor-prefixed'). Only providers
# whose BARE registry ids are not what the upstream serves appear here —
# every other provider's id is already wire-shaped (verbatim).
PROVIDER_DEFAULT_PREFIX = {
    'clinepass': 'cline-pass/',
    'cline-pass': 'cline-pass/',  # both registry spellings, same provider
}

# probe_fixes.jsonl memoization; reset_cache() is the test seam. The cache
# key includes the table's (mtime_ns, size) so a LONG-RUNNING consumer (the
# :9391 proxy) picks up a freshly appended battery verdict on the next hop
# instead of serving a stale one until restart.
_FIXES_CACHE = None


def _fixes_path():
    repo = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    data_dir = os.environ.get('ROUTING_DATA_DIR', os.path.join(repo, 'data', 'tables'))
    return os.path.join(data_dir, 'probe_fixes.jsonl')


def _load_fixes():
    """{(provider, model): fix_to} from probe_fixes.jsonl, file order — the
    LAST row for a lane wins (append-only ledger; latest battery verdict is
    the live truth). Malformed lines and rows without the three needed fields
    are skipped, never fatal: a broken row must not take dispatch down.
    Memoized on the table's (mtime_ns, size): a LONG-RUNNING consumer (the
    :9391 proxy) picks up a freshly appended battery verdict on the next hop
    instead of serving a stale one until restart."""
    global _FIXES_CACHE
    try:
        st = os.stat(_fixes_path())
        stamp = (st.st_mtime_ns, st.st_size)
    except OSError:
        stamp = None  # missing table = defaults only; the transform never raises
    if _FIXES_CACHE is not None and _FIXES_CACHE[0] == stamp:
        return _FIXES_CACHE[1]
    fixes = {}
    if stamp is not None:
        try:
            with open(_fixes_path()) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    prov, model, fix_to = r.get('provider'), r.get('model'), r.get('fix_to')
                    if prov and model and fix_to:
                        fixes[(prov, model)] = fix_to
        except OSError:
            pass
    _FIXES_CACHE = (stamp, fixes)
    return fixes


def reset_cache():
    """Forget the memoized probe_fixes table (test seam / data reload)."""
    global _FIXES_CACHE
    _FIXES_CACHE = None


def wire_model_id(provider, model):
    """Registry id -> the id to send upstream for this hop.

    WIRE-ONLY: the bare registry id remains the vocabulary of attempts,
    outcome rows, breaker keys and envelopes (TR-233). Idempotent; a
    slash-carrying id is already modelType/model-shaped and passes through.
    """
    if not model or not provider:
        return model
    model = str(model)
    if '/' in model:
        return model  # already wire-shaped: never transform
    fix = _load_fixes().get((provider, model))
    if fix is not None:
        return fix
    prefix = PROVIDER_DEFAULT_PREFIX.get(provider)
    return prefix + model if prefix else model
