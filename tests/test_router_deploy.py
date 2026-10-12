"""Pure decision tests for router_deploy: applying is explicit; readiness proves vintage."""
import sys
from types import SimpleNamespace
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import router_deploy as deploy  # noqa: E402


def payload(pid=2, ready=True, digest="expected"):
    return (
        200 if ready else 503,
        {"ready": ready, "draining": not ready, "inflight": 0},
        200,
        {"runtime": {"pid": pid, "source_digest": digest,
                      "source_revision": "abc123", "source_dirty": False}},
    )


def test_deploy_requires_apply_flag_before_restarting_anything():
    args = deploy.parse_args([])
    assert args.apply is False
    assert args.services == ["task-router-server", "task-router-proxy"]
    assert deploy.parse_args(["--service", "task-router-proxy"]).services == ["task-router-proxy"]


def test_dry_run_compiles_and_never_calls_systemd(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(deploy.router_drain, "runtime_identity", lambda _repo: {
        "source_revision": "abc", "source_dirty": False, "source_digest": "digest"})
    monkeypatch.setattr(deploy, "_preflight", lambda _repo: SimpleNamespace(returncode=0, stderr=""))
    code, report = deploy.deploy(repo=tmp_path, apply=False,
                                 systemctl=lambda *a, **k: calls.append(a))
    assert code == 0
    assert report["dry_run"] is True
    assert len(report["plan"]) == 2
    assert calls == []


def test_deploy_refuses_dirty_source_without_explicit_override(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(deploy.router_drain, "runtime_identity", lambda _repo: {
        "source_revision": "abc", "source_dirty": True, "source_digest": "digest"})
    code, report = deploy.deploy(repo=tmp_path, apply=True,
                                 systemctl=lambda *a, **k: calls.append(a))
    assert code == 2
    assert "dirty" in report["error"]
    assert calls == []


def test_apply_restarts_sequentially_and_requires_both_verdicts(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(deploy.router_drain, "runtime_identity", lambda _repo: {
        "source_revision": "abc", "source_dirty": False, "source_digest": "digest"})
    monkeypatch.setattr(deploy, "_preflight", lambda _repo: SimpleNamespace(returncode=0, stderr=""))
    monkeypatch.setattr(deploy, "_old_pid", lambda unit: 10 if "server" in unit else 20)
    monkeypatch.setattr(deploy, "_wait_ready", lambda port, old_pid, digest, timeout: {
        "ok": True, "runtime": {"pid": old_pid + 1, "source_digest": digest}, "checks": []})

    def fake_systemctl(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    code, report = deploy.deploy(apply=True, repo=tmp_path, systemctl=fake_systemctl)
    assert code == 0
    assert report["ok"] is True
    assert calls == [("restart", "task-router-server.service"),
                     ("restart", "task-router-proxy.service")]


def test_deploy_health_accepts_new_ready_process_with_expected_source():
    result = deploy.verify_deployment(*payload(pid=20), old_pid=10,
                                      expected_digest="expected")
    assert result["ok"] is True
    assert result["checks"] == []


def test_deploy_rejects_old_pid_even_if_health_is_green():
    result = deploy.verify_deployment(*payload(pid=10), old_pid=10,
                                      expected_digest="expected")
    assert result["ok"] is False
    assert "pid did not change" in result["checks"][0]


def test_deploy_rejects_source_digest_mismatch():
    result = deploy.verify_deployment(*payload(pid=20, digest="stale"),
                                      old_pid=10, expected_digest="expected")
    assert result["ok"] is False
    assert "source digest mismatch" in result["checks"][0]


def test_deploy_rejects_not_ready_process():
    result = deploy.verify_deployment(*payload(pid=20, ready=False),
                                      old_pid=10, expected_digest="expected")
    assert result["ok"] is False
    assert "not ready" in result["checks"][0]


def test_deploy_rejects_error_http_statuses():
    http, ready, hstatus, health = payload(pid=20)
    result = deploy.verify_deployment(503, ready, 500, health,
                                     old_pid=10, expected_digest="expected")
    assert result["ok"] is False
    assert any("HTTP" in x for x in result["checks"])
