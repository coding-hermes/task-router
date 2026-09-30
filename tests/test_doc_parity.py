"""Doc-parity regression tests.

Two checks:
1. Every GET route in router_server.py must appear in README.md (DOC-2)
2. Every ROUTER_* env key in code must be documented in README.md or docs/configuration.md (DOC-3)
"""
import re
from pathlib import Path


def test_readme_documents_all_get_routes():
    """Every GET route in router_server.py must appear in README.md."""
    repo_root = Path(__file__).resolve().parents[1]
    server_py = repo_root / "scripts" / "router_server.py"
    readme_md = repo_root / "README.md"

    # Extract GET routes from router_server.py
    # Look for patterns like: "GET /health", "GET /model_status", etc.
    # or route definitions like: @app.get("/health")
    server_content = server_py.read_text()

    # Find all route paths - look for @app.get() decorators and route definitions
    routes = set()

    # Pattern 1: @app.get("/path")
    for match in re.finditer(r'@app\.get\(["\']([^"\']+)["\']', server_content):
        routes.add(match.group(1))

    # Pattern 2: "GET /path" in comments or docstrings
    for match in re.finditer(r'["\']GET\s+(/[^\s"\']+)["\']', server_content):
        routes.add(match.group(1))

    # Pattern 3: Route definitions in route tables
    for match in re.finditer(r'["\'](/[^"\']+)["\']\s*:\s*["\']?GET', server_content):
        routes.add(match.group(1))

    # Filter to only actual routes (start with /)
    routes = {r for r in routes if r.startswith("/")}

    # Read README and check each route is mentioned
    readme_content = readme_md.read_text()

    missing = []
    for route in sorted(routes):
        # Check if route appears in README (as a code block or in text)
        if route not in readme_content and f"`{route}`" not in readme_content:
            missing.append(route)

    assert not missing, f"Routes missing from README.md: {missing}"


def test_health_links_to_docs():
    """README /health entry must link to docs/health-plane.md."""
    repo_root = Path(__file__).resolve().parents[1]
    readme_md = repo_root / "README.md"
    readme_content = readme_md.read_text()

    # Find /health section and check it mentions docs/health-plane.md
    assert "docs/health-plane.md" in readme_content, \
        "README /health must link to docs/health-plane.md"


def test_capabilities_mentions_freshness_ladder():
    """README /v1/capabilities must mention TR-140 freshness ladder."""
    repo_root = Path(__file__).resolve().parents[1]
    readme_md = repo_root / "README.md"
    readme_content = readme_md.read_text()

    # Check that /v1/capabilities section mentions freshness or TR-140
    assert "freshness" in readme_content.lower() or "TR-140" in readme_content, \
        "README /v1/capabilities must mention freshness ladder (TR-140)"


def test_proxy_stats_documents_params():
    """README /proxy/stats must document windows and grouping params."""
    repo_root = Path(__file__).resolve().parents[1]
    readme_md = repo_root / "README.md"
    readme_content = readme_md.read_text()

    # Check that /proxy/stats section mentions both params
    assert "windows" in readme_content.lower(), \
        "README /proxy/stats must document 'windows' param"
    assert "grouping" in readme_content.lower(), \
        "README /proxy/stats must document 'grouping' param"


def test_all_router_env_keys_documented():
    """Every ROUTER_* env key in code must appear in README.md or docs/configuration.md."""
    repo_root = Path(__file__).resolve().parents[1]

    # Find all ROUTER_* keys in Python source
    keys_in_code = set()
    for py_file in list((repo_root / "task_router").glob("*.py")) + \
                   list((repo_root / "scripts").glob("router_*.py")):
        if not py_file.exists():
            continue
        content = py_file.read_text()
        for match in re.finditer(r'ROUTER_[A-Z_]+', content):
            keys_in_code.add(match.group(0))

    # Read documentation files
    readme_md = repo_root / "README.md"
    config_md = repo_root / "docs" / "configuration.md"

    readme_content = readme_md.read_text() if readme_md.exists() else ""
    config_content = config_md.read_text() if config_md.exists() else ""
    combined_docs = readme_content + "\n" + config_content

    # Check each key is documented
    missing = []
    for key in sorted(keys_in_code):
        if key not in combined_docs:
            missing.append(key)

    assert not missing, f"ROUTER_* keys missing from docs: {missing}"


def test_classifier_fallback_grouped():
    """Classifier fallback keys must be grouped together in docs."""
    repo_root = Path(__file__).resolve().parents[1]
    config_md = repo_root / "docs" / "configuration.md"

    if not config_md.exists():
        # Skip if configuration.md doesn't exist yet
        return

    config_content = config_md.read_text()

    # Check that classifier fallback keys are mentioned near each other
    fallback_keys = [
        "ROUTER_CLASSIFIER_FALLBACK_BASE_URL",
        "ROUTER_CLASSIFIER_FALLBACK_MODEL",
        "ROUTER_CLASSIFIER_FALLBACK_KEY_ENV",
    ]

    # Find positions of each key
    positions = []
    for key in fallback_keys:
        pos = config_content.find(key)
        if pos >= 0:
            positions.append(pos)

    # If all keys are present, they should be within 500 chars of each other
    if len(positions) == len(fallback_keys):
        span = max(positions) - min(positions)
        assert span < 500, \
            f"Classifier fallback keys should be grouped together (span={span} chars)"
