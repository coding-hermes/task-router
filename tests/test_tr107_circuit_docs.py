"""TR-107 — circuit-cooldown docs must match the class-based flat code.

The code applies a FLAT per-class cooldown (CLASS_COOLDOWN_S in
scripts/router_circuit.py: overload=120s, quota_window=300s, api_down=1800s,
out_of_credit=14400s). Two prose surfaces still claimed the pre-TR-014
exponential algorithm ("5m, double per consecutive failure, cap 1h"). These
tests pin BOTH surfaces to the real table and forbid any doubling/backoff
claim in either file:

  1. docs/integration.md TASK-ROUTER-002 section carries the class table.
  2. The router_circuit.py module docstring carries the class table.
  3. Neither surface claims a doubling/backoff algorithm anymore.
  4. Code-level proof: consecutive failures keep the class cooldown flat.
"""
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "scripts", "router_circuit.py")
DOC = os.path.join(REPO, "docs", "integration.md")

#: The authoritative per-class cooldown table (seconds), as in the code.
EXPECTED = {
    "overload": 120,
    "quota_window": 300,
    "api_down": 1800,
    "out_of_credit": 14400,
}


def _load_module():
    """Import scripts/router_circuit.py as a module, immune to host env."""
    saved = os.environ.pop("ROUTING_CIRCUIT_COOLDOWN_JSON", None)
    try:
        spec = importlib.util.spec_from_file_location(
            "router_circuit_tr107", SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        if saved is not None:
            os.environ["ROUTING_CIRCUIT_COOLDOWN_JSON"] = saved


def test_class_cooldown_table_matches_the_documented_values():
    """The code table itself must be the flat per-class set the docs claim."""
    mod = _load_module()
    table = {k: int(v) for k, v in mod.CLASS_COOLDOWN_S.items()}
    assert table == EXPECTED


def test_script_docstring_states_the_flat_class_table():
    """The invariant must live in the script's own docstring, not only here."""
    doc = _load_module().__doc__ or ""
    for klass, seconds in EXPECTED.items():
        assert klass in doc, f"docstring must name class {klass}"
        assert str(seconds) in doc, f"docstring must state {klass}={seconds}s"


def test_integration_md_carries_the_class_cooldown_table():
    """The TASK-ROUTER-002 section must state the flat class table."""
    text = open(DOC, encoding="utf-8").read()
    section = text.split("## TASK-ROUTER-002", 1)[1].split("\n## ", 1)[0]
    assert "overload=120s" in section
    assert "quota_window=300s" in section
    assert "api_down=1800s" in section
    assert "out_of_credit=14400s" in section
    for klass in EXPECTED:
        assert klass in section, f"breaker section must name class {klass}"


def test_no_doubling_or_backoff_claim_survives():
    """No prose surface may claim the removed exponential algorithm."""
    for path in (SCRIPT, DOC):
        text = open(path, encoding="utf-8").read()
        for pat in (r"double\s+per\s+consecutive",
                    r"cap\s+1h",
                    r"5m,\s*double"):
            assert re.search(pat, text, re.IGNORECASE) is None, (path, pat)


def test_flat_cooldown_does_not_grow_with_consecutive_failures():
    """Code-level proof: repeating failures keeps the class cooldown flat."""
    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ, ROUTER_STATE_DIR=tmp)
        cds = []
        for _ in range(4):
            subprocess.run(
                [sys.executable, SCRIPT, "record-failure",
                 "prov", "m", "--class", "overload", "x"],
                env=env, check=True, capture_output=True, timeout=60)
            with open(os.path.join(tmp, "circuit-state.json")) as f:
                st = json.load(f)
            cds.append(st["pairs"]["prov/m"]["cooldown_s"])
        assert cds == [120, 120, 120, 120]
