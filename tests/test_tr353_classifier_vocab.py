"""TR-353: classifier vocabulary comes from reconciled generated tables."""
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DATA_DIR = REPO / "data" / "tables"
sys.path.insert(0, str(REPO / "scripts"))
import router_classify as rc  # noqa: E402


def _table_categories(name):
    path = DATA_DIR / f"{name}.jsonl"
    return {
        json.loads(line)["category"]
        for line in path.read_text().splitlines()
        if line.strip()
    }


def _registry_from_committed_tables(path):
    requirements = [
        json.loads(line)
        for line in (DATA_DIR / "task_profile_requirements.jsonl").read_text().splitlines()
        if line.strip()
    ]
    path.write_text(json.dumps({"tables": {"task_profile_requirements": requirements}}))


def test_committed_category_tables_have_one_reconciled_vocabulary():
    """The generated classifier source and capability ladder must not drift."""
    assert _table_categories("task_profile_requirements") == _table_categories("category_levels")


def test_registry_categories_and_matrix_preserve_ui_frontend(tmp_path, monkeypatch):
    """TR-353 regression: ui_frontend is a known, valid classifier category."""
    registry = tmp_path / "data" / "registry.json"
    registry.parent.mkdir()
    _registry_from_committed_tables(registry)
    monkeypatch.setenv("ROUTING_REGISTRY", str(registry))

    categories = rc.registry_categories()
    assert "ui_frontend" in categories

    matrix, confidence, problems = rc.validate_matrix(
        {"categories": {"ui_frontend": 2, "review": 3}}, categories
    )
    assert matrix == {"ui_frontend": 2, "review": 3}
    assert confidence is None
    assert problems == []
