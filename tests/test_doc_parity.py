"""DOC-2: Verify README documents every GET route in router_server.py.

Extracts GET routes from scripts/router_server.py's get_paths dict and
verifies each is mentioned in README.md. Zero misses allowed.
"""
import re
from pathlib import Path


def test_readme_documents_all_get_routes():
    """Every GET route in router_server.py must appear in README.md."""
    repo_root = Path(__file__).resolve().parents[1]
    server_path = repo_root / "scripts" / "router_server.py"
    readme_path = repo_root / "README.md"
    
    # Extract GET routes from get_paths dict in router_server.py
    server_content = server_path.read_text()
    match = re.search(r'get_paths = \{(.*?)\n    \}', server_content, re.DOTALL)
    assert match, "Could not find get_paths dict in router_server.py"
    
    routes = re.findall(r'"(/[^"]+)":', match.group(1))
    assert routes, "No GET routes found in get_paths dict"
    
    # Read README
    readme_content = readme_path.read_text()
    
    # Check each route is mentioned in README
    missing = []
    for route in routes:
        # Escape special regex chars in route path
        escaped = re.escape(route)
        # Look for the route in README (as literal text, possibly in backticks)
        if not re.search(escaped, readme_content):
            missing.append(route)
    
    assert not missing, f"README.md missing documentation for routes: {missing}"


def test_health_links_to_canary_contract():
    """/health entry must link to docs/health-plane.md."""
    repo_root = Path(__file__).resolve().parents[1]
    readme_path = repo_root / "README.md"
    readme_content = readme_path.read_text()
    
    # Find the /health documentation section
    health_match = re.search(r'`/health`[^)]*\)', readme_content)
    assert health_match, "Could not find /health documentation"
    
    health_section = health_match.group(0)
    assert "docs/health-plane.md" in health_section, \
        "/health entry must link to docs/health-plane.md"


def test_capabilities_explains_freshness_ladder():
    """/v1/capabilities must explain TR-140 freshness ladder."""
    repo_root = Path(__file__).resolve().parents[1]
    readme_path = repo_root / "README.md"
    readme_content = readme_path.read_text()
    
    # Find /v1/capabilities documentation
    caps_match = re.search(r'`/v1/capabilities`[^)]*\)', readme_content)
    assert caps_match, "Could not find /v1/capabilities documentation"
    
    caps_section = caps_match.group(0)
    
    # Must mention the freshness ladder concepts
    assert "live" in caps_section.lower(), \
        "/v1/capabilities must mention live probe"
    assert "stale" in caps_section.lower(), \
        "/v1/capabilities must mention stale startup probe"
    assert "error" in caps_section.lower() or "unavailable" in caps_section.lower(), \
        "/v1/capabilities must mention error/unavailable case"


def test_proxy_stats_documents_params():
    """/proxy/stats must document windows and grouping params."""
    repo_root = Path(__file__).resolve().parents[1]
    readme_path = repo_root / "README.md"
    readme_content = readme_path.read_text()
    
    # Find /proxy/stats documentation
    stats_match = re.search(r'`/proxy/stats`[^)]*\)', readme_content)
    assert stats_match, "Could not find /proxy/stats documentation"
    
    stats_section = stats_match.group(0)
    
    # Must document both params
    assert "windows" in stats_section.lower(), \
        "/proxy/stats must document windows param"
    assert "grouping" in stats_section.lower(), \
        "/proxy/stats must document grouping param"
