"""TR-142: selection evidence — one authority for the levels, and countable exclusions.

Two gaps this pins:

1. The levels a task must clear were derived in several independent places
   (profile_signature in router_outcomes, plus three separate reads of
   task_profile_requirements in router_spawn). A reference that disagrees with the
   thing it describes is worse than no reference, so there is now ONE function
   (required_levels) and profile_signature delegates to it.

2. The gate has always explained exclusions in PROSE ("health DOWN (...)",
   "model SLOW (7011ms)"). A human reads that fine; a program cannot bucket it, so
   "why is this lane not being used" could only be answered by regexing strings in
   someone's report. Every exclusion now carries reason CODES beside the prose.

Hermetic and worktree-safe: registry fixtures are written to tmp_path, so nothing
here depends on the generated registry.json.
"""
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'scripts'))
import router_outcomes as ro   # noqa: E402
import router_spawn as sp      # noqa: E402


@pytest.fixture()
def registry(tmp_path):
    """A registry with one profile, in the shape the seeder emits."""
    path = tmp_path / 'registry.json'
    path.write_text(json.dumps({'tables': {
        'task_profiles': [{'id': 'P1_CODING'}, {'id': 'P4_SECURITY'}],
        'task_profile_requirements': [
            {'task_id': 'P1_CODING', 'category': 'code_gen', 'level': 3},
            {'task_id': 'P1_CODING', 'category': 'test', 'level': 2},
            {'task_id': 'P1_CODING', 'category': 'debug', 'level': 1},
        ]}}))
    return str(path)


# ---------- the one authority ----------

def test_the_authority_reads_the_registry(registry):
    assert ro.required_levels(profile_id='P1_CODING', registry_path=registry) == {
        'code_gen': 3, 'test': 2, 'debug': 1}


def test_an_unknown_profile_refuses_to_invent_levels(registry):
    assert ro.required_levels(profile_id='NOPE', registry_path=registry) is None
    assert ro.required_levels() is None


def test_profile_signature_DELEGATES_to_the_authority(registry):
    """The drift this prevents: two derivations of the same reference."""
    assert (ro.profile_signature('P1_CODING', registry_path=registry)
            == ro.required_levels(profile_id='P1_CODING', registry_path=registry))


def test_every_matrix_shape_the_fleet_emits_normalizes_the_same():
    assert ro.required_levels(matrix={'code_gen': 3}) == {'code_gen': 3}
    assert ro.required_levels(matrix={'c=3', 'd=-2'}) == {'c': 3, 'd': -2}
    assert ro.required_levels(matrix=[{'category': 'a', 'level': 4}, 'b=1']) == {'a': 4, 'b': 1}
    assert ro.required_levels(matrix=[{'category': 'a', 'level': 'bad'}]) is None
    assert ro.required_levels(matrix={'a': True}) is None, 'a bool is not a level'


def test_a_supplied_matrix_wins_over_the_profile(registry):
    got = ro.required_levels(profile_id='P1_CODING', matrix={'code_gen': 5},
                             registry_path=registry)
    assert got['code_gen'] == 5, 'the live classification beats the declared profile'
    assert got['test'] == 2, 'and the profile still contributes what it knows'


def test_the_signature_is_stable_across_the_two_paths(registry):
    """complexity_sig(levels) is the bucket key; both paths must produce it."""
    levels = ro.required_levels(profile_id='P1_CODING', registry_path=registry)
    assert ro.complexity_sig(levels) == ro.complexity_sig(
        ro.profile_signature('P1_CODING', registry_path=registry))


# ---------- countable exclusions ----------

def test_every_prose_reason_the_gate_emits_maps_to_a_code():
    """The patterns below are the gate's own format strings."""
    cases = {
        'quota GATED: blocked': 'quota-gated',
        'health DOWN (2026-09-25T20:01:00+00:00)': 'health-down',
        'health SLOW (900ms)': 'health-slow',
        'model DOWN (2026-09-25T20:01:00+00:00)': 'model-down',
        'model SLOW (7011ms)': 'model-slow',
        'circuit OPEN until 2026-09-25T21:00:00+00:00 (3 failures)': 'circuit-open',
        'circuit OPEN (provider-level, api_down) until x': 'circuit-open',
        'model busy (3 in-flight >= limit 2)': 'model-busy',
        'training on prompts/completions (opt in with X)': 'training-optin',
        'consecutive cap 2': 'consecutive-cap',
        'chain cap 5': 'chain-cap',
    }
    for prose, expected in cases.items():
        codes = sp.exclusion_codes([prose])
        assert codes == [expected], f'{prose!r} -> {codes}'


def test_an_unmapped_reason_becomes_unknown_never_vanishes():
    """A cause that disappears is worse than one that is merely unclassified."""
    assert sp.exclusion_codes(['brand new gate nobody mapped']) == ['unknown']
    assert sp.exclusion_codes([]) == []
    assert sp.exclusion_codes(None) == []


def test_codes_are_parallel_to_the_prose_they_describe():
    why = ['health DOWN (t)', 'model SLOW (5ms)']
    codes = sp.exclusion_codes(why)
    assert len(codes) == len(why)


def test_every_code_is_declared_in_the_vocabulary():
    emitted = sp.exclusion_codes(
        ['health DOWN (t)', 'model SLOW (5ms)', 'quota GATED: x', 'circuit OPEN until y',
         'model busy (1 >= 1)', 'training on prompts/completions (opt in)',
         'consecutive cap 1', 'chain cap 1', 'something new'])
    assert set(emitted) <= set(sp.EXCLUSION_REASON_CODES), set(emitted) - set(sp.EXCLUSION_REASON_CODES)


def test_the_diversity_pruner_attaches_codes_to_its_exclusions():
    """The real construction site, not a re-implementation of it."""
    chain = [{'hop': 1, 'provider': 'p', 'model': 'm1'},
             {'hop': 2, 'provider': 'p', 'model': 'm2'},
             {'hop': 3, 'provider': 'p', 'model': 'm3'}]
    exclusions, reasons = [], []
    sp._prune_diversity(chain, exclusions, reasons, cons_cap=1, tot_cap=2)
    assert len(exclusions) == 2
    for ex in exclusions:
        assert 'why' in ex and 'codes' in ex
        assert ex['codes'] == sp.exclusion_codes(ex['why'])
    assert exclusions[0]['codes'] == ['consecutive-cap']
    assert all(c in sp.EXCLUSION_REASON_CODES for e in exclusions for c in e['codes'])


def test_the_pruner_is_unchanged_when_no_caps_are_set():
    chain = [{'hop': 1, 'provider': 'p', 'model': 'm1'}]
    exclusions, reasons = [], []
    sp._prune_diversity(chain, exclusions, reasons, cons_cap=None, tot_cap=None)
    assert exclusions == [] and reasons == []
