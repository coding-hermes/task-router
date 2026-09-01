"""TR-017 web UI tests — settings + resolve preview, key-gated edits.

Covers the HTTP handlers of scripts/router_web.py in-process (no real network
beyond localhost, unique ports, hermetic table copies). Writes go to a temp
copy of data/tables so the committed tables stay byte-identical.
"""
import contextlib
import json
import os
import sys
from pathlib import Path
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request

import pytest

REPO = Path(__file__).resolve().parents[1]
PY = (
    "/home/kara/.hermes/venvs/board/bin/python3"
    if os.path.exists("/home/kara/.hermes/venvs/board/bin/python3")
    else sys.executable  # CI / fresh clone: no Bane-host venv
)
WEB = REPO / "scripts" / "router_web.py"


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _request(port, path, *, method="GET", body=None, key=None):
    headers = {"Content-Type": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
    if key is not None:
        headers["X-API-Key"] = key
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {}


@contextlib.contextmanager
def _server(mode="read-only", key=None, data_dir=None):
    port = _free_port()
    env = dict(os.environ)
    if data_dir is not None:
        env["ROUTING_DATA_DIR"] = str(data_dir)
    env["ROUTING_REGISTRY"] = str(REPO / "registry.json")
    if key is None:
        env.pop("ROUTER_EDIT_API_KEY", None)
    else:
        env["ROUTER_EDIT_API_KEY"] = key
    proc = subprocess.Popen(
        [PY, str(WEB), "--mode", mode, "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(REPO), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        if mode == "edit" and key is None:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    out, err = proc.communicate()
                    yield {"refused": True, "stderr": err, "returncode": proc.returncode,
                           "port": port}
                    return
                time.sleep(0.05)
            yield {"refused": False, "port": port}
            return
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                out, err = proc.communicate()
                pytest.fail(f"server exited {proc.returncode}: {out} {err}")
            try:
                _request(port, "/api/status")
                break
            except OSError:
                time.sleep(0.05)
        else:
            pytest.fail("server did not become ready")
        yield {"port": port}
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


@pytest.fixture
def data_dir(tmp_path):
    d = tmp_path / "tables"
    shutil.copytree(REPO / "data" / "tables", d)
    return d


# ---------------------------------------------------------------- read-only #


def test_read_only_serves_html_and_endpoints(data_dir):
    with _server(mode="read-only", data_dir=data_dir) as s:
        port = s["port"]
        code, html = _request(port, "/")
        assert code == 200
        assert "router" in html.lower()
        assert "Resolve" in html
        assert "<!doctype html>" in html.lower()

        code, payload = _request(port, "/api/status")
        assert code == 200 and payload["mode"] == "read-only"

        code, payload = _request(port, "/api/profiles")
        assert code == 200
        assert any(p["id"] == "P0_FORE" for p in payload["profiles"])

        for path in ("/api/settings/providers", "/api/settings/profiles",
                     "/api/settings/discounts"):
            code, payload = _request(port, path)
            assert code == 200 and "rows" in payload


def test_read_only_blocks_writes(data_dir):
    with _server(mode="read-only", data_dir=data_dir) as s:
        port = s["port"]
        code, _ = _request(port, "/api/settings/providers", method="POST",
                           body={"id": "clinepass", "archive": True})
        assert code == 403


def test_preview_matches_real_resolve(data_dir):
    """Web preview head/chain must equal direct router_spawn.py output."""
    with _server(mode="read-only", data_dir=data_dir) as s:
        port = s["port"]
        code, web = _request(port, "/api/preview?project=my-project")
        assert code == 200 and web["ok"] is True
        direct = subprocess.run(
            [PY, str(REPO / "scripts" / "router_spawn.py"), "my-project", "--format", "json"],
            capture_output=True, text=True, cwd=str(REPO),
            env={**os.environ, "ROUTING_DATA_DIR": str(data_dir),
                 "ROUTING_REGISTRY": str(REPO / "registry.json")},
        ).stdout
        d = json.loads(direct)
        # CI has no gate-state dir — the resolver fail-closes to an empty
        # head (by design). The preview-parity assertion only makes sense
        # when a head resolved at all.
        if d.get("head") is None or web.get("head") is None:
            assert d.get("head") == web.get("head"), (
                "spawn and web preview disagree on fail-closed emptiness")
            return
        assert (d["head"]["provider"], d["head"]["model"], str(d["head"]["usd_1m"])) == \
               (web["head"]["provider"], web["head"]["model"], str(web["head"]["usd_1m"]))
        assert len(d["chain"]) == len(web["hops"])
        for a, b in zip(d["chain"], web["hops"]):
            assert (a["provider"], a["model"]) == (b["provider"], b["model"])
        assert len(d["gate_reasons"]) == len(web["gate_reasons"])
        assert len(d["exclusions"]) == len(web["exclusions"])
        assert web["hops"][0]["est_cost_usd"] is not None


def test_preview_by_profile(data_dir):
    with _server(mode="read-only", data_dir=data_dir) as s:
        port = s["port"]
        code, web = _request(port, "/api/preview?profile=P1_CODING")
        assert code == 200 and web["ok"] is True
        assert web["resolve"]["profile"] == "P1_CODING"


# ------------------------------------------------------------------- edit #


def test_edit_mode_refuses_without_key(tmp_path):
    d = tmp_path / "tables"
    shutil.copytree(REPO / "data" / "tables", d)
    with _server(mode="edit", key=None, data_dir=d) as s:
        assert s.get("refused") is True
        assert "ROUTER_EDIT_API_KEY" in s.get("stderr", "")
        try:
            _request(s["port"], "/api/status")
            assert False, "server should not be listening"
        except urllib.error.URLError:
            pass


def test_edit_gate_401_then_200_and_write(data_dir):
    with _server(mode="edit", key="testkey", data_dir=data_dir) as s:
        port = s["port"]
        code, _ = _request(port, "/api/settings/providers", method="POST",
                           body={"id": "clinepass", "archive": True})
        assert code == 401
        code, _ = _request(port, "/api/settings/providers", method="POST",
                           body={"id": "clinepass", "archive": True}, key="wrong")
        assert code == 401
        code, payload = _request(port, "/api/settings/providers", method="POST",
                                 body={"id": "clinepass", "archive": True}, key="testkey")
        assert code == 200 and payload["ok"] is True
        assert "backup" in payload
        rows = [json.loads(l) for l in (data_dir / "providers.jsonl").read_text().splitlines()
                if l.strip()]
        cline = next(r for r in rows if r["id"] == "clinepass")
        assert cline["archive"] is True
        assert list(data_dir.glob("providers.jsonl.bak-*"))


def test_edit_is_surgical_and_restorable(data_dir):
    before = (data_dir / "providers.jsonl").read_text()
    cline_before = next(l for l in before.splitlines() if '"clinepass"' in l)
    with _server(mode="edit", key="testkey", data_dir=data_dir) as s:
        port = s["port"]
        _request(port, "/api/settings/providers", method="POST",
                 body={"id": "clinepass", "archive": True}, key="testkey")
    after = (data_dir / "providers.jsonl").read_text()
    bef_lines = before.splitlines()
    aft_lines = after.splitlines()
    assert len(bef_lines) == len(aft_lines)
    changed = [i for i, (b, a) in enumerate(zip(bef_lines, aft_lines)) if b != a]
    assert len(changed) == 1
    _restore_row(data_dir, "providers.jsonl", "clinepass", cline_before)
    assert (data_dir / "providers.jsonl").read_text() == before


def _restore_row(data_dir, fname, row_id, original_line):
    import importlib.util
    spec = importlib.util.spec_from_file_location("rw", str(REPO / "scripts" / "router_web.py"))
    rw = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rw)
    path = data_dir / fname
    rows = rw._jsonl_read(path)
    for r in rows:
        if r.get("id") == row_id:
            r.clear()
            r.update(json.loads(original_line))
            break
    rw._jsonl_write_atomic(path, rows)


def test_profile_requirement_upsert_and_discount_upsert(data_dir):
    with _server(mode="edit", key="testkey", data_dir=data_dir) as s:
        port = s["port"]
        code, payload = _request(port, "/api/settings/profiles/requirements",
                                 method="POST",
                                 body={"task_id": "P0_FORE", "category": "code_gen",
                                       "level": 5}, key="testkey")
        assert code == 200 and payload["ok"] is True
        rows = [json.loads(l) for l in (data_dir / "task_profile_requirements.jsonl").read_text().splitlines()
                if l.strip()]
        rec = next(r for r in rows if r["task_id"] == "P0_FORE" and r["category"] == "code_gen")
        assert rec["level"] == 5

        code, payload = _request(port, "/api/settings/discounts", method="POST",
                                 body={"provider": "clinepass", "model": "kimi-k3",
                                       "discount_type": "percent", "value": 0.5},
                                 key="testkey")
        assert code == 200 and payload["ok"] is True
        rows = [json.loads(l) for l in (data_dir / "temporary_discounts.jsonl").read_text().splitlines()
                if l.strip()]
        rec = next(r for r in rows if r["provider"] == "clinepass" and r["model"] == "kimi-k3")
        assert rec["value"] == 0.5


def test_html_has_no_external_resources(data_dir):
    with _server(mode="read-only", data_dir=data_dir) as s:
        port = s["port"]
        _, html = _request(port, "/")
        assert "https://" not in html.lower()
        assert "cdn" not in html.lower()
        assert "<script>" in html
        assert "src=\"http" not in html


def test_missing_provider_edit_returns_404(data_dir):
    with _server(mode="edit", key="testkey", data_dir=data_dir) as s:
        port = s["port"]
        code, payload = _request(port, "/api/settings/providers", method="POST",
                                 body={"id": "does-not-exist", "archive": True}, key="testkey")
        assert code == 404
