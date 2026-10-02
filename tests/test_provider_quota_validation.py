"""TR-206 — provider_quota.jsonl L0 declaration contract (validator + data).

The quota-layers spec (docs/quota-layers-spec.md §2, §6, §7) makes L0 a DATA
contract: which windows a provider has, with unknown limits as NULL + reason
(never 0), provenance on every row, aliases inheriting their parent's windows
instead of declaring their own, and pools as their own scope. This file pins
that contract twice over:

  - the REAL committed table validates clean (a data regression fails here);
  - the validator REJECTS each contract break, proven on purpose-built bad
    tables (schema / missing-reason / alias / pool / coverage / duplicates).

Nothing in scripts/ hard-codes a limit: if a number is not in the table, it is
unknown — the no-hard-coded-limits scan below keeps it that way.
"""

import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import validate_provider_quota as vpq  # noqa: E402

TABLES = os.path.join(REPO, "data", "tables")
QUOTA = os.path.join(TABLES, "provider_quota.jsonl")
PROVIDERS = os.path.join(TABLES, "providers.jsonl")


def _load(path):
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _write_tables(tmp_path, quota_rows, provider_rows):
    tables = tmp_path / "tables"
    tables.mkdir()
    with open(tables / "provider_quota.jsonl", "w", encoding="utf-8") as fh:
        for r in quota_rows:
            fh.write(json.dumps(r) + "\n")
    with open(tables / "providers.jsonl", "w", encoding="utf-8") as fh:
        for r in provider_rows:
            fh.write(json.dumps(r) + "\n")
    return str(tables)


def _prov(pid, tos_class="reseller", archive=False):
    return {
        "id": pid,
        "plan": "test",
        "tos_class": tos_class,
        "archive": archive,
        "valid_to": None,
    }


def _row(
    pid, window_kind="rolling_5h", scope="account", limit=100, source=None, **extra
):
    row = {
        "provider_id": pid,
        "account": "default",
        "scope": scope,
        "window_kind": window_kind,
        "unit": "credits",
        "limit": limit,
        "reset_kind": "rolling",
        "source": source
        if source is not None
        else {"kind": "docs", "url": "https://example.com/quotas"},
    }
    row.update(extra)
    return row


# ---------------------------------------------------------------------------
# Control: the REAL committed table satisfies the whole contract.
# ---------------------------------------------------------------------------


def test_real_table_validates_clean():
    issues = vpq.validate(TABLES)
    assert issues == [], f"committed provider_quota.jsonl broke the contract: {issues}"


def test_real_table_schema_basics():
    rows = _load(QUOTA)
    assert rows, "provider_quota.jsonl is empty"
    for r in rows:
        for key in vpq.REQUIRED:
            assert key in r, f"{r.get('provider_id')}: missing {key}"
        assert r["window_kind"] in vpq.WINDOW_KINDS
        assert r["unit"] in vpq.UNITS
        if r["limit"] is not None:
            assert isinstance(r["limit"], (int, float)) and r["limit"] > 0
        src = r["source"]
        assert src["kind"] in vpq.SOURCE_KINDS


def test_real_table_every_null_limit_has_closed_vocab_reason():
    rows = _load(QUOTA)
    checked = 0
    for r in rows:
        if r["limit"] is None:
            assert r.get("reason") in vpq.REASONS, (
                f"{r['provider_id']}/{r['window_kind']}: silent NULL limit"
            )
            checked += 1
    assert checked >= 40, f"only {checked} reasoned NULLs — table shrank?"


def test_real_table_every_row_has_source_provenance():
    for r in _load(QUOTA):
        src = r["source"]
        assert src["kind"] in vpq.SOURCE_KINDS
        if src["kind"] == "docs":
            assert src.get("url", "").startswith("http"), (
                f"{r['provider_id']}: docs row without URL"
            )


# --------------------------------------------------------------- AC2 -------
# A window row with neither source.url nor source.kind is REJECTED.


def test_row_without_any_source_is_rejected(tmp_path):
    row = _row("p1")
    del row["source"]
    issues = vpq.validate(_write_tables(tmp_path, [row], [_prov("p1")]))
    assert any("no source block at all" in i for i in issues), issues


def test_row_with_degenerate_source_block_is_rejected(tmp_path):
    issues = vpq.validate(
        _write_tables(tmp_path, [_row("p1", source={})], [_prov("p1")])
    )
    assert any("source.kind" in i for i in issues), issues


def test_docs_row_without_url_is_rejected(tmp_path):
    issues = vpq.validate(
        _write_tables(
            tmp_path, [_row("p1", source={"kind": "docs", "url": ""})], [_prov("p1")]
        )
    )
    assert any("names no URL" in i for i in issues), issues


# ---------------------------------------------------- schema + reasons -----


def test_schema_breaks_are_rejected(tmp_path):
    bad_rows = [
        _row("p1", window_kind="fortnightly"),  # not in closed set
        _row("p2", limit=0),  # fake zero (honesty)
        _row("p3", limit="28000"),  # string limit
        _row("p4", unit="gigawatts"),  # not in closed set
    ]
    provs = [_prov("p1"), _prov("p2"), _prov("p3"), _prov("p4")]
    issues = vpq.validate(_write_tables(tmp_path, bad_rows, provs))
    for needle in ("window_kind", "never zero", "numeric or NULL", "unit"):
        assert any(needle in i for i in issues), (needle, issues)


def test_missing_reason_on_null_limit_is_rejected(tmp_path):
    row = _row("p1", limit=None)
    assert "reason" not in row
    issues = vpq.validate(_write_tables(tmp_path, [row], [_prov("p1")]))
    assert any("limit is NULL with no reason" in i for i in issues), issues


def test_invalid_reason_vocabulary_is_rejected(tmp_path):
    row = _row("p1", limit=None, reason="nobody-knows")
    issues = vpq.validate(_write_tables(tmp_path, [row], [_prov("p1")]))
    assert any("not in closed set" in i for i in issues), issues


def test_numeric_limit_must_not_carry_a_reason(tmp_path):
    row = _row("p1", limit=500, reason="not-published")
    issues = vpq.validate(_write_tables(tmp_path, [row], [_prov("p1")]))
    assert any("numeric limit carries a reason" in i for i in issues), issues


# ------------------------------------------------- AC3 alias inheritance ---


def test_real_alias_rows_inherit_parent_windows():
    providers = {p["id"]: p for p in _load(PROVIDERS)}
    aliases = [
        pid for pid, p in providers.items() if p.get("tos_class") == "gateway-alias"
    ]
    assert "gw-deepseek" in aliases and "myrouter:zai-glm" in aliases
    rows = _load(QUOTA)
    for alias in aliases:
        own = [r for r in rows if r["provider_id"] == alias]
        assert own, f"{alias}: no declaration row at all"
        for r in own:
            parent = r.get("alias_of")
            assert parent in providers, f"{alias}: alias_of {parent!r} unknown"
            assert r["window_kind"] == "none", (
                f"{alias}: declares its own window {r['window_kind']!r} — "
                f"aliases inherit the parent's windows"
            )
        # The parent's declaration set (whatever it is — for a PAYG parent like
        # deepseek that set is honestly "no time windows") is what the alias
        # inherits; it must EXIST, never be empty.
        parent_rows = [r for r in rows if r["provider_id"] == parent]
        assert parent_rows, (
            f"{alias}: parent {parent} has no declaration rows to inherit"
        )


def test_alias_row_without_alias_of_is_rejected(tmp_path):
    provs = [_prov("parent"), _prov("alias-x", tos_class="gateway-alias")]
    rows = [_row("parent"), _row("alias-x", window_kind="none", limit=None)]
    issues = vpq.validate(_write_tables(tmp_path, rows, provs))
    assert any("without alias_of" in i for i in issues), issues


def test_alias_declaring_own_window_is_rejected(tmp_path):
    provs = [_prov("parent"), _prov("alias-x", tos_class="gateway-alias")]
    rows = [
        _row("parent"),
        _row("alias-x", window_kind="rolling_5h", limit=None, alias_of="parent"),
    ]
    issues = vpq.validate(_write_tables(tmp_path, rows, provs))
    assert any("never declare their own" in i for i in issues), issues


def test_alias_to_unknown_parent_is_rejected(tmp_path):
    provs = [_prov("alias-x", tos_class="gateway-alias")]
    rows = [_row("alias-x", window_kind="none", limit=None, alias_of="ghost")]
    issues = vpq.validate(_write_tables(tmp_path, rows, provs))
    assert any("not a known provider" in i for i in issues), issues


# -------------------------------------------- AC4 pool vs account scope ----


def test_real_pool_rows_coexist_with_account_rows():
    rows = _load(QUOTA)
    by_provider = {}
    for r in rows:
        by_provider.setdefault(r["provider_id"], []).append(r)
    pool_providers = {r["provider_id"] for r in rows if r["scope"].startswith("pool:")}
    assert {"grok-build", "meta-model", "synthetic", "opencode-go"} <= pool_providers
    for pid in pool_providers:
        scopes = {r["scope"] for r in by_provider[pid]}
        assert "account" in scopes, f"{pid}: pool row replaced the account row"
        assert any(s.startswith("pool:") for s in scopes), f"{pid}: pool scope vanished"


def test_pool_and_account_rows_can_coexist_in_validator(tmp_path):
    provs = [_prov("p1")]
    rows = [
        _row("p1"),  # the account window
        _row("p1", scope="pool:free", pool="free", window_kind="pool", limit=5000),
    ]  # an independent pool budget
    issues = vpq.validate(_write_tables(tmp_path, rows, provs))
    assert issues == [], issues


def test_exact_duplicate_identity_is_rejected(tmp_path):
    provs = [_prov("p1")]
    rows = [_row("p1"), _row("p1")]
    issues = vpq.validate(_write_tables(tmp_path, rows, provs))
    assert any("duplicate identity" in i for i in issues), issues


def test_pool_row_without_matching_pool_field_is_rejected(tmp_path):
    issues = vpq.validate(
        _write_tables(tmp_path, [_row("p1", scope="pool:free")], [_prov("p1")])
    )
    assert any("pool field is" in i for i in issues), issues


def test_pool_window_with_account_scope_is_rejected(tmp_path):
    # RED-proven on the real table: grok-build's pool row demoted to plain
    # account scope was accepted until this rule landed (arm 3 of
    # scripts/mutate_quota_red.py).
    row = _row("p1", window_kind="pool", limit=5)  # scope stays "account"
    issues = vpq.validate(_write_tables(tmp_path, [row], [_prov("p1")]))
    assert any("must be pool-scoped" in i for i in issues), issues


# ------------------------------------------------------------- coverage ----


def test_provider_with_no_rows_and_no_reason_is_rejected(tmp_path):
    provs = [_prov("covered"), _prov("orphan-lane")]
    rows = [_row("covered")]
    issues = vpq.validate(_write_tables(tmp_path, rows, provs))
    assert any("orphan-lane has no quota rows" in i for i in issues), issues


def test_provider_covered_by_reasoned_null_passes(tmp_path):
    provs = [_prov("payg-lane")]
    rows = [_row("payg-lane", window_kind="none", limit=None, reason="not-published")]
    issues = vpq.validate(_write_tables(tmp_path, rows, provs))
    assert issues == [], issues


def test_archived_providers_are_out_of_coverage_scope(tmp_path):
    provs = [_prov("live-lane"), _prov("retired-lane", archive=True)]
    rows = [_row("live-lane")]
    issues = vpq.validate(_write_tables(tmp_path, rows, provs))
    assert issues == [], issues


# --------------------------------------- AC5b: no hard-coded limits --------
# The spec's non-goal, made executable: provider quota limits live in the
# table, not in scripts/. Every numeric limit declared in the table is scanned
# as a word-boundary literal across scripts/*.py; a hit must be a KNOWN
# coincidental constant (latency/ms/scan ceilings), listed here with its file.


def test_no_quota_limit_literals_in_scripts():
    # Only DISTINCTIVE limits are scannable: a $14/5h cap and `timeout=14` are
    # indistinguishable literals, but 28000 credits or 320M tokens are not
    # constants a script would have for any other reason. The spec's rule is
    # about provider limits living in data; the >=5000 floor skips 1000 (a
    # universal ms-conversion constant) while covering every large limit.
    limits = sorted(
        {
            r["limit"]
            for r in _load(QUOTA)
            if isinstance(r.get("limit"), (int, float)) and r["limit"] >= 5000
        }
    )
    assert limits, "no scannable limits in table — scan would be vacuous"
    allowed_hits = {
        # (script, literal): the constant's actual role, none of them quota
        ("provider_health_probe.py", 10000): "SLOW_MS latency ceiling",
        ("router_provider_import.py", 10000): "MAX_LANE_PRICE_PER_M price sanity cap",
        ("router_server.py", 200000): "ledger tail-scan default",
        ("router_ui_page.py", 200000): "ledger tail-scan default",
    }
    violations = []
    for name in sorted(os.listdir(SCRIPTS)):
        if not name.endswith(".py"):
            continue
        text = open(os.path.join(SCRIPTS, name), encoding="utf-8").read()
        for limit in limits:
            literal = str(int(limit))
            if re.search(rf"(?<![\w.]){re.escape(literal)}(?![\w.])", text):
                if (name, limit) in allowed_hits:
                    continue
                violations.append(f"scripts/{name}: literal {literal}")
    assert not violations, f"quota limits hard-coded outside the table: {violations}"
