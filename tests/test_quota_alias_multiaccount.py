"""TR-216 — quota alias inheritance + multi-account separation.

The quota plane held three implicit things the data had not made explicit:

  * alias rows (gw-deepseek -> deepseek, myrouter:zai-glm -> zai-glm) resolve
    to a parent and never own a window (TR-206 validator + this file's data
    checks);
  * multi-account providers (xkiro/-2, xkiro-/-2 under opencode-go/-2,
    commandcode/-2) are SEPARATE budgets per account — the L0 table carries a
    distinct `account` key per row group (AC2) and the L2 accounting view can
    split per account when the limit config declares it (AC3);
  * commandcode rollover semantics live in the DATA as a declared `rollover`
    field (AC4), not in anyone's memory.

Phase 2 (this dispatch) closes the REAL-ledger gap the first commit left:
the ledger keys each seat as a DISTINCT provider id (xkiro, xkiro-2, ...) and
carries no `account` field, so `router quota accounting --provider xkiro`
reported ONE SUMMED row. Now (AC5):

  * the L0 table is the authority for which ledger provider id is WHICH
    account: `load_account_map` reads the uniform per-provider `account`
    declarations at query time (no second store), `seat_folds` folds a seat
    id (xkiro-2) into its family (xkiro) ONLY when the table declares both
    sides as distinct accounts;
  * the view reports ONE ENTRY PER ACCOUNT — never a summed row — labeling
    folded blocks with `seats` (the absorbed ledger provider ids); the CLI
    --provider filter matches seat ids too;
  * aggregated display is enforced: the view never sums across accounts, and
    each split block surfaces the L0 `aggregate_display` declaration
    ("unknown" until a provider doc permits summing — quota-layers-spec §2);
  * xkiro/-2 and opencode-go/-2 declare rollover semantics explicitly as
    unknown-with-reason (AC4's "or explicitly mark unknown" branch).

The L2 tests are hermetic (temp ledger + temp limit config, same fixtures as
test_quota_accounting.py); the REAL outcome ledger is never read. Data
checks read the committed L0 table explicitly by path.
"""
import json
import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")

if REPO not in sys.path:
    sys.path.insert(0, REPO)
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import router_outcomes  # noqa: E402
import router_quota_accounting as acc  # noqa: E402
import validate_provider_quota as vpq  # noqa: E402

TABLES = os.path.join(REPO, "data", "tables")
QUOTA = os.path.join(TABLES, "provider_quota.jsonl")
UTC = None  # tests reuse acc's own clock helpers; ts values are epoch floats

T0 = 1780000000.0    # same fixed clock convention as test_quota_accounting.py


def _load(path):
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


# ==========================================================================
# AC1 — alias rows resolve to a parent and never double-count
# ==========================================================================

def test_ac1_alias_rows_resolve_to_parent():
    rows = _load(QUOTA)
    providers = {p["id"] for p in _load(os.path.join(TABLES, "providers.jsonl"))}
    alias_rows = [r for r in rows if "alias_of" in r]
    assert alias_rows, "no alias rows in the committed table"
    per_alias = {}
    for r in alias_rows:
        parent = r["alias_of"]
        assert parent in providers, f"{r['provider_id']}: alias_of {parent!r} unknown"
        assert r["window_kind"] == "none", (
            f"{r['provider_id']}: alias row declares its own window — an alias "
            f"inherits the parent's windows and never owns quota")
        # The parent must HAVE declaration rows for the alias to inherit —
        # otherwise the alias resolves to nothing and its (zero) spend would
        # vanish from every surface.
        parent_rows = [x for x in rows if x["provider_id"] == parent]
        assert parent_rows, f"{r['provider_id']}: parent {parent} has no rows"
        per_alias.setdefault(r["provider_id"], []).append(r)
    assert set(per_alias) >= {"gw-deepseek", "myrouter:zai-glm"}, per_alias


def test_ac1_aliases_never_double_count_their_parent():
    """The view's alias folding: when the limits config carries the L0 table's
    alias map (`alias_of`), an alias provider's spend reports under the
    PARENT's block — never as a second budget touching the same window."""
    rows = [
        {"provider": "parent", "model": "m", "tokens_in": 1000,
         "tokens_out": 0, "cost_usd": 2.0, "ts": T0, "session_id": "a"},
        {"provider": "alias-x", "model": "m", "tokens_in": 1000,
         "tokens_out": 0, "cost_usd": 2.0, "ts": T0, "session_id": "b"},
    ]
    limits = {"providers": {"parent": {"window": "rolling_5h",
                                       "limit_usd": 10.0}},
              "alias_of": {"alias-x": "parent"}}
    blocks = acc.account(rows, limits, T0)
    assert [b["provider"] for b in blocks] == ["parent"]   # ONE budget
    b = blocks[0]
    assert b["spent_usd"] == pytest.approx(4.0)   # both rows, summed ONCE
    assert b["remaining_usd"] == pytest.approx(6.0)
    assert "alias-x" not in json.dumps(json.dumps(b)) + b["provider"]


def test_ac1_alias_rows_in_l0_table_never_own_a_window():
    """Data-level: every alias row owns NO window and its parent HAS rows —
    combined with the view test, aliases resolve and never double-count."""
    rows = _load(QUOTA)
    by_pid = {}
    for r in rows:
        by_pid.setdefault(r["provider_id"], []).append(r)
    for r in rows:
        if "alias_of" not in r:
            continue
        parent_rows = by_pid.get(r["alias_of"], [])
        assert parent_rows, f"{r['provider_id']}: parent {r['alias_of']} has no rows"
        assert r["window_kind"] == "none"


# ==========================================================================
# AC2 — the four split providers carry separate account keys in the data
# ==========================================================================

def _rows_by_provider(pid):
    return [r for r in _load(QUOTA) if r["provider_id"] == pid]


@pytest.mark.parametrize("pid,account", [
    ("xkiro", "xkiro-1"),
    ("xkiro-2", "xkiro-2"),
    ("opencode-go", "opencode-go-1"),
    ("opencode-go-2", "opencode-go-2"),
])
def test_ac2_separate_account_keys_in_the_data(pid, account):
    rows = _rows_by_provider(pid)
    assert rows, f"{pid}: no row in the table"
    for r in rows:
        assert r["account"] == account, (
            f"{pid} row {r['window_kind']}: account {r['account']!r} != {account!r}")


def test_ac2_commandcode_accounts_are_already_split():
    assert {r["account"] for r in _rows_by_provider("commandcode")} == {"seat-1"}
    assert {r["account"] for r in _rows_by_provider("commandcode-2")} == {"seat-2"}


def test_ac2_the_committed_table_still_validates_clean():
    assert vpq.validate(TABLES) == []


# ==========================================================================
# AC3 — the accounting code keys on (provider_id, account) when told to
# ==========================================================================

def _led(tmp_path, rows):
    p = tmp_path / "outcomes.jsonl"
    with open(p, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return str(p)


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, tmp_path):
    monkeypatch.setenv("ROUTING_OUTCOMES_FILE", str(tmp_path / "unused.jsonl"))
    monkeypatch.setenv("ROUTER_QUOTA_LIMITS_FILE",
                       str(tmp_path / "unused-limits.json"))
    # the L0 account map is loaded at query time on the CLI/ledger path —
    # pin it away from the committed table unless a test points at it
    monkeypatch.setenv("ROUTER_PROVIDER_QUOTA_FILE",
                       str(tmp_path / "unused-quota-table.jsonl"))
    monkeypatch.setattr(router_outcomes, "_PRICE_MAP", {})


def _row(provider, account=None, cost=1.0, ts=T0, session="s"):
    r = {"provider": provider, "model": "m", "tokens_in": 100,
         "tokens_out": 0, "cost_usd": cost, "ts": ts, "session_id": session}
    if account is not None:
        r["account"] = account
    return r


def test_ac3_single_account_config_keeps_pre_216_behavior():
    """No `account` in the config entry -> ONE provider block, all rows
    summed (byte-identical semantics to pre-TR-216)."""
    rows = [_row("xkiro", cost=1.0, session="a"),
            _row("xkiro", account="xkiro-2", cost=2.0, session="b")]
    limits = {"providers": {"xkiro": {"window": "rolling_5h",
                                      "limit_usd": 10.0}}}
    blocks = acc.account(rows, limits, T0)
    assert len(blocks) == 1
    assert blocks[0]["spent_usd"] == pytest.approx(3.0)
    assert blocks[0]["account"] is None     # unset = the single-account shape


def test_ac3_xkiro_1_and_xkiro_2_account_separately():
    """Spend on one account NEVER moves the other's remaining (AC3)."""
    rows = [
        _row("xkiro", account="xkiro-1", cost=3.0, session="a"),
        _row("xkiro", account="xkiro-2", cost=0.0, session="b"),
    ]
    limits = {"providers": {"xkiro": {"account": "xkiro-1",
                                      "window": "rolling_5h",
                                      "limit_usd": 200.0}}}
    blocks = acc.account(rows, limits, T0)
    by_acct = {b["account"]: b for b in blocks}
    assert set(by_acct) == {"xkiro-1", "xkiro-2"}
    assert by_acct["xkiro-1"]["spent_usd"] == pytest.approx(3.0)
    assert by_acct["xkiro-1"]["remaining_usd"] == pytest.approx(197.0)
    assert by_acct["xkiro-2"]["spent_usd"] == pytest.approx(0.0)
    assert by_acct["xkiro-2"]["remaining_usd"] == pytest.approx(200.0)
    for b in blocks:
        assert b["provider"] == "xkiro"


def test_ac3_opencode_go_1_and_opencode_go_2_account_separately():
    rows = [
        _row("opencode-go", account="opencode-go-2", cost=5.0, session="a"),
    ]
    limits = {"providers": {"opencode-go": {
        "account": "opencode-go-1", "window": "rolling_5h",
        "limit_usd": 40.0}}}
    blocks = acc.account(rows, limits, T0)
    by_acct = {b["account"]: b for b in blocks}
    assert set(by_acct) == {"opencode-go-1", "opencode-go-2"}
    # the spend sits on opencode-go-2 ONLY — acct 1's window is untouched
    assert by_acct["opencode-go-2"]["spent_usd"] == pytest.approx(5.0)
    assert by_acct["opencode-go-1"]["spent_usd"] == pytest.approx(0.0)
    assert by_acct["opencode-go-1"]["remaining_usd"] == pytest.approx(40.0)


def test_ac3_rows_without_an_account_field_fall_in_the_declared_account():
    """The legacy ledger carries no `account` key — those rows belong to the
    account the config declares, never to a fake key or to the void."""
    rows = [_row("xkiro", cost=2.0, session="legacy")]      # no account field
    limits = {"providers": {"xkiro": {"account": "xkiro-1",
                                      "window": "rolling_5h",
                                      "limit_usd": 200.0}}}
    blocks = acc.account(rows, limits, T0)
    assert len(blocks) == 1
    assert blocks[0]["account"] == "xkiro-1"
    assert blocks[0]["spent_usd"] == pytest.approx(2.0)


def test_ac3_unconfigured_sibling_account_still_shows_its_spend():
    """An account the config does not name individually (no per-account
    entry) inherits the provider entry's window/limits shape minus the
    account marker — spend visible, remaining NULL with the reason."""
    rows = [_row("xkiro", account="xkiro-9", cost=4.0, session="new")]
    limits = {"providers": {"xkiro": {"account": "xkiro-1",
                                      "window": "rolling_5h"}}}
    blocks = acc.account(rows, limits, T0)
    by_acct = {b["account"]: b for b in blocks}
    # the DECLARED account stays visible even idle (same rule as providers),
    # and the unconfigured sibling's spend is still reported
    assert by_acct["xkiro-9"]["spent_usd"] == pytest.approx(4.0)
    assert by_acct["xkiro-9"]["remaining_usd"] is None
    assert "no limit configured" in (by_acct["xkiro-9"]["remaining_reason"] or "")


def test_ac3_real_table_accounts_match_live_lane_companies():
    """The limit CONFIG a caller builds from the L0 rows can declare the
    accounts for BOTH xkiro plans and BOTH opencode-go plans, and the view
    then yields four separate budgets over one ledger."""
    rows = [
        _row("xkiro", account="xkiro-1", cost=3.0, session="a"),
        _row("xkiro", account="xkiro-1", cost=1.0, session="b"),
        _row("xkiro", account="xkiro-2", cost=8.0, session="c"),
        _row("opencode-go", account="opencode-go-1", cost=2.0, session="d"),
        _row("opencode-go", account="opencode-go-2", cost=6.0, session="e"),
    ]
    limits = {"providers": {
        "xkiro": {"account": "xkiro-1", "window": "rolling_5h",
                  "limit_usd": 200.0},
        "opencode-go": {"account": "opencode-go-1", "window": "rolling_5h",
                        "limit_usd": 40.0},
    }}
    blocks = acc.account(rows, limits, T0)
    key = lambda b: (b["provider"], b["account"])
    got = {key(b): b["spent_usd"] for b in blocks}
    assert got == {
        ("xkiro", "xkiro-1"): 4.0,
        ("xkiro", "xkiro-2"): 8.0,
        ("opencode-go", "opencode-go-1"): 2.0,
        ("opencode-go", "opencode-go-2"): 6.0,
    }


# ==========================================================================
# AC4 — commandcode rollover semantics are declared IN THE DATA
# ==========================================================================

@pytest.mark.parametrize("pid", ["commandcode", "commandcode-2"])
def test_ac4_rollover_field_present_on_every_commandcode_row(pid):
    rows = _rows_by_provider(pid)
    assert rows, f"{pid}: no row"
    for r in rows:
        ro = r.get("rollover")
        assert isinstance(ro, dict), f"{pid}/{r['window_kind']}: no rollover field"
        assert ro.get("included_credits") == "no-rollover", (
            f"{pid}/{r['window_kind']}: included credits must declare forfeit")
        assert ro.get("topup_credits") == "rollover-never-expire", (
            f"{pid}/{r['window_kind']}: purchased top-up credits must declare "
            f"never-expiring rollover")


def test_ac4_rollover_agrees_with_the_providers_jsonl_note():
    """The quota table's rollover declaration and the registry plan note must
    AGREE (the correction history: commit 777c7dc fixed the half-wrong
    claim) — the note says rollover for the included allowance."""
    for pid in ("commandcode", "commandcode-2"):
        prov = next(p for p in _load(os.path.join(TABLES, "providers.jsonl"))
                    if p["id"] == pid)
        note = prov.get("notes") or ""
        if note:
            assert "does NOT roll over" in note or "rollover" in note.lower(), (
                f"{pid}: providers.jsonl note lost the rollover statement")
            assert "never expire" in note.lower() or "never expires" in note.lower() \
                or "top-up" in note.lower() or "topup" in note.lower(), (
                f"{pid}: providers.jsonl note lost the top-up rollover statement")


# ==========================================================================
# AC5 — the real-ledger shape: L0-derived seat fold, one entry per account,
# aggregated display enforced unknown
# ==========================================================================

SEAT_PROVIDERS = ("xkiro", "xkiro-2", "opencode-go", "opencode-go-2",
                  "commandcode", "commandcode-2")


def test_ac5_l0_map_declares_the_six_seat_identities():
    amap = acc.load_account_map(QUOTA)
    for pid in SEAT_PROVIDERS:
        assert amap.get(pid), f"{pid}: no account in the L0 map"
    assert amap["xkiro"] == "xkiro-1"
    assert amap["xkiro-2"] == "xkiro-2"
    assert amap["opencode-go"] == "opencode-go-1"
    assert amap["opencode-go-2"] == "opencode-go-2"
    assert amap["commandcode"] == "seat-1"
    assert amap["commandcode-2"] == "seat-2"


def test_ac5_seat_folds_come_from_the_table_not_the_name_shape():
    folds = acc.seat_folds(acc.load_account_map(QUOTA))
    assert folds == {"xkiro-2": "xkiro",
                     "opencode-go-2": "opencode-go",
                     "commandcode-2": "commandcode"}
    # a provider merely NAMED like a sibling folds to nothing...
    assert acc.seat_folds({"gwid-2": "some-account"}) == {}
    # ...and neither does a sibling whose account the table does NOT declare
    # as distinct (same account on both sides = not a seat split)
    assert acc.seat_folds({"x": "a", "x-2": "a"}) == {}


def test_ac5_one_entry_per_account_never_a_summed_row():
    """THE acceptance shape: ledger rows keyed by seat provider ids, the L0
    map from the committed table — the view reports (xkiro, xkiro-1) and
    (xkiro, xkiro-2) as two budgets; xkiro-2's spend never lands in the
    xkiro-1 window and no summed cross-account row exists."""
    rows = [
        _row("xkiro", cost=3.0, session="a"),
        _row("xkiro", cost=1.5, session="b"),
        _row("xkiro-2", cost=8.0, session="c"),
    ]
    blocks = acc.account(rows, {}, T0,
                         account_map=acc.load_account_map(QUOTA))
    assert len(blocks) == 2, "expected one entry per account, got a merge"
    by_acct = {b["account"]: b for b in blocks}
    assert set(by_acct) == {"xkiro-1", "xkiro-2"}
    assert all(b["provider"] == "xkiro" for b in blocks)
    assert by_acct["xkiro-1"]["spent_usd"] == pytest.approx(4.5)
    assert by_acct["xkiro-1"]["n_rows"] == 2
    assert by_acct["xkiro-2"]["spent_usd"] == pytest.approx(8.0)
    assert by_acct["xkiro-2"]["n_rows"] == 1
    # the folded block names the ledger provider id it absorbed
    assert by_acct["xkiro-2"]["seats"] == ["xkiro-2"]
    assert "seats" not in by_acct["xkiro-1"]      # the family id is not a seat


def test_ac5_aggregate_display_surfaces_unknown_no_aggregate_row():
    """Item 3: summing across accounts is a DISPLAY declaration from the L0
    table; the view surfaces it on split blocks and NEVER computes an
    aggregate row of its own."""
    rows = [_row("xkiro", cost=3.0, session="a"),
            _row("xkiro-2", cost=8.0, session="c")]
    blocks = acc.account(rows, {}, T0,
                         account_map=acc.load_account_map(QUOTA),
                         aggregate_display=acc.load_aggregate_display(QUOTA))
    assert len(blocks) == 2        # two budgets — not two budgets + a total
    for b in blocks:
        assert b["aggregate_display"]["sum_limits"] == "unknown"
        assert b["aggregate_display"]["reason"]
    # a single-account family surfaces no aggregation statement
    single = acc.account([_row("zai-glm", cost=1.0, session="a")], {}, T0,
                         account_map=acc.load_account_map(QUOTA))
    assert len(single) == 1
    assert single[0]["account"] == "default"       # labeled, not split
    assert "aggregate_display" not in single[0]


def test_ac5_unlabeled_rows_fall_in_the_maps_declared_account():
    """With the L0 map present the map is the AUTHORITY: a legacy row without
    an account field belongs to the account the table declares (xkiro -> xkiro-1),
    so the seat (xkiro-2, explicitly labeled) still reports separately.
    Without a map, the same mixed shape stays unlabeled (AC3 pins that)."""
    rows = [_row("xkiro", cost=1.0, session="legacy"),      # no account field
            _row("xkiro", account="xkiro-2", cost=2.0, session="new")]
    blocks = acc.account(rows, {}, T0, account_map={"xkiro": "xkiro-1"})
    by_acct = {b["account"]: b for b in blocks}
    assert set(by_acct) == {"xkiro-1", "xkiro-2"}
    assert by_acct["xkiro-1"]["spent_usd"] == pytest.approx(1.0)
    assert by_acct["xkiro-2"]["spent_usd"] == pytest.approx(2.0)


def test_ac5_cli_reports_distinct_accounts_for_xkiro(tmp_path, monkeypatch):
    """The acceptance command, replicated hermetically: temp ledger carrying
    BOTH seat provider ids, the committed L0 table as the account authority —
    `router_quota.py accounting --provider xkiro --json` yields two entries
    with distinct accounts; the SEAT id filters too."""
    led = _led(tmp_path, [
        {"provider": "xkiro", "model": "m", "tokens_in": 100, "tokens_out": 0,
         "cost_usd": 3.0, "ts": T0, "session_id": "a"},
        {"provider": "xkiro-2", "model": "m", "tokens_in": 100,
         "tokens_out": 0, "cost_usd": 8.0, "ts": T0, "session_id": "c"},
    ])
    monkeypatch.setenv("ROUTING_OUTCOMES_FILE", led)
    monkeypatch.setenv("ROUTER_QUOTA_LIMITS_FILE",
                       str(tmp_path / "unused-limits.json"))
    monkeypatch.setenv("ROUTER_PROVIDER_QUOTA_FILE", QUOTA)
    monkeypatch.setattr(router_outcomes, "_PRICE_MAP", {})
    script = os.path.join(SCRIPTS, "router_quota.py")
    out = {}
    for flag, want_accounts in ((["--provider", "xkiro"],
                                 {"xkiro-1", "xkiro-2"}),
                                (["--provider", "xkiro-2"],
                                 {"xkiro-2"}),):
        proc = subprocess.run(
            [sys.executable, script, "accounting", *flag,
             f"--now={int(T0)}", "--json"],
            capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
        out = json.loads(proc.stdout)
        got = {b["account"] for b in out["providers"]}
        assert got == want_accounts, (
            f"{flag}: expected distinct per-account entries, got {got}")
        for b in out["providers"]:
            assert b["provider"] == "xkiro"
    # the seat-id run names the family provider and its own spend only
    seat_block = next(b for b in out["providers"] if b["account"] == "xkiro-2")
    assert seat_block["spent_usd"] == pytest.approx(8.0)
    assert seat_block["seats"] == ["xkiro-2"]


def test_ac5_real_table_declares_aggregate_and_rollover_on_seat_rows():
    """Data-level (item 3 + item 4): every multi-account row carries
    aggregate_display unknown-with-reason; xkiro/-2 and opencode-go/-2 mark
    rollover explicitly unknown-with-reason; commandcode keeps its MODELED
    (non-unknown) rollover values."""
    for pid in SEAT_PROVIDERS:
        for r in _rows_by_provider(pid):
            decl = r.get("aggregate_display")
            assert isinstance(decl, dict), f"{pid}: no aggregate_display"
            assert decl.get("sum_limits") == "unknown", (
                f"{pid}: summing across accounts has no doc permission — "
                f"must stay unknown, got {decl.get('sum_limits')!r}")
            assert decl.get("reason"), f"{pid}: unknown without a reason"
    for pid in ("xkiro", "xkiro-2", "opencode-go", "opencode-go-2"):
        for r in _rows_by_provider(pid):
            ro = r.get("rollover")
            assert isinstance(ro, dict), f"{pid}: rollover not declared"
            assert ro.get("included_credits") == "unknown", (
                f"{pid}: rollover must be explicitly unknown (no doc "
                f"statement), got {ro.get('included_credits')!r}")
            assert ro.get("reason"), f"{pid}: unknown rollover without reason"
    for pid in ("commandcode", "commandcode-2"):
        for r in _rows_by_provider(pid):
            ro = r.get("rollover")
            assert ro.get("included_credits") == "no-rollover"
            assert ro.get("topup_credits") == "rollover-never-expire"
