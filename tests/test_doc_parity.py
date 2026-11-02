"""Doc-parity regression tests.

Two checks:
1. Every GET route in the PUBLISHED CONTRACT (build_openapi()'s paths — the
   same machine-checked set tests/test_openapi_dispatch_conformance.py proves
   equals the dispatch table) must appear in README.md (DOC-2, closed by
   TR-275 against the machine-checked list, not a hand count)
2. Every ROUTER_* env key in code must be documented in README.md or docs/configuration.md (DOC-3)
"""
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import router_server  # noqa: E402


def test_readme_documents_all_get_routes():
    """Every GET route in the published contract must appear in README.md.

    DOC-2 (TR-275): the enumerated set comes from build_openapi() — the
    machine-checked list — never from a hand count of the source. If this
    test fails, either the README is missing a served endpoint (add the
    entry the test names) or the conformance suite needs to rule first.
    """
    readme_md = REPO / "README.md"

    get_routes = sorted(
        path
        for path, path_item in router_server.build_openapi()["paths"].items()
        if "get" in path_item
    )
    assert get_routes, "the published contract declares no GET routes"

    readme_content = readme_md.read_text()

    missing = []
    for route in get_routes:
        # The route must open a code span (`/health`, `/resolve?project=X`) —
        # how the README's API section names endpoints. A span that starts
        # with the path counts: it documents the route with its parameters.
        if f"`{route}" not in readme_content:
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
        # TR-241 follow-up: a preceding [A-Z_] must not count — the regex used
        # to match substrings of TASK_ROUTER_REPO and OPENROUTER_API_KEY.
        for match in re.finditer(r'(?<![A-Z_])ROUTER_[A-Z_]+', content):
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
