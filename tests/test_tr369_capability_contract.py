"""TR-369 capability contract — one vocabulary across STORE + PUBLISH + ACCEPT.

Focused, fast, read-only tests over the committed data:

  - the seed's category list is all 24 categories and the models store schema
    carries every perf_* + cap_* column (parsed from the seed source via AST,
    never importing router_seed — importing it would RUN the seed);
  - models.jsonl rows carry the columns and live-lane nulls are stamped
    (null_reasons.jsonl, AC2 REASONS vocabulary);
  - GET /v1/models publishes an additive capabilities block per lane with
    context_limit and price for a routable lane, OpenAI keys byte-intact;
  - --profile-req accepts capability families (family=1), narrows the chain
    from the stored columns, and rejects unknown capabilities;
  - scripts/capability_census.py exits 0 (zero unexplained gaps).
"""
import ast
import json
import os
import subprocess
import sys
import tempfile

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")
TABLES = os.path.join(REPO, "data", "tables")
PY = sys.executable

# NOTE: never mutate ambient env at import/collection time. Importing this
# module happens BEFORE any test runs, so an os.environ assignment here leaks
# into every other module's tests (it did: test_cli_paths and test_quota_gate
# saw /tmp/tr369-state-* instead of their own tmp home in hosted CI).
# ROUTER_STATE_DIR is set per-subprocess where it is actually needed, below.
sys.path.insert(0, SCRIPTS)

import capability_vocab as cv  # noqa: E402


def _rows(name):
    with open(os.path.join(TABLES, name), encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


def _stamps():
    out = set()
    with open(os.path.join(REPO, "data", "null_reasons.jsonl"), encoding="utf-8") as f:
        for line in f:
            if line.strip():
                r = json.loads(line)
                out.add((r.get("provider"), r.get("model"), r.get("field")))
    return out


def _seed_models_columns():
    """BASE_COLUMNS['models'] column names, parsed from the seed source."""
    src = open(os.path.join(SCRIPTS, "router_seed.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "BASE_COLUMNS"
                        for t in node.targets)):
            return [name for name, _ty in ast.literal_eval(node.value)["models"]]
    raise AssertionError("BASE_COLUMNS not found in router_seed.py")


def test_seed_category_list_is_24_and_seed_schema_carries_it():
    assert len(cv.PERF_CATEGORIES) == 24
    assert len(set(cv.PERF_CATEGORIES)) == 24
    cols = _seed_models_columns()
    for cat in cv.PERF_CATEGORIES:
        assert f"perf_{cat}" in cols, f"seed BASE_COLUMNS lacks perf_{cat}"
    # and they appear exactly once (no accidental double-spellings)
    for cat in cv.PERF_CATEGORIES:
        assert cols.count(f"perf_{cat}") == 1
    # the six capability families are store columns too (same vocabulary)
    for fam in cv.CAPABILITY_FAMILIES:
        assert cv.family_column(fam) in cols
    # the UNPIVOT is generated from the shared vocabulary (single source)
    seed_src = open(os.path.join(SCRIPTS, "router_seed.py"), encoding="utf-8").read()
    assert "_cvocab.PERF_COLUMNS" in seed_src, (
        "seed UNPIVOT must build its column list from capability_vocab")


def test_models_table_has_all_perf_columns_and_live_nulls_stamped():
    rows = _rows("models.jsonl")
    assert rows
    stamps = _stamps()
    fields = list(cv.PERF_COLUMNS) + [cv.family_column(f) for f in cv.CAPABILITY_FAMILIES]
    evidence_any = 0
    live_null_unstamped = []
    for i, r in enumerate(rows):
        for f in fields:
            assert f in r, f"row {i + 1} lacks store column {f}"
        live = not r.get("archive") and not r.get("disabled") and not r.get("valid_to")
        if any(r.get(f) is not None for f in cv.PERF_COLUMNS):
            evidence_any += 1
        if live:
            for f in fields:
                if r.get(f) is None and (r.get("provider"), r.get("model"), f) not in stamps:
                    live_null_unstamped.append((r.get("provider"), r.get("model"), f))
    assert evidence_any, "no lane carries any perf evidence"
    assert not live_null_unstamped, (
        f"{len(live_null_unstamped)} live-lane capability null(s) unstamped, "
        f"e.g. {live_null_unstamped[:3]}")


def _stamps_use_reasons_vocabulary():
    import data_null_census as census
    with open(os.path.join(REPO, "data", "null_reasons.jsonl"), encoding="utf-8") as f:
        for line in f:
            if line.strip():
                assert json.loads(line).get("reason") in census.REASONS


def test_v1_models_publishes_capabilities_block():
    import router_server as srv
    payload = srv.openai_models_payload(data_dir=TABLES, max_age_s=0)
    lanes = payload["data"]
    assert lanes, "no routable lanes published"
    required = (["categories", "context_limit", "price", "price_basis",
                 "vision", "thinking", "modality", "tool_call"]
                + list(cv.CAPABILITY_FAMILIES))
    both = 0
    for lane in lanes:
        caps = lane.get("capabilities")
        assert isinstance(caps, dict), f"{lane.get('id')} lacks capabilities"
        for k in required:
            assert k in caps, f"{lane.get('id')} capabilities lacks {k}"
        for f in cv.CAPABILITY_FAMILIES:
            assert caps[f] in (True, False, None), (
                f"{lane.get('id')} {f} is {caps[f]!r} — not a three-state value")
        # the OpenAI-compatible contract stays byte-identical
        assert lane["object"] == "model"
        assert lane["created"] == 0
        assert isinstance(lane["id"], str) and isinstance(lane["owned_by"], str)
        if caps.get("context_limit") is not None and caps.get("price"):
            both += 1
    assert both, "no routable lane exposes context_limit AND price"
    example = next(l for l in lanes
                   if l["capabilities"].get("context_limit") is not None
                   and l["capabilities"].get("price"))
    assert example["capabilities"]["price_basis"]


def test_requirement_narrows_chain_and_unknown_rejected():
    import router_spawn as spawn
    tables = {name: _rows(f"{name}.jsonl")
              for name in ("category_levels", "level_defs", "models", "model_tier")}
    reqs, err = spawn._validate_adhoc(["image_generation=1"], tables)
    assert err is None and reqs == [("image_generation", 1)]
    _, err = spawn._validate_adhoc(["image_generation=3"], tables)
    assert err and err.get("code") == "INVALID_REQUIREMENT"
    _, err = spawn._validate_adhoc(["totally_unknown_capability=1"], tables)
    assert err and err.get("code") == "INVALID_REQUIREMENT"
    # narrowing from DATA: strict subset, every survivor evidentially True
    base = spawn._build_chain(tables, [])
    narrow = spawn._build_chain(tables, [("image_generation", 1)])
    assert 0 < len(narrow) < len(base), (
        f"image_generation=1 did not narrow: {len(base)} -> {len(narrow)}")
    assert all(m[5].get(cv.family_column("image_generation")) is True
               for m in narrow)
    # unresearched is NOT support: a family with no evidenced lanes empties
    # the chain rather than guessing
    empty = spawn._build_chain(tables, [("computer_use", 1)])
    assert empty == []


def test_capability_census_exits_zero():
    env = dict(os.environ)
    env.setdefault("ROUTER_STATE_DIR", tempfile.mkdtemp(prefix="tr369-state-"))
    p = subprocess.run([PY, os.path.join(SCRIPTS, "capability_census.py")],
                       capture_output=True, text=True, env=env, cwd=REPO,
                       timeout=300)
    assert p.returncode == 0, (
        f"census exited {p.returncode}\n{p.stdout[-2000:]}\n{p.stderr[-2000:]}")
    assert "OK: zero unexplained capability gaps" in p.stdout
    _stamps_use_reasons_vocabulary()
