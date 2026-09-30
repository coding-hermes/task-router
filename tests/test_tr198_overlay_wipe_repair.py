"""TR-198: the 8 overlay-wiped live lanes stay repaired.

The pre-TR-180 overlay applier SET every column and NULLed everything a thin
overlay row did not name; 8 live lanes still carried that damage (28-33 of 39
fields NULL — visible in scripts/data_null_census.py's WIPED-SHAPE section).
The 2026-09-29 repair disabled each lane through the SANCTIONED channel (a
thin provenance-carrying row in data/lifecycle.jsonl, merged key-wise by the
seed) after live catalog checks proved NO honest price source exists for any
of them (upstream placeholders -1/-1, no pricing block, or TR-070 ambiguity /
prefix refusals — the evidence lives in each row's disabled_reason).

These tests pin that state:
  1. no target lane is ever again LIVE while carrying the wipe signature —
     re-enabling one requires the filled fields to arrive first;
  2. the disable rides the overlay channel WITH provenance (a hand-stamp in
     the generated table would be destroyed by the next reseed);
  3. every overlay row targets a lane that exists (a dropped twin would make
     the seed INSERT a ghost row).
"""
import json
import os

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODELS = os.path.join(REPO, 'data', 'tables', 'models.jsonl')
LIFECYCLE = os.path.join(REPO, 'data', 'lifecycle.jsonl')

LANES = {
    ('openrouter', 'typesafe/jev-router'),
    ('openrouter', 'openrouter/auto-beta'),
    ('commandcode', 'stealth/pixel-canary'),
    ('clinepass', 'qwen3.8-27b:free'),
    ('clinepass', 'nex-n2.5-pro:free'),
    ('clinepass', 'nex-n2.5-mini:free'),
    ('clinepass', 'ling-3.0-flash-vl:free'),
    ('clinepass', 'ling-3.0-flash-sante:free'),
}

#: a lane carrying MORE than this many null fields is in the overlay-wipe
#: shape (the repaired lanes carried 28-33; an honestly re-filled lane with
#: prices + context + capability flags + evidence lands well under 20 —
#: only the probe-awaiting perf_* fields stay NULL by design, TR-044).
WIPE_NULL_THRESHOLD = 20


def _model_rows():
    return [json.loads(l) for l in open(MODELS) if l.strip()]


def _lifecycle_rows():
    if not os.path.exists(LIFECYCLE):
        return []
    return [json.loads(l) for l in open(LIFECYCLE) if l.strip()]


def _is_live(row):
    return not (row.get('disabled') or row.get('archive') or row.get('valid_to'))


def test_overlay_wiped_lanes_are_never_again_live_and_wiped():
    wiped = []
    for r in _model_rows():
        if (r.get('provider'), r.get('model')) not in LANES:
            continue
        nulls = sum(1 for v in r.values() if v is None)
        if _is_live(r) and nulls >= WIPE_NULL_THRESHOLD:
            wiped.append(f"{r['provider']}/{r['model']} ({nulls} nulls of "
                         f"{len(r)} cols)")
    assert not wiped, (
        'overlay-wipe shape is LIVE again — re-enable only from a real source '
        f'(fill the fields via the importer, or keep the lane disabled): {wiped}')


def test_repair_rides_the_overlay_channel_with_provenance():
    by_lane = {}
    for r in _lifecycle_rows():
        by_lane.setdefault((r.get('provider'), r.get('model')), []).append(r)
    missing, unproven, dup = [], [], []
    for lane in sorted(LANES):
        rows = by_lane.get(lane)
        if not rows:
            missing.append(f'{lane[0]}/{lane[1]}')
            continue
        row = rows[0]
        if row.get('disabled') is not True or not row.get('disabled_reason'):
            unproven.append(f'{lane[0]}/{lane[1]} (no disabled stamp)')
        src = str(row.get('lifecycle_source') or '')
        if 'TR-198' not in src or not row.get('lifecycle_checked_at'):
            unproven.append(f'{lane[0]}/{lane[1]} (anonymous state — R4)')
    dups = {k for k, v in by_lane.items() if len(v) > 1}
    dup = sorted(dups & LANES)
    assert not missing, f'overlay rows gone — the next reseed un-disables: {missing}'
    assert not unproven, f'disable without provenance (R4): {unproven}'
    assert not dup, f'target lane has multiple overlay rows (idempotence broke): {dup}'


def test_every_overlay_target_exists_in_the_registry():
    lanes = {(r.get('provider'), r.get('model')) for r in _model_rows()}
    ghosts = sorted((r.get('provider'), r.get('model')) for r in _lifecycle_rows()
                    if (r.get('provider'), r.get('model')) not in lanes)
    assert not ghosts, (
        'lifecycle overlay targets a lane that no longer exists — the seed '
        f'would INSERT a ghost row: {ghosts}')
