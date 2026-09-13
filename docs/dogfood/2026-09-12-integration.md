# Dogfood integration report — task-router (2026-09-12)

Field test by the dogfood cron (real use, not test scripts). Fresh-user
simulation on a clean clone plus an ephemeral install test on bunker-las-03,
then the documented workflows driven end-to-end.

## Promise under test

> A user can resolve a task to a price-ordered, gate-filtered model chain by
> installing the CLI (`pip install -e .`) and calling
> `router spawn <project> --format json`; state resolves under a configurable
> data home; the runtime contract is fail-open (errors never block the caller).

## Verdict: SHIPPABLE (with P1 docs/telemetry debt)

The core product — deterministic eligibility → price sort → runtime gates →
head + exclusions — works exactly as advertised, end to end, on a fresh
machine. The debt is in telemetry noise (TR-043), data-home consistency
across subcommands (TR-044), and the documented quickstart (TR-045).

## What was actually done (fresh-user path)

| Step | Command | Result |
|---|---|---|
| Clone | `git clone https://github.com/coding-hermes/task-router.git` | 1s, HEAD aa9f0ce |
| Install | `pip install -e .` (clean venv) | 3s; `router --help` lists 20 subcommands |
| First resolve | `router spawn my-project --format json` | exit 0, valid JSON, head `ollama-cloud/deepseek-v4-flash:0731` @ $0.033/1M eff., chain 34 hops, bootstrapped `quota-state.json` |
| Seed | `router seed` | ❌ first try: `No module named duckdb`; after `uv pip install duckdb`: 6s, wrote 1.4MB registry.json into the data home |
| Validate/status | `router validate`, `router status` | ❌ read repo-relative paths, ignore the seeded data home (TR-044) |
| Circuit loop | `router circuit record-failure ollama-cloud deepseek-v4-flash:0731 --class overload` ×2 → spawn → `record-success` → spawn | ✅ OPEN 120s → head demoted to `kimi-for-coding/k3-256k` with explicit exclusion reason → CLOSED → head restored |
| API server | `router server --mode read-only` | ✅ `/status`, `/resolve`, `/openapi.json` (15 paths), mutation → 403 as documented. ⚠️ server `/resolve` disagreed with CLI spawn on the head seconds apart (TR-044) |
| Web UI | `router web` | ✅ HTTP 200 on :9093, settings + resolve preview |
| Ad-hoc profile | `router spawn --profile-req 'reasoning=5 debug=3 min_context=100000' --format json` | ✅ resolves |
| Tests | `python3 -m pytest -q tests/` | 290 passed, 1 skipped, 220s |

## Ephemeral-bunker install proof (las-bunker-03, agent b6b57895, destroyed after)

- `git clone` from the public GitHub origin inside the bunker: OK (1s).
- `python3 -m venv .venv && pip install -e .`: **INSTALL_SECONDS=5**
  (Python 3.13, bare Debian user, no preinstalled toolchains — stdlib-only
  runtime claim held).
- Smoke: `router spawn my-project --format json` → EXIT=0, head
  `ollama-cloud/deepseek-v4-flash:0731`, `source: data/tables`;
  `router status --format json` → registry counts ok.
- No documented smoke check exists in the README — that itself is a small gap.

## Friction log (what a new user hits)

1. **1004 stderr lines per resolve** (`ROUTER-MISS: ...`) even after a full
   seed; 724 are `tier=None`. `--quiet` exists but is not mentioned in the
   README quickstart. TR-043.
2. **`router seed` fails on a clean venv** (`duckdb` undeclared, undocumented).
   TR-045.
3. **The data home is not honored by 7 subcommands** — validate/status/estimate
   disagree with spawn about which registry is live; server resolve returned a
   different head than CLI spawn. TR-044.
4. **Errors in `--format json` mode** still print human-readable lines to
   stdout before the JSON (parse cost: strip prefix lines).
5. **Bootstrap `quota-state.json` is sample policy** (all providers OPEN) with
   no pointer in the output that this is sample data, not discovered state.

## Working example (copy-paste, the right way)

```bash
git clone https://github.com/coding-hermes/task-router.git && cd task-router
python3 -m venv .venv && . .venv/bin/activate
pip install -e . && pip install duckdb      # duckdb needed for `router seed`
export TASK_ROUTER_HOME=/tmp/tr-home        # isolate your state
router seed                                 # build registry.json (once)
router spawn my-project --format json --quiet   # --quiet: clean JSON only
router circuit status --json
```

Fail-open held everywhere: every failure mode tested produced `{"error": ...}`
or a degradation marker and exit 0, never a crash.
