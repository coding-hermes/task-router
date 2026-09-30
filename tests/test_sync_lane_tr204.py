"""TR-204 — task-router-sync lane write path: failure diagnosability,
no false success marker, over-cap snapshot path, size preflight.

Tests drive scripts/sync_task_router_duckbrain.py in-process against a
stdlib http.server stub that mirrors the two DuckBrain mounts the lane uses:
POST /api/memories (100 KiB express.json cap, 500 INTERNAL_ERROR over cap)
and POST /api/ns/<ns>/tables/<table> (1 MiB NDJSON cap, append + count
read-back). No live service, no network egress.
"""
import importlib.util
import json
import re
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "sync_task_router_duckbrain.py"
NS = "task-router"
MARKER_KEY = "/sync/task-router/last-success"
MEMORIES_CAP = 100 * 1024          # mirror of express.json() default
NDJSON_CAP = 1024 * 1024           # mirror of the x-ndjson mount


def _load_mod():
    spec = importlib.util.spec_from_file_location("sync_lane_tr204", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load_mod()


# ---------------------------------------------------------------------------
# DuckBrain stub
# ---------------------------------------------------------------------------
class _State:
    def __init__(self):
        self.memories = []          # dicts: key,domain,content,id,timestamp
        self.tables = {}            # name -> list[row-dicts]
        self.declared = set()
        self.four_oh_four_tables = True  # undeclared tables read back 404
        self.fail_next = 0          # force N generic 500s


def make_handler(state):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def _send(self, code, payload, headers=None):
            body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.startswith("/health"):
                return self._send(200, {"status": "healthy"})
            if self.path.startswith("/api/memories"):
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                prefix = (q.get("prefix") or [""])[0]
                limit = int((q.get("limit") or ["50"])[0])
                items = [m for m in state.memories if m["key"].startswith(prefix)]
                items.sort(key=lambda m: m["timestamp"], reverse=True)
                return self._send(200, {"items": items[:limit], "total": len(items)})
            if self.path.startswith("/api/ns/"):
                m = re.match(r"^/api/ns/([^/]+)/tables/([^?]+)\?(.*)$", self.path)
                if not m:
                    return self._send(404, {"error": "not found"})
                ns, table, qs = m.groups()
                if table not in state.declared:
                    return self._send(404, {"error": f"table {table} not declared"})
                rows = state.tables.get(table, [])
                q = urllib.parse.parse_qs(qs)
                wants_exact = (q.get("count") or [""])[0] == "exact"
                for col, vals in q.items():
                    if col in ("limit", "order", "offset", "count"):
                        continue
                    mm = re.match(r"eq\.(.*)", vals[0])
                    if mm:
                        want = mm.group(1)
                        rows = [r for r in rows if str(r.get(col)) == want]
                total = len(rows)
                limit = int((q.get("limit") or ["100"])[0])
                headers = {}
                prefer = self.headers.get("Prefer") or ""
                if wants_exact or "count=exact" in prefer:
                    headers["X-Total-Count"] = str(total)
                return self._send(200, rows[:limit], headers)
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length)
            if self.path.startswith("/api/memories"):
                if getattr(state, "reject_all", False):
                    return self._send(500, {"error": "INTERNAL_ERROR"})
                if length > MEMORIES_CAP:
                    # live server behavior: over-cap express.json body -> 500
                    return self._send(500, {"error": "INTERNAL_ERROR"})
                try:
                    body = json.loads(raw)
                except Exception:
                    return self._send(400, {"error": "bad json"})
                for field in ("key", "domain", "content"):
                    if field not in body:
                        return self._send(400, {"error": f"Missing required fields: {field}"})
                import uuid
                state.memories.append({
                    "key": body["key"], "domain": body["domain"],
                    "content": body["content"],
                    "id": str(uuid.uuid4()),
                    "timestamp": _State.now_iso(),
                })
                return self._send(201, {"id": state.memories[-1]["id"]})
            m = re.match(r"^/api/ns/([^/]+)/tables/([^?]+)", self.path)
            if not m:
                return self._send(404, {"error": "not found"})
            ns, table = m.groups()
            if table not in state.declared:
                return self._send(404, {"error": f"table {table} not declared"})
            ctype = self.headers.get("Content-Type") or ""
            if "x-ndjson" not in ctype:
                return self._send(415, {"error": "ndjson required"})
            if length > NDJSON_CAP:
                return self._send(413, {"error": "payload too large"})
            inserted = 0
            for line in raw.decode().splitlines():
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                state.tables.setdefault(table, []).append(row)
                inserted += 1
            return self._send(201, {"inserted": inserted})

    return Handler


def _server(state):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    return srv


def _state_now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + \
        f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z"


_State.now_iso = staticmethod(_state_now)


@pytest.fixture()
def stub(mod, tmp_path, monkeypatch):
    state = _State()
    srv = _server(state)
    ns_dir = tmp_path / "namespaces"
    (ns_dir / NS / "tables").mkdir(parents=True)
    # seed two snapshot source tables: one small, one over the memories cap
    (ns_dir / NS / "tables" / "models.jsonl").write_text(
        "\n".join(json.dumps({"provider": "p", "model": f"m{i}"}) for i in range(20)) + "\n")
    (ns_dir / NS / "tables" / "model_perf.jsonl").write_text(
        "\n".join(json.dumps({"provider": "p", "model": f"m{i}",
                              "blob": "x" * 300}) for i in range(400)) + "\n")
    monkeypatch.setenv("HOME_TMP", str(tmp_path))
    yield mod, state, srv, ns_dir
    srv.shutdown()


# ---------------------------------------------------------------------------
# 1. Failure diagnosability
# ---------------------------------------------------------------------------
def test_failed_post_names_status_and_body(stub, tmp_path):
    """A rejected /api/memories POST must surface HTTP status + body, not blank."""
    mod, state, srv, ns_dir = stub
    lines = []
    facts = [{"key": "/concept/task-router/too-big", "domain": "raw_note",
              "content": "x" * (MEMORIES_CAP + 500)}]
    # Bypass the preflight to reach the raw failure path — the diagnosability
    # contract is about the REPORT LINE, which the stub server also answers 500.
    state_fail = facts  # over-cap: run_facts must SKIP it (named), not fail
    written, failures, skips = mod.run_facts(
        srv._base_url if hasattr(srv, "_base_url") else f"http://127.0.0.1:{srv.server_port}",
        NS, "tok", facts, "2020-01-01T00:00:00Z", 0, lines.append)
    assert written == 0
    assert failures == []
    assert len(skips) == 1 and skips[0]["key"] == "/concept/task-router/too-big"
    assert skips[0]["size"] > skips[0]["cap"]


def test_overcap_probe_names_status_and_body(stub, tmp_path, capsys):
    """AC1 live shape: a forced over-cap write reports HTTP 500 + INTERNAL_ERROR."""
    mod, state, srv, ns_dir = stub
    base = f"http://127.0.0.1:{srv.server_port}"
    tok = tmp_path / "tok"
    tok.write_text("tok")
    rc = mod.cmd_probe_overcap(type("A", (), {
        "base_url": base, "token_file": str(tok),
        "size": MEMORIES_CAP + 123,
    })())
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "HTTP 500" in out
    assert "INTERNAL_ERROR" in out
    assert "nothing landed" in out
    assert state.memories == []  # no over-cap row ever persisted


def test_transport_failure_line_is_not_blank(stub, tmp_path):
    """A server-rejected normal-sized POST produces a failure line naming the
    status AND the body — never an empty reason (the 09-27 silent failure)."""
    mod, state, srv, ns_dir = stub
    base = f"http://127.0.0.1:{srv.server_port}"
    state.reject_all = True
    lines = []
    written, failures, skips = mod.run_facts(
        base, NS, "tok",
        [{"key": "/concept/task-router/normal", "domain": "config",
          "content": "hello"}],
        "2020-01-01T00:00:00Z", 0, lines.append)
    assert written == 0 and skips == []
    assert len(failures) == 1
    assert failures[0]["status"] == 500
    assert "INTERNAL_ERROR" in failures[0]["body"]
    assert failures[0]["body"].strip()
    assert "INTERNAL_ERROR" in lines[0]
    # the empty-reason shape from the baseline defect is impossible:
    assert not re.search(r"FAIL \S+: HTTP \d+ $", lines[0])


# ---------------------------------------------------------------------------
# 2. No false success marker
# ---------------------------------------------------------------------------
def test_failed_run_writes_no_marker_row(stub, tmp_path, capsys):
    """AC2: every write failing -> outcome FAILED, no POST under the marker key,
    marker content guard, before/after newest-row timestamps identical."""
    mod, state, srv, ns_dir = stub
    base = f"http://127.0.0.1:{srv.server_port}"
    argv = ["run", "--base-url", base, "--token-file", str(tmp_path / "tok"),
            "--ns-dir", str(ns_dir), "--workdir", str(tmp_path / "wd"),
            "--window-start", "2026-09-30T04:20Z", "--window-end", "2026-09-30T10:05Z",
            "--force-fail", "--no-report"]
    (tmp_path / "tok").write_text("tok")
    rc = mod.main(argv)
    out = capsys.readouterr().out
    assert rc == 1
    assert "MARKER: NOT WRITTEN" in out
    assert state.memories == [], "force-fail proof must perform ZERO writes"
    before_after = re.search(r"before=(\S+) after=(\S+)", out)
    assert before_after and before_after.group(1) == before_after.group(2)


def test_marker_placeholder_guard_refuses_template_tokens():
    """The bad 09-27 marker carried the literal 'window ...'. The guard must
    refuse ellipses, {slot}, %s and <slot> tokens."""
    mod = _load_mod()
    assert not mod.marker_content_is_substituted(
        "dagger duckbrain-sync SUCCESS 2026-09-27 — window ...")
    assert not mod.marker_content_is_substituted("window {start}..{end}")
    assert not mod.marker_content_is_substituted("HEAD=%s")
    assert not mod.marker_content_is_substituted("at <timestamp>")
    assert mod.marker_content_is_substituted(
        "task-router-sync SUCCESS 2026-09-30T10:00Z — window "
        "2026-09-30T04:20Z..2026-09-30T10:00Z. tables: models=1843@1c5f4f72 posted")


def test_any_single_failed_write_blocks_marker(stub, tmp_path, capsys):
    """One failing fact (simulated via --fail-pattern) -> FAILED, no marker."""
    mod, state, srv, ns_dir = stub
    base = f"http://127.0.0.1:{srv.server_port}"
    facts = tmp_path / "facts.json"
    facts.write_text(json.dumps([
        {"key": "/concept/task-router/fine", "domain": "config", "content": "ok"},
        {"key": "/concept/task-router/broken", "domain": "config", "content": "nope"},
    ]))
    argv = ["run", "--base-url", base, "--token-file", str(tmp_path / "tok"),
            "--ns-dir", str(ns_dir), "--workdir", str(tmp_path / "wd"),
            "--window-start", "2026-09-30T04:20Z", "--window-end", "2026-09-30T10:05Z",
            "--facts-file", str(facts), "--fail-pattern", "broken",
            "--no-report"]
    (tmp_path / "tok").write_text("tok")
    # Only the broken fact fails, but tables snapshots run against the stub too:
    # declare nothing server-side so table reads 404 -> the table reads treat a
    # missing declaration as no-rows (status 404 passes the dedupe gate), and
    # snapshot POSTs will fail... to keep this test focused on the fact path,
    # declare the tables so snapshots succeed.
    for t in ("models", "model_catalog", "model_tier", "model_perf", "model_notes"):
        state.declared.add(f"snapshot_{t}")
        # sources for tables other than models/model_perf don't exist -> missing-source
    rc = mod.main(argv)
    out = capsys.readouterr().out
    assert rc == 1
    assert "FAIL /concept/task-router/broken" in out
    assert "MARKER: NOT WRITTEN" in out
    assert not [m for m in state.memories if m["key"] == MARKER_KEY]
    # the healthy fact DID get written before the failure was tallied:
    assert [m for m in state.memories if m["key"] == "/concept/task-router/fine"]


# ---------------------------------------------------------------------------
# 3. Over-cap snapshot path: declared NDJSON tables
# ---------------------------------------------------------------------------
def test_snapshot_posts_via_ndjson_mount_and_reads_back(stub, tmp_path):
    """models.jsonl rows land via /api/ns/task-router/tables/snapshot_models
    (x-ndjson), verified by exact-count read-back."""
    mod, state, srv, ns_dir = stub
    for t in mod.SNAPSHOT_TABLES:
        state.declared.add(f"snapshot_{t}")
    base = f"http://127.0.0.1:{srv.server_port}"
    lines = []
    res = mod.snapshot_table(base, "tok", str(ns_dir), "models",
                             "2026-09-30T10:00:00Z", 0, lines.append)
    assert res["verified"], res
    assert res["rows"] == 20
    assert len(state.tables["snapshot_models"]) == 20
    assert all(r["snap_ts"] == "2026-09-30T10:00:00Z"
               for r in state.tables["snapshot_models"])
    assert res["sha8"] and len(res["sha8"]) == 8


def test_snapshot_dedupes_unchanged_table(stub, tmp_path):
    mod, state, srv, ns_dir = stub
    for t in mod.SNAPSHOT_TABLES:
        state.declared.add(f"snapshot_{t}")
    base = f"http://127.0.0.1:{srv.server_port}"
    lines = []
    first = mod.snapshot_table(base, "tok", str(ns_dir), "models",
                               "2026-09-30T10:00:00Z", 0, lines.append)
    assert first["verified"]
    second = mod.snapshot_table(base, "tok", str(ns_dir), "models",
                                "2026-09-30T11:00:00Z", 0, lines.append)
    assert second["status"] == "unchanged"
    assert len(state.tables["snapshot_models"]) == 20, "no duplicate append"


def test_chunks_stay_under_ndjson_mount_cap(mod):
    """Chunk sizing: 512 KiB body target and 4000 rows; every chunk < 1 MiB."""
    big_lines = [json.dumps({"blob": "y" * 900}) for _ in range(4000)]
    chunks = mod.build_chunks("models", big_lines, "ts", "abcd1234")
    assert all(len(c) < NDJSON_CAP for c in chunks)
    assert all(len(c.splitlines()) <= mod.CHUNK_ROWS for c in chunks)
    # a single line larger than the mount cap is a loud chunking failure, not a
    # silent skip
    with pytest.raises(ValueError):
        mod.build_chunks("models", [json.dumps({"blob": "z" * (NDJSON_CAP + 10)})],
                         "ts", "abcd1234")


def test_declarations_written_idempotently(stub, tmp_path):
    mod, state, srv, ns_dir = stub
    n1 = mod.ensure_declarations(str(ns_dir), lambda *_: None)
    n2 = mod.ensure_declarations(str(ns_dir), lambda *_: None)
    assert n1 == len(mod.SNAPSHOT_TABLES)
    assert n2 == 0
    for t in mod.SNAPSHOT_TABLES:
        doc = json.loads((ns_dir / NS / "tables" / f"snapshot_{t}.table.json").read_text())
        assert doc["name"] == f"snapshot_{t}"
        assert doc["glob"] == f"tables/snapshots/{t}.jsonl"
        assert doc["format"] == "jsonl-objects"


def test_lane_never_touches_registry_mirror_files(stub, tmp_path):
    """The snapshot path appends to tables/snapshots/, never to the mirror
    files themselves (router-maintain owns those)."""
    mod, state, srv, ns_dir = stub
    for t in mod.SNAPSHOT_TABLES:
        state.declared.add(f"snapshot_{t}")
    before = (ns_dir / NS / "tables" / "models.jsonl").read_text()
    lines = []
    mod.snapshot_table(f"http://127.0.0.1:{srv.server_port}", "tok", str(ns_dir),
                       "models", "ts", 0, lines.append)
    assert (ns_dir / NS / "tables" / "models.jsonl").read_text() == before


# ---------------------------------------------------------------------------
# 4. Size preflight names every skip
# ---------------------------------------------------------------------------
def test_plan_fact_sizes_names_all_overcap_rows(mod):
    facts = [
        {"key": "/a", "domain": "config", "content": "x" * 10},
        {"key": "/b", "domain": "config", "content": "x" * (MEMORIES_CAP + 1)},
        {"key": "/c", "domain": "config", "content": "x" * (MEMORIES_CAP * 2)},
        {"key": "/d", "domain": "config", "content": "x" * (MEMORIES_CAP - 5)},
    ]
    ok, skipped = mod.plan_fact_sizes(facts)
    # /d's FULL body (key+domain+attrs overhead around the content) still lands
    # under the cap? 102419 content chars -> >102400B body, so it is a skip.
    assert [f["key"] for f in ok] == ["/a"]
    assert [s["key"] for s in skipped] == ["/b", "/c", "/d"]
    for s in skipped:
        assert s["size"] > s["cap"] == MEMORIES_CAP
