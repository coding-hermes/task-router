"""TR-324: verify /api/ui/quota route exists on router_web.py :9093 door."""
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WEB_SCRIPT = REPO / "scripts" / "router_web.py"


def test_quota_route_exists_in_source():
    """The route handler must be present in router_web.py source."""
    source = WEB_SCRIPT.read_text()
    assert '"/api/ui/quota"' in source, "quota route missing from router_web.py"
    assert "router_quota_panel" in source, "quota panel import missing"


def test_quota_route_returns_200_not_404():
    """Live probe: the route must return 200, not 404."""
    # Start the web server
    proc = subprocess.Popen(
        [sys.executable, str(WEB_SCRIPT), "--port", "19093"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=str(REPO)
    )
    try:
        time.sleep(2)  # let server start
        result = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
             "http://127.0.0.1:19093/api/ui/quota"],
            capture_output=True, text=True, timeout=10
        )
        status = result.stdout.strip()
        assert status == "200", f"quota route returned {status}, expected 200"
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_quota_route_returns_windows_structure():
    """Live probe: response must contain windows array (fail-open shape)."""
    proc = subprocess.Popen(
        [sys.executable, str(WEB_SCRIPT), "--port", "19094"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=str(REPO)
    )
    try:
        time.sleep(2)
        result = subprocess.run(
            ["curl", "-s", "http://127.0.0.1:19094/api/ui/quota"],
            capture_output=True, text=True, timeout=10
        )
        import json
        data = json.loads(result.stdout)
        assert "windows" in data, "response missing 'windows' key"
        assert isinstance(data["windows"], list), "'windows' must be a list"
    finally:
        proc.terminate()
        proc.wait(timeout=5)
