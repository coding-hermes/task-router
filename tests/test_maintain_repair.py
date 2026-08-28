"""TR-028 regression battery — router_maintain repair (2026-08-28).

Four Sol-probe failures locked by this battery:
1. Repricing DISCARDED — `maintain all` repriced registry.json, then seed
   loaded committed data/tables FIRST and overwrote the registry (price
   0.0795 -> 9.999 -> back). Fix: reprice mirrors into data/tables so the
   seed consumes the repriced generation.
2. Env isolation broken — REGISTRY_DEFAULT defaulted to the already-overridden
   REGISTRY, so step_seed popped ROUTING_REGISTRY and the child seed resolved
   to the LIVE registry (scratch runs could write production state). Fix:
   step_seed always passes the resolved REGISTRY to the child.
3. Snapshot crash — router_maintain.py unpacked 5 values from _build_chain()
   which returns 6 (hop, provider, model, price, dclass, row). Fix: 6-value
   unpack + failure propagation (non-zero on snapshot errors).
4. router_clinepass --dry-run --commit still ran git add -A + commit + push.
   Fix: --dry-run mutually exclusive with --commit/--push; scoped git paths
   (never -A); --push implies --commit; repo identity + co-author trailer.

Doctrine: probes run in scratch dirs (ROUTING_REGISTRY/ROUTING_DATA_DIR/
ROUTING_NS/ROUTING_DOCS_DIR env overrides) — never against the live registry.
"""
import datetime
import hashlib
import json
import os
import shutil
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")
DATA_DIR = os.path.join(REPO, "data", "tables")
MAINTAIN = os.path.join(SCRIPTS, "router_maintain.py")
CLINEPASS = os.path.join(SCRIPTS, "router_clinepass.py")
PY = sys.executable

sys.path.insert(0, SCRIPTS)  # import router_clinepass (commit-scope tests)

# spot-check price that differs from the committed deepseek-v4-flash 0.0795 by
# far more than the 0.5% change threshold
NEW_PRICE = "0.1234"
NEW_OUT = "0.2468"

FAKE_SPOT_TMPL = (
    "#!/usr/bin/env python3\n"
    "# TR-028 fake or-family-spot-check: deterministic prices, no network.\n"
    "print('deepseek/deepseek-v4-flash | in=@@PRICE@@ out=@@OUT@@ cache=0.0617 "
    "| ctx=1000000 | overrides=N')\n"
    "print('TOTAL_MODELS: 1')\n"
)


def _write_fake_spot(path, price=NEW_PRICE, out=NEW_OUT):
    script = FAKE_SPOT_TMPL.replace("@@PRICE@@", price).replace("@@OUT@@", out)
    with open(path, "w") as f:
        f.write(script)
    os.chmod(path, 0o755)
    return path


def _sha(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


@pytest.fixture()
def scratch(tmp_path):
    """Hermetic scratch tree: data copy + registry copy + empty ns/docs dirs."""
    data = tmp_path / "data"
    shutil.copytree(DATA_DIR, data)
    reg = tmp_path / "registry.json"
    shutil.copyfile(os.path.join(REPO, "registry.json"), reg)
    ns = tmp_path / "routing-ns"
    (ns / "tables").mkdir(parents=True)
    tns = tmp_path / "taskrouter-ns"
    (tns / "tables").mkdir(parents=True)
    docs = tmp_path / "docs"
    docs.mkdir()
    spot = _write_fake_spot(str(tmp_path / "fake-spot.py"))
    env = dict(os.environ)
    env.update({
        "ROUTING_REGISTRY": str(reg),
        "ROUTING_DATA_DIR": str(data),
        "ROUTING_NS": str(ns),
        "TASKROUTER_NS": str(tns),
        "ROUTING_DOCS_DIR": str(docs),
        "ROUTING_SPOT_CHECK": spot,
        "ROUTING_BOARD_PY": PY,  # child seed uses the test interpreter (has duckdb)
    })
    return {"tmp": tmp_path, "data": data, "reg": reg, "ns": ns, "tns": tns,
            "docs": docs, "env": env}


def _run_maintain(env, *args):
    return subprocess.run([PY, MAINTAIN, *args], capture_output=True, text=True,
                          env=env, timeout=240)


def _reg_price(reg, provider="deepseek", model="deepseek-v4-flash"):
    doc = json.load(open(reg))
    for m in doc["tables"]["models"]:
        if m.get("provider") == provider and m.get("model") == model \
                and m.get("valid_to") is None and not m.get("archive"):
            return m.get("normalized_price")
    return None


# ====================================================== 1. reprice survival ==

def test_reprice_survives_seed_full_maintain_flow(scratch):
    """maintain all: repriced value must survive the seed (AC1).

    Before the fix the seed loaded committed data/tables first (stale price)
    and overwrote registry.json, reverting the reprice. The fake spot-check
    returns 0.1234 for deepseek/deepseek-v4-flash; after `all` BOTH the
    registry and data/tables must carry 0.1234.
    """
    p = _run_maintain(scratch["env"], "all")
    assert p.returncode == 0, p.stderr[-800:]
    assert _reg_price(scratch["reg"]) == float(NEW_PRICE), \
        "repriced value was reverted by the seed — survival broken"
    # the data file the seed consumes must also carry the repriced value
    data_rows = [json.loads(l) for l in open(scratch["data"] / "models.jsonl")
                 if l.strip()]
    ds = [r for r in data_rows if r.get("provider") == "deepseek"
          and r.get("model") == "deepseek-v4-flash"
          and r.get("valid_to") is None and not r.get("archive")]
    assert ds and ds[0]["normalized_price"] == float(NEW_PRICE), \
        "data/tables/models.jsonl was not reprice-mirrored — seed source stale"
    assert ds[0]["price_evidence"].startswith("or-spot-"), \
        "price_evidence not updated on the mirrored data row"


def test_reprice_survives_seed_standalone(scratch):
    """reprice step alone, then seed step: value survives across steps."""
    p = _run_maintain(scratch["env"], "reprice")
    assert p.returncode == 0, p.stderr[-800:]
    assert _reg_price(scratch["reg"]) == float(NEW_PRICE)
    p = _run_maintain(scratch["env"], "seed")
    assert p.returncode == 0, p.stderr[-800:]
    assert _reg_price(scratch["reg"]) == float(NEW_PRICE), \
        "price reverted after standalone seed"


# ====================================================== 2. env isolation =====

def test_seed_scratch_never_touches_live_registry(tmp_path):
    """ROUTING_REGISTRY honored end-to-end: scratch seed must not write the
    live registry nor the live data/tables (AC2). Before the fix step_seed
    popped ROUTING_REGISTRY (REGISTRY_DEFAULT trap) and the child seed wrote
    the LIVE registry.json."""
    live_reg = os.path.join(REPO, "registry.json")
    live_data_models = os.path.join(DATA_DIR, "models.jsonl")
    before_reg = _sha(live_reg)
    before_data = _sha(live_data_models)

    data = tmp_path / "data"
    shutil.copytree(DATA_DIR, data)
    reg = tmp_path / "registry.json"
    shutil.copyfile(live_reg, reg)
    ns = tmp_path / "ns"
    ns.mkdir()
    env = dict(os.environ)
    env.update({"ROUTING_REGISTRY": str(reg), "ROUTING_DATA_DIR": str(data),
                "ROUTING_NS": str(ns), "ROUTING_BOARD_PY": PY})

    p = _run_maintain(env, "seed")
    assert p.returncode == 0, p.stderr[-800:]
    # scratch registry was written
    assert os.path.exists(reg) and os.path.getsize(reg) > 1000
    # live store untouched
    assert _sha(live_reg) == before_reg, "LIVE registry.json was modified!"
    assert _sha(live_data_models) == before_data, "LIVE data/tables modified!"


# ====================================================== 3. snapshot ==========

def test_snapshot_builds_without_crash(scratch):
    """Snapshot generation fixed (6-value unpack) + writes chains md (AC3)."""
    p = _run_maintain(scratch["env"], "snapshot")
    assert p.returncode == 0, p.stderr[-800:]
    date = datetime.date.today().strftime("%Y-%m-%d")
    doc_chain = scratch["docs"] / f"chains-{date}.md"
    assert doc_chain.exists(), "docs chains file not written"
    text = doc_chain.read_text()
    assert text.startswith("# Chains snapshot"), "snapshot header missing"
    assert "## " in text, "no profile sections in snapshot"
    hop_lines = [ln for ln in text.splitlines() if ln.strip().startswith(("1.", "2.")) and "$" in ln]
    assert hop_lines, "no chain hop lines rendered"


def test_snapshot_failure_propagates(tmp_path):
    """Snapshot on a missing registry must exit non-zero (AC5: propagate)."""
    env = dict(os.environ)
    env.update({"ROUTING_REGISTRY": str(tmp_path / "absent.json"),
                "ROUTING_DATA_DIR": str(tmp_path / "nodata"),
                "ROUTING_DOCS_DIR": str(tmp_path / "docs"),
                "TASKROUTER_NS": str(tmp_path / "tns")})
    p = _run_maintain(env, "snapshot")
    assert p.returncode != 0, "snapshot must fail on missing registry (not exit 0)"


# ====================================================== 4. clinepass =========

def test_clinepass_dry_run_commit_mutually_exclusive(tmp_path):
    """--dry-run with --commit or --push is an argparse error (exit 2), and
    NO git call may run (AC4)."""
    for extra in ("--commit", "--push", "--commit --push"):
        p = subprocess.run([PY, CLINEPASS, "sync", "--dry-run"] + extra.split(),
                           capture_output=True, text=True, env=dict(os.environ),
                           timeout=30)
        assert p.returncode == 2, f"{extra}: expected argparse exit 2, got {p.returncode}"
        assert "mutually exclusive" in (p.stderr + p.stdout), \
            f"{extra}: no mutual-exclusion error"


def test_clinepass_source_never_adds_dash_A(tmp_path):
    """Regime lock: git add -A and the old author override are gone."""
    src = open(CLINEPASS).read()
    assert "git add -A" not in src, "git add -A must never appear"
    assert "'-A'" not in src and '"-A"' not in src, "bare -A pathspec must never appear"
    assert "--author" not in src, "repo identity override removed"


def test_clinepass_commit_scoped_to_sync_files(tmp_path):
    """commit_and_push stages ONLY the 4 owned data paths — a pre-staged
    unrelated file must never ride along (AC4)."""
    import router_clinepass
    repo = tmp_path / "gitrepo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
    tables = repo / "data" / "tables"
    tables.mkdir(parents=True)
    for fn in ("models.jsonl", "model_catalog.jsonl", "plan_terms.jsonl",
               "temporary_discounts.jsonl"):
        (tables / fn).write_text("{}\n")
    (tables / "models.jsonl").write_text('{"model": "a", "p": 1}\n')
    (repo / "unrelated.txt").write_text("dirty\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
    # now modify one owned file AND pre-stage an unrelated file
    (tables / "models.jsonl").write_text('{"model": "a", "p": 2}\n')
    (repo / "unrelated.txt").write_text("dirty2\n")
    subprocess.run(["git", "add", "unrelated.txt"], cwd=repo, check=True)

    old_repo = router_clinepass._REPO
    router_clinepass._REPO = str(repo)
    try:
        rc = router_clinepass.commit_and_push(do_push=False)
    finally:
        router_clinepass._REPO = old_repo
    assert rc == 0
    files = subprocess.run(["git", "show", "--name-only", "--format=", "HEAD"],
                           cwd=repo, capture_output=True, text=True, check=True)
    changed = [ln.strip() for ln in files.stdout.splitlines() if ln.strip()]
    assert "data/tables/models.jsonl" in changed
    assert "unrelated.txt" not in changed, "unrelated pre-staged file swept in!"
    body = subprocess.run(["git", "log", "-1", "--format=%B"], cwd=repo,
                          capture_output=True, text=True, check=True).stdout
    assert "Co-authored-by: Alexis Okuwa <wojonstech@gmail.com>" in body, \
        "co-author trailer missing"


def test_clinepass_commit_failure_propagates(tmp_path):
    """Git failure inside commit_and_push returns non-zero (AC5)."""
    import router_clinepass
    not_git = tmp_path / "not-a-repo"
    not_git.mkdir()
    old_repo = router_clinepass._REPO
    router_clinepass._REPO = str(not_git)
    try:
        rc = router_clinepass.commit_and_push(do_push=False)
    finally:
        router_clinepass._REPO = old_repo
    assert rc != 0, "commit must fail non-zero outside a git repo"


# ====================================================== 5. failure prop ======

def test_maintain_seed_failure_aborts_nonzero(tmp_path, scratch):
    """Seed failure must abort the maintain run with non-zero (AC5). The old
    code called sys.exit inside step_seed; the new code returns rc and the
    main loop aborts the remaining steps."""
    env = dict(scratch["env"])
    env["ROUTING_BOARD_PY"] = "/bin/false"  # child seed always fails
    p = _run_maintain(env, "seed")
    assert p.returncode != 0, "seed failure must exit non-zero"
    assert "ABORTING" in p.stderr or "FAILED" in p.stderr, p.stderr[-500:]
