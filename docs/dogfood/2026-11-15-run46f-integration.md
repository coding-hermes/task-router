# Run 46f integration report — `router chain-run` as a user would wire it

Fire 46f of tick `task-router-dogfood-2026-11-15-15-59-32`, HEAD `b6731033`. The question this run answers:
*if I put `router chain-run` inside my own agent loop, what happens to me?*

## What a user gets

`chain-run` resolves a project's chain and re-runs **one shell command** once per hop, handing each attempt
`ROUTER_HOP`, `ROUTER_PROVIDER` and `ROUTER_MODEL` in the environment, and it emits a JSON document with
`attempts[]` (`hop`, `provider`, `model`, `rc`, `latency`, `outcome`, `error`), `success` and `final`. That is
exactly the integration a harness needs: the caller stays a dumb script, the router owns the ladder.

It works. With a caller that fails only on hop 13, the walk advanced 13 → 14 and stopped on the first success,
with correct per-attempt env and `success=true final=xkiro-2`. A stranger can therefore depend on the *happy
path* after reading the README.

## What breaks a user's loop

1. **The exit code lies on exhaustion.** All hops failing returns rc 0 with `success=false final=null`. In bash,
   `if router chain-run …; then deploy; fi` deploys on a total failure. A user must parse JSON to find out
   whether their command ever succeeded — the opposite of what an exit status is for (`TR-1000976681059264`).
2. **The wrong failure is retried.** Any non-zero exit is classified `transport-failure`
   (`scripts/router_chain_run.py:104-106`), so a caller's deterministic error — bad config, exit 3 — is replayed
   down the whole 71-hop chain, ~5-6 minutes, with no `--stop-on-rc` (`TR-1000976681059265`).
3. **Each hop costs ~5 s of local bookkeeping** for a 0.019 s command, because the 413 MB outcomes ledger is
   re-parsed per attempt (~475k `json.loads`/hop) although the hop already carries its outcome stats
   (`PERF-1`). The fallback ladder aimed at latency ends up dominated by disk.
4. **It is not "everywhere".** `chain-run` needs a checkout (rc 2 from a scratch cwd, and its own message says
   the `scripts/` tools ship with the checkout, not the wheel) while `spawn`/`status`/`estimate` work anywhere —
   contradicting `README.md:632` (`DOC-19`).
5. **Ledger corruption is stderr noise, not a number.** 2 torn lines (436576, 436577) from interleaved appends
   are skipped with a warning repeated on every append; nothing reports the count on demand
   (`TR-1000976681059266`).

## Usability judgement

The *design* is good and the *diagnostics* are unusually good for this codebase — the hop-13 refusal names the
cause and gives three concrete workarounds, per-attempt JSON carries rc/latency/error, and the success path
stops at the first working hop. What is missing is the contract layer: an exit status that means what a shell
thinks it means, a way to say "this failure is not worth retrying", and a cost per hop that does not grow with
the operator's history.

## Verdict

**PROMISING-BUT-UNSAFE-TO-AUTOMATE.** Usable today by a human watching the JSON; not safe to put behind
`&&` in a pipeline until the exit status is honest. Five rows filed — one P1 (`TR-1000976681059264`), two P2
(`TR-1000976681059265`, `PERF-1`), two P3 (`TR-1000976681059266`, `DOC-19`).

## Left behind

This report, `docs/dogfood/2026-11-15-run46f-diagnostics.md` (commands, exact outputs, timings, profile), the
run record in `.coding-hermes/dogfood-log.md`, and
`.coding-hermes/evidence/DOGFOOD-2026-11-15-2015-taskrouter-46f.jsonl` (8 records — 1 run header + 7 steps,
sha256
`75b822e8016ed2faa806ef3c65c47fa11a060bb8efd2ad3cfe83d9ee9b966758`). No product code, no existing row's status,
no visibility, permission, scheduler state or credential touched.
