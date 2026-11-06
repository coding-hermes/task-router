#!/usr/bin/env python3
"""TR-369 capability contract — ONE vocabulary across STORE + PUBLISH + ACCEPT.

Every capability name the router stores, publishes or accepts as a requirement
is defined HERE, once. The three surfaces import these names; nothing
re-spells them:

  STORE    scripts/router_seed.py builds the models.jsonl / registry schema
           from PERF_CATEGORIES (the 24 text-quality task categories behind
           the perf_* columns and the model_perf UNPIVOT) and populates
           cap_<family> columns via derive_lane_capabilities().
  PUBLISH  scripts/router_server.py GET /v1/models emits a per-lane
           `capabilities` block keyed by these same names.
  ACCEPT   scripts/router_spawn.py --profile-req accepts a requirement naming
           a CAPABILITY_FAMILIES member and filters lane eligibility from the
           stored cap_<family> values.

Capability families are properties of the MODEL (weights), not the provider
lane — the same doctrine as model_perf evidence (Bane 2026-08-27). A lane
serving deepseek-v4-flash inherits the deepseek catalog facts regardless of
which provider sells it. Values are the honest three-state:

  True   evidence ON record (models.dev modality string / model name marker)
  False  evidence OF ABSENCE on record (a published modality string that
         does not carry the capability — absence inside a published modality
         is upstream's claim about the model)
  None   unresearched (no catalog row, no marker) — never guessed

Two families have NO modality evidence class: built-in web search and
computer use are product features, not I/O modalities, so a modality string
says nothing about them; they resolve only from explicit name markers and
stay None otherwise.

Stdlib only (the spawn/server runtime imports this module with no
dependencies installed).
"""
from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# STORE vocabulary
# ---------------------------------------------------------------------------

#: The 24 text-quality task categories — models.jsonl perf_* columns, the
#: seed's model_perf UNPIVOT source, and /v1/models capabilities.categories.
#: Order matches the seed's CATS tuple (registry column order).
PERF_CATEGORIES = (
    'agent_tick', 'long_doc', 'debug', 'schema', 'e2e_vision', 'review',
    'delegation', 'guard', 'mock', 'reasoning', 'code_gen', 'refactor',
    'terminal', 'mechanical', 'test', 'math', 'tool_use', 'long_horizon',
    'vision', 'ui_frontend', 'spec_docs', 'creative', 'multilingual',
    'security',
)

#: models.jsonl / registry column names for the categories above.
PERF_COLUMNS = tuple('perf_' + c for c in PERF_CATEGORIES)

# ---------------------------------------------------------------------------
# ACCEPT vocabulary — the six capability families (TR-369 GAP 3)
# ---------------------------------------------------------------------------

#: Non-tier capability families accepted as first-class --profile-req
#: requirements. Stored on models.jsonl as cap_<family> boolean columns
#: (None = unresearched), published on /v1/models under these exact names.
CAPABILITY_FAMILIES = (
    'image_generation',
    'embedding',
    'computer_use',
    'audio',
    'web_search',
    'video',
)


def family_column(family: Any) -> str:
    """The models.jsonl column carrying a capability family's value."""
    return 'cap_' + str(family)


# ---------------------------------------------------------------------------
# ACCEPT vocabulary — the flag capabilities (TR-363)
# ---------------------------------------------------------------------------

#: Boolean lane columns accepted as first-class requirement POINTERS
#: (TR-363): a profile requirement `name=1` resolves to the registry's own
#: stored column data instead of a tier-quantile lookup. Same three-state
#: contract as the families — True = evidenced, False = evidence of absence,
#: None = unresearched — and the same surfaces: STORED on models.jsonl,
#: PUBLISHED on /v1/models capabilities, ACCEPTED by --profile-req.
#: 'vision' is dual-natured: it is ALSO a tier category, so levels <= 0 keep
#: the historical tier semantics and only level >= 1 rides the flag.
FLAG_CAPABILITIES = ('vision', 'thinking')


#: Name markers -> family. A marker hit means the capability is ON record
#: (True) regardless of catalog rows. Substring match on the lane model name
#: and the catalog model/family names. Kept deliberately tight — 'search'
#: would false-positive 'nousresearch' and the relace code-search models
#: (measured 2026-11-05), so web_search matches ONLY 'sonar'.
FAMILY_NAME_MARKERS = {
    'embedding': ('embed',),
    'computer_use': ('computer',),
    'web_search': ('sonar',),
    'audio': ('tts', 'audio', 'speech', 'whisper', 'voice'),
    'video': ('video', 'veo', 'sora'),
    'image_generation': ('imagen', 'dall', 'flux'),
}


# ---------------------------------------------------------------------------
# Derivation — the single writer of cap_<family> store values
# ---------------------------------------------------------------------------

def _split_modality(row: Any) -> tuple:
    """(inputs, outputs|None) from a models.dev modality string.

    outputs is None when the string does not parse into the inputs->outputs
    shape — then no evidence-of-absence may be claimed from it.
    """
    mod = str((row or {}).get('modality') or '')
    if '->' in mod:
        ins, outs = mod.split('->', 1)
        return ([s.strip().lower() for s in ins.split('+')],
                [s.strip().lower() for s in outs.split('+')])
    return ([s.strip().lower() for s in mod.split('+')] if mod else [], None)


def _name_text(row: Any) -> str:
    """Marker-matching text for a catalog row (model + family)."""
    row = row or {}
    return (str(row.get('model') or '') + ' ' +
            str(row.get('family') or '')).lower()


def derive_lane_capabilities(model: Any, catalog_rows: Any) -> dict:
    """-> {family: True/False/None} for ONE lane.

    catalog_rows: every catalog row that describes this lane's MODEL — the
    exact (provider, model) hit and/or unique model-name matches (callers
    use catalog_candidates()). Multiple candidate rows are OR-aggregated:
    any True wins; False requires EVERY parsed candidate to claim absence.

    Name markers beat modality absence: 'sonar' in the name is evidence of
    web search even if a modality string exists.
    """
    out = {f: None for f in CAPABILITY_FAMILIES}
    lane_name = str(model or '').lower()
    for fam, pats in FAMILY_NAME_MARKERS.items():
        if any(p in lane_name for p in pats):
            out[fam] = True
    votes: dict = {}
    for row in (catalog_rows or []):
        ins, outs = _split_modality(row)
        text = _name_text(row)
        parsed = outs is not None
        votes.setdefault('audio', []).append(
            True if ('audio' in ins or 'audio' in (outs or ()))
            or any(p in text for p in FAMILY_NAME_MARKERS['audio'])
            else (False if parsed else None))
        votes.setdefault('video', []).append(
            True if ('video' in ins or 'video' in (outs or ()))
            or any(p in text for p in FAMILY_NAME_MARKERS['video'])
            else (False if parsed else None))
        votes.setdefault('image_generation', []).append(
            True if 'image' in (outs or ())
            or any(p in text for p in FAMILY_NAME_MARKERS['image_generation'])
            else (False if parsed else None))
        votes.setdefault('embedding', []).append(
            True if 'embedding' in (outs or ())
            or any(p in text for p in FAMILY_NAME_MARKERS['embedding'])
            else (False if parsed else None))
        votes.setdefault('web_search', []).append(
            True if any(p in text for p in FAMILY_NAME_MARKERS['web_search'])
            else None)
        votes.setdefault('computer_use', []).append(
            True if any(p in text for p in FAMILY_NAME_MARKERS['computer_use'])
            else None)
    for fam, vs in votes.items():
        if any(v is True for v in vs):
            out[fam] = True
        elif out[fam] is None and vs and all(v is False for v in vs):
            out[fam] = False
    return out


def catalog_candidates(catalog_rows: Any) -> Any:
    """Lookup for a lane's candidate catalog rows (call it with (provider, model)).

    Exact (provider, model) first; otherwise every catalog row whose model
    name matches UNIQUELY across providers (capability evidence describes the
    model — a lane inherits it regardless of the selling provider, same
    doctrine as model_perf). An ambiguous name (two distinct catalog models)
    matches nothing: guessing between candidates would fabricate evidence.
    """
    exact: dict = {}
    by_name: dict = {}
    for row in (catalog_rows or []):
        exact[(row.get('provider'), row.get('model'))] = row
        by_name.setdefault(str(row.get('model') or '').lower(), []).append(row)

    def lookup(provider: Any, model: Any):
        hit = exact.get((provider, model))
        if hit is not None:
            return [hit]
        cands = by_name.get(str(model or '').lower(), [])
        if len({str(r.get('model') or '').lower() for r in cands}) == 1:
            return list(cands)
        return []  # ambiguous or absent — unresearched, never guessed

    return lookup
