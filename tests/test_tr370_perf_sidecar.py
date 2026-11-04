"""TR-370 / TR-370a regression guards.

Two things this file exists to stop anyone undoing:

1. The seed must keep loading the 24-category perf sidecar. Building
   `model_perf` from the 10 legacy `perf_*` columns alone is what made 14 of
   the 24 categories unreachable through the seed path (measured: 284 perf rows,
   10 categories, 29 rankable models).

2. The bottom tier level must be anchored to the worst MEASURED performer.
   `quantile_cont(perf, 0.01)` interpolates ABOVE the minimum on small or
   near-unanimous samples, so every model at the modal value falls below level
   -5, `max(level)` returns NULL, and the measurement is silently discarded —
   which is exactly how 13 of 14 math measurements produced no tier.

These are source/derivation guards, not a re-run of the seed: the seed needs a
full DuckDB build and a live registry, which a unit test must not depend on.
"""
import json
import os
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
DATA = ROOT / 'data' / 'tables'
SEED = ROOT / 'scripts' / 'router_seed.py'

CATS = ['agent_tick', 'long_doc', 'debug', 'schema', 'e2e_vision', 'review', 'delegation',
        'guard', 'mock', 'reasoning', 'code_gen', 'refactor', 'terminal', 'mechanical',
        'test', 'math', 'tool_use', 'long_horizon', 'vision', 'ui_frontend', 'spec_docs',
        'creative', 'multilingual', 'security']


def test_sidecar_carries_every_category():
    """The perf sidecar must speak the full vocabulary the profiles use."""
    cats = set()
    with open(DATA / 'model_perf.jsonl') as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            cats.add(json.loads(line).get('category'))
    assert len(cats) == 24, f'sidecar carries {len(cats)} categories, expected 24: {sorted(cats)}'
    assert cats == set(CATS), f'vocabulary drift: {cats ^ set(CATS)}'


def test_seed_loads_the_sidecar_not_only_the_legacy_columns():
    """Reintroducing the 10-column-only UNPIVOT is the defect this pins down."""
    src = SEED.read_text()
    assert 'model_perf.jsonl' in src, (
        'router_seed.py no longer mentions model_perf.jsonl — the 24-category '
        'sidecar is not being loaded, so 14 categories are unreachable again')
    assert 'INSERT INTO model_perf' in src, (
        'router_seed.py does not INSERT the sidecar rows into model_perf')


def test_bottom_level_is_the_measured_minimum():
    """The q01 floor must be fused with min(perf), not left as the 1st percentile."""
    src = SEED.read_text()
    assert 'least(quantile_cont(perf, 0.01), min(perf))' in src, (
        'the bottom level is back to a bare quantile_cont(perf, 0.01) — on a '
        'near-unanimous sample that sits ABOVE the modal value, so every model '
        'at the mode gets no tier row and its measurement is discarded')


def test_legacy_columns_are_still_only_ten():
    """Documents WHY the sidecar is needed: the lane table cannot express 24."""
    with open(DATA / 'models.jsonl') as fh:
        first = json.loads(fh.readline())
    perf_cols = [k for k in first if k.startswith('perf_')]
    assert len(perf_cols) == 10, (
        f'models.jsonl now has {len(perf_cols)} perf_* columns; if the lane table '
        'gained the other 14 this test (and the sidecar rationale) needs revisiting')
