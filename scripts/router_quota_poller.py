#!/usr/bin/env python3
"""router_quota_poller.py — bounded, cached usage/balance endpoint poller (TR-208).

WHY: the router's quota plane (router_quota.py + data/tables/provider_quota.jsonl)
knows plan LIMITS per provider, but whether a lane is actually exhausted is only
visible after a 429. Providers that expose a usage/balance endpoint let the
router see the number BEFORE the wasted hop. This tool polls those endpoints on
an explicit interval and caches the result; consumers read the CACHE — a poller
never runs per-request (AC1).

CONTRACT (stdlib only; no new pip deps):

  poll --provider P | --all [--force] [--interval N] [--json] [--state-dir D]
      Poll each endpoint row for the provider (or every row in the table).
      - Cache hit: a cached row younger than --interval (default
        ROUTER_QUOTA_POLL_INTERVAL_S env, else DEFAULT_INTERVAL_S=900) is
        returned WITHOUT a network call; --force bypasses the age check only.
      - Every outcome (ok, degraded) is recorded in the cache with its basis,
        so `show` reflects reality even when polling fails.
      - Exit 0 always (fail-open: a diagnostic must never break a caller);
        exit 2 only for usage/validation errors (bad table row, unknown
        provider when it matches no row at all is reported, not fatal).

  show [--provider P] [--json] [--state-dir D]
      Print the cached rows (no network). Empty cache -> empty result, never
      a fabricated number.

DATA TABLE (AC2): data/tables/provider_usage_endpoints.jsonl — one row per
(provider, endpoint). Method + URL + response field paths + auth env var name +
source citation all come from the TABLE, never from code comments. Rows with
status=no-endpoint carry the reason (unexplained nulls are junk): the provider
has no pollable balance endpoint and says why.

DEGRADATION (AC4): a failed / unauthorised / absent endpoint NEVER yields a
number. The output row is {status: degraded, basis: <token>, detail: ...} with
basis from a finite vocabulary:
  ok | no-endpoint | no-key-in-env | http-error | timeout | network-error |
  bad-json | body-too-large | shape-unmapped | provider-error
The actual derivation from plan terms stays where it lives today
(provider_quota.jsonl + router_quota.py); this poller only records the OBSERVED
basis for that derivation.

AUTH (AC3): the key is read from the environment at fetch time via the row's
auth_env name. It never appears in argv, logs, the cache, or error details (any
echoed detail is scrubbed against the key value before use).

BOUNDED: one request per endpoint per poll invocation, 10s timeout, 1 MiB body
cap, no retries, no background threads.

STATE: cache at <state_dir>/usage-cache.json where state_dir comes from the
shared resolver (state_dir.resolve_state_dir — ROUTER_STATE_DIR env override,
same convention as router_quota/ledger; tests set ROUTER_STATE_DIR to isolate).

Integration: run `poll --all` from cron/tick on the quota cadence; the spawn
path stays untouched (fail-open is sacred — this module is diagnostic and never
gates anything by itself).
"""
import argparse
import datetime
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request

_HERE = os.path.dirname(os.path.realpath(__file__))
_REPO = os.path.dirname(_HERE)

# Shared state-dir resolver (TR-REV-20261005-3): env override wins silently,
# default ~/.hermes/model-router, non-canonical invocations warn on stderr.
try:
    import state_dir as _state_dir_mod
except ImportError:  # live byte-copy not yet synced: fail open to old default
    _state_dir_mod = None

CACHE_FILE = "usage-cache.json"
CACHE_VERSION = 1
DEFAULT_INTERVAL_S = 900
INTERVAL_ENV = "ROUTER_QUOTA_POLL_INTERVAL_S"
TIMEOUT_S = 10.0
MAX_BODY = 1024 * 1024  # 1 MiB — a usage JSON is never bigger; cap the read
UA = "task-router-quota-poller/1.0"
DETAIL_CAP = 200  # chars of any echoed error detail

STATUSES = ("endpoint", "no-endpoint")
RESULT_KINDS = ("balance", "credits", "usage-window")
SOURCE_KINDS = ("docs", "third-party-client", "provider-research")


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def state_dir():
    if _state_dir_mod is not None:
        return _state_dir_mod.resolve_state_dir(script_file=__file__)
    return os.environ.get("ROUTER_STATE_DIR",
                          os.path.expanduser("~/.hermes/model-router"))


def cache_path(state_dir_arg=None):
    d = state_dir_arg or state_dir()
    return os.path.join(d, CACHE_FILE)


def data_dir():
    """Repo-relative default with the historical home fallback (same candidate
    order as provider_health_probe.py: repo first, then the live copy)."""
    env = os.environ.get("ROUTING_DATA_DIR")
    if env:
        return env
    cands = (os.path.join(_REPO, "data", "tables"),
             os.path.expanduser("~/task-router/data/tables"))
    return next((d for d in cands if os.path.isdir(d)), cands[0])


def endpoints_table_path():
    return os.path.join(data_dir(), "provider_usage_endpoints.jsonl")


# ---------------------------------------------------------------------------
# Data table (AC2) — method+URL+fields come from here, validated loudly
# ---------------------------------------------------------------------------

def _fail(msg):
    print(f"router_quota_poller: {msg}", file=sys.stderr)
    raise SystemExit(2)


def _iso_date(v):
    datetime.date.fromisoformat(v)  # raises on malformed
    return v


def load_endpoints(path=None):
    """Parse + validate the table. A malformed row is a DATA bug: refuse
    loudly (exit 2) rather than silently skipping — this is config, not the
    spawn path, so strictness beats fail-open here."""
    path = path or endpoints_table_path()
    rows = []
    try:
        with open(path) as f:
            for ln, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception as e:
                    _fail(f"{path}:{ln}: not JSON ({e})")
    except SystemExit:
        raise
    except OSError as e:
        _fail(f"cannot read endpoints table {path}: {e}")
    for ln_offset, r in enumerate(rows, 1):
        _validate_row(r, f"{path} (record {ln_offset})")
    return rows


def _require(r, key, where):
    v = r.get(key)
    if not isinstance(v, str) or not v.strip():
        _fail(f"{where}: field {key!r} must be a non-empty string")
    return v


def _absent(r, key, where):
    if key in r and r[key] not in (None, ""):
        _fail(f"{where}: field {key!r} must be ABSENT on a "
              f"status={r.get('status')!r} row (present-but-null fields are "
              f"junk; omit the key instead)")


def _validate_row(r, where):
    if not isinstance(r, dict):
        _fail(f"{where}: row must be a JSON object")
    _require(r, "provider_id", where)
    _require(r, "endpoint_id", where)
    status = _require(r, "status", where)
    if status not in STATUSES:
        _fail(f"{where}: status {status!r} not in {STATUSES}")
    _require(r, "source_url", where)
    try:
        _iso_date(_require(r, "source_date", where))
    except ValueError:
        _fail(f"{where}: source_date must be YYYY-MM-DD")
    sk = r.get("source_kind") or "provider-research"
    if sk not in SOURCE_KINDS:
        _fail(f"{where}: source_kind {sk!r} not in {SOURCE_KINDS}")
    if status == "endpoint":
        _require(r, "method", where)
        url = _require(r, "url_template", where)
        if not url.startswith(("http://", "https://")):
            _fail(f"{where}: url_template must be an absolute http(s) URL")
        _require(r, "auth_env", where)
        _require(r, "auth_style", where)
        kind = _require(r, "result_kind", where)
        if kind not in RESULT_KINDS:
            _fail(f"{where}: result_kind {kind!r} not in {RESULT_KINDS}")
        fields = r.get("response_fields")
        if not isinstance(fields, dict) or not fields:
            _fail(f"{where}: response_fields must be a non-empty object of "
                  f"semantic-name -> dotted path")
        for name, p in fields.items():
            if not isinstance(p, str) or not p.strip():
                _fail(f"{where}: response_fields[{name!r}] must be a "
                      f"non-empty dotted path")
        _absent(r, "reason", where)
    else:  # no-endpoint
        _require(r, "reason", where)
        for k in ("method", "url_template", "auth_env", "auth_style",
                  "result_kind", "response_fields"):
            _absent(r, k, where)


def rows_for(rows, provider):
    return [r for r in rows if r["provider_id"] == provider]


# ---------------------------------------------------------------------------
# Dotted-path extraction: "a.b[0].c" over parsed JSON. A miss yields ABSENCE
# (the field is omitted), never None, never a guess.
# ---------------------------------------------------------------------------

def _parse_path(p):
    segs = []
    for part in p.split("."):
        name, idx = part, None
        if part.endswith("]"):
            i = part.rindex("[")
            name, idx = part[:i], int(part[i + 1:-1])
        segs.append((name, idx))
    return segs


def extract(doc, path):
    """-> (True, value) | (False, None). Never raises on a shape mismatch."""
    cur = doc
    for name, idx in _parse_path(path):
        if not isinstance(cur, dict) or name not in cur:
            return False, None
        cur = cur[name]
        if idx is not None:
            if not isinstance(cur, list) or idx >= len(cur):
                return False, None
            cur = cur[idx]
    return True, cur


def extract_fields(doc, mapping):
    out, found = {}, 0
    for name, path in mapping.items():
        ok, v = extract(doc, path)
        if ok:
            out[name] = v
            found += 1
    return out, found, len(mapping)


# ---------------------------------------------------------------------------
# Bounded HTTP (AC3: the key touches nothing but the request)
# ---------------------------------------------------------------------------

class PollError(Exception):
    def __init__(self, basis, detail="", http_status=None):
        super().__init__(detail or basis)
        self.basis = basis
        self.detail = detail
        self.http_status = http_status


def _open(req, timeout):  # seam for tests (monkeypatch this)
    return urllib.request.urlopen(req, timeout=timeout)


def _scrub(text, key):
    """AC3: an echoed provider error must never carry the key value."""
    if key and text:
        return text.replace(key, "<redacted>")
    return text


def _read_bounded(fp):
    body = fp.read(MAX_BODY + 1)
    if len(body) > MAX_BODY:
        raise PollError("body-too-large", f"response body exceeds {MAX_BODY}B")
    return body


def fetch_json(url, method, key, timeout_s=TIMEOUT_S):
    """One bounded request. Returns parsed JSON. Raises PollError with a basis
    from the finite vocabulary. The key is used ONLY in the Authorization
    header; it never reaches an exception message, log line, or the cache."""
    headers = {"Accept": "application/json", "User-Agent": UA}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(url, headers=headers, method=method)
    try:
        with _open(req, timeout=timeout_s) as r:
            body = _read_bounded(r)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = _scrub(_read_bounded(e).decode("utf-8", "replace"),
                            key)[:DETAIL_CAP]
        except Exception:
            pass
        raise PollError("http-error", detail, http_status=e.code)
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", e)
        if isinstance(reason, (TimeoutError,)) or "timed out" in str(reason):
            raise PollError("timeout", str(reason)[:DETAIL_CAP])
        raise PollError("network-error", str(reason)[:DETAIL_CAP])
    try:
        return json.loads(body.decode("utf-8", "replace"))
    except (ValueError, UnicodeDecodeError) as e:
        raise PollError("bad-json", str(e)[:DETAIL_CAP])


# ---------------------------------------------------------------------------
# Cache (explicit path under the shared state dir; atomic replace)
# ---------------------------------------------------------------------------

def load_cache(path):
    try:
        with open(path) as f:
            doc = json.load(f)
        if isinstance(doc, dict) and doc.get("version") == CACHE_VERSION:
            return doc
    except Exception:
        pass
    return {"version": CACHE_VERSION, "updated": None, "providers": {}}


def save_cache(path, doc):
    doc["updated"] = _now_iso()
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".usage-cache-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(doc, f, indent=1)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="seconds")


def _parse_utc(ts):
    try:
        dt = datetime.datetime.fromisoformat(str(ts))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(datetime.timezone.utc)
    except Exception:
        return None


def _age_s(row):
    ts = _parse_utc(row.get("fetched_at"))
    if ts is None:
        return None
    return (datetime.datetime.now(datetime.timezone.utc) - ts).total_seconds()


# ---------------------------------------------------------------------------
# Poll
# ---------------------------------------------------------------------------

def poll_row(row, cache, interval_s, force):
    """-> (output_row, cache_dirty). Never raises; never fabricates a number."""
    provider = row["provider_id"]
    eid = row["endpoint_id"]
    prov_cache = cache["providers"].get(provider)
    if prov_cache and prov_cache.get("endpoint_id") == eid:
        age = _age_s(prov_cache)
        if age is not None and age < interval_s and not force:
            out = dict(prov_cache)
            out.update({"provider": provider, "from_cache": True,
                        "age_s": int(age)})
            return out, False

    if row["status"] == "no-endpoint":
        return {"provider": provider, "endpoint_id": eid, "status": "degraded",
                "basis": "no-endpoint", "detail": row["reason"],
                "source_url": row["source_url"],
                "source_date": row["source_date"]}, False

    key = os.environ.get(row["auth_env"], "")
    if not key:
        # Defer the verdict until after a cache-freshness check would have
        # mattered: an existing cached row was already returned above, so a
        # missing key on a live poll is the honest basis here.
        return {"provider": provider, "endpoint_id": eid, "status": "degraded",
                "basis": "no-key-in-env",
                "detail": f"env var {row['auth_env']} is unset",
                "source_date": row["source_date"]}, False

    try:
        doc = fetch_json(row["url_template"], row["method"], key)
    except PollError as e:
        out = {"provider": provider, "endpoint_id": eid, "status": "degraded",
               "basis": e.basis, "detail": _scrub(e.detail, key),
               "source_date": row["source_date"]}
        if e.http_status is not None:
            out["http_status"] = e.http_status
        # cache the degraded outcome (reality), keep any prior good row? No:
        # one row per provider — a fresh degraded observation REPLACES a stale
        # good one, so `show` never reports a number older than the last fact.
        cache["providers"][provider] = {
            "endpoint_id": eid, "fetched_at": _now_iso(), "status": "degraded",
            "basis": e.basis, "detail": _scrub(e.detail, key),
            **({"http_status": e.http_status} if e.http_status else {}),
        }
        return out, True

    fields, found, total = extract_fields(doc, row["response_fields"])
    if found == 0:
        out = {"provider": provider, "endpoint_id": eid, "status": "degraded",
               "basis": "shape-unmapped",
               "detail": f"0/{total} mapped fields present in the response "
                         f"(raw cached for re-mapping)",
               "source_date": row["source_date"]}
        cache["providers"][provider] = {
            "endpoint_id": eid, "fetched_at": _now_iso(), "status": "degraded",
            "basis": "shape-unmapped", "fields": {}, "raw": doc,
            "source_date": row["source_date"],
        }
        return out, True

    out = {"provider": provider, "endpoint_id": eid, "status": "ok",
           "basis": "ok", "fields": fields,
           "mapped": f"{found}/{total}",
           "fetched_at": _now_iso(), "from_cache": False,
           "source_date": row["source_date"]}
    cache["providers"][provider] = {
        "endpoint_id": eid, "fetched_at": out["fetched_at"], "status": "ok",
        "fields": fields, "raw": doc, "source_date": row["source_date"],
    }
    return out, True


def run_poll(providers_arg, interval_s, force, as_json, state_dir_arg=None):
    rows = load_endpoints()
    if providers_arg:
        wanted = []
        unknown = []
        for p in providers_arg:
            got = rows_for(rows, p)
            if got:
                wanted.extend(got)
            else:
                unknown.append(p)
        rows = wanted
    cpath = cache_path(state_dir_arg)
    cache = load_cache(cpath)
    dirty = False
    results = []
    for r in rows:
        out, d = poll_row(r, cache, interval_s, force)
        dirty = dirty or d
        results.append(out)
    if dirty:
        save_cache(cpath, cache)
    for p in (unknown if providers_arg else []):
        results.append({"provider": p, "status": "degraded",
                        "basis": "not-in-table",
                        "detail": "no endpoint row for this provider in "
                                  "provider_usage_endpoints.jsonl"})
    if as_json:
        print(json.dumps({"results": results,
                          "cache": cpath,
                          "interval_s": interval_s}, indent=1))
    else:
        print(f"usage/balance poll (interval {interval_s}s, cache {cpath}):")
        for e in results:
            if e["status"] == "ok":
                brief = ", ".join(f"{k}={e['fields'][k]!r}"
                                  for k in sorted(e["fields"]))
                tag = " (cache)" if e.get("from_cache") else ""
                print(f"  {e['provider']}/{e['endpoint_id']}: OK{tag} "
                      f"[{e.get('mapped')}] {brief}")
            else:
                detail = e.get("detail") or ""
                if detail:
                    detail = " — " + detail
                print(f"  {e['provider']}/{e.get('endpoint_id', '-')}: "
                      f"DEGRADED {e['basis']}{detail}")
                print("    (derive from plan terms; no number observed)")
    return 0


def run_show(provider, as_json, state_dir_arg=None):
    cache = load_cache(cache_path(state_dir_arg))
    provs = cache["providers"]
    if provider:
        provs = {k: v for k, v in provs.items() if k == provider}
    if as_json:
        print(json.dumps({"cache": cache_path(state_dir_arg),
                          "providers": provs}, indent=1))
    else:
        if not provs:
            print("(cache empty — run `poll` first; nothing derived, "
                  "nothing fabricated)")
        for p, row in sorted(provs.items()):
            age = _age_s(row)
            age_txt = f"{int(age)}s old" if age is not None else "age unknown"
            if row.get("status") == "ok":
                brief = ", ".join(f"{k}={row['fields'][k]!r}"
                                  for k in sorted(row["fields"]))
                print(f"  {p}/{row['endpoint_id']}: OK ({age_txt}) {brief}")
            else:
                print(f"  {p}/{row.get('endpoint_id', '-')}: "
                      f"DEGRADED {row.get('basis')} ({age_txt})")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _interval(argv_val):
    if argv_val is not None:
        return argv_val
    try:
        return int(os.environ.get(INTERVAL_ENV, "") or DEFAULT_INTERVAL_S)
    except ValueError:
        return DEFAULT_INTERVAL_S


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="usage/balance endpoint poller — bounded, cached (TR-208)"
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    ap_poll = sub.add_parser(
        "poll", help="poll endpoints (cache-aware; one request per endpoint)")
    ap_poll.add_argument("--provider", action="append", default=None,
                         help="provider id (repeatable); default: --all rows")
    ap_poll.add_argument("--all", action="store_true",
                         help="poll every row in the table")
    ap_poll.add_argument("--force", action="store_true",
                         help="bypass the cache-freshness age check")
    ap_poll.add_argument("--interval", type=int, default=None,
                         help=f"cache freshness in seconds "
                              f"(default {INTERVAL_ENV} or {DEFAULT_INTERVAL_S})")
    ap_poll.add_argument("--state-dir", default=None)
    ap_poll.add_argument("--json", action="store_true")

    ap_show = sub.add_parser("show", help="print the cache (no network)")
    ap_show.add_argument("--provider")
    ap_show.add_argument("--state-dir", default=None)
    ap_show.add_argument("--json", action="store_true")

    args = ap.parse_args(argv)
    if args.cmd == "poll":
        if not args.all and not args.provider:
            ap.error("poll needs --provider P (repeatable) or --all")
        sys.exit(run_poll(args.provider, _interval(args.interval), args.force,
                          args.json, args.state_dir))
    elif args.cmd == "show":
        sys.exit(run_show(args.provider, args.json, args.state_dir))


if __name__ == "__main__":
    main()
