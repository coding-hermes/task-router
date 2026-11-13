"""TR-350 — the routing-quality regression corpus.

The suite had ~2,371 tests proving the router does not CRASH and zero tests
proving it picks the RIGHT model: a change that routes a cheap satellite lane
onto an expensive head passed every test. This corpus pins the DECISIONS —
each case maps a task input (profile name + its capability tags, or an ad-hoc
requirement band) to the head the router MUST pick and the lanes that must /
must not be eligible, or to the exact refusal shape when nothing may serve.

Design (fixed corpus, not a property test):
  * Hermetic by construction — resolves run against a registry seeded ONCE per
    pytest session from the COMMITTED data/tables (conftest's
    seed_registry_copy), with a tmp policy plane (quota-state all-OPEN,
    empty health/circuit), absent outcome stats/ledger/demotions, and every
    behavior env knob scrubbed. No live fleet state is read or written.
  * Sort is pinned to 'price' (the documented ordering law: plan-tier bucket,
    effective price, PAYG last). The measured sort is evidence-driven and
    drifts by design; one case additionally proves the DEFAULT sort resolves
    to the same decision hermetically (stats absent -> documented degrade to
    price), so the corpus and production agree on what "the decision" is.
  * Every assertion names the case, the expected outcome and the actual one;
    the runner exits non-zero on any miss (pytest does this natively — the
    corpus adds a per-case named message so the miss is actionable).
  * Every printed rate names its denominator ("25/25 cases matched...", never
    "100%").

What each eligible case pins:
  head                the exact (provider, model) the router picks
  head_usd_1m         the public sticker price the scheduler sees for it
  must_include        named lanes that MUST be in the eligible chain
  must_exclude        named lanes that MUST NOT (they fail the capability bar)
  min_chain           floor on the eligible pool (collapse detector)
plus invariants asserted for every eligible case: the head independently
clears every declared capability tag from the data (model_tier), the chain
has no duplicate lanes, PAYG lanes sort after every plan lane (price sort),
and the resolve doc names the profile it actually resolved.

Refusal cases pin the honest no-eligibility shapes (TR-194/TR-176 doctrine):
the structured unservable-rating error, the fail-closed head-None-plus-
exclusions shape, the DEGRADED always-run serve (named, with
requirements_unmet), and the unknown-profile PROFILE_NOT_FOUND error.

BASELINE (live run, 2026-11-02, worktree wt/TR-350 @ 6b408eb, registry seeded
from that tree's committed data/tables): 25/25 cases matched their pinned
decisions. The pinned values below are that run's observed outcomes — a
future diff that changes a head or an eligibility set is a ROUTING REGRESSION
until a human re-pins the case consciously (the failure message names both
sides). Pools may GROW freely (min_chain is a floor); head changes and
must_exclude violations always fail.

INT-CI-20 (2026-11-05, TR-363 aftermath): profile/P4_SECURITY consciously
re-pinned to the DECLARED bars (-1/-1/2 per
data/tables/task_profile_requirements.jsonl) and their measured decision.
profile/P5_VISION_E2E and adhoc/visual-qa-band are measured-RED on this tree
and deliberately left unpinned: the new vision head
(opencode-go/deepseek-v4.1-flash) fails the independent check — vision>=1
but its model_tier row is tier=-1 (family alias) while models.jsonl declares
vision=true (a router-answer vs tier-data contradiction, not a stale pin) —
deepseek/deepseek-flash (now vision=true declared) entered both vision pools
across two must_exclude pins, and the visual-qa pool shrank 36 -> 31 lanes
(below its floor; lowering min_chain is not allowed). Reported to the
foreman; the data-plane residual row is theirs to file.

INT-CI-23 (2026-11-07, model-router-refresh): conscious re-pin wave after the
951-row tier landing (3a4af93, landed 00:23 AFTER the last seed) raised the
shared quantile scales. (a) deepseek/deepseek-flash left P2_AGENTIC and
adhoc/cron-band — its tool_use evidence (0.75 n=1 probe; 41/66 BFCL exact-match
third-party via sference) sits below the raised tool_use tier-0 cut (0.85);
(b) the adhoc/security-band head flipped to synthetic's cheaper
hf:zai-org/GLM-5.3-Flash after the 11-06 probe wave measured its guard 4/4;
(c) pools re-measured (P9 582->577, senior-coding 79->78) under the raised
scales; (d) the case-fold shadow fixes (stepfun/Step-3.7-Flash and
Qwen/Qwen3.8-Flash canonicalization) are data-plane, documented in
model_notes.jsonl.

TR-403 (2026-11-12, effort backfill) — SUPERSEDED by INT-CI-26, see below.
The seven pins it moved are reverted; the reasoning it recorded was sound for
the data it had, but that data was degenerate.

INT-CI-26 (2026-11-13, per-effort ladder population): TR-403's backfill wave
had measured only two lanes, so every quantile of every (category, effort)
collapsed onto a single value — the ladder read "perf >= 0.667 -> tier 5", every
measured lane scored 5 at that effort, and the effort rows claimed tiers their
perf did not support. Those inflated rows leaked into the effort-agnostic index
via max-merge, which is what made xkiro/nvidia/nemotron-3-nano-omni the cheapest
"qualifying" head at $0.056 and moved the seven cases below. The seed now reuses
the calibrated 'default' ladder for a non-default effort until MIN_LADDER_FAMILIES
(10) distinct families back it, and a row whose effort provenance is alias
inheritance (tier_source='family') is 'alias-proxy', never 'measured' — a variant
that never ran at that effort must not outrank a real measurement. With the
ladder honest, Nemotron no longer clears these bars and the golden head returns:
- profile/P0_FORE, profile/P2_AGENTIC, profile/P6_DEFAULT, profile/P8_SYNC,
  adhoc/senior-coding-band, adhoc/cron-band,
  adhoc/default-sort-same-decision-P8_SYNC:
  xkiro/nvidia/nemotron-3-nano-omni -> xkiro/openai/gpt-6-luna ($0.116).
adhoc/long-context-doc-band still resolves to Nemotron and is deliberately left
pinned there — the revert is per-case evidence, not a blanket rollback.

ch:trace row=TR-350 spec=board-row-TR-350 test=tests/test_routing_quality_corpus.py
"""
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'scripts'))
import router_spawn  # noqa: E402

# ---------------------------------------------------------------------------
# Hermetic env contract. Every knob here can change a routing decision, so the
# corpus pins them to their defaults (or to throwaway paths) for every case.
# Behavior vars are DELETED (module defaults rule); state paths are pointed at
# per-test throwaways so neither this worktree's runtime state nor the live
# fleet's (~/.hermes/model-router) can leak into a pinned decision.
# ---------------------------------------------------------------------------
SCRUB_ENV = (
    'ROUTER_HOP0', 'ROUTER_HOP0_MAX_DRY', 'ROUTER_HOP0_MIN_KNOWN',
    'ROUTER_OUTCOME_FLOOR', 'ROUTER_OUTCOME_FLOOR_RATE',
    'ROUTER_OUTCOME_FLOOR_MIN_KNOWN',
    'ROUTER_QUOTA_ORDER_GUARD',
    'ROUTER_SORT_EXPLORE_SHARE', 'ROUTER_SORT_EXPLORE_MIN_AGE_H',
    'ROUTER_SORT_MIN_SAMPLES', 'ROUTER_SORT_MIN_COVERAGE',
    'ROUTER_SORT_BLEND_WEIGHT', 'ROUTER_SORT_BLEND_CEIL',
    'ROUTER_SORT_COMPLETION_FLOOR',
    'ROUTER_DEMOTE_AFTER_FAILURES',
    'ROUTER_SPAWN_SORT', 'ROUTER_MISS_VERBOSE',
    'ROUTER_STATE_DIR', 'TASK_ROUTER_HOME',
    'ROUTING_REGISTRY', 'ROUTING_DATA_DIR',
)
STATE_ENV = ('ROUTING_AVERAGES_FILE', 'LEDGER_FILE',
             'ROUTING_DEMOTIONS_FILE', 'ROUTER_UNSERVABLE_FILE')


def _pairs(chain):
    """{(provider, model)} over a resolve chain."""
    return {(h.get('provider'), h.get('model')) for h in (chain or [])}


def _listify(value):
    """JSON-stable form of requirements_unmet (resolve returns tuples)."""
    try:
        return [list(u) for u in (value or [])]
    except TypeError:
        return value


def _stable(value):
    """Resolve docs mix tuples and dicts; normalize without splitting strs."""
    if isinstance(value, dict):
        return {str(k): v for k, v in value.items()}
    return _listify(value)


class CorpusRunner:
    """Resolves corpus cases against the hermetic registry + policy plane.

    One instance per test (the fixture builds it). Per-case table variants
    (fallback_lanes removed / every provider gated) are written per call and
    restored, so cases cannot leak state into each other.
    """

    def __init__(self, registry_path, state_dir, tables):
        self.registry_path = str(registry_path)
        self.state_dir = str(state_dir)
        self.tables = tables

    # -- per-case variants --------------------------------------------------
    def _variant_registry(self, tmp_path, drop_tables):
        doc = json.load(open(self.registry_path))
        for name in drop_tables:
            doc.get('tables', {}).pop(name, None)
        variant = Path(tmp_path) / 'registry-variant.json'
        variant.write_text(json.dumps(doc))
        old = router_spawn.REGISTRY
        router_spawn.REGISTRY = str(variant)
        return old

    def _gated_quota_state(self):
        path = Path(self.state_dir) / 'quota-state.json'
        old_text = path.read_text()
        doc = json.loads(old_text)
        provs = doc.get('providers') or {}
        doc['providers'] = {p: {'status': 'gated',
                                'reason': 'corpus: all-gated refusal case'}
                            for p in provs}
        path.write_text(json.dumps(doc))
        return old_text

    # -- the resolve --------------------------------------------------------
    def run(self, case, tmp_path):
        old_registry = None
        old_quota = None
        try:
            if case.get('no_fallback'):
                old_registry = self._variant_registry(tmp_path, ['fallback_lanes'])
            if case.get('gate_all'):
                old_quota = self._gated_quota_state()
            kwargs = {'use_health': True}
            sort = case.get('sort', 'price')
            if sort is not None:
                kwargs['sort'] = sort
            for opt in ('allow_slow', 'allow_training'):
                if case.get(opt) is not None:
                    kwargs[opt] = case[opt]
            inp = case['input']
            if 'adhoc' in inp:
                result = router_spawn.resolve(adhoc=list(inp['adhoc']), **kwargs)
            else:
                result = router_spawn.resolve(profile_id=inp['profile'], **kwargs)
        finally:
            if old_registry is not None:
                router_spawn.REGISTRY = old_registry
            if old_quota is not None:
                (Path(self.state_dir) / 'quota-state.json').write_text(old_quota)
        head = result.get('head') or None
        head_pair = ((head.get('provider'), head.get('model')) if head else None)
        return {
            'error': result.get('error'),
            'code': result.get('code'),
            'profile': result.get('profile'),
            'resolved_as': result.get('resolved_as'),
            'head': head_pair,
            'head_usd_1m': (head or {}).get('usd_1m'),
            'head_effective_price': (head or {}).get('effective_price'),
            'head_unmet': _listify((head or {}).get('requirements_unmet')),
            'head_context_limit': (head or {}).get('context_limit'),
            'chain_pairs': [tuple(p) for p in sorted(_pairs(result.get('chain')))],
            'chain_effective_prices': [(h.get('provider'), h.get('model'),
                                        h.get('effective_price'))
                                       for h in (result.get('chain') or [])],
            'chain_len': len(result.get('chain') or []),
            'exclusions_n': len(result.get('exclusions') or []),
            'exclusion_codes': sorted({c for e in (result.get('exclusions') or [])
                                       for c in (e.get('codes') or [])}),
            'gate_reasons': (result.get('gate_reasons')
                             if result.get('gate_reasons') is not None
                             else result.get('reasons')),
            'required_categories': _stable(result.get('required_categories')),
        }

    # -- data views used by the assertions ----------------------------------
    def profile_tags(self, pid):
        """{category: level} the registry DECLARES for a profile."""
        return {r.get('category'): r.get('level')
                for r in (self.tables.get('task_profile_requirements') or [])
                if r.get('task_id') == pid}

    def tier_index(self):
        tiers = {}
        for r in (self.tables.get('model_tier') or []):
            tiers.setdefault(r.get('model'), {})[r.get('category')] = r.get('tier')
        return tiers

    def payg_providers(self):
        return {p.get('id') for p in (self.tables.get('providers') or [])
                if str(p.get('plan') or '').upper() == 'PAYG'}


# ---------------------------------------------------------------------------
# THE CORPUS. Each case: id, input (profile or adhoc requirement band), the
# capability tags the input demands, and the expected decision. Expected
# values are the BASELINE RUN pinned in the module docstring.
# ---------------------------------------------------------------------------
CASES = [
    # --- the 11 declared profiles, exactly as task_profiles.jsonl names them ---
    {
        'id': 'profile/P0_FORE', 'kind': 'eligible',
        'input': {'profile': 'P0_FORE'},
        'capability_tags': {'agent_tick': 2, 'code_gen': -1, 'creative': -3,
                            'debug': -2, 'delegation': 2, 'e2e_vision': -2,
                            'guard': -3, 'long_doc': 0, 'math': -5,
                            'mock': -3, 'multilingual': -5, 'reasoning': 0,
                            'review': -1, 'schema': 1, 'terminal': -1,
                            'ui_frontend': -5, 'vision': -3},
        'expect': {
            # 2026-11-08 (XK-404-001-R3 archival): the dead xkiro :free head lane was
                # archived (catalog-absent + chat 404 on both seats, 44 lanes); the next
                # cheapest acceptable lane takes the head (09-18 golden-head law).
            # INT-CI-26 (2026-11-13): reverted to the golden head — TR-403's
            # effort re-pin rested on a degenerate ladder (see module docstring).
            'head': ['xkiro', 'openai/gpt-6-luna'],
            'head_usd_1m': 0.116,
            'must_include': [['commandcode', 'Qwen/Qwen3.8-27B'],
                             ['commandcode', 'google/gemini-3.8-flash']],
            'must_exclude': [['clinepass', 'dots-3-note-preview:free'],
                             ['deepseek', 'deepseek-flash']],
            'min_chain': 50,
        },
    },
    {
        'id': 'profile/P1_CODING', 'kind': 'eligible',
        'input': {'profile': 'P1_CODING'},
        'capability_tags': {'code_gen': 0, 'debug': 0, 'refactor': 0, 'test': 0},
        'expect': {
            # 2026-11-09 xkiro generation refresh: gpt-5.6-terra retired (last live
            # 5.6 lane); head is the cheapest refactor-clearing plan lane gpt-6-luna
            # (perf_refactor=0.5 family-inherited from the measured predecessor,
            # live-probe replacement pending).
            'head': ['xkiro', 'openai/gpt-6-luna'],
            'head_usd_1m': 0.116,
            'must_include': [['commandcode', 'moonshotai/Kimi-K2.7-Code'],
                             ['deepseek', 'deepseek-flash']],
            'must_exclude': [['clinepass', 'dots-3-note-preview:free']],
            'min_chain': 26,
        },
    },
    {
        'id': 'profile/P1_WORKER', 'kind': 'eligible',
        'input': {'profile': 'P1_WORKER'},
        'capability_tags': {'code_gen': -2, 'debug': -3, 'refactor': -5,
                            'test': 0},
        'expect': {
            # 2026-11-08 (XK-404-001-R3 archival): the dead xkiro :free head lane was
                # archived (catalog-absent + chat 404 on both seats, 44 lanes); the next
                # cheapest acceptable lane takes the head (09-18 golden-head law).
            'head': ['xkiro', 'cohere/command-r7b-12-2024'],
            'head_usd_1m': 0.0375,
            'must_include': [['commandcode', 'deepseek/deepseek-v4-pro'],
                             ['deepseek', 'deepseek-flash']],
            'must_exclude': [['clinepass', 'dots-3-note-preview:free']],
            'min_chain': 204,
        },
    },
    {
        'id': 'profile/P2_AGENTIC', 'kind': 'eligible',
        'input': {'profile': 'P2_AGENTIC'},
        'capability_tags': {'agent_tick': 0, 'delegation': 0,
                            'long_horizon': 1, 'tool_use': 0},
        'expect': {
            # 2026-11-08 (XK-404-001-R3 archival): the dead xkiro :free head lane was
                # archived (catalog-absent + chat 404 on both seats, 44 lanes); the next
                # cheapest acceptable lane takes the head (09-18 golden-head law).
            # INT-CI-26 (2026-11-13): reverted to the golden head — TR-403's
            # effort re-pin rested on a degenerate ladder (see module docstring).
            'head': ['xkiro', 'openai/gpt-6-luna'],
            'head_usd_1m': 0.116,
            'must_include': [['commandcode', 'meituan/LongCat-2.0']],
            'must_exclude': [['clinepass', 'dots-3-note-preview:free']],
            # 2026-11-08: floor 130->117 — XK-404-001-R3 archival removed the
            # dead xkiro :free lanes (+ mercury trio) from this pool (measured).
            # 2026-11-09: floor 117->115 — gpt-5.6-terra retirement removed the
            # xkiro + xkiro-2 5.6-terra lanes from this pool (measured).
            'min_chain': 115,
        },
    },
    {
        'id': 'profile/P3_DOCS', 'kind': 'eligible',
        'input': {'profile': 'P3_DOCS'},
        'capability_tags': {'long_doc': -1, 'review': -1, 'spec_docs': 0},
        'expect': {
            # 2026-11-08 (XK-404-001-R3 archival): the dead xkiro :free head lane was
                # archived (catalog-absent + chat 404 on both seats, 44 lanes); the next
                # cheapest acceptable lane takes the head (09-18 golden-head law).
            'head': ['xkiro', 'openai/gpt-6-luna'],
            'head_usd_1m': 0.116,
            'must_include': [['commandcode', 'Qwen/Qwen3.7-Plus'],
                             ['deepseek', 'deepseek-flash']],
            'must_exclude': [['clinepass', 'dots-3-note-preview:free']],
            'min_chain': 86,
        },
    },
    {
        'id': 'profile/P4_SECURITY', 'kind': 'eligible',
        'input': {'profile': 'P4_SECURITY'},
        # INT-CI-20 (TR-363): the DECLARED bars in
        # data/tables/task_profile_requirements.jsonl moved guard/review
        # 0 -> -1 (security stays 2); pinned to the committed values.
        'capability_tags': {'guard': -1, 'review': -1, 'security': 2},
        # security>=2 is still the hard bar; guard/review at -1 re-admit the
        # GLM-5.2 line through the blank -1 guard tier. Measured 2026-11-05
        # (INT-CI-20, this tree @ 4e04d68): the head moved to the cheapest
        # security>=2 lane, the $0.786-sticker neuralwatt flex lane
        # (effective 0.725), and the pool grew 4 -> 9 lanes (floor stays 4 —
        # pools may grow freely). deepseek-flash stays OUT: no security tier
        # row -> blank -1 < 2.
        'expect': {
            'head': ['neuralwatt', 'glm-5.2-short-fast-flex'],
            'head_usd_1m': 0.786,
            'must_include': [['commandcode', 'gpt-6.1-sol'],
                             ['commandcode', 'zai-org/GLM-5.2-Fast']],
            'must_exclude': [['deepseek', 'deepseek-flash'],
                             ['clinepass', 'dots-3-note-preview:free']],
            'min_chain': 4,
        },
    },
    {
        'id': 'profile/P5_VISION_E2E', 'kind': 'eligible',
        'input': {'profile': 'P5_VISION_E2E'},
        'capability_tags': {'creative': -3, 'debug': -2, 'e2e_vision': 1,
                            'long_doc': 0, 'reasoning': -1, 'terminal': 0,
                            'vision': 1},
        # vision+e2e_vision+terminal: 3 lanes clear it. The curated vision
        # FALLBACK lane stays fallback-only — it must never become ELIGIBLE.
        'expect': {
            'head': ['openrouter', 'moonshotai/kimi-k3'],
            'head_usd_1m': 3.48,
            'must_include': [['commandcode', 'moonshotai/Kimi-K3']],
            'must_exclude': [['deepseek-foreman', 'deepseek-v4-flash-vision-exp'],
                             ['deepseek', 'deepseek-flash']],
            'min_chain': 3,
        },
    },
    {
        'id': 'profile/P6_DEFAULT', 'kind': 'eligible',
        'input': {'profile': 'P6_DEFAULT'},
        'capability_tags': {'agent_tick': 0, 'code_gen': -2, 'delegation': 0,
                            'long_doc': -1, 'reasoning': -1, 'schema': -1,
                            'terminal': -1, 'tool_use': -1},
        'expect': {
            # 2026-11-08 (XK-404-001-R3 archival): the dead xkiro :free head lane was
                # archived (catalog-absent + chat 404 on both seats, 44 lanes); the next
                # cheapest acceptable lane takes the head (09-18 golden-head law).
            # INT-CI-26 (2026-11-13): reverted to the golden head — TR-403's
            # effort re-pin rested on a degenerate ladder (see module docstring).
            'head': ['xkiro', 'openai/gpt-6-luna'],
            'head_usd_1m': 0.116,
            'must_include': [['commandcode', 'meituan/LongCat-2.0'],
                             ['deepseek', 'deepseek-flash']],
            'must_exclude': [['clinepass', 'dots-3-note-preview:free']],
            'min_chain': 108,
        },
    },
    {
        'id': 'profile/P7_MOCK', 'kind': 'eligible',
        'input': {'profile': 'P7_MOCK'},
        'capability_tags': {'code_gen': -2, 'creative': -3, 'long_doc': -2,
                            'mechanical': 2, 'mock': -3, 'reasoning': -1},
        'expect': {
            'head': ['xkiro', 'openai/gpt-6-luna'],
            'head_usd_1m': 0.116,
            'must_include': [['commandcode', 'Qwen/Qwen3.8-27B'],
                             ['deepseek', 'deepseek-flash']],
            'must_exclude': [['clinepass', 'dots-3-note-preview:free']],
            # 2026-11-05: floor 91->90 — the five removed clinepass :free SKUs
            # left GET /models (3 dead at every form, 2 catalog-removed-
            # passthrough-alive); one mock-pool lane disabled.
            'min_chain': 90,
        },
    },
    {
        'id': 'profile/P8_SYNC', 'kind': 'eligible',
        'input': {'profile': 'P8_SYNC'},
        'capability_tags': {'agent_tick': 0, 'code_gen': -2, 'delegation': -1,
                            'long_doc': -1, 'reasoning': -1, 'schema': -1,
                            'terminal': -3, 'tool_use': -1},
        'expect': {
            # 2026-11-08 (XK-404-001-R3 archival): the dead xkiro :free head lane was
                # archived (catalog-absent + chat 404 on both seats, 44 lanes); the next
                # cheapest acceptable lane takes the head (09-18 golden-head law).
            # INT-CI-26 (2026-11-13): reverted to the golden head — TR-403's
            # effort re-pin rested on a degenerate ladder (see module docstring).
            'head': ['xkiro', 'openai/gpt-6-luna'],
            'head_usd_1m': 0.116,
            'must_include': [['commandcode', 'gpt-6-astra'],
                             ['deepseek', 'deepseek-flash']],
            'must_exclude': [['clinepass', 'dots-3-note-preview:free']],
            'min_chain': 110,
        },
    },
    {
        'id': 'profile/P9_REVIEW', 'kind': 'eligible',
        'input': {'profile': 'P9_REVIEW'},
        'capability_tags': {'code_gen': -2, 'creative': -3, 'e2e_vision': -2,
                            'mock': -3, 'reasoning': -1, 'review': -2,
                            'schema': -1, 'security': -1},
        'expect': {
            # 2026-11-08 (XK-404-001-R3 archival): the dead xkiro :free head lane was
                # archived (catalog-absent + chat 404 on both seats, 44 lanes); the next
                # cheapest acceptable lane takes the head (09-18 golden-head law).
            'head': ['xkiro', 'cohere/command-r7b-12-2024'],
            'head_usd_1m': 0.0375,
            'must_include': [['aws-bedrock', 'anthropic.claude-fable-5'],
                             ['deepseek', 'deepseek-flash']],
            'must_exclude': [['opencode-go', 'space-bunny-free'],
                             ['xkiro', 'qwen/qwen3.5-397b-a17b:free']],
            # 2026-11-03: floor 592->591 — the live openrouter hermes-3-llama row
            # left the pool when its vanished carrier id was archived (XK-404-001-R2).
            # 2026-11-06: floor 591->582 — XK-404-001-R3 wave archived 9 pool lanes
            # (xkiro/xkiro-2/openrouter minimax + nemotron :free; catalog-absent + chat-404).
            # 2026-11-07: floor 582->577 — the 951-row tier landing (3a4af93) raised
            # the shared quantile scales; five more lanes fall below the declared
            # P9 bars (measured, INT-CI-23 re-pin wave).
            # 2026-11-07 (same tick, gate ratchet): floor 577->569 — the 8 unmeasured
            # priced arrivals (6 openrouter :batch variants + 2 liquid lanes) were
            # taken out of service per the TR-362 usability ratchet (disabled +
            # 2026-11-08: floor 569->547 — XK-404-001-R3 archival removed the
            # dead xkiro :free lanes (+ mercury trio) from this pool (measured).
            # 2026-11-09: floor 547->545 — gpt-5.6-terra retirement removed the
            # xkiro + xkiro-2 5.6-terra lanes from this pool (measured).
            # disabled_reason; re-enable on measurement or the owner batch-lane
            # ruling); they had been pool members via the blank-default -1.
            'min_chain': 540,  # 545->541 2026-11-12: carrier-death archival wave (openrouter seed/xing4.0 lanes + TR-362 arrivals disabled) removed 4 live review-eligible lanes; 541->540 2026-11-13: TR-362 battery day-2 re-disabled the probe-alive unmeasured arrivals on TR-369 zero headroom (particle-1:free clinepass left the live review pool); per the 09-18 law the floor follows the fleet, dead lanes are never re-enabled to silence it,
        },
    },
    # --- ad-hoc capability bands (the classifier's output space) ------------
    {
        'id': 'adhoc/senior-coding-band', 'kind': 'eligible',
        'input': {'adhoc': ['code_gen=2', 'debug=1']},
        'capability_tags': {'code_gen': 2, 'debug': 1},
        'expect': {
            # 2026-11-08 (XK-404-001-R3 archival): the dead xkiro :free head lane was
                # archived (catalog-absent + chat 404 on both seats, 44 lanes); the next
                # cheapest acceptable lane takes the head (09-18 golden-head law).
            # INT-CI-26 (2026-11-13): reverted to the golden head — TR-403's
            # effort re-pin rested on a degenerate ladder (see module docstring).
            'head': ['xkiro', 'openai/gpt-6-luna'],
            'head_usd_1m': 0.116,
            'must_include': [['clinepass', 'qwen3.8-max'],
                             ['commandcode', 'Qwen/Qwen3.8-Max-0902']],
            'must_exclude': [['clinepass', 'dots-3-note-preview:free'],
                             ['deepseek', 'deepseek-flash']],
            # 2026-11-07: floor 79->78 — the 951-row tier landing raised the
            # code_gen/debug scales; one pool lane fell below the declared band
            # (measured, INT-CI-23 re-pin wave).
            # 2026-11-08: floor 78->70 — XK-404-001-R3 archival removed the dead
            # xkiro :free lanes (+ mercury trio) from this pool (measured).
            'min_chain': 70,
        },
    },
    {
        'id': 'adhoc/deep-reasoning-band', 'kind': 'eligible',
        'input': {'adhoc': ['reasoning=2']},
        'capability_tags': {'reasoning': 2},
        'expect': {
            # 2026-11-08 (XK-404-001-R3 archival): the dead xkiro :free head lane was
                # archived (catalog-absent + chat 404 on both seats, 44 lanes); the next
                # cheapest acceptable lane takes the head (09-18 golden-head law).
            'head': ['xkiro', 'cohere/command-r7b-12-2024'],
            'head_usd_1m': 0.0375,
            'must_include': [['commandcode', 'deepseek/deepseek-v4-pro'],
                             ['deepseek', 'deepseek-flash']],
            'must_exclude': [['clinepass', 'dots-3-note-preview:free']],
            'min_chain': 203,
        },
    },
    {
        'id': 'adhoc/visual-qa-band', 'kind': 'eligible',
        'input': {'adhoc': ['vision=1', 'e2e_vision=1']},
        'capability_tags': {'vision': 1, 'e2e_vision': 1},
        'expect': {
            'head': ['stepfun', 'step-3.7-flash'],
            'head_usd_1m': 0.222,
            'must_include': [['clinepass', 'kimi-k3'],
                             ['commandcode', 'stepfun/Step-3.7-Flash']],
            'must_exclude': [['clinepass', 'dots-3-note-preview:free'],
                             ['deepseek', 'deepseek-flash']],
            'min_chain': 36,
        },
    },
    {
        'id': 'adhoc/long-context-doc-band', 'kind': 'eligible',
        'input': {'adhoc': ['long_doc=0', 'min_context=200000']},
        'capability_tags': {'long_doc': 0, 'min_context': 200000},
        'expect': {
            # 2026-11-08 (XK-404-001-R3 archival): the dead xkiro :free head lane was
                # archived (catalog-absent + chat 404 on both seats, 44 lanes); the next
                # cheapest acceptable lane takes the head (09-18 golden-head law).
            'head': ['xkiro', 'nvidia/nemotron-3-nano-omni'],
            'head_usd_1m': 0.056,
            'head_context_min': 200000,
            'must_include': [['commandcode', 'MiniMaxAI/MiniMax-M3'],
                             ['deepseek', 'deepseek-flash']],
            'must_exclude': [['clinepass', 'dots-3-note-preview:free']],
            'min_chain': 173,
        },
    },
    {
        'id': 'adhoc/security-band', 'kind': 'eligible',
        'input': {'adhoc': ['security=2', 'guard=0']},
        'capability_tags': {'security': 2, 'guard': 0},
        # The 4-lane pool P4_SECURITY had BEFORE TR-363 moved its profile
        # bars to guard=-1/review=-1 (data/tables/task_profile_requirements.jsonl).
        # This adhoc band keeps guard=0, so the GLM-5.2 line stays out and the
        # $1.52 openrouter lane still heads the original 4-lane pool.
        # 2026-11-07 (INT-CI-23 re-pin): the head flipped to synthetic's cheaper
        # twin — the 11-06 probe wave landed hf:zai-org/GLM-5.3-Flash guard 4/4
        # (measured) where the openrouter glm-5.3 lane carries no guard evidence,
        # and the synthetic twin's public price ($0.164/M) undercuts openrouter ($1.52).
        'expect': {
            'head': ['synthetic', 'hf:zai-org/GLM-5.3-Flash'],
            'head_usd_1m': 0.164,
            'must_include': [['commandcode', 'gpt-6.1-sol']],
            'must_exclude': [['deepseek', 'deepseek-flash']],
            'min_chain': 4,
        },
    },
    {
        'id': 'adhoc/mechanical-band', 'kind': 'eligible',
        'input': {'adhoc': ['mechanical=2']},
        'capability_tags': {'mechanical': 2},
        'expect': {
            'head': ['xkiro', 'openai/gpt-6-luna'],
            'head_usd_1m': 0.116,
            'must_include': [['commandcode', 'Qwen/Qwen3.8-Max-0902'],
                             ['deepseek', 'deepseek-flash']],
            'must_exclude': [['clinepass', 'dots-3-note-preview:free']],
            # 2026-11-05: floor 108->107 — same clinepass :free removal wave
            # (5 SKUs left GET /models); one mechanical-pool lane disabled.
            'min_chain': 107,
        },
    },
    {
        'id': 'adhoc/docs-band', 'kind': 'eligible',
        'input': {'adhoc': ['spec_docs=0', 'review=-1']},
        'capability_tags': {'spec_docs': 0, 'review': -1},
        'expect': {
            # 2026-11-08 (XK-404-001-R3 archival): the dead xkiro :free head lane was
                # archived (catalog-absent + chat 404 on both seats, 44 lanes); the next
                # cheapest acceptable lane takes the head (09-18 golden-head law).
            'head': ['xkiro', 'openai/gpt-6-luna'],
            'head_usd_1m': 0.116,
            'must_include': [['commandcode', 'Qwen/Qwen3.8-Max-0902'],
                             ['deepseek', 'deepseek-flash']],
            'must_exclude': [['clinepass', 'dots-3-note-preview:free']],
            'min_chain': 103,
        },
    },
    {
        'id': 'adhoc/cron-band', 'kind': 'eligible',
        'input': {'adhoc': ['agent_tick=0', 'tool_use=0', 'delegation=0']},
        'capability_tags': {'agent_tick': 0, 'tool_use': 0, 'delegation': 0},
        'expect': {
            # 2026-11-08 (XK-404-001-R3 archival): the dead xkiro :free head lane was
                # archived (catalog-absent + chat 404 on both seats, 44 lanes); the next
                # cheapest acceptable lane takes the head (09-18 golden-head law).
            # INT-CI-26 (2026-11-13): reverted to the golden head — TR-403's
            # effort re-pin rested on a degenerate ladder (see module docstring).
            'head': ['xkiro', 'openai/gpt-6-luna'],
            'head_usd_1m': 0.116,
            # 2026-11-07 (INT-CI-23 re-pin): deepseek-flash left the pool — its
            # measured tool_use (0.75, n=1 probe; BFCL 41/66 third-party) sits
            # below the TR-363-raised tier-0 cut (0.85), so the lane is excluded
            # 2026-11-08: floor 153->137 — XK-404-001-R3 archival removed the
            # dead xkiro :free lanes (+ mercury trio) from this pool (measured).
            # from every tool_use>=0 band until a qualifying sample lands.
            'must_include': [['clinepass', 'deepseek-v4-flash']],
            'must_exclude': [['clinepass', 'dots-3-note-preview:free']],
            # 2026-11-07: floor 192->153 — the 951-row landing raised the shared
            # tool_use scale, so every lane whose tool_use tier fell below the
            # band's >=0 bar left the pool at once (measured, INT-CI-23 re-pin).
            # 2026-11-09: floor 137->135 — gpt-5.6-terra retirement removed the
            # xkiro + xkiro-2 5.6-terra lanes from this pool (measured).
            # 2026-11-08: floor 153->137 — XK-404-001-R3 archival removed the dead
            # xkiro :free lanes (+ mercury trio) from this pool (measured).
            'min_chain': 135,
        },
    },
    {
        'id': 'adhoc/floor-band-cheap-head', 'kind': 'eligible',
        'input': {'adhoc': ['code_gen=-5']},
        'capability_tags': {'code_gen': -5},
        # TR-161 doctrine pinned: "this prompt needs nothing special" resolves
        # CHEAP (a $0.0 free lane heads a 780-lane pool), never the priciest.
        'expect': {
            # 2026-11-08 (XK-404-001-R3 archival): the dead xkiro :free head lane was
                # archived (catalog-absent + chat 404 on both seats, 44 lanes); the next
                # cheapest acceptable lane takes the head (09-18 golden-head law).
            # 2026-11-09: floor 760->758 — gpt-5.6-terra retirement removed the
            # xkiro + xkiro-2 5.6-terra lanes from this pool (measured).
            # 2026-11-08: floor 769->760 — XK-404-001-R3 archival removed the
            # dead xkiro :free lanes (+ mercury trio) from this pool (measured).
            'head': ['xkiro', 'cohere/command-r7b-12-2024'],
            'head_usd_1m': 0.0375,
            'must_include': [['aws-bedrock', 'amazon.nova-micro-v1:0'],
                             ['deepseek', 'deepseek-flash']],
            'must_exclude': [],
            # 2026-11-03: floor 780->779 — vanished openrouter hermes-3 archived.
            # 2026-11-06: floor 779->769 — XK-404-001-R3 wave archived 10 pool lanes
            # (catalog-absent, chat-404 per id on both xkiro seats).
            # 2026-11-08: floor 769->760 — second XK-404-001-R3 wave (44 free lanes
            # + mercury trio, same-day chat 404s) removed 9 more pool lanes.
            'min_chain': 758,
        },
    },
    {
        'id': 'adhoc/default-sort-same-decision-P8_SYNC', 'kind': 'eligible',
        'input': {'profile': 'P8_SYNC'},
        'capability_tags': None,  # cross-checked against profile/P8_SYNC
        'sort': None,  # the resolve() default (measured) sort
        # Default-sort equivalence: with outcome stats absent (the hermetic
        # condition) the default sort must degrade to the price sort and land
        # on the SAME head and eligible set as profile/P8_SYNC above.
        'expect': {
            # 2026-11-08 (XK-404-001-R3 archival): the dead xkiro :free head lane was
                # archived (catalog-absent + chat 404 on both seats, 44 lanes); the next
                # cheapest acceptable lane takes the head (09-18 golden-head law).
            # INT-CI-26 (2026-11-13): reverted to the golden head — TR-403's
            # effort re-pin rested on a degenerate ladder (see module docstring).
            'head': ['xkiro', 'openai/gpt-6-luna'],
            'head_usd_1m': 0.116,
            'must_include': [['commandcode', 'gpt-6-astra'],
                             ['deepseek', 'deepseek-flash']],
            'must_exclude': [['clinepass', 'dots-3-note-preview:free']],
            'min_chain': 110,
        },
    },
    # --- refusals -----------------------------------------------------------
    {
        'id': 'refusal/unsatisfiable-matrix-no-fallback', 'kind': 'refusal',
        'shape': 'error',
        'no_fallback': True,
        'input': {'adhoc': ['math=5', 'security=5']},
        'capability_tags': {'math': 5, 'security': 5},
        # math>=5 and security>=5 have disjoint tier-5 holder sets in the
        # committed tables — no model clears both. With the always-run
        # fallback table removed, the ONLY honest answer is the structured
        # unservable-rating error (TR-194 EMPTY LADDER shape).
        'expect': {
            'error_contains': 'no chain',
            'code': None,
            'needs_reasons_list': True,
            'required_categories': {'math': 5, 'security': 5},
        },
    },
    {
        'id': 'refusal/all-providers-gated-P1_CODING', 'kind': 'refusal',
        'shape': 'fail_closed',
        'gate_all': True,
        'no_fallback': True,
        'input': {'profile': 'P1_CODING'},
        'capability_tags': {'code_gen': 0, 'debug': 0, 'refactor': 0,
                            'test': 0},
        # Eligible hops exist but every provider is policy-gated and the
        # fallback table is gone: the structured fail-closed SUCCESS shape
        # (head None + full named exclusion list), never a fabricated chain.
        'expect': {
            'head': None,
            'chain_empty': True,
            'min_exclusions': 1,
        },
    },
    {
        'id': 'refusal/unsatisfiable-matrix-degraded-serve', 'kind': 'degraded',
        'input': {'adhoc': ['math=5', 'security=5']},
        'capability_tags': {'math': 5, 'security': 5},
        # With the fallback table present, the unsatisfiable rating is served
        # DEGRADED on the always-run lane — visible requirements_unmet + the
        # FALLBACK reason on the record, never a silent mis-route.
        'expect': {
            # 2026-11-09: fallback order-1 retargeted gpt-5.6-terra (retired) -> gpt-6.1-terra
            'head': ['xkiro', 'openai/gpt-6.1-terra'],
            'head_unmet': [['math', 5, -1], ['security', 5, -1]],
        },
    },
    {
        'id': 'refusal/unknown-profile', 'kind': 'refusal',
        'shape': 'error',
        'input': {'profile': 'P404_NOT_A_PROFILE'},
        'capability_tags': {},
        'expect': {
            'error_contains': 'not in registry',
            'code': 'PROFILE_NOT_FOUND',
            'needs_reasons_list': False,
        },
    },
]

# Case ids whose expected decision is DEFINED AS EQUALITY with another case
# (the default-sort equivalence arm).
_EQUIVALENCE_PAIRS = {
    'adhoc/default-sort-same-decision-P8_SYNC': 'profile/P8_SYNC',
}


# ---------------------------------------------------------------------------
# pytest wiring
# ---------------------------------------------------------------------------
@pytest.fixture()
def corpus_env(tmp_path, monkeypatch, seed_registry_copy):
    """Hermetic policy plane + scrubbed env around the session-seeded registry.

    The policy gate is FAIL-CLOSED on absence (TR-203/TR-345), so quota-state
    must name every provider OPEN for a decision to be observable at all;
    health/circuit stay empty (nothing DOWN, nothing open). Outcome stats,
    spawn ledger, demotions and the unservable census point at per-test
    throwaways. This worktree may carry runtime state (data/state,
    ~/.hermes/model-router) — none of it is read.
    """
    for var in SCRUB_ENV:
        monkeypatch.delenv(var, raising=False)
    for var in STATE_ENV:
        monkeypatch.setenv(var, str(tmp_path / f'absent-{var.lower()}.jsonl'))

    registry = seed_registry_copy['ROUTING_REGISTRY']
    state = Path(seed_registry_copy['ROUTER_STATE_DIR'])
    state.mkdir(parents=True, exist_ok=True)
    tables = json.load(open(registry)).get('tables') or {}
    provs = ({r.get('id') for r in (tables.get('providers') or []) if r.get('id')}
             | {m.get('provider') for m in (tables.get('models') or [])
                if m.get('provider')})
    (state / 'quota-state.json').write_text(json.dumps(
        {'updated': 'tr350-corpus',
         'providers': {p: {'status': 'open'} for p in sorted(provs)}}))
    (state / 'health-state.json').write_text(json.dumps({'providers': {}}))
    (state / 'circuit-state.json').write_text(json.dumps({'pairs': {}}))

    monkeypatch.setattr(router_spawn, 'REGISTRY', registry)
    monkeypatch.setattr(router_spawn, 'MR', str(state))
    return CorpusRunner(registry, state, tables)


_RESULTS = []  # (case_id, passed: bool) — tallied by the summary test


def _ctx(case):
    inp = case['input']
    if 'adhoc' in inp:
        return 'adhoc reqs ' + json.dumps(inp['adhoc'], sort_keys=True)
    return f"profile {inp.get('profile')}"


def _named(case, what, expected, actual):
    """A failure message that names the case, the expectation and the fact."""
    return (f"[{case['id']}] {_ctx(case)} — {what}: "
            f"expected {expected!r}, actual {actual!r}")


def _assert_eligible(case, actual, runner, check_order=True):
    exp = case['expect']
    pid = case['input'].get('profile')

    # The resolve doc must name the profile it actually resolved.
    if pid is not None:
        assert actual['profile'] == pid, _named(
            case, 'resolved profile id', pid, actual['profile'])
        assert actual['resolved_as'] == 'profile-arg', _named(
            case, "resolved_as for a --profile resolve", 'profile-arg',
            actual['resolved_as'])
    else:
        assert actual['resolved_as'] == 'adhoc', _named(
            case, "resolved_as for an adhoc resolve", 'adhoc',
            actual['resolved_as'])
    assert not actual['error'], _named(case, 'no resolve error', None,
                                       actual['error'])
    assert actual['head'], _named(case, 'a chain head', '<(provider, model)>',
                                  None)

    # THE decision: the head.
    assert actual['head'] == tuple(exp['head']), _named(
        case, 'head routing decision', tuple(exp['head']), actual['head'])
    assert actual['head_usd_1m'] == exp['head_usd_1m'], _named(
        case, 'head public price (usd_1m)', exp['head_usd_1m'],
        actual['head_usd_1m'])

    # Independent capability check: the head must clear every declared tag,
    # verified from the DATA (model_tier), not from the router's own answer.
    if case.get('capability_tags'):
        tiers = runner.tier_index()
        head_model = actual['head'][1]
        own = tiers.get(head_model) or {}
        folded = {str(k).lower(): v for k, v in tiers.items()}.get(
            str(head_model).lower()) or {}
        misses = []
        for cat, lvl in sorted(case['capability_tags'].items()):
            if cat == 'min_context':
                got = actual['head_context_limit']
                if got is not None and got < lvl:
                    misses.append(f'{cat}>={lvl} (context_limit={got})')
                continue
            tier = own.get(cat, folded.get(cat))
            tier = -1 if tier is None else tier
            if tier < lvl:
                misses.append(f'{cat}>={lvl} (tier={tier})')
        assert not misses, _named(
            case, 'head clears every capability tag from model_tier data',
            'no misses', misses)

    # An explicit context floor on the case (min_context bands).
    if exp.get('head_context_min'):
        got = actual['head_context_limit']
        assert got is not None and got >= exp['head_context_min'], _named(
            case, f"head context window >= {exp['head_context_min']} tokens",
            f'>= {exp["head_context_min"]}', got)

    # The declared eligible set: named lanes that must / must not be present.
    present = set(actual['chain_pairs'])
    missing = [p for p in map(tuple, exp['must_include']) if p not in present]
    assert not missing, _named(
        case, 'required lanes present in the eligible chain',
        f"{len(exp['must_include'])}/{len(exp['must_include'])} named lanes in",
        f"{len(exp['must_include']) - len(missing)}/"
        f"{len(exp['must_include'])} — missing {missing}")
    barred = [p for p in map(tuple, exp['must_exclude']) if p in present]
    assert not barred, _named(
        case, 'barred lanes absent from the eligible chain',
        f"0/{len(exp['must_exclude'])} barred lanes in",
        f"{len(barred)}/{len(exp['must_exclude'])} — wrongly eligible: {barred}")

    # Pool-collapse detector.
    assert actual['chain_len'] >= exp['min_chain'], _named(
        case, 'eligible pool size floor', f'>= {exp["min_chain"]} lanes',
        f'{actual["chain_len"]} lanes')

    # Ordering laws, asserted on every eligible case (check_order=False for
    # the default-sort equivalence arm, whose chain ORDER is the measured
    # factory's degrade shape, not the price order — set + head equality is
    # that case's contract).
    pairs_list = [(p, m) for (p, m, _e) in actual['chain_effective_prices']]
    assert len(pairs_list) == len(set(pairs_list)), _named(
        case, 'no duplicate lanes in the chain', 'unique (provider, model)',
        f'{len(pairs_list) - len(set(pairs_list))} duplicates')
    if not check_order:
        return
    payg = runner.payg_providers()
    if payg and len(pairs_list) > 1:
        payg_idx = [i for i, (p, _m) in enumerate(pairs_list) if p in payg]
        plan_idx = [i for i, (p, _m) in enumerate(pairs_list) if p not in payg]
        if payg_idx and plan_idx:
            assert min(payg_idx) > max(plan_idx), _named(
                case, 'PAYG-LAST ordering law (TR-039)',
                'every PAYG lane after every plan lane',
                f'first PAYG lane at index {min(payg_idx)}, '
                f'last plan lane at index {max(plan_idx)}')


def _assert_refusal(case, actual):
    exp = case['expect']
    if exp.get('head') is not None or exp.get('chain_empty'):
        # The structured fail-closed shape: lanes WERE eligible, every one was
        # excluded — head None + the named exclusion list (TR-194 doctrine).
        assert actual['error'] is None, _named(
            case, 'fail-closed shape (no error key; head None + exclusions)',
            None, actual['error'])
        assert actual['head'] is None, _named(
            case, 'head after gating every provider', None, actual['head'])
        assert actual['chain_len'] == 0, _named(
            case, 'empty chain after gating every provider', 0,
            actual['chain_len'])
        assert actual['exclusions_n'] >= exp['min_exclusions'], _named(
            case, 'named exclusions', f'>= {exp["min_exclusions"]}',
            actual['exclusions_n'])
        assert actual['exclusion_codes'], _named(
            case, 'machine-readable exclusion codes', 'at least one code',
            'none')
        return
    # The structured error shape (unservable rating / unknown profile).
    assert actual['error'], _named(case, 'a refusal error doc', 'an error key',
                                   'no error — the router served a chain')
    assert exp['error_contains'] in str(actual['error']), _named(
        case, 'refusal error message names the cause',
        f"contains {exp['error_contains']!r}", str(actual['error']))
    assert actual['code'] == exp['code'], _named(
        case, 'refusal error code', exp['code'], actual['code'])
    assert actual['head'] is None, _named(
        case, 'no chain head on a refusal', None, actual['head'])
    if exp.get('needs_reasons_list'):
        assert isinstance(actual['gate_reasons'], list) or \
            actual['required_categories'] is not None, _named(
            case, 'structured reasons on the unservable-rating doc',
            'a reasons list or required_categories', 'neither')
    if exp.get('required_categories') is not None:
        assert actual['required_categories'] == exp['required_categories'], \
            _named(case, 'required_categories echoed on the unservable doc',
                   exp['required_categories'], actual['required_categories'])


def _assert_degraded(case, actual):
    exp = case['expect']
    assert not actual['error'], _named(case, 'degraded serve (no error key)',
                                       None, actual['error'])
    assert actual['head'] == tuple(exp['head']), _named(
        case, 'degraded always-run head', tuple(exp['head']), actual['head'])
    assert actual['head_unmet'] == exp['head_unmet'], _named(
        case, 'requirements_unmet named on the degraded head',
        exp['head_unmet'], actual['head_unmet'])
    assert any('FALLBACK' in str(r) for r in (actual['gate_reasons'] or [])), \
        _named(case, 'the static-fallback reason is on the record',
               'a FALLBACK gate_reason', (actual['gate_reasons'] or [])[:3])


@pytest.mark.parametrize('case', CASES, ids=[c['id'] for c in CASES])
def test_routing_decision_matches_pinned_corpus(case, corpus_env, tmp_path):
    """One pinned routing decision per case; any miss fails the run."""
    actual = corpus_env.run(case, tmp_path)
    ok = True
    try:
        if case['kind'] == 'eligible':
            if case['id'] in _EQUIVALENCE_PAIRS:
                other_id = _EQUIVALENCE_PAIRS[case['id']]
                other = next(c for c in CASES if c['id'] == other_id)
                reference = corpus_env.run(other, tmp_path)
                assert actual['head'] == reference['head'], _named(
                    case, 'default sort picks the same head as the price '
                          'sort (stats absent -> documented price degrade)',
                    reference['head'], actual['head'])
                assert set(actual['chain_pairs']) == \
                    set(reference['chain_pairs']), _named(
                    case, 'default sort yields the same eligible set as the '
                          'price sort',
                    f'{len(reference["chain_pairs"])} lanes',
                    f'{len(actual["chain_pairs"])} lanes')
                _assert_eligible(case, actual, corpus_env, check_order=False)
            else:
                _assert_eligible(case, actual, corpus_env)
        elif case['kind'] == 'refusal':
            _assert_refusal(case, actual)
        else:
            _assert_degraded(case, actual)
    except AssertionError:
        ok = False
        raise
    finally:
        _RESULTS.append((case['id'], ok))
        if ok:
            print(f"CORPUS PASS [{case['id']}] head={actual['head']} "
                  f"chain={actual['chain_len']} "
                  f"exclusions={actual['exclusions_n']}")


def test_corpus_summary_every_case_accounted():
    """Prints the pass rate WITH ITS DENOMINATOR and proves full accounting.

    Runs last (file order): by the time it executes, every corpus case has
    recorded an outcome. The printed rate always names the denominator —
    "25/25 cases matched", never "100%".
    """
    ran = {cid for cid, _ok in _RESULTS}
    wanted = {c['id'] for c in CASES}
    assert ran == wanted, (
        f'corpus accounting broken: {len(ran)}/{len(wanted)} case ids ran — '
        f'missing {sorted(wanted - ran)}, unexpected {sorted(ran - wanted)}')
    passed = sum(1 for _cid, ok in _RESULTS if ok)
    rate = (f'{passed}/{len(CASES)} corpus cases matched their pinned '
            f'routing decisions')
    print(f'ROUTING CORPUS SUMMARY: {rate} '
          f'(denominator = all {len(CASES)} pinned cases)')
    assert passed == len(CASES), (
        f'ROUTING CORPUS FAILED: {rate} — the misses above name the profile, '
        f'the expected decision and the actual one for every failed case')
