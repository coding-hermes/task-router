"""TR-362 — INV4 null-debt COMMIT gate: red/green acceptance evidence.

INV4 forbids a models.jsonl lane that is routable (not disabled, not
archived, no valid_to — data_null_census.is_live) and carries NO perf_*
evidence for ANY of the ten perf categories from being ADDED or CHANGED
without a stated reason: every perf field stamped for that (provider,
model) in null_reasons.jsonl with a reason from the AC2 REASONS
vocabulary, or disabled=true with a non-empty disabled_reason. A partial
stamp set is a violation naming the unstamped fields. Vision exemption:
a lane missing ONLY perf_e2e_vision accepts an unknown-by-design stamp
(the stamp rule; no code special-case).

Two modes over the same predicate:
  * whole-tree (default, no flags): count + report only, NEVER fails —
    ~1900 pre-existing perf-less lanes live in the committed table and
    the guard/CI suites run this mode (TR-201 kept its exact behavior).
  * --inv4-diff --base R1 --head R2: the COMMIT gate, wired into
    .githooks/pre-push (this repo has NO pre-commit hook, so pre-push is
    the commit gate). Fails (exit 1) naming provider, model and the
    missing perf fields; missing table on either side, or no added/
    changed lane, exits 0.

Hermetic: every test drives scripts/null_debt_invariants.py as a
subprocess against a SCRATCH fixture dir via ROUTING_DATA_DIR (the
pattern TR-005 pinned, reused by test_null_debt_invariants.py — stamps
resolve from <scratch>/../null_reasons.jsonl). Diff-mode tests build a
throwaway two-commit git repo under tmp_path with GIT_CONFIG_GLOBAL and
GIT_CONFIG_SYSTEM pinned to /dev/null (host git config — identity,
core.hooksPath — must not leak into scratch repos), never the real repo
history.
"""

import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "scripts", "null_debt_invariants.py")
PY = sys.executable

PERF_FIELDS = (
    "perf_agent_tick",
    "perf_long_doc",
    "perf_debug",
    "perf_schema",
    "perf_e2e_vision",
    "perf_review",
    "perf_delegation",
    "perf_guard",
    "perf_mock",
    "perf_reasoning",
)

# Scratch git repos must not see the host's global git config (identity,
# core.hooksPath). GIT_CONFIG_GLOBAL/SYSTEM=/dev/null pins a clean config.
GIT_ENV = dict(os.environ)
GIT_ENV["GIT_CONFIG_GLOBAL"] = "/dev/null"
GIT_ENV["GIT_CONFIG_SYSTEM"] = "/dev/null"


def _run(tables_dir, *args):
    env = {k: v for k, v in os.environ.items() if k != "ROUTING_DATA_DIR"}
    env["ROUTING_DATA_DIR"] = str(tables_dir)
    return subprocess.run(
        [PY, SCRIPT, *args],
        capture_output=True, text=True, timeout=120, env=env, cwd=REPO,
    )


def _run_diff(repo, base, head, tables_dir, *extra):
    return _run(tables_dir, "--inv4-diff", "--base", base, "--head", head,
                "--repo", str(repo), *extra)


def _row(prov="prov-a", model="m1", **kw):
    r = {"provider": prov, "model": model}
    r.update(kw)
    return r


def _write_models(tables_dir, rows):
    os.makedirs(str(tables_dir), exist_ok=True)
    with open(os.path.join(str(tables_dir), "models.jsonl"), "w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _write_stamps(tables_dir, entries):
    """entries: [(provider, model, field, reason)] -> null_reasons.jsonl
    beside the tables dir (the census STAMP_PATH rule: dirname(T))."""
    parent = os.path.dirname(str(tables_dir).rstrip("/"))
    p = os.path.join(parent, "null_reasons.jsonl")
    with open(p, "w") as f:
        for prov, mod, field, reason in entries:
            f.write(json.dumps({
                "provider": prov,
                "model": mod,
                "field": field,
                "reason": reason,
                "evidence": "TR-362 test stamp",
                "ts": "2026-11-05T00:00:00Z",
            }, ensure_ascii=False) + "\n")
    return p


def _git(*args, cwd):
    return subprocess.run(["git", *args], capture_output=True, text=True,
                          cwd=str(cwd), env=GIT_ENV, timeout=60)


def _scratch_repo(tmp_path, base_rows, head_rows):
    """Two-commit scratch git repo; returns (repo, base_sha, head_sha).
    data/tables/models.jsonl holds base_rows at base and head_rows at head."""
    repo = tmp_path / "repo"
    (repo / "data" / "tables").mkdir(parents=True)
    r = _git("init", "-q", cwd=repo)
    assert r.returncode == 0, r.stderr

    def _commit(rows, msg):
        with open(repo / "data" / "tables" / "models.jsonl", "w") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        r = _git("add", "data/tables/models.jsonl", cwd=repo)
        assert r.returncode == 0, r.stderr
        r = _git("-c", "user.email=tr362@invalid", "-c", "user.name=TR362 Test",
                 "commit", "-q", "-m", msg, cwd=repo)
        assert r.returncode == 0, r.stderr
        return _git("rev-parse", "HEAD", cwd=repo).stdout.strip()

    base_sha = _commit(base_rows, "base")
    head_sha = _commit(head_rows, "head")
    return repo, base_sha, head_sha


# --------------------------------------------------------------------------
# diff mode — the COMMIT gate (red/green acceptance evidence)
# --------------------------------------------------------------------------

def test_inv4_diff_red_perfless_added_lane_fails(tmp_path):
    repo, base, head = _scratch_repo(
        tmp_path,
        [_row("prov-a", "old-lane", perf_agent_tick=0.5)],
        [_row("prov-a", "old-lane", perf_agent_tick=0.5),
         _row("prov-a", "new-lane")],
    )
    r = _run_diff(repo, base, head, tmp_path / "tables")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "prov-a/new-lane" in r.stdout, r.stdout
    assert "added" in r.stdout, r.stdout
    assert "perf_agent_tick" in r.stdout, r.stdout  # names a perf field
    assert "FAIL: 1 routable perf-less lane" in r.stderr, r.stderr


def test_inv4_diff_red_changed_lane_still_perfless_fails(tmp_path):
    """A lane that EXISTS at base and is CHANGED in the range without
    gaining perf evidence is gated too (the gate is ADDED-or-CHANGED)."""
    repo, base, head = _scratch_repo(
        tmp_path,
        [_row("prov-a", "chg-lane", normalized_price=1.0)],
        [_row("prov-a", "chg-lane", normalized_price=2.0)],
    )
    r = _run_diff(repo, base, head, tmp_path / "tables")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "changed lane prov-a/chg-lane" in r.stdout, r.stdout


def test_inv4_diff_red_partial_stamp_set_fails(tmp_path):
    """A partial stamp set is a violation naming exactly the UNSTAMPED
    fields (the stamped three must not appear as missing)."""
    stamped = {"perf_agent_tick", "perf_long_doc", "perf_debug"}
    repo, base, head = _scratch_repo(
        tmp_path,
        [],
        [_row("prov-a", "stamped-lane")],
    )
    tables = tmp_path / "tables"
    _write_stamps(tables, [("prov-a", "stamped-lane", f, "no-sample-yet")
                           for f in sorted(stamped)])
    r = _run_diff(repo, base, head, tables, "--json")
    assert r.returncode == 1, r.stdout + r.stderr
    v = json.loads(r.stdout)["inv4"]["violations"]
    assert len(v) == 1, v
    assert v[0]["lane"] == "prov-a/stamped-lane"
    assert v[0]["missing_fields"] == [f for f in PERF_FIELDS
                                      if f not in stamped]


def test_inv4_diff_green_any_perf_evidence_passes(tmp_path):
    repo, base, head = _scratch_repo(
        tmp_path,
        [],
        [_row("prov-a", "new-lane", perf_agent_tick=0.6)],
    )
    r = _run_diff(repo, base, head, tmp_path / "tables")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "PASS" in r.stdout
    assert "new-lane" not in r.stdout  # never named when green


def test_inv4_diff_green_disabled_with_reason_passes(tmp_path):
    repo, base, head = _scratch_repo(
        tmp_path,
        [],
        [_row("prov-a", "new-lane", disabled=True,
              disabled_reason="no sample yet")],
    )
    r = _run_diff(repo, base, head, tmp_path / "tables")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "new-lane" not in r.stdout


def test_inv4_diff_green_all_ten_stamped_passes(tmp_path):
    """Every one of the ten perf fields stamped with an AC2 REASONS member
    (no-sample-yet is legitimate — the gate's job is that the reason is
    STATED, not inferred)."""
    repo, base, head = _scratch_repo(tmp_path, [], [_row("prov-a", "new-lane")])
    tables = tmp_path / "tables"
    _write_stamps(tables, [("prov-a", "new-lane", f, "no-sample-yet")
                           for f in PERF_FIELDS])
    r = _run_diff(repo, base, head, tables)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "new-lane" not in r.stdout


def test_inv4_diff_green_vision_only_unknown_by_design_passes(tmp_path):
    """Vision exemption: a lane whose ONLY missing capability evidence is
    perf_e2e_vision passes with that single field stamped
    unknown-by-design — the stamp rule, no code special-case."""
    vals = {f: 0.6 for f in PERF_FIELDS if f != "perf_e2e_vision"}
    repo, base, head = _scratch_repo(
        tmp_path, [],
        [_row("prov-a", "vision-lane", perf_e2e_vision=None, **vals)],
    )
    tables = tmp_path / "tables"
    _write_stamps(tables, [("prov-a", "vision-lane", "perf_e2e_vision",
                            "unknown-by-design")])
    r = _run_diff(repo, base, head, tables)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "vision-lane" not in r.stdout


def test_inv4_diff_green_unchanged_perfless_lane_ignored(tmp_path):
    """The committed tree already holds hundreds of perf-less lanes; the
    gate scopes to lanes the range ADDS or CHANGES. A perf-less lane
    byte-identical on both sides is not this push's debt."""
    same = _row("prov-a", "old-lane")
    repo, base, head = _scratch_repo(
        tmp_path,
        [same],
        [same, _row("prov-b", "ok-lane", perf_guard=0.7)],
    )
    r = _run_diff(repo, base, head, tmp_path / "tables")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "prov-a/old-lane" not in r.stdout


def test_inv4_diff_missing_table_both_sides_exits_zero(tmp_path):
    """models.jsonl absent on BOTH sides (or an empty range) -> exit 0."""
    repo = tmp_path / "repo"
    repo.mkdir()
    assert _git("init", "-q", cwd=repo).returncode == 0
    for msg in ("base", "head"):
        r = _git("-c", "user.email=tr362@invalid", "-c",
                 "user.name=TR362 Test", "commit", "--allow-empty",
                 "-q", "-m", msg, cwd=repo)
        assert r.returncode == 0, r.stderr
    base = _git("rev-parse", "HEAD~1", cwd=repo).stdout.strip()
    head = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    r = _run_diff(repo, base, head, tmp_path / "tables")
    assert r.returncode == 0, r.stdout + r.stderr


def test_inv4_diff_base_predates_table_still_gates_added_lanes(tmp_path):
    """Regression (TR-362): git reports a path missing at a ref but present
    on disk as "exists on disk, but not in '<ref>'" — that is still an
    absent base side, NOT an internal error. A base that predates
    models.jsonl with a perf-less lane added at head must FAIL (exit 1)."""
    repo2 = tmp_path / "repo2"
    (repo2 / "data" / "tables").mkdir(parents=True)
    assert _git("init", "-q", cwd=repo2).returncode == 0
    r = _git("-c", "user.email=tr362@invalid", "-c", "user.name=TR362 Test",
             "commit", "--allow-empty", "-q", "-m", "base-no-table", cwd=repo2)
    assert r.returncode == 0, r.stderr
    base2 = _git("rev-parse", "HEAD", cwd=repo2).stdout.strip()
    with open(repo2 / "data" / "tables" / "models.jsonl", "w") as f:
        f.write(json.dumps(_row("prov-a", "new-lane"), ensure_ascii=False) + "\n")
    assert _git("add", "data/tables/models.jsonl", cwd=repo2).returncode == 0
    r = _git("-c", "user.email=tr362@invalid", "-c", "user.name=TR362 Test",
             "commit", "-q", "-m", "add-perfless", cwd=repo2)
    assert r.returncode == 0, r.stderr
    head2 = _git("rev-parse", "HEAD", cwd=repo2).stdout.strip()
    r = _run_diff(repo2, base2, head2, tmp_path / "tables")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "prov-a/new-lane" in r.stdout, r.stdout


# --------------------------------------------------------------------------
# whole-tree mode — report-only, NEVER fatal (guard/CI run this mode)
# --------------------------------------------------------------------------

def test_inv4_whole_tree_never_fails_and_reports_count(tmp_path):
    tables = tmp_path / "tables"
    rows = [_row("prov-a", "lane-%d" % i) for i in range(5)]
    rows.append(_row("prov-a", "measured-lane", perf_agent_tick=0.5))
    _write_models(tables, rows)
    # default mode: exit 0 even with 5 violating lanes present
    r = _run(tables)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "REPORT-ONLY: 5 routable perf-less lane(s)" in r.stdout, r.stdout
    assert "measured-lane" not in r.stdout  # the evidenced lane is not counted
    # --json: inv4 is a new key; inv1/inv2/inv3 keys unchanged (sibling
    # consumers parse them)
    rj = _run(tables, "--json")
    assert rj.returncode == 0, rj.stdout + rj.stderr
    doc = json.loads(rj.stdout)
    assert set(doc) == {"inv1", "inv2", "inv3", "inv4"}
    for k in ("inv1", "inv2", "inv3"):
        assert doc[k]["violations"] == [], doc[k]
    v = doc["inv4"]["violations"]
    assert len(v) == 5, v
    assert v[0]["lane"] == "prov-a/lane-0"
    assert len(v[0]["missing_fields"]) == 10


def test_inv4_whole_tree_stamped_lanes_not_counted(tmp_path):
    """A fully-stamped perf-less lane drops out of the whole-tree count —
    the same escape the diff mode honors."""
    tables = tmp_path / "tables"
    _write_models(tables, [_row("prov-a", "stamped-lane")])
    _write_stamps(tables, [("prov-a", "stamped-lane", f, "no-sample-yet")
                           for f in PERF_FIELDS])
    r = _run(tables, "--json")
    assert r.returncode == 0, r.stdout + r.stderr
    assert json.loads(r.stdout)["inv4"]["violations"] == []


def test_inv4_diff_requires_base_and_head(tmp_path):
    r = _run(tmp_path / "tables", "--inv4-diff")
    assert r.returncode == 2, r.stdout + r.stderr
    assert "--base" in r.stderr and "--head" in r.stderr
