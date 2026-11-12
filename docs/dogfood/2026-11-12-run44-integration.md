# Task-router dogfood integration report — 2026-11-12 run 44

Tick: `task-router-dogfood-2026-11-12-13-41-25`
Tested product HEAD: `af84c2e6477d5ebab99d45c68dde0a6d3f685d3f` (`origin/main`, clean after the product fast-forward)
Canonical checkout: `/home/kara/task-router`

Verdict: PROMISING-BUT-ROUGH. The documented local install and seeded CLI workflow works, and effort-aware CLI resolution is present at this HEAD. Trust remains limited by the already-owned live-plane findings TR-1000976681059252, TR-1000976681059253, and TR-1000976681059254, plus the prescribed bunker leg could not be launched.

Real-use workflow

- Fresh isolated clone from the canonical checkout: 0.36 s.
- Fresh venv and editable install: 1.37 s.
- DuckDB install required by the documented seed step: 0.86 s.
- `router seed --allow-unsatisfiable`: 12.80 s; wrote a 7,675.4 KB registry and reported the known unsatisfiable `math` ratings. The explicit opt-out was required for a usable seeded registry.
- `router validate --json`: exit 0, `{"valid":true,"issues":[]}`.
- `router spawn task-router --effort low|high|max --format json --quiet`: exit 0, non-empty head, `reasoning_effort` matched the request, `source=registry.json`, `fallback_used=false`, five measured effort promotions and 98 proxy lanes.
- `router spawn task-router --effort bogus`: exit 0, warning on stderr that the value was ignored, and a default/effort-absent result. This is fail-open behavior, not a crash; no new row was filed because the current usage documentation does not promise rejection of unknown effort values.
- The served `:9092` endpoint still returned `effort.requested=null` and `source=absent` for `?effort=high`; `/health` reported `stale=true`, loaded commit `6cbc6aefa573`, while the repo was `af84c2e6`. This re-proves the existing rows TR-1000976681059253 and TR-1000976681059254 rather than creating siblings.

Performance

Headline operation: `router spawn task-router --effort high --format json --quiet` in the fresh seeded clone.

- Cold subprocess: 0.06 s wall on loadavg `3.31 4.26 4.97`.
- Warm `hyperfine --warmup 2 --runs 6`: mean 62.4 ms ± 2.6 ms, range 58.5–65.8 ms.
- Warm `router status --format json`: mean 241.5 ms ± 5.6 ms.
- These are below a user-noticeable threshold for interactive use; no PERF row and no profile were warranted. The 12.80 s seed is a one-shot administrative operation and was not treated as a latency defect.

Install leg

The required `bunker-qa.sh` leg was attempted, not silently passed. `bunker 0.3.0` is installed, but `~/.bunker/config.yaml` is absent and `bunker status` returned `no servers configured; run 'bunker connect' first`. `BUNKER_QA_SERVER=bunker-mvp bunker-qa.sh launch /home/kara/task-router` refused before spawn with `target host bunker-mvp not configured`. No credential was minted and no visibility or permission was changed. The fresh local clone/install above is compensating evidence only; the prescribed bunker leg is skipped and tracked as `TR-1000976681059256`.

Existing findings re-proved

- `TR-1000976681059252`: the prior circuit-plane split remains the relevant silent routing-risk finding; no duplicate row was filed.
- `TR-1000976681059253`: current served `GET /resolve?project=task-router&effort=high` still returns 200 with `effort.requested=null`, `source=absent`, and no warning.
- `TR-1000976681059254`: current `:9092` health reports stale code identity and no effort metric series was exported by that served process in the prior evidence; no duplicate row was filed.

Product code was not changed. Scheduler state, visibility, permissions, and credentials were not changed.
