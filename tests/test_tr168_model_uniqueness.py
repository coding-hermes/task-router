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


#: Lanes legitimately appended to models.jsonl AFTER the dedupe baseline
#: (1fcd9aa) and BEFORE this pin was relaxed to a subsequence walk. Each entry
#: must name its importer commit — an append that is not in this list (and not
#: added here with its evidence) fails the pin exactly like a lost lane.
POST_DEDUPE_ADDITIONS = {
    # TR-217 weekly MODEL lane 2026-09-29: router_modelsdev.py sync imports.
    # sonnet-5.5 live-verified (chat 200); the two daybreak ids are catalog
    # rows for the documented-gap model_notes (absent from the live listing).
    ("openrouter", "anthropic/claude-sonnet-5.5"): "TR-217 (7de3c9b)",
    ("openai-codex", "gpt-daybreak-blue-latest"): "TR-217 (7de3c9b)",
    ("openai-codex", "gpt-daybreak-red-latest"): "TR-217 (7de3c9b)",
    # TR-217 continuation 2026-09-29/30 (7de3c9b + 3a88c97): weekly MODEL lane
    # imports — gpt-6.1-sol across lanes + provider syncs. Each row's
    # price_evidence carries the import provenance (TR-199 rule).
    ("commandcode", "claude-sonnet-5-5"): "TR-217 (3a88c97)",
    ("commandcode", "deepseek/deepseek-v4.1-flash-fast"): "TR-217 (3a88c97)",
    ("commandcode", "gpt-6.1-sol"): "TR-217 (3a88c97)",
    ("commandcode", "inclusionai/ling-3.1-flash:free"): "TR-217 (3a88c97)",
    ("opencode-go-2", "longcat-2.5-preview-free"): "TR-217 (3a88c97)",
    ("openrouter", "anthropic/claude-sonnet-5.5:batch"): "TR-217 (3a88c97)",
    ("openrouter", "openai/gpt-6.1-sol"): "TR-217 (3a88c97)",
    ("openrouter", "openai/gpt-6.1-sol-pro"): "TR-217 (3a88c97)",
    ("openrouter", "openai/gpt-6.1-sol-pro:batch"): "TR-217 (3a88c97)",
    ("openrouter", "openai/gpt-6.1-sol:batch"): "TR-217 (3a88c97)",
    ("xkiro", "openai/gpt-6.1-sol"): "TR-217 (3a88c97)",
    ("xkiro-2", "openai/gpt-6.1-sol"): "TR-217 (3a88c97)",
    ("xkiro-2", "moonshotai/kimi-k2.7-code-highspeed"): "TR-217 (3a88c97)",
    ("xkiro-2", "moonshotai/kimi-k2.8-preview"): "TR-217 (3a88c97)",
    ("xkiro-2", "moonshotai/kimi-k3-256k"): "TR-217 (3a88c97)",
    # xkiro/xkiro-2 preset import (3a88c97): plan-covered free SKUs; each row's
    # price_evidence now carries the TR-070 window-cost-pending tag.
    # 2026-09-30 pre-run syncs: clinepass API +6 lanes (claude-sonnet-5.5
    # paid+batch, gpt-6.1-sol family) and models.dev +1 (openai-codex
    # gpt-6.1-sol, priced same day, research row 2026-09-29 DevDay $2/$10).
    ("clinepass", "claude-sonnet-5.5"): "TR-217 (2026-09-30 clinepass API sync)",
    ("clinepass", "claude-sonnet-5.5:batch"): "TR-217 (2026-09-30 clinepass API sync)",
    ("clinepass", "gpt-6.1-sol"): "TR-217 (2026-09-30 clinepass API sync)",
    ("clinepass", "gpt-6.1-sol-pro"): "TR-217 (2026-09-30 clinepass API sync)",
    ("clinepass", "gpt-6.1-sol-pro:batch"): "TR-217 (2026-09-30 clinepass API sync)",
    ("clinepass", "gpt-6.1-sol:batch"): "TR-217 (2026-09-30 clinepass API sync)",
    ("openai-codex", "gpt-6.1-sol"): "TR-217 (2026-09-30 models.dev sync)",
    # 2026-10-02 pre-run syncs: models.dev +2 (openrouter apodex :free +
    # pareto-26.10-preview, both paid-sticker priced except the :free twin)
    # and clinepass API +2 (apodex-1.1-mini:free discount row +
    # pareto-26.10-preview, the latter quality-disabled on import).
    ("clinepass", "apodex-1.1-mini:free"): "TR-217 (2026-10-02 clinepass API sync)",
    ("clinepass", "pareto-26.10-preview"): "TR-217 (2026-10-02 clinepass API sync)",
    ("openrouter", "apodex/apodex-1.1-mini:free"): "TR-217 (2026-10-02 models.dev sync)",
    ("openrouter", "unbiased/pareto-26.10-preview"): "TR-217 (2026-10-02 models.dev sync)",
    ("xkiro", "dots-studio/dots-3-note-preview:free"): "TR-217 (3a88c97)",
    ("xkiro", "inclusionai/ling-3.0-flash-sante:free"): "TR-217 (3a88c97)",
    ("xkiro", "liquid/lfm-2.5-2.6b:free"): "TR-217 (3a88c97)",
    ("xkiro", "meta/muse-spark-1.3-contributor:free"): "TR-217 (3a88c97)",
    ("xkiro", "stealth/pixel-canary:free"): "TR-217 (3a88c97)",
    ("xkiro", "stealth/space-bunny-alpha:free"): "TR-217 (3a88c97)",
    ("xkiro", "xiaomi/mimo-v2.6-flash:free"): "TR-217 (3a88c97)",
    ("xkiro-2", "dots-studio/dots-3-note-preview:free"): "TR-217 (3a88c97)",
    ("xkiro-2", "inclusionai/ling-3.0-flash-sante:free"): "TR-217 (3a88c97)",
    ("xkiro-2", "liquid/lfm-2.5-2.6b:free"): "TR-217 (3a88c97)",
    ("xkiro-2", "meta/muse-spark-1.3-contributor:free"): "TR-217 (3a88c97)",
    ("xkiro-2", "stealth/pixel-canary:free"): "TR-217 (3a88c97)",
    ("xkiro-2", "stealth/space-bunny-alpha:free"): "TR-217 (3a88c97)",
    ("xkiro-2", "xiaomi/mimo-v2.6-flash:free"): "TR-217 (3a88c97)",
    # 2026-10-03 pre-run syncs: models.dev +2 (openrouter inclusionai/
    # ling-3.1-flash $0 catalog-verified free launch 2026-10-02, synthetic
    # hf:zai-org/GLM-5.3 $1.4 sticker) and clinepass API +2 (ling-3.1-flash +
    # switchyard, both plan-swept PAYG-disabled at import). Note: the first
    # 10-03 seed silently dropped all 4 (lesson seed-silent-fallback-restore-
    # lanes); re-running the sync writers restored them verbatim before commit.
    ("clinepass", "ling-3.1-flash"): "TR-217 (2026-10-03 clinepass API sync)",
    ("clinepass", "switchyard"): "TR-217 (2026-10-03 clinepass API sync)",
    ("openrouter", "inclusionai/ling-3.1-flash"): "TR-217 (2026-10-03 models.dev sync)",
    ("synthetic", "hf:zai-org/GLM-5.3"): "TR-217 (2026-10-03 models.dev sync)",
    # 2026-10-05/06 pre-run models.dev sync: +1 (opencode-go/space-bunny,
    # catalog-only import; priced 0.168 sub-bucket blended est same run; the
    # stealth/space-bunny-alpha + pixel-canary mirror lanes were disabled
    # upstream-unpriceable 2026-10-06 — row EDITS do not affect this pin).
    ("opencode-go", "space-bunny"): "TR-217 (2026-10-05 models.dev sync)",
}


def test_only_the_five_twin_lines_were_removed_and_survivors_edited():
    """The dedupe touched ONLY the 5 fold sites — and later, ONLY the lanes
    named in POST_DEDUPE_ADDITIONS may have been appended: every pre-dedupe
    line of the file is lane-identical (same provider/model) to the pre-dedupe
    commit (1fcd9aa), the 5 twin lines are gone, and exactly the 5 survivor
    lines differ (union + note).

    2026-09-29: two post-dedupe evolutions broke the original byte-pin
    (date-rot, TR-135 class): (a) TR-199 ported dated `lifecycle_source`s onto
    post-dedupe rows (values legitimately evolve), and (b) the seed's
    tail-sync stamps schema columns (`note`, null) onto every rewritten row,
    which AC3's roundtrip test below REQUIRES to survive. A frozen-value pin
    against a historical snapshot can therefore not hold. What stays pinned:
    the line count (exactly the 5 twins removed), the lane SEQUENCE (every
    position keeps its provider/model — no accidental line replacement), and
    exactly the 5 survivor lines differing. Substance is pinned by the
    uniqueness test above and the per-pair union/provenance test below.

    2026-09-29 (TR-217, same day): the registry legitimately GROWS — the
    models.dev sync imports new lanes as dated rows (anthropic/
    claude-sonnet-5.5, gpt-daybreak-{blue,red}-latest). The sequence pin
    therefore becomes an ordered-subsequence pin: every pre-dedupe lane must
    still appear, in order, minus exactly the 5 twins, and the ONLY lanes
    allowed to be new are the ones named in POST_DEDUPE_ADDITIONS (an
    unexplained addition fails as loud as a lost lane)."""
    pre = subprocess.run(["git", "show", "1fcd9aa:data/tables/models.jsonl"],
                         capture_output=True, text=True, check=True,
                         cwd=REPO).stdout
    pre_lines = pre.splitlines()
    post_lines = open(MODELS, encoding="utf-8").read().splitlines()
    assert len(post_lines) == len(pre_lines) - 5 + len(POST_DEDUPE_ADDITIONS), (
        f"expected exactly 5 twins removed + the {len(POST_DEDUPE_ADDITIONS)} "
        f"pinned additions, {len(pre_lines)} -> {len(post_lines)}")
    survivors_edited = []
    pi = 0  # pre-file cursor (0-based)
    additions_seen = []
    for post in post_lines:
        post_row = json.loads(post)
        post_key = (post_row.get("provider"), post_row.get("model"))
        while (pi + 1) in TWIN_LINES:  # deleted twin lines consume no post line
            pi += 1
        if pi < len(pre_lines):
            pre_row = json.loads(pre_lines[pi])
            pre_key = (pre_row.get("provider"), pre_row.get("model"))
            if pre_key != post_key:
                # not the next pre lane: an APPEND (imported lane). Allow only
                # the pinned additions, at any position (the seed tail-sync
                # rewrites in key order, so imports can land mid-file).
                assert post_key in POST_DEDUPE_ADDITIONS, (
                    f"lane {post_key} is neither the expected next lane "
                    f"({pre_key}) nor a pinned POST_DEDUPE_ADDITIONS entry — "
                    f"unexplained models.jsonl change")
                additions_seen.append(post_key)
                continue
        else:
            # past the pre file's end: pure tail appends must also be pinned
            assert post_key in POST_DEDUPE_ADDITIONS, (
                f"lane {post_key} appended past the pre-dedupe baseline and "
                f"not in POST_DEDUPE_ADDITIONS — unexplained models.jsonl change")
            additions_seen.append(post_key)
        if (pi + 1) in SURVIVOR_LINES:
            survivors_edited.append(pi + 1)
        else:
            pre_row = json.loads(pre_lines[pi])
            # 2026-09-29: values legitimately evolve post-dedupe (TR-199
            # lifecycle ports, schema stamping on seed rewrite) — pin the
            # lane IDENTITY at every position instead of frozen values.
            assert (pre_row.get("provider"), pre_row.get("model")) == (
                post_row.get("provider"), post_row.get("model")), (
                f"line {pi + 1} must keep its lane identity "
                f"({pre_row.get('provider')}/{pre_row.get('model')} must not "
                f"be replaced): {pre_lines[pi][:100]!r} vs {post[:100]!r}")
        pi += 1
    while (pi + 1) in TWIN_LINES:
        pi += 1
    assert pi == len(pre_lines), "tail lines lost by the dedupe"
    assert sorted(survivors_edited) == SURVIVOR_LINES, (
        f"exactly the 5 survivor lines may differ, got {survivors_edited}")
    assert sorted(additions_seen) == sorted(POST_DEDUPE_ADDITIONS), (
        f"the pinned additions must each appear exactly once: expected "
        f"{sorted(POST_DEDUPE_ADDITIONS)}, saw {additions_seen}")


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
