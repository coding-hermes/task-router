# Control-plane health and the :9391 instance (TR-REVIEW-001)

Two listeners serve the router, both managed by `systemd --user`, both with a
`/health` surface a probe can assert on. This document is the authoritative
answer to "what is the second one for", and the contract `/health` guarantees.

## 1. The two listeners

| Port | Unit | Purpose | Auth posture |
|------|------|---------|--------------|
| `127.0.0.1:9092` | `task-router-server.service` | **The fleet API.** OpenAPI reads (`/resolve`, `/profiles`, `/status`, `/health`, `/model_status`, …), the MCP bridge at `POST /mcp`, gated mutations in edit mode, and the outcome ingest `POST /api/v1/outcomes`. This is the control plane the fleet queries. | read-only mode by default; mutations need edit mode **and** `X-API-Key` |
| `127.0.0.1:9391` | `task-router-proxy.service` | **The classified proxy (TR-067 Path B).** Speaks `POST /v1/chat/completions` and `POST /v1/responses`: classifies the request (or takes the caller's declared profile), builds the chain, walks a bounded fallback ladder against the upstream gateway, and returns the upstream shape plus the additive `_router` envelope. | `ROUTER_PROXY_AUTH=passthrough` — the caller's own upstream credential gates the mirror paths |

The `:9391` instance is **not** a second control plane and not a stray
process: it is the fleet's routing entry point for chat traffic, and it also
serves the same read-only API routes (`/status`, `/health`, `/openapi.json`),
which is why `curl :9391/health` answers.

### Why it looked undocumented

Historically `:9391` was a bare child of `systemd --user` with no unit file —
a live listener that nothing could health-check and no file explained. It is
now `task-router-proxy.service`, enabled with `Restart=on-failure`; the
classifier credential lives in
`~/.config/systemd/user/task-router-proxy.env` (mode `600`, never in the unit
file). The recipe, the port-collision check and the client cutover note live
in `README.md` §"Classified proxy deployment".

Operate it like any other unit:

```bash
systemctl --user status  task-router-proxy.service
systemctl --user restart task-router-proxy.service
journalctl --user -u task-router-proxy.service -n 50
```

## 2. What `/health` guarantees

`GET /health` (and `GET /`, which serves the same payload plus a `_links`
map) returns JSON. Every block is fail-open: a broken block becomes an `error`
key and the response still answers `200` — a health endpoint that dies on the
condition it exists to report is worse than useless.

```jsonc
{
  "status": "ok",
  "service": "task-router/1.0",
  "commit": "df592c4c2d09",        // running commit SHA ("" -> "unknown")
  "mode": "read-only",
  "ts": "2026-09-23T21:24:03+00:00",
  "registry":      { /* DATA freshness: newest valid_from date + age_days */ },
  "registry_age":  { /* FILE freshness: mtime of registry.json */ },
  "gate":          { /* the `router validate` verdict, in-process */ },
  "probe":         { /* hourly provider_health_probe state */ },
  "circuit":       { /* circuit-breaker / policy-gate pairs */ },
  "chains_snapshot": { /* newest data/state/chains/<date>.md */ }
}
```

### `registry_age` — registry freshness by mtime

The `registry` block above reports **data** freshness: the newest `valid_from`
DATE inside `models.jsonl` (when the prices were last true). `registry_age`
answers the other question — when the file's **bytes** were last written:

| Field | Meaning |
|-------|---------|
| `path` | the resolved registry (`ROUTING_REGISTRY`, else `<repo>/registry.json`) |
| `exists` | `false` means absent, **not** stale — the `gate` block reports it |
| `mtime` | UTC ISO-8601 write time |
| `age_s` / `age_h` | seconds / hours since `mtime` |
| `newest_table` / `newest_table_age_s` | newest `data/tables/*.jsonl` and its age |
| `lag_s` | `newest_table_mtime - registry_mtime` (positive = registry is older) |
| `content_match` | the tables are row-for-row identical to the registry's copy |
| `stale` | the verdict, from the validator's own predicate (see below) |

**The staleness predicate has a content tiebreak, and it matters.** `stale` is
`true` only when the registry is older than the newest table by more than
`FRESHNESS_SLACK_S` (1 s) **and** the table content differs. The seed writes
`registry.json` *before* syncing `data/tables`, so a perfectly correct
checkout routinely shows a large mtime lag with identical content — measured
on the live tree 2026-09-23: `lag_s` 37855, `content_match` true, gate
`valid: true`. A probe that keys on `lag_s` alone cries wolf on every healthy
host. Both this block and the gate call
`router_validate.freshness_check()`, so there is exactly one predicate.

### `gate` — the `router validate` verdict

The same checks `router validate` prints, run in-process (`router_validate`
is stdlib-only; the full set costs ~0.04 s, so no subprocess).

| Field | Meaning |
|-------|---------|
| `valid` | `true` / `false` — the gate's verdict. **`null` means the checks could not RUN** (see `error`); never conflate that with "the data is bad" |
| `failed_checks` | names of the failed checks, e.g. `["freshness"]` |
| `issues` | up to 10 issue strings, verbatim from the validator |
| `issues_total` | total issue count (may exceed `len(issues)`) |
| `check_names` / `checks` | the full check list and its length |

### `commit` — the running revision

Read from the git dir, handling both a normal checkout (`.git` directory) and
a linked worktree (`.git` file pointing at `gitdir: …`, with loose refs in the
common dir). `"unknown"` means the revision could not be resolved — treat that
as a failure, not as a value.

## 3. Probing it (the canary contract)

`scripts/router_health_probe.py` is the probe — self-contained, stdlib-only,
and exit-coded so a cron/canary can branch without parsing JSON:

```bash
~/.hermes/venvs/board/bin/python3 scripts/router_health_probe.py
# exit 0 PASS | 1 FAIL (answered + unhealthy) | 2 cannot-run (no answer)

# both listeners, one line each
scripts/router_health_probe.py --url http://127.0.0.1:9092
scripts/router_health_probe.py --url http://127.0.0.1:9391
scripts/router_health_probe.py --url http://127.0.0.1:9092 --json
scripts/router_health_probe.py --max-age-h 48   # "must be reseeded daily"
```

It asserts the same three facts as the shell recipe below:

1. the process answers and its `commit` is a real revision (not `"unknown"`),
2. the gate RAN and passed (`gate.valid is true` — `null` means it could not
   run, a different failure with a different message),
3. the registry is not stale (`registry_age.stale is false`).

`--max-age-h` adds an absolute cap on `registry_age.age_h`; it is reported as
its own failure, never folded into staleness.

Exit `1` vs `2` matters: a router that ANSWERED and is unhealthy is the loud
case (the control plane is up and wrong — a silent WARN lets it drift), while
"no answer" is the caller's call — an off-host canary WARNs, an on-host
watchdog FAILs.

### The shell equivalent

If a probe must be a `curl` line:

```bash
# 1) the endpoint answers and the process is alive
curl -sf --max-time 10 http://127.0.0.1:9092/health -o /tmp/health.json || fail

# 2) the revision is real
jq -e '.commit != "unknown" and (.commit | length) >= 7' /tmp/health.json

# 3) the gate ran and passed
jq -e '.gate.valid == true' /tmp/health.json

# 4) the registry is not stale
jq -e '.registry_age.stale == false' /tmp/health.json
```

Steps 3 and 4 are independent on purpose: a registry can be stale while every
other check passes, and the gate's `valid` is `null` (not `false`) when the
validator could not run.

### Verifying with a deliberately stale registry

`tests/test_health_plane.py` proves all four assertions against a serving
instance, including the stale case. To reproduce by hand:

```bash
# copy a real registry home to a scratch dir, then age the registry and
# rewrite a table so the content differs
cp -r <repo>/data/tables /tmp/stale/tables
cp <repo>/registry.json /tmp/stale/registry.json
printf 'tampered\n' >> /tmp/stale/tables/models.jsonl

ROUTING_REGISTRY=/tmp/stale/registry.json \
ROUTING_DATA_DIR=/tmp/stale/tables \
  python3 scripts/router_server.py --mode read-only --port 9392 &

curl -s localhost:9392/health | jq '{stale: .registry_age.stale, valid: .gate.valid}'
# -> {"stale": true, "valid": false}
```

Note the `ROUTING_REGISTRY` / `ROUTING_DATA_DIR` export is what makes the copy
the served home: the server resolves both once at start, so a probe without
them describes the real registry, not the fixture.

## 4. Deployed-copy warning (why the sync matters)

`scripts/sync_runtime.sh` splits the runtime tools by consumer:

- **symlinks** for subprocess + manual consumers (`router_spawn.py`,
  `router_validate.py`, `router_server.py`, …);
- **byte-identical copies** for anything the Hermes cron runner execs, because
  cron resolves symlinks and blocks any script whose real path falls outside
  `~/.hermes/scripts/`.

`router_health.py` is a copy, and it derives the repo root from its own
location (`Path(__file__).resolve().parents[1]`). A copy sitting in
`~/.hermes/scripts/` therefore resolves `REPO` to `~/.hermes` — its
`registry`, `registry_age` and `chains_snapshot` blocks describe
`~/.hermes/data/tables`, which does not exist, and its `commit` is `unknown`.

Consequence: **run the health probe through the server process** (which execs
`router_server.py`, a symlink resolved back to the repo) or through the repo
path directly. `router_server.py` imports `router_health` from the repo's
`scripts/`, so a `curl` against a live listener is always correct. Only a
direct `python3 ~/.hermes/scripts/router_health.py` sees the wrong tree —
which is why `router_validate.py` joined the byte-identical copy list: the
health plane imports it, and a symlinked dependency would be blocked by the
same cron guard.

## 5. Verification

```bash
~/.hermes/venvs/board/bin/python3 -m pytest -q tests/test_health_plane.py
~/.hermes/venvs/board/bin/python3 -m pytest -q tests/test_validate.py
curl -s http://127.0.0.1:9092/health | jq '{commit, registry_age, gate}'
curl -s http://127.0.0.1:9391/health | jq '{commit, registry_age, gate}'
```
