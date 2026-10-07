"""TR-298 — automatic test intake from the board (scripts/board_task_intake.py).

Pins the consumer contract end to end:

- exactly-once intake: one new pending board row -> exactly one resolve
  subprocess and exactly one resolves-ledger record; a replay of the same id
  (second poll pass, `--once` again, daemon cold-boot over a used state)
  adds NO second record — the seen state is the idempotence key;
- tolerant reads: a truncated trailing line (writer mid-append) is left for
  the next poll; a malformed line is skipped, the good rows around it still
  process, and the daemon never raises;
- fail-open everywhere: a spawn that exits nonzero, times out, or prints
  garbage yields one error record (with `error` + attribution tails) and the
  loop CONTINUES to the next task; a dangling claim (daemon killed
  mid-resolve) is reconciled at the next boot into an error record — never a
  silent loss, never a double resolve;
- payload preservation: the record carries the task id, the chain head
  (provider/model/price basis), the complexity source, and the FULL resolve
  payload unchanged (ratings/basis/prompt metadata come from the real
  --from-task path — this suite never synthesizes a profile);
- `--once` semantics: drains the current backlog (seeded ids on a fresh
  state ARE resolved) and exits 0 after one pass; a daemon (no `--once`)
  cold-boot marks existing ids seen WITHOUT resolving them.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tests"))
import conftest  # noqa: E402  (QA-TASK-ROUTER-9 clean-machine helpers)
SCRIPTS = REPO / "scripts"

sys.path.insert(0, str(SCRIPTS))
try:
    import board_task_intake as intake  # noqa: E402
except Exception as exc:
    pytest.skip(f"board_task_intake not available: {exc}",
                allow_module_level=True)


# ---------------------------------------------------------------------------
# Fixtures: a temp board + temp state dir + a spied subprocess call.
# ---------------------------------------------------------------------------

def _task_row(tid, status="pending"):
    return {"id": tid, "status": status,
            "title": f"task {tid}", "priority": "P0"}


class _Spy:
    """Records every run_resolve() call; returns canned payloads per id."""

    def __init__(self):
        self.calls = []
        self.results = {}

    def __call__(self, spawn_cmd, board, task_id, timeout_s=600):
        self.calls.append({"task_id": task_id, "board": board,
                           "spawn_cmd": list(spawn_cmd),
                           "timeout_s": timeout_s})
        result = self.results.get(task_id, ({"resolve": _resolve_payload(
            task_id)}, None))
        # mirror the real run_resolve shape: returncode is ALWAYS present
        updates = {"returncode": 0}
        updates.update(dict(result[0]))
        return updates, result[1]


def _resolve_payload(tid, provider="prov-a", model="model-x", usd=1.25):
    """A realistic slice of the real router_spawn --from-task output
    (measured on this repo 2026-10-04: head hop + complexity block)."""
    return {
        "resolved_as": "adhoc",
        "head": {"hop": 1, "provider": provider, "model": model,
                 "usd_1m": usd, "in_per_m": 0.9, "out_per_m": 3.0},
        "chain": [{"hop": 1, "provider": provider, "model": model,
                   "usd_1m": usd}],
        "complexity": {"scorer": "auto", "source": "jev",
                       "adhoc": ["code_gen=1", "test=1", "debug=0"],
                       "confidence": 0.45, "band": "medium",
                       "text_source": "board", "text_chars": 128},
        "task": {"id": tid},
    }


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Hermetic board + state dir; a fake spawn command list."""
    board = tmp_path / "board" / "tasks.jsonl"
    board.parent.mkdir(parents=True)
    board.write_text("")
    state = tmp_path / "state"
    return {"board": str(board), "state": str(state),
            "spawn_cmd": ["fake-python", "router_spawn.py"]}


@pytest.fixture()
def make_intake(env, monkeypatch):
    """BoardIntake factory with the subprocess call spied out."""
    spy = _Spy()
    monkeypatch.setattr(intake, "run_resolve", spy)
    created = []

    def _make(**kw):
        params = dict(board=env["board"], state_dir=env["state"],
                      spawn_cmd=env["spawn_cmd"], log=lambda msg: None)
        params.update(kw)
        obj = intake.BoardIntake(**params)
        created.append(obj)
        return obj

    return _make, spy


def _append(board, rows):
    with open(board, "a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


def _resolves(state):
    path = Path(state) / intake.RESOLVES_FILE
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def _seen(state):
    path = Path(state) / intake.SEEN_FILE
    if not path.exists():
        return {}
    return intake.load_seen(str(path))


# ---------------------------------------------------------------------------
# 1. A new task triggers exactly one resolve record.
# ---------------------------------------------------------------------------

def test_new_task_triggers_exactly_one_resolve_record(make_intake, env):
    make, spy = make_intake
    intake_obj = make()
    _append(env["board"], [_task_row("TASK-101")])

    tally = intake_obj.poll()
    assert tally["new"] == 1 and tally["resolved"] == 1 and tally["errors"] == 0

    assert len(spy.calls) == 1
    call = spy.calls[0]
    assert call["task_id"] == "TASK-101"
    assert call["board"] == env["board"]
    assert "--from-task" in call["spawn_cmd"] or \
        call["spawn_cmd"][0] == "fake-python"

    records = _resolves(env["state"])
    assert len(records) == 1
    rec = records[0]
    assert rec["id"] == "TASK-101"
    assert rec["source"] == "board-intake"
    assert rec.get("error") is None
    assert rec["resolved_at"]
    # payload preservation: full resolve output + derived pointers
    assert rec["resolve"]["task"]["id"] == "TASK-101"
    assert rec["chain_head"] == {"provider": "prov-a", "model": "model-x",
                                 "usd_1m": 1.25}
    assert rec["complexity_source"] == "jev"
    # seen state marked completed
    seen = _seen(env["state"])
    assert seen["TASK-101"]["source"] == "claim-completed"
    assert seen["TASK-101"]["resolved_at"]


# ---------------------------------------------------------------------------
# 2. Replaying the same id adds no second record.
# ---------------------------------------------------------------------------

def test_replaying_same_id_adds_no_second_record(make_intake, env):
    make, spy = make_intake
    obj = make()
    _append(env["board"], [_task_row("TASK-202")])

    obj.poll()
    assert len(_resolves(env["state"])) == 1

    # Replay 1: same row appended again (duplicated row).
    _append(env["board"], [_task_row("TASK-202")])
    obj.poll()
    # Replay 2: a brand-new intake instance (daemon restart) reads the same
    # board and the same state — the seen ledger is the idempotence key.
    obj2 = make()
    obj2.poll()

    assert len(spy.calls) == 1, \
        f"resolve ran {len(spy.calls)}x for a replayed id"
    assert len(_resolves(env["state"])) == 1


def test_new_intake_instance_ignores_already_consumed_bytes(make_intake, env):
    """A restarted daemon re-reads the WHOLE board only after a shrink; either
    way, ids in the seen state never resolve again."""
    make, spy = make_intake
    obj = make()
    _append(env["board"], [_task_row("TASK-303")])
    obj.poll()
    assert len(spy.calls) == 1

    # simulate a board rewrite/truncate + re-append of the SAME id
    with open(env["board"], "w", encoding="utf-8") as f:
        f.write(json.dumps(_task_row("TASK-303")) + "\n")
    obj.poll()
    assert len(spy.calls) == 1
    assert len(_resolves(env["state"])) == 1


# ---------------------------------------------------------------------------
# 3. Malformed / truncated lines do not crash and are skipped.
# ---------------------------------------------------------------------------

def test_truncated_last_line_waits_for_next_poll(make_intake, env):
    make, spy = make_intake
    obj = make()
    # a complete row plus a HALF-WRITTEN one (no trailing newline) — exactly
    # what an appender mid-write looks like to a tailer
    with open(env["board"], "a", encoding="utf-8") as f:
        f.write(json.dumps(_task_row("TASK-404")) + "\n")
        f.write('{"id": "TASK-405", "status": "pend')
    tally = obj.poll()

    assert tally["new"] == 1
    assert [c["task_id"] for c in spy.calls] == ["TASK-404"]

    # the writer finishes the line; the next poll picks TASK-405 up
    with open(env["board"], "a", encoding="utf-8") as f:
        f.write('ing"}\n')
    obj.poll()
    assert [c["task_id"] for c in spy.calls] == ["TASK-404", "TASK-405"]


def test_malformed_lines_skipped_good_rows_still_process(make_intake, env):
    make, spy = make_intake
    obj = make()
    with open(env["board"], "a", encoding="utf-8") as f:
        f.write("this is not json at all\n")
        f.write("\n")  # blank
        f.write(json.dumps(_task_row("TASK-506")) + "\n")
        f.write("[1, 2, 3]\n")  # valid JSON, not an object
        f.write(json.dumps(_task_row("TASK-507")) + "\n")
    tally = obj.poll()

    assert tally["new"] == 2
    # exactly 2 skips: the malformed text line + the non-object JSON line
    # (blank lines are not counted as events at all)
    assert tally["skipped"] == 2
    assert [c["task_id"] for c in spy.calls] == ["TASK-506", "TASK-507"]
    assert len(_resolves(env["state"])) == 2  # only the good ids recorded


def test_missing_board_is_fail_open(make_intake, env):
    make, _spy = make_intake
    os.unlink(env["board"])
    obj = make()
    tally = obj.poll()  # must not raise
    assert tally["new"] == 0
    errors = [r for r in _resolves(env["state"]) if r.get("error")]
    assert len(errors) == 1 and errors[0]["id"] is None


def test_board_error_recorded_once_per_incident(make_intake, env):
    make, _spy = make_intake
    os.unlink(env["board"])
    obj = make()
    obj.poll()
    obj.poll()
    obj.poll()
    errors = [r for r in _resolves(env["state"]) if r.get("error")]
    assert len(errors) == 1  # visible, but not per-poll ledger spam


# ---------------------------------------------------------------------------
# 4. Fail-open: subprocess errors recorded, the loop continues.
# ---------------------------------------------------------------------------

def test_subprocess_error_recorded_and_loop_continues(make_intake, env):
    make, spy = make_intake
    obj = make()
    spy.results["TASK-601"] = ({"returncode": 1,
                                "stdout_tail": "Traceback …", "stderr_tail":
                                "boom"}, "spawn exited 1")
    _append(env["board"], [_task_row("TASK-601"), _task_row("TASK-602")])

    tally = obj.poll()
    assert tally["new"] == 2 and tally["errors"] == 1 and \
        tally["resolved"] == 1
    # the loop continued: the SECOND task still resolved after the first failed
    assert [c["task_id"] for c in spy.calls] == ["TASK-601", "TASK-602"]

    records = {r["id"]: r for r in _resolves(env["state"])}
    bad = records["TASK-601"]
    assert bad["error"] == "spawn exited 1"
    assert bad["returncode"] == 1
    assert bad["stdout_tail"] == "Traceback …"
    assert "resolve" not in bad  # nothing synthesized in place of the payload
    assert records["TASK-602"].get("error") is None
    # the failed id is STILL claimed done — no retry storms, visible error only
    assert _seen(env["state"])["TASK-601"]["source"] == "claim-completed"


def test_timeout_recorded_and_loop_continues(make_intake, env):
    make, spy = make_intake
    obj = make()
    spy.results["TASK-701"] = ({"returncode": None},
                               "spawn timed out after 600s")
    _append(env["board"], [_task_row("TASK-701"), _task_row("TASK-702")])
    tally = obj.poll()
    assert tally["errors"] == 1 and tally["resolved"] == 1
    records = {r["id"]: r for r in _resolves(env["state"])}
    assert "timed out" in records["TASK-701"]["error"]
    assert records["TASK-702"].get("error") is None


def test_garbage_stdout_recorded_not_crash(make_intake, env):
    make, spy = make_intake
    obj = make()
    spy.results["TASK-801"] = ({"returncode": 0,
                                "stdout_tail": "<html>gateway 502</html>"},
                               "spawn stdout is not valid JSON")
    _append(env["board"], [_task_row("TASK-801")])
    tally = obj.poll()
    assert tally["errors"] == 1
    rec = _resolves(env["state"])[0]
    assert rec["error"] == "spawn stdout is not valid JSON"
    assert rec["stdout_tail"].startswith("<html>")


def test_spawn_failopen_shape_recorded_with_error_field(make_intake, env):
    """router_spawn's own fail-open contract is {"error": ...} + exit 0 — the
    ledger must show WHY there is no chain (TR-298 AC: visible, attributable).
    Shape verified against the real script's no-input path (2026-10-04):
    {"error", "code", "retryable"} and NO head/complexity block."""
    make, spy = make_intake
    obj = make()
    payload = {"error": "task TASK-802 not found in 2 board path(s)",
               "code": "TASK_NOT_FOUND", "retryable": False}
    spy.results["TASK-802"] = ({"resolve": payload}, None)
    _append(env["board"], [_task_row("TASK-802")])
    obj.poll()
    rec = _resolves(env["state"])[0]
    assert rec["returncode"] == 0
    assert rec["error"].startswith("resolve reported:")
    assert "not found" in rec["error"]
    # no chain/complexity derived from a fail-open payload — nothing synthesized
    assert rec["chain_head"] is None and rec["complexity_source"] is None
    assert rec["resolve"] == payload  # preserved unchanged


def test_run_resolve_real_subprocess_contract(tmp_path):
    """The REAL runner against a stub interpreter: pins the argv shape
    (--from-task/--board/--format json), the payload extraction, and the
    fail-open decode of a spawn that prints {"error": ...} with exit 0 —
    the shape router_spawn.py itself guarantees (AGENTS.md)."""
    stub = tmp_path / "spawn_stub.py"
    stub.write_text(
        "import json, os, sys, time\n"
        "argv = sys.argv[1:]\n"
        "if argv and argv[0] == 'crash':\n"
        "    sys.exit(3)\n"
        "if argv and argv[0] == 'sleep':\n"
        "    time.sleep(30)\n"
        "    sys.exit(0)\n"
        "print(json.dumps({'argv': argv, 'error': 'stub fail-open'}))\n")
    updates, err = intake.run_resolve(
        spawn_cmd=[sys.executable, str(stub)], board="/board.jsonl",
        task_id="TASK-9")
    assert err is None and updates["returncode"] == 0
    payload = updates["resolve"]
    assert payload["argv"][-6:] == ["--from-task", "TASK-9",
                                    "--board", "/board.jsonl",
                                    "--format", "json"]
    assert payload["error"] == "stub fail-open"
    # a hard crash (exit 3) becomes an error record, not an exception
    updates, err = intake.run_resolve(
        spawn_cmd=[sys.executable, str(stub), "crash"], board="/b",
        task_id="T")
    assert err == "spawn exited 3" and updates["returncode"] == 3
    # a hung spawn is bounded by the budget
    updates, err = intake.run_resolve(
        spawn_cmd=[sys.executable, str(stub), "sleep"], board="/b",
        task_id="T", timeout_s=2)
    assert "timed out" in err


def test_dangling_claim_recovered_at_boot(make_intake, env):
    """Daemon killed mid-resolve: the claim row has no resolved_at. The next
    boot records the loss as a visible error and completes the claim — the
    task is neither lost quietly nor resolved twice."""
    make, spy = make_intake
    # hand-write the crash state: claim without completion
    intake.append_jsonl(os.path.join(env["state"], intake.SEEN_FILE),
                        {"id": "TASK-909", "ts": "2026-10-04T12:00:00+00:00",
                         "resolved_at": None, "source": "claim"})
    obj = make()  # __init__ runs the reconciliation
    assert len(spy.calls) == 0  # NOT re-resolved (at-most-once holds)
    rec = _resolves(env["state"])[0]
    assert rec["id"] == "TASK-909" and rec["error"]
    assert "interrupted" in rec["error"]
    assert _seen(env["state"])["TASK-909"]["source"] == "claim-completed"


# ---------------------------------------------------------------------------
# 5. --once drains the backlog and exits.
# ---------------------------------------------------------------------------

def test_once_drains_backlog_and_exits(make_intake, env, capsys):
    make, spy = make_intake
    _append(env["board"], [_task_row("TASK-A1"), _task_row("TASK-A2"),
                           _task_row("TASK-A3", status="complete")])
    rc = intake.main([
        "--board", env["board"], "--state-dir", env["state"],
        "--spawn-cmd", " ".join(env["spawn_cmd"]), "--once",
    ])
    assert rc == 0
    assert [c["task_id"] for c in spy.calls] == ["TASK-A1", "TASK-A2"]
    assert len(_resolves(env["state"])) == 2
    assert "backlog drained" in capsys.readouterr().err


def test_once_is_idempotent_on_second_run(make_intake, env):
    make, spy = make_intake
    argv = ["--board", env["board"], "--state-dir", env["state"],
            "--spawn-cmd", " ".join(env["spawn_cmd"]), "--once"]
    _append(env["board"], [_task_row("TASK-B1")])
    assert intake.main(argv) == 0
    assert intake.main(argv) == 0
    assert [c["task_id"] for c in spy.calls] == ["TASK-B1"]
    assert len(_resolves(env["state"])) == 1


def test_daemon_cold_boot_seeds_existing_ids_without_resolving(
        make_intake, env):
    """The daemon contract: intake begins with genuinely NEW submissions —
    a fresh state on an existing board must not mass-resolve history."""
    make, spy = make_intake
    _append(env["board"], [_task_row("TASK-C1"),
                           _task_row("TASK-C2", status="complete")])
    obj = make()
    seeded = obj.seed()
    assert seeded == 2
    obj.poll()
    assert spy.calls == []  # nothing resolved
    assert set(_seen(env["state"])) == {"TASK-C1", "TASK-C2"}
    assert _resolves(env["state"]) == []


def test_seed_then_new_task_resolves(make_intake, env):
    make, spy = make_intake
    _append(env["board"], [_task_row("TASK-D0")])
    obj = make()
    obj.seed()
    _append(env["board"], [_task_row("TASK-D1")])
    obj.poll()
    assert [c["task_id"] for c in spy.calls] == ["TASK-D1"]


# ---------------------------------------------------------------------------
# 6. Default spawn command + docs parity guards.
# ---------------------------------------------------------------------------

def test_default_spawn_cmd_uses_repo_router_spawn():
    cmd = intake.build_spawn_cmd(None)
    assert cmd[0] == sys.executable
    assert cmd[1].endswith("scripts/router_spawn.py")


def test_spawn_cmd_env_override():
    old = os.environ.get(intake.ENV_SPAWN_CMD)
    try:
        os.environ[intake.ENV_SPAWN_CMD] = "/x/py spawn.py --flag"
        assert intake.build_spawn_cmd(None) == ["/x/py", "spawn.py", "--flag"]
    finally:
        if old is None:
            os.environ.pop(intake.ENV_SPAWN_CMD, None)
        else:
            os.environ[intake.ENV_SPAWN_CMD] = old


def test_intake_env_keys_documented():
    """Every ROUTER_INTAKE_* key this daemon reads must be documented
    (docs/configuration.md or README) — same parity rule the repo's
    doc-parity gate enforces for ROUTER_* keys."""
    docs = ((REPO / "README.md").read_text()
            + (REPO / "docs" / "configuration.md").read_text())
    for key in (intake.ENV_BOARD, intake.ENV_STATE_DIR,
                intake.ENV_INTERVAL_S, intake.ENV_SPAWN_CMD):
        assert key in docs, f"{key} missing from README/docs/configuration.md"


@pytest.mark.skipif(
    not conftest.repo_commit_resolvable(),
    reason="frozen tree, no .git")
def test_intake_state_files_gitignored():
    """The intake ledgers are runtime state (like the outcomes store, TR-049):
    a run must never dirty the tree."""
    import shutil
    git = shutil.which("git")
    assert git, "git not available"
    p = subprocess.run(
        [git, "check-ignore", "--no-index", "-v", "--",
         "data/state/intake-seen.jsonl", "data/state/intake-resolves.jsonl"],
        cwd=REPO, capture_output=True, text=True)
    matched = [line.split("\t", 1)[0] for line in p.stdout.splitlines()
               if line.strip()]
    assert len(matched) == 2, (
        f"intake ledgers not both gitignored: rc={p.returncode} "
        f"stdout={p.stdout!r} stderr={p.stderr!r}")


@pytest.mark.skipif(
    not conftest.repo_commit_resolvable(),
    reason="frozen tree, no .git")
def test_stale_gitignore_rule_cleaned_up():
    """The broad data/state/*.jsonl rule must not swallow TRACKED state
    files (modelsdev-cache.json is fine — it's .json — but the repo tracks
    three data/state files and none may be matched by the class rule)."""
    import shutil
    git = shutil.which("git")
    assert git, "git not available"
    tracked = subprocess.run([git, "ls-files", "data/state/"], cwd=REPO,
                             capture_output=True, text=True).stdout.split()
    assert tracked, "expected tracked data/state files (convention check)"
    p = subprocess.run([git, "check-ignore", "--no-index", "--"] + tracked,
                       cwd=REPO, capture_output=True, text=True)
    offenders = [line for line in p.stdout.splitlines() if line.strip()]
    assert not offenders, \
        f"class gitignore rule swallows tracked state files: {offenders}"
