"""TR-238 — bunker provisioning + proxy client-auth docs must state the real contract.

The board row is a docs/provisioning task. These tests pin the DOCUMENTATION
contract (no network, no live bunker provisioning):

  1. docs/router-in-bunker-shortcut.md carries the one-command Hermes install
     (uv tool install), the api_key_env/literal api_key auth trap, the
     stdin-piped ASCII remote-script convention with the $( ) --script trap,
     and the recorded 2026-09-29 smoke (BUNKER-OK-FINAL, exit 0, one ledger
     row) labelled as history, NOT as something re-run for this document.
  2. docs/configuration.md states how remote clients authenticate to the
     proxy: loopback bind by default, off-box via tunnel/facade,
     ROUTER_PROXY_AUTH=passthrough as an explicit opt-in for the proxy paths
     only, and normal mutations still requiring edit authorization.
  3. Code cross-check: the documented passthrough semantics match
     scripts/router_server.py (literal 'passthrough', PROXY_PATHS only).
  4. Neither doc leaks a credential-shaped literal.
"""
import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUNKER_DOC = os.path.join(REPO, "docs", "router-in-bunker-shortcut.md")
CONFIG_DOC = os.path.join(REPO, "docs", "configuration.md")
SERVER_PY = os.path.join(REPO, "scripts", "router_server.py")


def _read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _read_flat(path):
    """Whitespace-normalized text so phrase pins survive markdown reflow."""
    return " ".join(_read(path).split())


# --- 1. Provisioning doc (router-in-bunker-shortcut.md) -------------------


def test_bunker_doc_carries_the_one_command_hermes_install():
    doc = _read(BUNKER_DOC)
    assert doc.count("uv tool install") >= 1, (
        "docs/router-in-bunker-shortcut.md must contain the uv install line "
        "so one Hermes per person is one command"
    )
    assert "hermes-agent" in doc  # the package name on the uv line
    assert "hermes" in doc and "hermes-acp" in doc  # the executables it provides


def test_bunker_doc_states_the_api_key_env_trap_and_the_literal_form():
    doc = _read(BUNKER_DOC)
    assert "api_key_env" in doc, "the auth trap must be named"
    assert "literal" in doc and "api_key" in doc, (
        "the literal api_key-in-config.yaml form (recorded verified) must be stated"
    )
    assert "process environment" in doc, (
        "api_key_env resolves from the client PROCESS environment - that is the trap"
    )


def test_bunker_doc_records_the_remote_script_convention():
    doc = _read(BUNKER_DOC)
    assert "stdin" in doc, "scripts are piped on stdin"
    assert "ASCII" in doc, "scripts must be ASCII-only"
    assert "$( )" in doc, "the $( ) quoting trap must be recorded verbatim"
    assert "--script" in doc, "the trap names the mangled transport"


def test_bunker_doc_records_the_smoke_as_history_not_a_rerun():
    doc = _read(BUNKER_DOC)
    assert "BUNKER-OK-FINAL" in doc, "the recorded sentinel must appear"
    assert "hermes chat -q" in doc, "the recorded hermes chat -q pattern must appear"
    assert "exit code: 0" in doc, "the recorded exit status must appear"
    assert "recorded history" in doc, "the smoke must be labelled recorded history"
    assert "re-ran" not in doc.lower(), (
        "the doc must NOT claim the external smoke was executed for this task"
    )


def test_bunker_doc_leaks_no_credential_shaped_literal():
    doc = _read(BUNKER_DOC)
    # sk- style keys, or a bearer-ish long secret, must never appear in docs.
    assert not re.search(r"\bsk-[A-Za-z0-9_-]{8,}", doc)
    assert not re.search(r"(?i)\b(api[_-]?key|token|secret)\s*[:=]\s*['\"]?[A-Za-z0-9+/_-]{16,}", doc)


# --- 2. Client-auth doc (configuration.md) --------------------------------


def test_config_doc_states_proxy_client_authentication_contract():
    doc = _read_flat(CONFIG_DOC)
    assert "ROUTER_PROXY_AUTH" in doc, (
        "docs/configuration.md must document ROUTER_PROXY_AUTH (it is read "
        "by scripts/router_server.py)"
    )
    assert "passthrough" in doc, "the opt-in value must be named"
    assert "explicit opt-in" in doc, "passthrough is an explicit opt-in, not a default"
    assert "127.0.0.1" in doc, "the loopback-by-default bind must be stated"
    assert "tunnel" in doc, "off-box clients arrive over a tunnel/facade, not a rebind"
    assert "still require edit authorization" in doc, (
        "passthrough must not be readable as disabling all auth: normal router "
        "API mutations still require edit authorization"
    )


def test_config_doc_proxy_auth_section_sits_in_the_proxy_group():
    doc = _read(CONFIG_DOC)
    # The section must live inside the Proxy chapter (after its header, before
    # the next top-level chapter), so the ToC and readers find it.
    proxy_at = doc.index("## Proxy")
    spawn_at = doc.index("## Spawn and sorting")
    section = doc.index("### Proxy client authentication")
    assert proxy_at < section < spawn_at


def test_config_doc_leaks_no_credential_shaped_literal():
    doc = _read(CONFIG_DOC)
    assert not re.search(r"\bsk-[A-Za-z0-9_-]{8,}", doc)
    assert not re.search(r"(?i)\b(api[_-]?key|token|secret)\s*[:=]\s*['\"]?[A-Za-z0-9+/_-]{16,}", doc)


# --- 3. Code cross-check: docs must match scripts/router_server.py --------


def test_documented_passthrough_semantics_match_the_code():
    server = _read(SERVER_PY)
    # literal comparison (no prefix/lowercase variants) - docs say exact value
    assert "os.environ.get('ROUTER_PROXY_AUTH') == 'passthrough'" in server
    # gate applies only to the proxy paths, and defaults OFF (absence of the
    # env var leaves _authorize in place for every POST)
    m = re.search(r"PROXY_PATHS = \((.*?)\)", server, re.S)
    assert m, "PROXY_PATHS table must exist"
    for path in ("/v1/chat/completions", "/v1/responses"):
        assert path in m.group(1), f"{path} must be a proxy path"
        assert path in _read(CONFIG_DOC), (
            f"{path} must be named in the client-auth doc section"
        )
