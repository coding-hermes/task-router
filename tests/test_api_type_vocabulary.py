"""Closed-vocabulary ratchet for models.jsonl api_type / data_class.

Card t_api_type_truncation: 512 of 2,179 rows carried a single-character
api_type (o/r/c) and 40 carried a single-character data_class (u/z), all
introduced by the 2026-10-14 filing wave b62eb204. The importers write full
strings, so nothing in the code path can be blamed — the only durable guard is
a test on the landed table. A truncated value is not a value: any consumer that
switches on api_type sees an unknown lane, and the async-only :batch lanes stop
being distinguishable from interactive ones.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TABLES = ROOT / "data" / "tables" / "models.jsonl"

API_TYPES = {"openai-chat", "responses", "chat_completions"}
DATA_CLASSES = {"zdr", "unknown", "training-opt-in"}


def _rows():
    with TABLES.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def test_api_type_is_in_the_closed_vocabulary():
    offenders = [
        (row.get("provider"), row.get("model"), row.get("api_type"))
        for row in _rows()
        if row.get("api_type") is not None and row.get("api_type") not in API_TYPES
    ]
    assert offenders == [], f"api_type outside {sorted(API_TYPES)}: {offenders[:5]}"


def test_data_class_is_in_the_closed_vocabulary():
    offenders = [
        (row.get("provider"), row.get("model"), row.get("data_class"))
        for row in _rows()
        if row.get("data_class") is not None and row.get("data_class") not in DATA_CLASSES
    ]
    assert offenders == [], f"data_class outside {sorted(DATA_CLASSES)}: {offenders[:5]}"


def test_no_single_character_api_type_or_data_class():
    """The exact shape of the defect: a one-character value is a truncation."""
    offenders = [
        (row.get("provider"), row.get("model"), row.get("api_type"), row.get("data_class"))
        for row in _rows()
        if (isinstance(row.get("api_type"), str) and len(row["api_type"]) == 1)
        or (isinstance(row.get("data_class"), str) and len(row["data_class"]) == 1)
    ]
    assert offenders == [], f"single-character values: {offenders[:5]}"
