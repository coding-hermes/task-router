# Docs-audit transcript — tick task-router-docs-2026-11-17-02-40-53 (4th delivery)

Frozen evidence artifact for the documentation-gap audit lane. Produced by the lane, not by a worker.

- TICK: `task-router-docs-2026-11-17-02-40-53` (fourth delivery of this attempt: 22:15Z, 23:57Z, 00:00Z, 05:1xZ)
- AUDITED TIP: `a278616ef489e1485800cc594a5187d5e1747717` (origin/main, fetched 2026-11-17T06:03Z)
- PROBE ROOT: clean detached worktree `wt-docs-0240` at that tip (the canonical checkout `/home/kara/task-router` is diverged — 112 behind / 7 ahead / dirty — so it was never used as a probe root and never written to)
- FROZEN: compute `sha256sum` of this file at the moment of filing; the value is recorded in the row's evidence note.

## 0. Re-fire reconciliation (before any measurement)

This attempt already filed rows in earlier deliveries. Published board state at the tip:

```
$ jq -r 'select(type=="object")|select((.id//"")|test("^DOC"))|"\(.id)\t\(.status)\t\(.priority)"' published-tasks.jsonl
DOC-5 pending P3   DOC-6 pending P3   DOC-7 pending P3   DOC-8 pending P3
DOC-9 pending P3   DOC-10 pending P3  DOC-11 pending P3  DOC-12 pending P3
DOC-13 pending P3  DOC-14 pending P3  DOC-15 pending P3  DOC-16 pending P3
DOC-17 superseded P3  DOC-18 pending P3  DOC-19 pending P3
DOC-22 pending P3  DOC-23 pending P3  DOC-24 pending P3  DOC-25 pending P3
DOC-26 pending P3  DOC-27 pending P3  DOC-28 pending P3  DOC-29 pending P3
DOC-30 pending P3
```

Stranded local copies (NOT filed, NOT pushed): the diverged canonical checkout holds local commits
`75c11adc` (rows DOC-20, DOC-21) and `3d4fae67` (their markers). Those two rows describe the SAME
findings the published board already owns as **DOC-22** (health §2 under-enumerates the /health
payload) and **DOC-23** (no documented refresh path for benchmarks.jsonl / cost_effort.jsonl).
Owner resolution was by finding fingerprint on the published board, not by id. The local copies are
superseded duplicates and were deliberately left unpushed.

Every earlier delivery's marker count was re-checked (exactly one `ch:trace row=` per row):

```
$ jq -r 'select(type=="object")|select((.id//"")|test("^DOC-(2[2-9]|30)$"))|[.id, (([to_entries[]|select(.value|type=="string")|select(.value|contains("ch:trace row="))]|length)|tostring)]|@tsv' published-tasks.jsonl
DOC-22 1  DOC-23 1  DOC-24 1  DOC-25 1  DOC-26 1  DOC-27 1
DOC-28 1  DOC-29 1  DOC-30 1
```

## 1. Env-var surface census (178 distinct vars read in source)

Corpora were classified before the diff: reader-facing docs (53 files; `docs/evidence/`,
`docs/dogfood/`, `docs/reviews/`, chain logs, coverage logs, decisions/specs/releases excluded),
in-repo skills (2 files), and generated evidence separately.

```
$ grep -rhoE '(ROUTER|ROUTING|TASK_ROUTER)_[A-Z0-9_]+' scripts/ task_router/ | sort -u | wc -l
178
$ grep -ohE 'ROUTER_[A-Z0-9_]+' docs/configuration.md | sort -u | wc -l
131
```

Vars named in NEITHER reader docs NOR skills, with the first source read:

```
ROUTER_BASE_URL              scripts/record_outcome.py:53      -> owned by DOC-27
ROUTER_E2E_KEY               scripts/proxy_e2e.py:350          -> probe harness only, not operator surface
ROUTER_GENERATED_MARKERS     scripts/router_server.py:4283     -> Python constant, NOT an env read (census false positive)
ROUTER_HOP0_RULE             scripts/router_spawn.py:2164      -> Python constant, NOT an env read (census false positive)
ROUTER_STATE_DIR_WARN        scripts/state_dir.py:36           -> owned by DOC-29
ROUTING_ALLOW_UNSATISFIABLE  scripts/router_tier_calibrate.py:72 -> owned by DOC-25
ROUTING_BOARD_PY             scripts/router_maintain.py:107    -> owned by DOC-11
ROUTING_CACHE                scripts/router_server.py:2928     -> Python constant, NOT an env read (census false positive)
ROUTING_DUCKBRAIN_HOME       scripts/router_seed.py:111        -> owned by DOC-11
ROUTING_LIFECYCLE_FILE       scripts/router_seed.py:599        -> owned by DOC-11
ROUTING_MD_CACHE             scripts/router_pricing_audit.py:63 -> owned by DOC-11
ROUTING_STATE_DB             scripts/router_pricing_audit.py:65 -> owned by DOC-11
ROUTING_TIER_LADDER_IGNORE   scripts/router_tier_calibrate.py:13 -> owned by DOC-26
TASK_ROUTER_REPO             task_router/cli.py:156            -> documented in skills/task-router-usage/SKILL.md:314
```

Result: **zero unowned env-var gaps.** Nothing filed.

## 2. Route surface (17 route literals in scripts/router_server.py)

Per-route reader-doc coverage (`grep -rlF <route> $READER`):

```
/api/provider-rollups -> NONE                      -> owned by DOC-6/DOC-15 ("rollup" route)
/api/models -> docs/research/TR-036 (not reader)   -> owned by DOC-6/DOC-15 ("model-browser" route)
everything else -> README.md and/or docs/
```

Result: **zero unowned route gaps.** Nothing filed.

## 3. CLI surface (28 commands)

```
$ PYTHONPATH=. python3 -m task_router.cli --help   # rc=0
command lines: 28
blank-description lines: chain-run, context-call, ingress, lifecycle, outcomes, pricing-audit
$ grep -cF '`router context-call' README.md
0
```

`context-call` README absence -> owned by DOC-13. Blank descriptions -> owned by DOC-28.
The `make` surface is self-documenting (`make help` prints every target with a description),
so undocumented Makefile targets are not a gap.

Result: **zero unowned CLI gaps.** Nothing filed.

## 4. Internal anchors and relative links

```
$ python3 anchor-census.py        # GitHub slug rules: lowercase, strip punctuation, spaces -> '-', underscores preserved
reader-facing md files scanned: 53
dead internal anchors: 0
```

The first run of this script reported one dead anchor
(`docs/configuration.md -> router-in-bunker-shortcut.md#42-...`); that was a **defect in the
checker** (it folded `api_key_env` to `api-key-env`, which GitHub does not do). The heading exists
at `docs/router-in-bunker-shortcut.md:136` and the anchor resolves. Discarded.

`doc_link_census.py` reported 1 dead relative link, `docs/reviews/tr388-quorum/seat1-glm.md -> r`:
that is diff syntax (`](r)`) inside a fenced code block in a generated quorum seat artifact, not a
reader-facing link. Discarded.

Result: **zero real dead links/anchors.** Nothing filed.

## 5. Live `/health` vs docs (re-verification of DOC-22)

```
$ PYTHONPATH=. ROUTER_STATE_DIR=<scratch> ROUTING_REGISTRY=/home/kara/task-router/registry.json \
  python3 scripts/router_server.py --mode read-only --host 127.0.0.1 --port 9471
$ curl -s -o /dev/null -w 'GET /health -> %{http_code}\n' http://127.0.0.1:9471/health
GET /health -> 200
```

Returned top-level keys (26), documented in `docs/health-plane.md` §2 (14), undocumented (12):

```
undocumented: cache_generation classify_cache code context_fidelity data_cache eligibility
              registry_state runtime source_sha stale surface_mode unmeasured_categories
documented-but-not-returned: (none)
```

DOC-22's premise holds at the new tip and is broader than its title (12 missing, not 6).
No new row: the owning row's criterion already requires the complete key set.

## 6. NEW finding — the `GET /` identity claim (filed as DOC-31)

Doc claim under test, `docs/health-plane.md:39-40`:

```
`GET /health` (and `GET /`, which serves the same payload plus a `_links`
map) returns JSON.
```

Measured on the same read-only instance (`--port 9472`):

```
GET / -> 200
/health keys: 26      / keys: 20
keys in / but NOT in /health:        _links, hermes_upstream
keys in /health but NOT in /:        admission, cache_generation, classify_cache, data_cache,
                                     eligibility, runtime, serving, source_sha, surface_mode
```

Code anchors: `scripts/router_server.py:758-771` (`/` = `router_health.health()` + `hermes_upstream`
+ `_links`) versus `scripts/router_server.py:916-944` (`/health` additionally attaches `eligibility`
and `admission`, and the rest of the nine).

`hermes_upstream` is named in zero reader-facing docs:

```
$ git grep -n 'hermes_upstream' -- '*.md' ':!docs/evidence/*'
(no output)
```

Different mechanism from DOC-22 (which owns the §2 key ENUMERATION of `/health`): a worker fixing
DOC-22 adds keys to the `/health` block and leaves the false `/` identity claim standing — and makes
it more misleading, because the block it points at then no longer describes `/`.

## 7. Re-verification of open rows at this tip (premises still live)

```
DOC-24  git grep -n "failure-contract" HEAD -- '*.md' ':!docs/evidence/*' | grep -v '^HEAD:docs/failure-contract.md'
        -> zero inbound links
DOC-25  python3 scripts/router_seed.py --help  -> "usage: router_seed.py [-h]" (only -h)
        git grep -F 'allow-unsatisfiable' -- README.md docs/configuration.md  -> zero hits
DOC-26  git grep -F 'TIER_LADDER_IGNORE' -- '*.md' ':!docs/evidence/*'      -> zero hits
DOC-27  git grep -F 'ROUTER_BASE_URL' -- '*.md' ':!docs/evidence/*'         -> zero hits
DOC-28  router --help -> 28 commands, 6 with a blank description (unchanged)
DOC-29  git grep -F 'ROUTER_STATE_DIR_WARN' -- '*.md' ':!docs/evidence/*'   -> zero hits
DOC-30  grep -c 'accounting' README.md -> 0 ; grep -c 'pacing' README.md -> 0 ; grep -c 'burn' README.md -> 2
DOC-8   docs/configuration.md ROUTER_CLASSIFIER_MODEL default `deepseek-v4.1-flash`
        vs scripts/router_classify.py default 'glm-5.3-flash'  -> still drifted
DOC-10  README.md:690 "For the remaining 44 ROUTER_* environment variables" vs 131 documented
        -> still drifted; the row's foreman_note already carries the corrected anchor (:690) and count (131)
```

## 8. Board baseline (pre-write)

```
$ boardctl -C <worktree> validate
RESULT: FAIL (455 error(s), 601 warning(s))
```

Pre-existing; none of this lane's rows is the subject of an error (checked after the write).

## 9. Disclosure — this artifact is itself a tracked `*.md`

Committing this transcript under `docs/evidence/` makes it a tracked Markdown file that NAMES
`ROUTER_BASE_URL`, `ROUTER_STATE_DIR_WARN`, `ROUTING_TIER_LADDER_IGNORE`, `ROUTING_ALLOW_UNSATISFIABLE`
and `hermes_upstream`. Every "zero doc hits" claim above therefore excludes `docs/evidence/*` and says
so in its own command; a bare `git grep -- '*.md'` run after this commit will return this artifact and
prove nothing. The affected rows (DOC-25/26/27/29) carry file-scoped or `:!docs/evidence/*` verifiers;
DOC-29's criterion makes that exclusion mandatory.

