"""TR-208 — the usage/balance endpoint poller: bounded, cached, env-key auth.

Pins (from the board row's acceptance criteria):

  AC1  poll interval + cache are explicit config; a cache hit NEVER opens a
       socket (the poller never runs per-request)
  AC2  method+URL+field names come from the DATA TABLE, never from code
       comments; the committed table validates (both endpoint and
       no-endpoint row shapes)
  AC3  the key is read from the env at fetch time and never appears in
       argv, logs, the cache, or error details (scrubbed)
  AC4  every failure degrades to a finite basis token with the plan-terms
       derivation intact — never a fabricated number
  AC5  fixture responses for each endpoint shape + one error-shape fixture
       per pollable provider; all hermetic (the socket seam is stubbed, no
       network in tests)
"""
import datetime
import io
import json
import os
import sys
import urllib.error

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")
sys.path.insert(0, SCRIPTS)

import router_quota_poller as qp  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures: one success shape per pollable endpoint (from the data table's
# documented source), + one error shape per provider (AC5).
# ---------------------------------------------------------------------------

FIXTURE_OK = {
    "deepseek": {
        "is_available": True,
        "balance_infos": [{"currency": "USD", "total_balance": "110.00",
                           "granted_balance": "10.00",
                           "topped_up_balance": "100.00"}],
    },
    # openrouter has TWO endpoint rows; fixture covers both shapes.
    "openrouter": {
        "credits": {"data": {"total_credits": 100.5, "total_usage": 25.75}},
        "key-info": {"data": {"label": "sk-or-v1-au7...890", "usage": 25.5,
                              "limit": 100, "limit_remaining": 74.5,
                              "limit_reset": "monthly",
                              "is_free_tier": False}},
    },
    "minimax": {
        "code": 0,
        "data": {"current_interval_total_count": 5000000,
                 "current_interval_usage_count": 1245300,
                 "current_interval_reset_time": "2026-06-19T21:00:00+08:00",
                 "current_weekly_total_count": 35000000,
                 "current_weekly_usage_count": 8420000,
                 "current_weekly_reset_time": "2026-06-23T00:00:00+08:00",
                 "model_remains": [{"model": "MiniMax-M3", "remains": 4500000,
                                    "total": 5000000}]},
    },
    "stepfun": {"type": "individual", "balance": "42.50",
                "total_cash_balance": "40.00",
                "total_voucher_balance": "2.50"},
    "kimi-for-coding": {
        "usage": {"limit": "5000000", "used": "1245300",
                  "remaining": "3754700",
                  "resetTime": "2026-06-19T21:00:00+08:00"},
        "limits": [{"window": {"duration": 5, "timeUnit": "TIME_UNIT_HOUR"},
                    "detail": {"limit": "1000000", "used": "245300",
                               "remaining": "754700",
                               "resetTime": "2026-06-19T21:00:00+08:00"}}],
    },
}

FIXTURE_ERR = {
    # one error-shape fixture per pollable provider (AC5)
    "deepseek": (401, {"error": {"message": "Authentication Fails, Your api "
                                         "key is invalid", "code": 401}}),
    "openrouter": (403, {"error": {"code": 403,
                                   "message": "Only management keys can "
                                              "fetch credits for an account"}}),
    "minimax": (401, {"base_resp": {"status_code": 1004,
                                    "status_msg": "invalid api key"}}),
    "stepfun": (401, {"error": {"code": "Unauthorized",
                                "message": "Invalid API key"}}),
    "kimi-for-coding": (401, {"error": {"message": "invalid token"}}),
}


def _rows():
    return qp.load_endpoints()


def _row(provider, endpoint_id=None):
    rows = _rows()
    for r in rows:
        if r["provider_id"] == provider and (
                endpoint_id is None or r["endpoint_id"] == endpoint_id):
            return r
    pytest.fail(f"no table row for {provider}/{endpoint_id}")


def _fake_urlopen(payload, status=200, record=None):
    """Build a urlopen stand-in that asserts the REQUEST it received."""
    def fake(req, timeout):
        if record is not None:
            record.append(req)
        body = json.dumps(payload).encode()
        if status != 200:
            raise urllib.error.HTTPError(
                req.full_url, status, "err", hdrs=None, fp=io.BytesIO(body))
        return _Ctx(body)
    return fake


class _Ctx(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Env:
    """Isolated env for one poll: state dir, key, freshness default."""

    def __init__(self, tmp_path, provider, key="sk-test-secret-000111222333"):
        self.state = str(tmp_path / "state")
        self.env = {"ROUTER_STATE_DIR": self.state,
                    "ROUTER_QUOTA_POLL_INTERVAL_S": "900",
                    qp.INTERVAL_ENV: "900"}
        self.rows = _rows()
        for r in self.rows:
            if r["status"] == "endpoint":
                self.env.setdefault(r["auth_env"], "")  # no accidental live keys
        if key is not None and provider:
            for r in qp.rows_for(self.rows, provider):
                if r["status"] == "endpoint":
                    self.env[r["auth_env"]] = key

    def poll(self, provider, fake=None, force=False):
        old_env = dict(os.environ)
        os.environ.clear()
        os.environ.update(self.env)
        try:
            if fake is not None:
                qp._open = fake
            else:
                def deny(req, timeout):  # no accidental network
                    raise AssertionError(f"unexpected network call to "
                                         f"{req.full_url}")
                qp._open = deny
            cache = qp.load_cache(qp.cache_path(self.state))
            dirty = False
            outs = []
            for r in qp.rows_for(self.rows, provider):
                out, d = qp.poll_row(r, cache, 900, force)
                dirty = dirty or d
                outs.append(out)
            if dirty:
                qp.save_cache(qp.cache_path(self.state), cache)
            return outs
        finally:
            qp._open = urllib.request.urlopen
            os.environ.clear()
            os.environ.update(old_env)

    def cache(self):
        return qp.load_cache(qp.cache_path(self.state))


# ---------------------------------------------------------------------------
# AC2 — the committed table validates and is the only source of wire facts
# ---------------------------------------------------------------------------

def test_table_rows_all_validate():
    rows = _rows()  # raises SystemExit(2) on any malformed row
    assert len(rows) >= 9
    provs = {r["provider_id"] for r in rows}
    for p in ("deepseek", "openrouter", "minimax", "stepfun", "kimi-for-coding",
              "zai-glm", "openai-codex", "grok-build"):
        assert p in provs, p


def test_every_pollable_provider_has_error_fixture_and_shape_fixture():
    """AC5 completeness: the fixtures cover exactly the pollable set."""
    pollable = {r["provider_id"] for r in _rows()
                if r["status"] == "endpoint"}
    assert pollable == set(FIXTURE_OK) == set(FIXTURE_ERR)


def test_no_endpoint_rows_carry_reason_and_omit_endpoint_fields():
    for r in _rows():
        if r["status"] == "no-endpoint":
            assert r.get("reason"), r["provider_id"]
            for k in ("method", "url_template", "auth_env", "response_fields"):
                assert r.get(k) in (None, ""), (r["provider_id"], k)


def test_malformed_row_refuses_loudly(tmp_path):
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"provider_id": "x", "endpoint_id": "y", '
                   '"status": "wat"}\n')
    with pytest.raises(SystemExit) as e:
        qp.load_endpoints(str(bad))
    assert e.value.code == 2


def test_source_date_is_iso_on_every_row():
    for r in _rows():
        datetime.date.fromisoformat(r["source_date"])  # raises on garbage


# ---------------------------------------------------------------------------
# AC1 — cache: explicit interval, no socket on a fresh cache
# ---------------------------------------------------------------------------

def test_cache_hit_within_interval_never_opens_a_socket(tmp_path):
    env = _Env(tmp_path, "deepseek")
    outs = env.poll("deepseek",
                    _fake_urlopen(FIXTURE_OK["deepseek"], record=None))
    assert outs[0]["status"] == "ok" and not outs[0]["from_cache"]

    # second poll inside the interval must NOT touch _open (deny would raise)
    outs2 = env.poll("deepseek", fake=None)
    assert outs2[0]["from_cache"] is True
    assert outs2[0]["fields"]["total_balance"] == "110.00"


def test_force_bypasses_age_but_interval_still_explicit(tmp_path):
    env = _Env(tmp_path, "deepseek")
    env.poll("deepseek", _fake_urlopen(FIXTURE_OK["deepseek"]))
    calls = []
    outs = env.poll("deepseek",
                    _fake_urlopen(FIXTURE_OK["deepseek"], record=calls),
                    force=True)
    assert len(calls) == 1 and outs[0]["from_cache"] is False


def test_expired_cache_repolls(tmp_path):
    env = _Env(tmp_path, "deepseek")
    env.poll("deepseek", _fake_urlopen(FIXTURE_OK["deepseek"]))
    # backdate the cache row beyond the interval
    cpath = qp.cache_path(env.state)
    cache = qp.load_cache(cpath)
    cache["providers"]["deepseek"]["fetched_at"] = (
        datetime.datetime.now(datetime.timezone.utc)
        - datetime.timedelta(seconds=901)).isoformat(timespec="seconds")
    qp.save_cache(cpath, cache)
    calls = []
    env.poll("deepseek", _fake_urlopen(FIXTURE_OK["deepseek"], record=calls))
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# AC3 — auth: env-read key, never in argv/logs/cache/errors
# ---------------------------------------------------------------------------

def test_key_reaches_authorization_header_only(tmp_path):
    env = _Env(tmp_path, "deepseek", key="sk-SECRET-abc123")
    seen = []
    env.poll("deepseek", _fake_urlopen(FIXTURE_OK["deepseek"], record=seen))
    (req,) = seen
    assert req.get_header("Authorization") == "Bearer sk-SECRET-abc123"
    # never in the URL (argv-equivalent), never in the cache, never in output
    assert "sk-SECRET-abc123" not in req.full_url
    dumped = json.dumps(env.cache())
    out = json.dumps(env.poll("deepseek", fake=None))
    assert "sk-SECRET-abc123" not in dumped
    assert "sk-SECRET-abc123" not in out


def test_http_error_detail_is_scrubbed_of_key(tmp_path):
    env = _Env(tmp_path, "stepfun", key="sk-SECRET-abc123")
    # provider echoes the presented key back in its error body
    payload = {"error": {"message": "bad key sk-SECRET-abc123 presented"}}
    outs = env.poll("stepfun", _fake_urlopen(payload, status=401))
    assert outs[0]["basis"] == "http-error"
    assert "sk-SECRET-abc123" not in json.dumps(outs)
    assert "sk-SECRET-abc123" not in json.dumps(env.cache())
    assert "<redacted>" in outs[0]["detail"]


# ---------------------------------------------------------------------------
# AC4 — degradation: finite basis, no fabricated number
# ---------------------------------------------------------------------------

def test_each_provider_error_fixture_degrades_never_fabricates(tmp_path):
    for provider, (status, body) in FIXTURE_ERR.items():
        env = _Env(tmp_path / provider, provider)
        outs = env.poll(provider, _fake_urlopen(body, status=status))
        for out in outs:
            assert out["status"] == "degraded", (provider, out)
            assert "fields" not in out, (provider, out)
            assert out["basis"] == "http-error", (provider, out)
            assert out["http_status"] == status
        # and the degraded reality is cached for `show`
        cached = env.cache()["providers"][provider]
        assert cached["status"] == "degraded" and cached["basis"] == "http-error"


def test_no_key_in_env_degrades_without_network(tmp_path):
    env = _Env(tmp_path, "deepseek", key=None)
    outs = env.poll("deepseek", fake=None)  # deny would raise on any call
    assert outs[0]["basis"] == "no-key-in-env"
    assert "fields" not in outs[0]


def test_missing_env_var_reports_its_name(tmp_path):
    env = _Env(tmp_path, "deepseek", key=None)
    outs = env.poll("deepseek", fake=None)
    assert "DEEPSEEK_API_KEY" in outs[0]["detail"]
    assert outs[0]["detail"] != ""


def test_shape_mismatch_degrades_and_keeps_raw_for_remap(tmp_path):
    env = _Env(tmp_path, "deepseek")
    # a 200 whose body maps ZERO of the table's field paths
    outs = env.poll("deepseek", _fake_urlopen({"unexpected": "shape"}))
    assert outs[0]["basis"] == "shape-unmapped"
    assert "fields" not in outs[0]
    cached = env.cache()["providers"]["deepseek"]
    assert cached.get("raw") == {"unexpected": "shape"}


def test_partial_shape_maps_what_is_there_omits_the_rest(tmp_path):
    env = _Env(tmp_path, "deepseek")
    partial = {"balance_infos": [{"currency": "USD",
                                  "total_balance": "5.00"}]}  # no is_available
    outs = env.poll("deepseek", _fake_urlopen(partial))
    assert outs[0]["status"] == "ok" and outs[0]["mapped"] == "2/5"
    assert outs[0]["fields"] == {"currency": "USD", "total_balance": "5.00"}
    assert "is_available" not in outs[0]["fields"]  # absent, never None


def test_timeout_and_network_degrade(tmp_path):
    import socket

    def timeout_die(req, timeout):
        raise urllib.error.URLError(socket.timeout("timed out"))

    env = _Env(tmp_path, "deepseek")
    outs = env.poll("deepseek", timeout_die)
    assert outs[0]["basis"] == "timeout"

    def net_die(req, timeout):
        raise urllib.error.URLError(ConnectionRefusedError("refused"))

    outs = env.poll("deepseek", net_die, force=True)  # cached timeout otherwise
    assert outs[0]["basis"] == "network-error"


def test_bad_json_degrades(tmp_path):
    def garbage(req, timeout):
        return _Ctx(b"<html>not json</html>")

    env = _Env(tmp_path, "deepseek")
    outs = env.poll("deepseek", garbage)
    assert outs[0]["basis"] == "bad-json"


def test_oversized_body_degrades(tmp_path):
    def big(req, timeout):
        return _Ctx(b"x" * (qp.MAX_BODY + 1))

    env = _Env(tmp_path, "deepseek")
    outs = env.poll("deepseek", big)
    assert outs[0]["basis"] == "body-too-large"


def test_no_endpoint_row_degrades_with_reason_no_network(tmp_path):
    env = _Env(tmp_path, "zai-glm", key=None)
    outs = env.poll("zai-glm", fake=None)
    (out,) = outs
    assert out["basis"] == "no-endpoint"
    assert "dashboard-only" in out["detail"]


def test_unknown_provider_reports_not_in_table(tmp_path, capsys):
    env = _Env(tmp_path, None, key=None)
    old = dict(os.environ)
    os.environ.clear()
    os.environ.update(env.env)
    try:
        qp._open = lambda req, t: (_ for _ in ()).throw(
            AssertionError("no network"))
        rc = qp.run_poll(["not-a-provider"], 900, False, False, env.state)
        assert rc == 0
    finally:
        qp._open = urllib.request.urlopen
        os.environ.clear()
        os.environ.update(old)
    out = capsys.readouterr().out
    assert "not-a-provider" in out and "not-in-table" in out


# ---------------------------------------------------------------------------
# AC5 — every fixture shape maps through the REAL table rows end-to-end
# ---------------------------------------------------------------------------

def test_every_fixture_shape_maps_end_to_end(tmp_path, monkeypatch):
    # per-endpoint fixtures: one entry per (provider, endpoint_id)
    fixtures = {
        "deepseek": {"balance": FIXTURE_OK["deepseek"]},
        "minimax": {"token-plan-remains": FIXTURE_OK["minimax"]},
        "stepfun": {"accounts": FIXTURE_OK["stepfun"]},
        "kimi-for-coding": {"usages": FIXTURE_OK["kimi-for-coding"]},
        "openrouter": {k: v for k, v in FIXTURE_OK["openrouter"].items()},
    }
    for provider, shape in fixtures.items():
        env = _Env(tmp_path / provider, provider)
        rows = qp.rows_for(env.rows, provider)
        # poll_row reads the key from os.environ at fetch time (AC3) — set it
        for r in rows:
            if r["status"] == "endpoint":
                monkeypatch.setenv(r["auth_env"], "sk-test")
        # stub the socket seam: dispatch on URL so each endpoint row sees its
        # own fixture (no network — tests must never leave the box)
        def fake(req, timeout=None, _shape=shape):
            url = req.full_url
            for r in rows:
                if r["url_template"] == url:
                    return _Ctx(json.dumps(_shape[r["endpoint_id"]]).encode())
            raise AssertionError(f"unexpected URL {url}")
        monkeypatch.setattr(qp, "_open", fake)
        # poll each endpoint row with its own fixture (openrouter has 2)
        cache = qp.load_cache(qp.cache_path(env.state))
        for r in rows:
            out, _ = qp.poll_row(r, cache, 3600, force=True)
            assert out["status"] == "ok", (provider, r["endpoint_id"], out)
            assert out["mapped"] == f"{len(r['response_fields'])}/" \
                                    f"{len(r['response_fields'])}", out


def test_extraction_dotted_and_index_paths():
    doc = {"a": {"b": [{"c": 1}, {"c": 2}]}}
    assert qp.extract(doc, "a.b[1].c") == (True, 2)
    assert qp.extract(doc, "a.b[5].c") == (False, None)
    assert qp.extract(doc, "a.x") == (False, None)
    assert qp.extract(doc, "a.b") == (True, [{"c": 1}, {"c": 2}])


# ---------------------------------------------------------------------------
# CLI contract (argparse surface, fail-open exit codes)
# ---------------------------------------------------------------------------

def _run_cli(argv, env_extra, capsys):
    old = dict(os.environ)
    os.environ.clear()
    os.environ.update(env_extra)
    try:
        qp._open = lambda req, timeout=None: (_ for _ in ()).throw(
            AssertionError("CLI tests never touch the network"))
        rc = 0
        try:
            qp.main(argv)
        except SystemExit as e:
            rc = e.code
        finally:
            qp._open = urllib.request.urlopen
    finally:
        os.environ.clear()
        os.environ.update(old)
    res = capsys.readouterr()
    return rc, res.out


def test_cli_poll_all_json_writes_cache(tmp_path, capsys):
    state = str(tmp_path / "s")
    envd = {"ROUTER_STATE_DIR": state, qp.INTERVAL_ENV: "3600"}
    for r in _rows():
        if r["status"] == "endpoint":
            envd.setdefault(r["auth_env"], "")  # keys ABSENT: no endpoint
    # polls run: no-endpoint rows degrade, endpoint rows degrade no-key —
    # the network tripwire must stay silent the whole run
    rc, out = _run_cli(["poll", "--all", "--json", "--state-dir", state], envd,
                       capsys)
    assert rc == 0
    doc = json.loads(out)
    provs = {r["provider"] for r in doc["results"]}
    assert "deepseek" in provs and "zai-glm" in provs
    assert all(r["status"] == "degraded" for r in doc["results"])
    # nothing was OBSERVED (no key, no-endpoint rows) -> no cache write:
    # the cache records fetch outcomes, never absence-of-config
    assert not os.path.exists(os.path.join(state, qp.CACHE_FILE))


def test_cli_poll_requires_selection(tmp_path, capsys):
    rc, _ = _run_cli(["poll", "--state-dir", str(tmp_path / "s")], {},
                     capsys)
    assert rc == 2  # argparse usage error: explicit selection is the contract


def test_cli_show_empty_cache_is_empty_not_fabricated(tmp_path, capsys):
    rc, out = _run_cli(["show", "--state-dir", str(tmp_path / "s")], {}, capsys)
    assert rc == 0
    assert "cache empty" in out


def test_cli_show_json_roundtrip(tmp_path, capsys):
    env = _Env(tmp_path, "deepseek")
    env.poll("deepseek", _fake_urlopen(FIXTURE_OK["deepseek"]))
    rc, out = _run_cli(["show", "--provider", "deepseek", "--json",
                        "--state-dir", env.state], env.env, capsys)
    assert rc == 0
    doc = json.loads(out)
    assert doc["providers"]["deepseek"]["fields"]["total_balance"] == "110.00"


# ---------------------------------------------------------------------------
# State-dir hygiene: same convention as the other router tools
# ---------------------------------------------------------------------------

def test_cache_path_follows_router_state_dir_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ROUTER_STATE_DIR", str(tmp_path / "x"))
    assert qp.cache_path().startswith(str(tmp_path / "x"))
