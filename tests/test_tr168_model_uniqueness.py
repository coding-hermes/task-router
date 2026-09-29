"""TR-168 — (provider, model) uniqueness on the models table.

Origin: data/tables/models.jsonl carried 5 duplicate (provider, model) pairs
(four "payg-sticker duplicate of the offered tier row" tombstone twins from the
2026-08-28 id-fix, one byte-identical groq/qwen/qwen3.6-27b pair). A duplicate
row means TWO prices for one lane: the resolver picks whichever it meets first,
so chain order depends on file order — an accident, not a decision.

Rules locked here:
  1. The committed data/tables/models.jsonl has NO duplicate (provider, model)
     pair. A deduped survivor keeps the UNION of the twin's evidence (the
     better-evidenced row wins as the base; non-empty fields carry over;
     valid_from takes the max) and records the deleted twin's provenance in
     its `note` field ("deduped from twin row <n> on <date>").
  2. The survivor's ROUTABLE STATE is the better-evidenced row's: a dead
     tombstone twin ("duplicate of the offered tier row", id-fix 2026-08-28)
     never disables the live lane the id-fix deliberately kept. disabled /
     disabled_reason are therefore never carried across a fold.
  3. The writer path cannot re-introduce duplicates: router_seed.py dedupes
     the models table at LOAD time (before the duckdb build — one chokepoint
     the registry dump, the ns export and the data/tables tail-sync all read
     from). Measured before the fix: a full seed run carried all 5 twins
     through into the rewritten data/tables/models.jsonl.

Seed-driving tests run the REAL scripts/router_seed.py in a scratch dir
(ROUTING_REGISTRY / ROUTING_DATA_DIR / ROUTING_NS — same hermetic shape as
test_regression.py / test_seed_ns_guard.py); the script is top-level code, so
subprocess is the only honest wiring proof. No test touches the live registry
or the fleet mirror.
"""
import json
import os
import shutil
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(REPO, "data", "tables")
MODELS = os.path.join(DATA_DIR, "models.jsonl")
SEED = os.path.join(REPO, "scripts", "router_seed.py")
PY = sys.executable

try:
    import duckdb  # noqa: F401
    _HAS_DUCKDB = True
except ImportError:
    _HAS_DUCKDB = False

#: The 5 pairs TR-168 deduped: (provider, model, deleted twin's 1-based line
#: number in the pre-dedupe commit 1fcd9aa, whether the surviving lane must
#: stay ROUTABLE). The four live lanes were kept routable on purpose by the
#: 2026-08-28 id-fix ("payg-sticker duplicate of the offered tier row" is a
#: tombstone on the TWIN); groq/qwen/qwen3.6-27b is a retired lane (replaced_by
#: qwen3.8) whose twins were byte-identical, so the survivor stays disabled.
TR168_PAIRS = (
    ("groq", "openai/gpt-oss-120b", 815, True),
    ("groq", "openai/gpt-oss-20b", 817, True),
    ("groq", "qwen/qwen3.6-27b", 820, False),
    ("synthetic", "hf:Qwen/Qwen3.6-27B", 1525, True),
    ("synthetic", "hf:openai/gpt-oss-120b", 1531, True),
)
TWIN_LINES = [p[2] for p in TR168_PAIRS]
SURVIVOR_LINES = [p[2] - 1 for p in TR168_PAIRS]


def _rows(path=MODELS):
    out = []
    for n, line in enumerate(open(path, encoding="utf-8"), 1):
        if line.strip():
            out.append((n, json.loads(line)))
    return out


def _dup_pairs(path=MODELS):
    seen, dups = {}, {}
    for n, r in _rows(path):
        key = (r.get("provider"), r.get("model"))
        if key in seen:
            dups.setdefault(key, [seen[key]]).append(n)
        else:
            seen[key] = n
    return dups


def _run_seed(registry, data, ns):
    env = dict(os.environ, ROUTING_REGISTRY=registry, ROUTING_DATA_DIR=data,
               ROUTING_NS=ns)
    return subprocess.run([PY, SEED], capture_output=True, text=True,
                          env=env, timeout=600)


# ---------------------------------------------------------------------------
# 1. AC1: the committed data file has unique (provider, model) pairs.
# ---------------------------------------------------------------------------
def test_models_jsonl_provider_model_pairs_unique():
    dups = _dup_pairs()
    assert not dups, (
        "duplicate (provider, model) pairs in data/tables/models.jsonl — "
        "two prices for one lane, the resolver picks whichever it meets "
        f"first: {dups}")


def test_missing_provider_or_model_is_a_loud_failure():
    """A row without provider/model is not 'unique by accident' — name it."""
    bad = [(n, r) for n, r in _rows()
           if not r.get("provider") or not r.get("model")]
    assert not bad, f"models.jsonl rows missing provider/model: {bad[:5]}"


# ---------------------------------------------------------------------------
# 2. AC1: survivors kept the union + the twin's provenance in `note`.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("provider,model,twin_line,must_be_routable",
                         list(TR168_PAIRS))
def test_deduped_pair_keeps_union_row_with_twin_provenance(
        provider, model, twin_line, must_be_routable):
    hits = [r for _, r in _rows()
            if r.get("provider") == provider and r.get("model") == model]
    assert len(hits) == 1, f"{provider}/{model}: expected 1 survivor, got {len(hits)}"
    row = hits[0]
    note = row.get("note") or ""
    assert f"twin row {twin_line}" in note and "deduped" in note, (
        f"{provider}/{model}: survivor must record the deleted twin's "
        f"provenance in `note`, got {note!r}")
    # the survivor is the UNION row: prices + evidence + perf survived
    assert row.get("normalized_price") is not None, (
        f"{provider}/{model}: survivor lost its price")
    assert row.get("price_evidence"), f"{provider}/{model}: survivor lost its evidence"
    if must_be_routable:
        # rule 2: a tombstone twin never disables the live lane
        assert not row.get("disabled"), (
            f"{provider}/{model}: the live lane must stay routable — the twin's "
            f"disabled=True (a tombstone on the TWIN) must not be carried over")
        assert not row.get("disabled_reason")
    else:
        # the retired qwen3.6-27b pair was byte-identical tombstones: the
        # survivor keeps the retirement record (replaced_by qwen3.8)
        assert row.get("disabled"), "retired lane must keep its tombstone"
        assert row.get("replaced_by") == "groq/qwen/qwen3.8-27b"
        assert row.get("lifecycle_checked_at"), "retired lane lost its lifecycle record"


def test_only_the_five_twin_lines_were_removed_and_survivors_edited():
    """The dedupe touched ONLY the 5 fold sites: every other line of the file
    is byte-identical to the pre-dedupe commit (1fcd9aa), the 5 twin lines are
    gone, and exactly the 5 survivor lines differ (union + note)."""
    pre = subprocess.run(["git", "show", "1fcd9aa:data/tables/models.jsonl"],
                         capture_output=True, text=True, check=True,
                         cwd=REPO).stdout
    pre_lines = pre.splitlines()
    post_lines = open(MODELS, encoding="utf-8").read().splitlines()
    assert len(post_lines) == len(pre_lines) - 5, (
        f"expected exactly 5 lines removed, {len(pre_lines)} -> {len(post_lines)}")
    survivors_edited = []
    pi = 0  # pre-file cursor (0-based)
    for post in post_lines:
        while (pi + 1) in TWIN_LINES:  # deleted twin lines consume no post line
            pi += 1
        assert pi < len(pre_lines), "post file is longer than pre minus the twins"
        if (pi + 1) in SURVIVOR_LINES:
            survivors_edited.append(pi + 1)
        else:
            assert pre_lines[pi] == post, (
                f"line {pi + 1} must be byte-identical — only the 5 fold sites "
                f"may change: {pre_lines[pi][:100]!r} vs {post[:100]!r}")
        pi += 1
    while (pi + 1) in TWIN_LINES:
        pi += 1
    assert pi == len(pre_lines), "tail lines lost by the dedupe"
    assert sorted(survivors_edited) == SURVIVOR_LINES, (
        f"exactly the 5 survivor lines may differ, got {survivors_edited}")


# ---------------------------------------------------------------------------
# 3. AC3: the seed cannot re-introduce duplicates (persistence across reseed).
# ---------------------------------------------------------------------------
@pytest.mark.skipif(not _HAS_DUCKDB, reason="duckdb not importable")
def test_seed_roundtrip_keeps_the_committed_data_deduped(tmp_path):
    """Committed (deduped) data -> seed -> data/tables STILL unique, and the
    `note` provenance survives the registry round-trip (the tail-sync rewrites
    data/tables from the registry — the note column must ride along)."""
    reg = str(tmp_path / "registry.json")
    data = str(tmp_path / "data")
    shutil.copytree(DATA_DIR, data)
    p = _run_seed(reg, data, str(tmp_path / "ns"))
    assert p.returncode == 0, p.stderr[-2000:]
    out = os.path.join(data, "models.jsonl")
    dups = _dup_pairs(out)
    assert not dups, f"seed re-introduced duplicate pairs: {dups}"
    survivors = {(r.get("provider"), r.get("model")): r for _, r in _rows(out)}
    for provider, model, twin_line, _ in TR168_PAIRS:
        note = (survivors[(provider, model)].get("note") or "")
        assert f"twin row {twin_line}" in note, (
            f"{provider}/{model}: note provenance lost across the seed "
            f"round-trip, got {note!r}")


# ---------------------------------------------------------------------------
# 4. AC3: a file that ARRIVES with duplicates is deduped at seed LOAD time —
#    the registry and the data/tables tail-sync inherit the unique table
#    (one chokepoint; measured pre-fix: both surfaces kept the dups).
# ---------------------------------------------------------------------------
@pytest.mark.skipif(not _HAS_DUCKDB, reason="duckdb not importable")
def test_seed_dedupes_a_file_that_arrives_with_duplicates(tmp_path):
    data = tmp_path / "data"
    shutil.copytree(DATA_DIR, data)
    models_path = data / "models.jsonl"
    base_rows = [json.loads(l) for l in open(models_path, encoding="utf-8")
                 if l.strip()]
    base = dict(base_rows[0])
    assert not base.get("public_cache_read_per_m"), (
        "fixture premise: row 1 must not carry a cache-read price")
    assert (base.get("valid_from") or "") < "2029-12-31"
    key = (base["provider"], base["model"])
    # a twin of row 1 with two non-empty fields the base lacks: a price field
    # and a NEWER valid_from. The base row wins on evidence count; the twin's
    # two fields must carry into the survivor, and the survivor must record
    # the dedupe.
    twin = {"provider": key[0], "model": key[1],
            "public_cache_read_per_m": 0.5,
            "valid_from": "2029-12-31"}
    # and a byte-identical twin pair (the qwen3.6-27b shape): folds to one row
    twin2 = dict(base_rows[1])
    with open(models_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(twin, ensure_ascii=False) + "\n")
        f.write(json.dumps(twin2, ensure_ascii=False) + "\n")

    reg = str(tmp_path / "registry.json")
    p = _run_seed(reg, str(data), str(tmp_path / "ns"))
    assert p.returncode == 0, p.stderr[-2000:]

    out_path = os.path.join(str(data), "models.jsonl")
    dups = _dup_pairs(out_path)
    assert not dups, f"seed left duplicate pairs in data/tables: {list(dups)}"
    out = _rows(out_path)
    hits = [r for _, r in out if (r.get("provider"), r.get("model")) == key]
    assert len(hits) == 1, f"expected the union survivor, got {len(hits)}"
    survivor = hits[0]
    # more evidence wins as the base: the base row's own price is intact…
    assert survivor.get("normalized_price") == base.get("normalized_price")
    # …and the twin's non-empty extras carried in (the union)
    assert survivor.get("public_cache_read_per_m") == 0.5, (
        "union must carry the twin's non-empty fields the base lacks")
    assert survivor.get("valid_from") == "2029-12-31", (
        "valid_from must take the max (the newest admission date)")
    assert "twin row" in (survivor.get("note") or ""), (
        f"survivor must record the dedupe in `note`, got {survivor.get('note')!r}")
    # the registry (the other write surface) inherited the unique table too
    doc = json.load(open(reg))
    reg_hits = [r for r in (doc.get("tables") or {}).get("models") or []
                if (r.get("provider"), r.get("model")) == key]
    assert len(reg_hits) == 1, "registry kept the duplicate pair"
    assert reg_hits[0].get("public_cache_read_per_m") == 0.5, \
        "registry did not inherit the union row"
    assert "twin row" in (reg_hits[0].get("note") or "")
    # the byte-identical pair folded to one row, provenance naming the twin
    key2 = (base_rows[1].get("provider"), base_rows[1].get("model"))
    hits2 = [r for _, r in out if (r.get("provider"), r.get("model")) == key2]
    assert len(hits2) == 1, "byte-identical twin pair was not folded"
    assert "twin row" in (hits2[0].get("note") or ""), \
        "the fold must name the removed twin line"


# ---------------------------------------------------------------------------
# 5. the disabled-state rule as data: a routable survivor never inherits a
#    tombstone twin's disabled=True (rule 2, exercised through the real seed).
# ---------------------------------------------------------------------------
@pytest.mark.skipif(not _HAS_DUCKDB, reason="duckdb not importable")
def test_seed_union_never_disables_a_routable_survivor(tmp_path):
    data = tmp_path / "data"
    shutil.copytree(DATA_DIR, data)
    models_path = data / "models.jsonl"
    rows = [json.loads(l) for l in open(models_path, encoding="utf-8")
            if l.strip()]
    base = dict(rows[0])
    base.pop("disabled", None)
    base.pop("disabled_reason", None)
    # rewrite the file with the base row ACTIVE, then add its tombstone twin
    rows[0] = base
    twin = {"provider": base["provider"], "model": base["model"],
            "disabled": True, "disabled_reason": "tombstone twin (test)"}
    with open(models_path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
        f.write(json.dumps(twin, ensure_ascii=False) + "\n")

    reg = str(tmp_path / "registry.json")
    p = _run_seed(reg, str(data), str(tmp_path / "ns"))
    assert p.returncode == 0, p.stderr[-2000:]
    out = _rows(os.path.join(str(data), "models.jsonl"))
    key = (base["provider"], base["model"])
    hits = [r for _, r in out if (r.get("provider"), r.get("model")) == key]
    assert len(hits) == 1, f"expected 1 survivor, got {len(hits)}"
    assert not hits[0].get("disabled"), (
        "the active base row has more evidence than a 2-field tombstone — "
        "carrying the twin's disabled=True would kill the live lane")
    assert hits[0].get("disabled_reason") != "tombstone twin (test)", (
        "the tombstone's reason must not ride onto the live survivor")
