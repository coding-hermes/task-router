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
    # the alias is NAMED as spend this budget absorbed — never emitted as a
    # second budget of its own (which is what double-counting looked like)
    assert b.get("aliases") == ["alias-x"]
    assert [x for x in blocks if x["provider"] == "alias-x"] == []


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
    # the reason names the ACCOUNT, not just the family — two split blocks
    # otherwise carry the same sentence and the operator cannot tell which
    assert "xkiro-9" in by_acct["xkiro-9"]["remaining_reason"]


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


def test_ac5_every_multi_account_family_splits_one_entry_per_account():
    """Data-driven over the committed table: EVERY family the L0 declares with
    two account-bearing ids splits into one entry per account — no family is
    special-cased in code, and no block is a summed cross-account row."""
    amap = acc.load_account_map(QUOTA)
    folds = acc.seat_folds(amap)
    families = sorted(set(folds.values()))
    assert set(families) == {"commandcode", "opencode-go", "xkiro"}, families
    rows = []
    for fam in families:
        rows.append(_row(fam, cost=1.0, session=f"{fam}-family"))
        for j, seat in enumerate(s for s, f in folds.items() if f == fam):
            rows.append(_row(seat, cost=2.0 + j, session=f"{fam}-seat{j}"))
    blocks = acc.account(rows, {}, T0, account_map=amap)
    per_family = {}
    for b in blocks:
        per_family.setdefault(b["provider"], []).append(b)
    assert set(per_family) == set(families), "a seat leaked a block of its own"
    assert sum(b["n_rows"] for b in blocks) == len(rows)
    for fam in families:
        seats = sorted(s for s, f in folds.items() if f == fam)
        assert len(per_family[fam]) == 1 + len(seats), (
            f"{fam}: expected one entry per account ({1 + len(seats)}), "
            f"got {len(per_family[fam])}")
        assert {b["account"] for b in per_family[fam]} == \
            {amap[fam]} | {amap[s] for s in seats}
        for b in per_family[fam]:
            assert b["n_rows"] == 1, "a block summed more than its own account"


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


# ==========================================================================
# AC6 — alias inheritance on the REAL path: L0 table -> view -> CLI
#
# The first landing only honored `alias_of` from the limits CONFIG, which the
# operator never populated from the table — so `gw-deepseek` spend would have
# reported as its own budget and the parent's window would have been drawn
# down twice. The table is the authority (spec §2: "an alias does not own
# quota"), read at query time like the account map.
# ==========================================================================

GATEWAY_ALIASES = {"gw-deepseek": "deepseek",
                   "myrouter:zai-glm": "zai-glm"}


def test_ac6_l0_alias_map_declares_the_gateway_aliases():
    amap = acc.load_alias_map(QUOTA)
    for alias, parent in GATEWAY_ALIASES.items():
        assert amap.get(alias) == parent, (
            f"{alias}: alias_of lost from the L0 table — an alias that "
            f"resolves to nothing double-counts its parent's window")
    pids = {r["provider_id"] for r in _load(QUOTA)}
    for alias, parent in amap.items():
        assert alias != parent, f"{alias}: self-alias is not an identity"
        assert parent in pids, f"{alias}: parent {parent} has no L0 rows"


def test_ac6_alias_rows_fold_into_the_parent_not_a_second_budget():
    rows = [_row("deepseek", cost=2.0, session="a"),
            _row("gw-deepseek", cost=3.0, session="b")]
    blocks = acc.account(rows, {}, T0, alias_map=acc.load_alias_map(QUOTA))
    assert [b["provider"] for b in blocks] == ["deepseek"], (
        "alias emitted its own budget — the parent window is drawn down twice")
    b = blocks[0]
    assert b["spent_usd"] == pytest.approx(5.0)
    assert b["n_rows"] == 2
    assert b["aliases"] == ["gw-deepseek"]


def test_ac6_alias_spend_is_counted_exactly_once():
    """The invariant behind "never double-count": every ledger row lands in
    exactly one block and the money sums to the ledger — no block may be a
    second view of the same window."""
    rows = [_row("zai-glm", cost=1.0, session="a"),
            _row("myrouter:zai-glm", cost=4.0, session="b"),
            _row("myrouter:zai-glm", cost=0.5, session="c")]
    blocks = acc.account(rows, {}, T0, alias_map=acc.load_alias_map(QUOTA))
    assert len(blocks) == 1
    assert sum(b["n_rows"] for b in blocks) == len(rows)
    assert sum(b["spent_usd"] for b in blocks) == pytest.approx(5.5)


def test_ac6_alias_chain_resolves_the_same_in_any_declaration_order():
    rows = [_row("a", cost=1.0, session="1"),
            _row("b", cost=2.0, session="2"),
            _row("c", cost=4.0, session="3")]
    one = acc.account(rows, {}, T0, alias_map={"a": "b", "b": "c"})
    other = acc.account(rows, {}, T0, alias_map={"b": "c", "a": "b"})
    assert [b["provider"] for b in one] == ["c"]
    assert [b["provider"] for b in other] == ["c"]
    assert one[0]["spent_usd"] == pytest.approx(7.0)
    assert other[0]["spent_usd"] == pytest.approx(7.0)
    assert one[0]["n_rows"] == 3 and other[0]["n_rows"] == 3   # none dropped
    assert one[0]["aliases"] == ["a", "b"]                     # both named


def test_ac6_alias_cycle_folds_nothing_and_drops_nothing():
    rows = [_row("a", cost=1.0, session="1"),
            _row("b", cost=2.0, session="2")]
    blocks = acc.account(rows, {}, T0, alias_map={"a": "b", "b": "a"})
    # a cycle is not an identity: neither id is picked as the winner, both keep
    # their own budget, and every row still lands exactly once
    assert [b["provider"] for b in blocks] == ["a", "b"]
    assert [b["n_rows"] for b in blocks] == [1, 1]
    assert sum(b["spent_usd"] for b in blocks) == pytest.approx(3.0)


def test_ac6_the_view_is_pure_and_never_mutates_the_callers_rows():
    rows = [_row("xkiro", cost=1.0, session="a"),
            _row("gw-deepseek", cost=2.0, session="b")]
    snapshot = json.dumps(rows, sort_keys=True)
    acc.account(rows, {}, T0, account_map={"xkiro": "xkiro-1"},
                alias_map={"gw-deepseek": "deepseek"})
    assert json.dumps(rows, sort_keys=True) == snapshot, (
        "account() leaked labels onto the caller's rows — the next query over "
        "the same ledger would answer differently (a view must not drift)")
    first = acc.account(rows, {}, T0, account_map=acc.load_account_map(QUOTA),
                        alias_map=acc.load_alias_map(QUOTA))
    second = acc.account(rows, {}, T0, account_map=acc.load_account_map(QUOTA),
                         alias_map=acc.load_alias_map(QUOTA))
    assert first == second


def test_ac6_a_limits_config_alias_overrides_the_table_edge():
    rows = [_row("gw-deepseek", cost=2.0, session="a"),
            _row("other-parent", cost=1.0, session="b")]
    blocks = acc.account(rows, {"alias_of": {"gw-deepseek": "other-parent"}},
                         T0, alias_map={"gw-deepseek": "deepseek"})
    assert [b["provider"] for b in blocks] == ["other-parent"]
    assert blocks[0]["spent_usd"] == pytest.approx(3.0)


def test_ac6_an_alias_keyed_config_still_describes_the_parent_window():
    """An operator may write the limit under the ALIAS id (that is the id
    they call). The alias owns no budget, so the entry describes the PARENT's
    window — it must not be silently dropped into a separate block or into
    'no limit configured'."""
    rows = [_row("gw-deepseek", cost=9.0, session="a")]
    limits = {"providers": {"gw-deepseek": {"window": "rolling_5h",
                                            "limit_usd": 10.0}}}
    blocks = acc.account(rows, limits, T0, alias_map=acc.load_alias_map(QUOTA))
    assert len(blocks) == 1
    b = blocks[0]
    assert b["provider"] == "deepseek"
    assert b["limit_usd"] == pytest.approx(10.0)
    assert b["remaining_usd"] == pytest.approx(1.0)


def test_ac6_a_torn_tail_does_not_blank_the_alias_map(tmp_path):
    """The table is append-written, so a half-flushed last line is normal.
    Skipping only the bad line keeps every alias/account declaration readable;
    blanking the whole table would silently turn alias folding off — the exact
    failure that lets a window be drawn down twice."""
    tbl = tmp_path / "provider_quota.jsonl"
    with tbl.open("w") as f:
        f.write(json.dumps({"provider_id": "deepseek", "account": "default",
                            "window_kind": "rolling_5h", "unit": "usd"}) + "\n")
        f.write(json.dumps({"provider_id": "gw-deepseek",
                            "account": "default", "window_kind": "none",
                            "unit": "usd", "alias_of": "deepseek"}) + "\n")
        f.write('{"provider_id": "torn"')          # torn tail, as on disk
    assert acc.load_alias_map(str(tbl)) == {"gw-deepseek": "deepseek"}
    assert acc.load_account_map(str(tbl)) == {"deepseek": "default",
                                              "gw-deepseek": "default"}


def test_ac6_cli_resolves_an_alias_to_its_parent_block(tmp_path, monkeypatch):
    """The acceptance command with an alias id: `accounting --provider
    gw-deepseek` reports the PARENT's single budget (spend counted once), and
    names the alias it absorbed."""
    led = _led(tmp_path, [
        {"provider": "gw-deepseek", "model": "m", "tokens_in": 100,
         "tokens_out": 0, "cost_usd": 3.0, "ts": T0, "session_id": "a"},
        {"provider": "deepseek", "model": "m", "tokens_in": 100,
         "tokens_out": 0, "cost_usd": 1.0, "ts": T0, "session_id": "b"},
    ])
    monkeypatch.setenv("ROUTING_OUTCOMES_FILE", led)
    monkeypatch.setenv("ROUTER_QUOTA_LIMITS_FILE",
                       str(tmp_path / "unused-limits.json"))
    monkeypatch.setenv("ROUTER_PROVIDER_QUOTA_FILE", QUOTA)
    monkeypatch.setattr(router_outcomes, "_PRICE_MAP", {})
    script = os.path.join(SCRIPTS, "router_quota.py")
    for want in ("gw-deepseek", "deepseek"):
        proc = subprocess.run(
            [sys.executable, script, "accounting", "--provider", want,
             f"--now={int(T0)}", "--json"],
            capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
        out = json.loads(proc.stdout)
        assert len(out["providers"]) == 1, (
            f"--provider {want}: expected ONE parent block, got "
            f"{[b['provider'] for b in out['providers']]}")
        b = out["providers"][0]
        assert b["provider"] == "deepseek"
        assert b["spent_usd"] == pytest.approx(4.0)
        assert b.get("aliases") == ["gw-deepseek"]


@pytest.mark.parametrize("family,seat,acct1,acct2", [
    ("opencode-go", "opencode-go-2", "opencode-go-1", "opencode-go-2"),
    ("commandcode", "commandcode-2", "seat-1", "seat-2"),
])
def test_ac5_cli_reports_distinct_accounts_for_every_multi_account_family(
        tmp_path, monkeypatch, family, seat, acct1, acct2):
    """Item 3 breadth: the per-account rule holds for ALL seat families, not
    just xkiro — two ledger provider ids, two entries, never a summed row."""
    led = _led(tmp_path, [
        {"provider": family, "model": "m", "tokens_in": 100, "tokens_out": 0,
         "cost_usd": 2.0, "ts": T0, "session_id": "a"},
        {"provider": seat, "model": "m", "tokens_in": 100, "tokens_out": 0,
         "cost_usd": 5.0, "ts": T0, "session_id": "b"},
    ])
    monkeypatch.setenv("ROUTING_OUTCOMES_FILE", led)
    monkeypatch.setenv("ROUTER_QUOTA_LIMITS_FILE",
                       str(tmp_path / "unused-limits.json"))
    monkeypatch.setenv("ROUTER_PROVIDER_QUOTA_FILE", QUOTA)
    monkeypatch.setattr(router_outcomes, "_PRICE_MAP", {})
    proc = subprocess.run(
        [sys.executable, os.path.join(SCRIPTS, "router_quota.py"),
         "accounting", "--provider", family, f"--now={int(T0)}", "--json"],
        capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert len(out["providers"]) == 2, "summed rows instead of one per account"
    got = {b["account"]: b for b in out["providers"]}
    assert set(got) == {acct1, acct2}
    assert got[acct1]["spent_usd"] == pytest.approx(2.0)
    assert got[acct2]["spent_usd"] == pytest.approx(5.0)
    assert got[acct2]["seats"] == [seat]
    assert all(b["provider"] == family for b in out["providers"])
