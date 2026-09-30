"""Doc-parity regression test: every ROUTER_* env key in code must be documented.

This test greps task_router/*.py and scripts/router_*.py for ROUTER_[A-Z_]+ keys,
then checks that each key appears in README.md or docs/configuration.md.
"""
import re
from pathlib import Path


def test_all_router_env_keys_documented():
    """Every ROUTER_* env key used in code must appear in documentation."""
    repo_root = Path(__file__).parent.parent
    
    # Collect all ROUTER_* keys from source code
    source_dirs = [
        repo_root / "task_router",
        repo_root / "scripts",
    ]
    
    keys_in_code = set()
    pattern = re.compile(r'\b(ROUTER_[A-Z_]+|ROUTERMODEL_[A-Z_]+)\b')
    
    for source_dir in source_dirs:
        if not source_dir.exists():
            continue
        for py_file in source_dir.glob("*.py"):
            content = py_file.read_text(errors="ignore")
            keys_in_code.update(pattern.findall(content))
    
    # Read documentation files
    readme_path = repo_root / "README.md"
    config_doc_path = repo_root / "docs" / "configuration.md"
    
    readme_content = readme_path.read_text() if readme_path.exists() else ""
    config_doc_content = config_doc_path.read_text() if config_doc_path.exists() else ""
    all_docs = readme_content + "\n" + config_doc_content
    
    # Check each key is documented
    missing = []
    for key in sorted(keys_in_code):
        if key not in all_docs:
            missing.append(key)
    
    assert not missing, (
        f"The following {len(missing)} ROUTER_* env keys are used in code but not documented "
        f"in README.md or docs/configuration.md:\n" +
        "\n".join(f"  - {k}" for k in missing)
    )


def test_classifier_fallback_grouped():
    """The classifier fallback keys must be grouped together in documentation."""
    repo_root = Path(__file__).parent.parent
    config_doc_path = repo_root / "docs" / "configuration.md"
    
    if not config_doc_path.exists():
        # Fallback to README if docs/configuration.md doesn't exist
        config_doc_path = repo_root / "README.md"
    
    content = config_doc_path.read_text()
    
    # Check for the classifier fallback subsection
    assert "Classifier fallback" in content or "classifier fallback" in content.lower(), (
        "Documentation must have a 'Classifier fallback' subsection grouping the "
        "ROUTER_CLASSIFIER_FALLBACK_* keys"
    )
    
    # Check all fallback keys are present
    fallback_keys = [
        "ROUTER_CLASSIFIER_FALLBACK_BASE_URL",
        "ROUTER_CLASSIFIER_FALLBACK_MODEL",
        "ROUTER_CLASSIFIER_FALLBACK_KEY_ENV",
        "ROUTER_CLASSIFIER_FALLBACK_KEY_VALUE",
        "ROUTER_CLASSIFIER_FALLBACK_TIMEOUT_S",
    ]
    
    for key in fallback_keys:
        assert key in content, f"{key} must be documented in the classifier fallback section"


def test_documentation_links():
    """README.md must link to docs/configuration.md for the complete reference."""
    repo_root = Path(__file__).parent.parent
    readme_path = repo_root / "README.md"
    
    readme_content = readme_path.read_text()
    
    assert "docs/configuration.md" in readme_content, (
        "README.md must link to docs/configuration.md for the complete configuration reference"
    )

</content>
</invoke>

@@CALL_TOOL name=terminal