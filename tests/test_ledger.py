"""TR-007 AC3 — router_ledger.py CLI contract tests (hermetic: LEDGER_FILE
points at a tmp_path file; the real ~/.hermes/model-router/ledger.jsonl is
never touched). The ledger tool is stdlib-only, so these run under any python
with pytest — no duckdb importorskip needed, matching the guard's split.
"""
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")
TOOL = os.path.join(SCRIPTS, "router_ledger.py")


def _run(tmp_path, *args):
    env = dict(os.environ)
    env["LEDGER_FILE"] = str(tmp_path / "ledger.jsonl")
    return subprocess.run([sys.executable, TOOL, *args],
                          capture_output=True, text=True, env=env, timeout=30)


def _rows(tmp_path):
    p = tmp_path / "ledger.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def test_start_appends_row_and_prints_trace_id(tmp_path):
    p = _run(tmp_path, "start", "--provider", "prov-a", "--model", "m1",
             "--project", "proj", "--profile", "P0_FORE", "--hop", "1",
             "--reason", "router head")
    assert p.returncode == 0
    tid = p.stdout.strip()
    assert tid.startswith("tr-") and len(tid) == 11  # tr- + 8 hex chars
    rows = _rows(tmp_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["outcome"] == "started"
    assert row["trace_id"] == tid
    assert row["provider"] == "prov-a" and row["model"] == "m1"
    assert row["requested_pair"] == "prov-a/m1" and row["served_pair"] == "prov-a/m1"
    assert row["chain_hop"] == 1
    # schema v2 subset: no fabricated fields (quota/cache/tokens absent)
    assert "quota_before" not in row and "cache_hit" not in row


def test_start_explicit_trace_id_respected(tmp_path):
    p = _run(tmp_path, "start", "--provider", "p", "--model", "m",
             "--trace-id", "tr-custom1")
    assert p.returncode == 0
    assert p.stdout.strip() == "tr-custom1"
    assert _rows(tmp_path)[0]["trace_id"] == "tr-custom1"


def test_start_end_cycle_success(tmp_path):
    s = _run(tmp_path, "start", "--provider", "prov-a", "--model", "m1")
    tid = s.stdout.strip()
    e = _run(tmp_path, "end", "--trace-id", tid,
             "--outcome", "success", "--latency-ms", "1234",
             "--tokens-in", "100", "--tokens-out", "200", "--error-class", "")
    assert e.returncode == 0, e.stderr
    rows = _rows(tmp_path)
    assert len(rows) == 2
    end = rows[1]
    assert end["trace_id"] == tid and end["outcome"] == "success"
    assert end["latency_ms"] == 1234 and end["tokens_in"] == 100 \
        and end["tokens_out"] == 200
    # empty-string optional args are dropped by _subset (no fabricated values)
    assert "error_class" not in end


def test_end_invalid_outcome_exits_2(tmp_path):
    _run(tmp_path, "start", "--provider", "p", "--model", "m")
    p = _run(tmp_path, "end", "--trace-id", "whatever", "--outcome", "bogus")
    assert p.returncode == 2
    rows = _rows(tmp_path)
    assert len(rows) == 1 and rows[0]["outcome"] == "started"  # nothing appended


def test_end_unknown_trace_warns_but_appends_fail_open(tmp_path):
    e = _run(tmp_path, "end", "--trace-id", "tr-unknown00", "--outcome", "failure",
             "--error-class", "timeout")
    assert e.returncode == 0
    assert "warning" in e.stderr
    rows = _rows(tmp_path)
    assert rows[-1]["trace_id"] == "tr-unknown00"
    assert rows[-1]["outcome"] == "failure"
    assert rows[-1]["error_class"] == "timeout"


def test_status_inflight_and_last_outcome_text(tmp_path):
    s = _run(tmp_path, "start", "--provider", "prov-a", "--model", "m1")
    tid = s.stdout.strip()
    st = _run(tmp_path, "status")
    assert st.returncode == 0
    assert "prov-a/m1" in st.stdout and "in_flight=1" in st.stdout
    assert "WARNING" not in st.stdout  # wired: traces exist → no warning

    _run(tmp_path, "end", "--trace-id", tid, "--outcome", "success",
         "--latency-ms", "10")
    st2 = _run(tmp_path, "status")
    assert "in_flight=0" in st2.stdout and "last=success" in st2.stdout


def test_status_json_parses(tmp_path):
    _run(tmp_path, "start", "--provider", "prov-a", "--model", "m1")
    _run(tmp_path, "start", "--provider", "prov-b", "--model", "m2")
    st = _run(tmp_path, "status", "--json")
    assert st.returncode == 0
    data = json.loads(st.stdout)
    pairs = {e["pair"]: e for e in data["pairs"]}
    assert pairs["prov-a/m1"]["in_flight"] == 1
    assert pairs["prov-b/m2"]["in_flight"] == 1
    assert data["stale_after_minutes"] == 30
    # TR-026: a trace exists → the ledger is wired
    assert data["wired"] is True
    assert data["rows"] == 2
    assert data["started_open"] == 2
    assert data["terminal"] == 0


def test_status_provider_filter(tmp_path):
    _run(tmp_path, "start", "--provider", "prov-a", "--model", "m1")
    _run(tmp_path, "start", "--provider", "prov-b", "--model", "m2")
    st = _run(tmp_path, "status", "--provider", "prov-b", "--json")
    data = json.loads(st.stdout)
    names = [e["pair"] for e in data["pairs"]]
    assert names == ["prov-b/m2"]


def test_status_empty_or_missing_file_exit0(tmp_path):
    # LEDGER_FILE missing entirely → status still exits 0 with parseable output
    env = dict(os.environ)
    env["LEDGER_FILE"] = str(tmp_path / "nonexistent" / "ledger.jsonl")
    p = subprocess.run([sys.executable, TOOL, "status", "--json"],
                       capture_output=True, text=True, env=env, timeout=30)
    assert p.returncode == 0
    data = json.loads(p.stdout)
    assert data["pairs"] == []
    # TR-026: zero traces → explicitly NOT wired (never a silent empty)
    assert data["wired"] is False
    assert data["rows"] == 0
    assert data["started_open"] == 0
    assert data["terminal"] == 0


def test_status_empty_file_reports_unwired_loudly(tmp_path):
    # TR-026: a present-but-EMPTY ledger (file created, never written) is the
    # exact unwired state — status must say wired:false + WARNING, so any
    # consumer knows concurrency accounting is not active.
    (tmp_path / "ledger.jsonl").write_text("")
    p = _run(tmp_path, "status")
    assert p.returncode == 0
    assert "NOT WIRED" in p.stdout
    assert "model busy" in p.stdout
    pj = _run(tmp_path, "status", "--json")
    data = json.loads(pj.stdout)
    assert data["wired"] is False
    assert data["rows"] == 0
    assert data["pairs"] == []


def test_two_started_same_pair_count_twice(tmp_path):
    a = _run(tmp_path, "start", "--provider", "prov-a", "--model", "m1").stdout.strip()
    b = _run(tmp_path, "start", "--provider", "prov-a", "--model", "m1").stdout.strip()
    assert a != b  # auto trace ids unique
    st = _run(tmp_path, "status", "--json")
    pairs = {e["pair"]: e for e in json.loads(st.stdout)["pairs"]}
    assert pairs["prov-a/m1"]["in_flight"] == 2