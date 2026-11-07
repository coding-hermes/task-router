"""TR-1000976681059248 — a REFUSED seed must publish NOTHING.

The defect, measured at 8524321: `router seed` ran the TR-383 satisfiability
gate AFTER `_dump_registry()` had written registry.json and after the
data/tables sync had rewritten every tracked ``*.jsonl``. A REFUSED run
therefore exited 1 while leaving a freshly published registry behind —
``make seed && make validate`` failed at ``make seed`` while ``validate`` went
green on the very artifact the refusal claimed not to seed, and two
consecutive refused runs bumped the mtime of all 34 tracked data/tables
files.

Three legs, each running the REAL script as a subprocess:

  1. refusal    — default (fatal) gate: rc=1, "Seeding REFUSED", and
                  registry.json + every data/tables/*.jsonl byte-identical to
                  a pre-run capture; no publish marker printed; no staging
                  artifact inside the fixture tree.
  2. opt-in     — the same fixture with ROUTING_ALLOW_UNSATISFIABLE=1: rc=0
                  and registry.json's sha256 CHANGES — proof the gate moved
                  before publishing, not that it was disabled.
  3. ordering   — the child runs under a sys.addaudithook open recorder (the
                  proven tests/test_seed_ns_guard.py pattern, chosen over an
                  in-process monkeypatch because the seed's whole body is
                  module-level: the only honest instrumentation window is
                  inside the child). On the refusal path the gate's staged
                  model_tier.jsonl read is recorded, ZERO write-mode opens
                  touch registry.json, and the ONLY data/tables write allowed
                  is the pre-gate TR-232 undamp rewrite of benchmarks.jsonl —
                  fingerprinted by its data/state backup twin so a regression
                  that re-adds the sync (all 12 tables, no backup) or a
                  publish-before-grade (registry write) turns red. The opt-in
                  control run proves the same recorder DOES see publish writes
                  when they happen (no vacuous pass).

Environment discipline: a MINIMAL child env, modelled on
tests/test_tr383_satisfiability.py — inheriting the suite env would leak
conftest's session-wide ROUTING_ALLOW_UNSATISFIABLE=1 into the refusal legs
(and the operator's data home into every leg). Fixtures are tmp copies of the
committed tables; the real checkout is never touched.

Known shared premise: the committed tables currently carry an unservable
rating (the TR-383 `math` gap), which is what makes the refusal legs refuse.
tests/test_tr383_satisfiability.py pins the same premise for the same reason.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SEED = REPO / "scripts" / "router_seed.py"
COMMITTED_TABLES = REPO / "data" / "tables"
PY = sys.executable

#: scripts/router_seed.py's run-unique staging prefix (the publish staging
#: dir must never appear under the repo or the fixture tree).
STAGING_PREFIX = "router-seed-stage-"

#: stdout markers of the three publish steps, in their printed order
#: (ns mirror export, registry write, data/tables sync).
PUBLISH_MARKERS = ("exported tables to", "wrote ", "synced data/tables ->")


def _sha256(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _table_hashes(data_dir) -> dict:
    return {p.name: _sha256(p) for p in sorted(Path(data_dir).glob("*.jsonl"))}


def _make_fixture(tmp_path):
    """A committed-tables copy + a SENTINEL registry.json with known bytes.

    The sentinel matters: "unchanged" must mean an EXISTING registry is not
    clobbered (the live shape — a refusal must not overwrite the store the
    fleet is currently using), not merely that no file appeared.
    """
    data = tmp_path / "data" / "tables"
    data.parent.mkdir()
    shutil.copytree(COMMITTED_TABLES, data)
    reg = tmp_path / "registry.json"
    reg.write_text(json.dumps({"version": 0, "sentinel": "pre-run"}) + "\n")
    (tmp_path / "home").mkdir()
    (tmp_path / "state").mkdir()
    return reg, data


def _child_env(tmp_path, reg, data) -> dict:
    """Minimal env: test_tr383's allowlist, everything else pinned to tmp."""
    env = {k: v for k, v in os.environ.items()
           if k in ("PATH", "LANG", "LC_ALL", "PYTHONPATH", "VIRTUAL_ENV",
                    "PYTHONHOME", "SYSTEMROOT")}
    env.update({
        "HOME": str(tmp_path / "home"),
        "XDG_DATA_HOME": str(tmp_path / "xdg"),
        "TASK_ROUTER_HOME": str(tmp_path / "home"),
        "ROUTER_STATE_DIR": str(tmp_path / "state"),
        "ROUTING_DATA_DIR": str(data),
        "ROUTING_REGISTRY": str(reg),
        "ROUTING_NS": str(tmp_path / "ns"),
    })
    return env


def _run_seed(env, cwd):
    return subprocess.run([PY, str(SEED)], capture_output=True, text=True,
                          env=env, cwd=str(cwd), timeout=900)


# --------------------------------------------------------------------------
# Leg 1 — the refusal arm: exit 1 AND byte-identical outputs.
# --------------------------------------------------------------------------

@pytest.mark.timeout(1200)
def test_refused_seed_publishes_nothing(tmp_path):
    reg, data = _make_fixture(tmp_path)
    reg_before = _sha256(reg)
    tables_before = _table_hashes(data)

    env = _child_env(tmp_path, reg, data)  # no opt-out: the default stays fatal
    p = _run_seed(env, tmp_path)
    assert p.returncode == 1, (
        f"seed without the opt-out must refuse; rc={p.returncode}\n"
        f"stdout tail:\n{p.stdout[-1500:]}\nstderr tail:\n{p.stderr[-800:]}")
    assert "SATISFIABILITY FAIL" in p.stdout
    assert "Seeding REFUSED" in p.stdout

    # The atomicity contract: the refusal left the published surfaces alone.
    # Content-hash identity, deliberately NOT mtime identity: the pre-gate
    # TR-232 undamp pass legitimately rewrites benchmarks.jsonl (same bytes on
    # the committed tree) on every run, refused or not.
    assert _sha256(reg) == reg_before, (
        "a refused seed rewrote registry.json — publishing is not atomic "
        "with the gate")
    assert _table_hashes(data) == tables_before, (
        "a refused seed rewrote data/tables/*.jsonl — publishing is not "
        "atomic with the gate")

    # No publish step even STARTED on the refusal path (print trail).
    for marker in PUBLISH_MARKERS:
        assert marker not in p.stdout, (
            f"refused seed printed the publish marker {marker!r}")

    # Staging is a tempfile.mkdtemp artifact — never inside the fixture tree,
    # so a refused run leaves no artifacts anywhere it was pointed at.
    assert not list(tmp_path.rglob(STAGING_PREFIX + "*")), (
        "staging artifacts landed inside the fixture tree")


# --------------------------------------------------------------------------
# Leg 2 — the opt-in arm: the gate moved, it was not disabled.
# --------------------------------------------------------------------------

@pytest.mark.timeout(1200)
def test_allow_unsatisfiable_still_publishes(tmp_path):
    reg, data = _make_fixture(tmp_path)
    sentinel_sha = _sha256(reg)

    env = _child_env(tmp_path, reg, data)
    env["ROUTING_ALLOW_UNSATISFIABLE"] = "1"
    p = _run_seed(env, tmp_path)
    assert p.returncode == 0, (
        f"opt-out run must seed; rc={p.returncode}\nstderr tail:\n{p.stderr[-800:]}")
    # The findings stay LOUD on the opt-in path.
    assert "SATISFIABILITY FAIL" in p.stdout
    assert "seeding anyway" in p.stdout

    # ...and the run really published: the sentinel registry was replaced.
    assert reg.exists()
    assert _sha256(reg) != sentinel_sha, (
        "the opt-in run did not rewrite registry.json — the gate was "
        "disabled rather than moved before publishing")
    assert "wrote " in p.stdout and "synced data/tables ->" in p.stdout
    assert (data / "model_tier.jsonl").exists()


# --------------------------------------------------------------------------
# Leg 3 — the ordering guard: grade staged bytes, then publish, never the
# reverse. Instrumentation = an in-child audit hook recording every open;
# publish writes are caught at the syscall-adjacent layer, not via stdout.
# --------------------------------------------------------------------------

_AUDIT_RUNNER = r'''
import json, os, runpy, sys
out, script = sys.argv[1], sys.argv[2]
sys.argv = [script] + sys.argv[3:]
opens = []
def _hook(event, args):
    if event == "open":
        path, mode = args[0], args[1]
        if isinstance(path, (str, bytes, os.PathLike)):
            opens.append([os.path.abspath(os.fsdecode(path)), mode or ""])
sys.addaudithook(_hook)
rc = 0
try:
    runpy.run_path(script, run_name="__main__")
except SystemExit as e:
    rc = e.code or 0
except BaseException:
    rc = 1
with open(out, "w") as f:
    json.dump({"rc": rc, "opens": opens}, f)
sys.exit(rc)
'''


def _run_audited(env, cwd, audit_json: Path):
    runner = cwd / "_seed_audit_runner.py"
    runner.write_text(_AUDIT_RUNNER)
    proc = subprocess.run([PY, str(runner), str(audit_json), str(SEED)],
                          capture_output=True, text=True, env=env,
                          cwd=str(cwd), timeout=900)
    report = json.loads(audit_json.read_text())
    return proc, report


def _write_opens(opens):
    return [p for p, m in opens if any(c in m for c in "wax+")]


@pytest.mark.timeout(1200)
def test_gate_grades_staged_tables_before_any_publish(tmp_path):
    reg, data = _make_fixture(tmp_path)
    env = _child_env(tmp_path, reg, data)  # refusal path: no opt-out
    proc, report = _run_audited(env, tmp_path, tmp_path / "audit-refusal.json")

    assert proc.returncode == 1, (
        f"refusal run rc={proc.returncode}\nstdout tail:\n{proc.stdout[-1500:]}")
    assert "Seeding REFUSED" in proc.stdout
    opens = report["opens"]

    # (a) Ordering, filesystem truth: ZERO write-mode opens touched the
    #     registry path, and the ONLY data/tables write allowed is the
    #     PRE-GATE TR-232 undamp rewrite of benchmarks.jsonl — build-time
    #     input normalization (scripts/router_seed.py, undamp pass, before
    #     staging/gate/publish, identical on refused and allowed runs,
    #     byte-preserving on the committed tree: leg 1 proves the bytes). A
    #     regression that re-adds the tail-sync (12+ tables, no backup) or
    #     publishes before grading (registry write) turns this red.
    #     Fingerprint, not filename allowlist: the undamp pass is the ONLY
    #     writer that pairs its rewrite with a data/state/benchmarks
    #     .bak-<ts> backup (_backup_with_retention); the sync pairs none.
    write_paths = _write_opens(opens)
    reg_writes = [w for w in write_paths if w == str(reg)]
    assert reg_writes == [], (
        f"the refusal path wrote registry.json: {reg_writes}")
    data_writes = [w for w in write_paths
                   if w.startswith(str(data) + os.sep)]
    unexpected = [w for w in data_writes
                  if w != str(data / "benchmarks.jsonl")]
    assert unexpected == [], (
        f"the refusal path performed publish writes: {unexpected}")
    if data_writes:
        baks = list((tmp_path / "data" / "state")
                    .glob("benchmarks.jsonl.bak-*"))
        assert baks, (
            f"data/tables was written on the refusal path ({data_writes}) "
            "without the TR-232 undamp backup fingerprint — that is the "
            "tail-sync or another publish writer, not the pre-gate build pass")

    # (b) The gate graded the STAGED bytes: a read of a staged
    #     model_tier.jsonl under the run-unique staging prefix is on record.
    staged_gate_reads = [p for p, m in opens
                         if STAGING_PREFIX in p and p.endswith("model_tier.jsonl")]
    assert staged_gate_reads, (
        "the gate never read a staged model_tier.jsonl — it is not grading "
        "the bytes this run built")

    # (c) Staging stayed outside the fixture tree (tempfile default dir).
    staged_writes = [p for p in _write_opens(opens) if STAGING_PREFIX in p]
    assert staged_writes, "nothing was staged — the gate graded stale inputs"
    assert not [p for p in staged_writes if p.startswith(str(tmp_path) + os.sep)], (
        f"staging writes landed inside the fixture tree: "
        f"{[p for p in staged_writes if p.startswith(str(tmp_path))]}")

    # Positive control (no vacuous pass): the SAME recorder, on the opt-in
    # path, MUST see the publish writes it just asserted absent above.
    env2 = dict(env)
    env2["ROUTING_ALLOW_UNSATISFIABLE"] = "1"
    proc2, report2 = _run_audited(env2, tmp_path,
                                  tmp_path / "audit-allow.json")
    assert proc2.returncode == 0, proc2.stderr[-800:]
    writes2 = _write_opens(report2["opens"])
    assert str(reg) in writes2, (
        "the audit recorder did not see the registry publish — the refusal-"
        "arm assertions above would pass vacuously")
    assert any(w.startswith(str(data) + os.sep) for w in writes2), (
        "the audit recorder did not see the data/tables sync — the refusal-"
        "arm assertions above would pass vacuously")
