# Owner asks — the standing ledger

Purpose: the owner's requests, in his words, with the date, so that work is never reported as a *finding*
when it is actually an *open ask*. Every core behaviour the router promises traces to a line here.

Rule for this file: a row moves to VERIFIED only with a measurement that shows the behaviour on live
traffic. "Landed", "spec'd", "component complete" are not verified.

Sources: this repo's transcripts (2,287 real owner messages; 92 ask-shaped), the board, and the ledger.

---

## Cost per task, not cost per token

| date | ask (verbatim, trimmed) | state now |
|---|---|---|
| 2026-09-16 | "the issue has been that we are routing things by the cost by token but we are not routing by the cost per task" | **NOT DONE.** `DEFAULT_SORT = os.environ.get('ROUTER_SPAWN_SORT') or 'price'` (router_spawn.py:149). Ranking is still sticker price — the exact complaint. `_sort_predicted_cost_per_task()` and `measured_basis()` (TR-183 floor + coverage bar) exist and are off. |
| 2026-09-16 | "i want us to have a rolling average that we are storing in the jsonl database" | **DONE.** `data/state/outcomes-averages.jsonl`, 398 groups, keyed source+provider+model+complexity_sig, windows 24/72/168h. |
| 2026-09-17 | "did we ever build the system for the rolling average calculations of the cost per task based on task complexity and the task routing by reading the prompts and stuff" | **PARTLY.** Averages built; routing does not read them; prompt-reading is rare (see below). Board row TR-283. |
| 2026-09-30 | "Does it build the metrics so we can do cost per task complexity based routing" | **NOT YET.** Blocked by the band-key join: averages are keyed by `complexity_sig` = hashes (214) / `profile:P0_FORE` (39) / None (145), none mappable from a task. |
| 2026-10-03 | "use the rolling averages to help you figure out what the right models to be using on tasks over time" | **NOT YET** — same row (TR-283). |

## Complexity from the prompt, not from a fixed list

| date | ask (verbatim, trimmed) | state now |
|---|---|---|
| 2026-09-19 | "we want the model selector to first select which models match based on complexity" | **PARTLY** — matching exists; the input is usually missing. |
| 2026-09-23 | "make sure we are not always just going to cheapest model just because it is in that [list]" | **NOT DONE.** A permissive floor resolves to the cheapest lane. |
| 2026-09-23 | "the goal … was that the model was saying P2_agentic and it was always a fixed list and we were never getting it to actually look at complexity directly" | **NOT DONE.** Measured on the first hour of flipped traffic: `complexity_source` = default 70 / None 17 / classifier 7 / classifier-empty 3; `profile_id` = `P0_FORE` on 65 of 97 rows. |
| 2026-09-24 | "you look at the prompt that comes in and you can say oh okay this needs some level of complexity" | **NOT DONE** — the prompt is read on ~10% of requests. |
| 2026-09-28 | "use an llm or jev … to fill in those classifiers for us for the prompt … no more fixed lists" | **NOT DONE** — same evidence. JEV exists; `classifier-empty` was 221 of all-time proxy rows. |
| 2026-10-03 | "is it profile_id that we should be using or are we not actually looking at input prompt" / "start passing in the raw complexities for us of each task, not the profile" | **DECIDED, NOT BUILT** — TR-237 rewritten: prompt is the source, callers pass raw levels, `profile_id` demoted to policy (allow/deny, floor, ceiling), board gains a raw-level field, prompts instruct raw levels. |

## Log what we proxied, the option chain, and the model actually run

| date | ask (verbatim, trimmed) | state now |
|---|---|---|
| 2026-09-25 | "make sure we are logging and tracking everything — knowing what we proxied, what the option chain was, what the complexities were" | **DONE on the proxy path.** Post-flip router rows carry `chain`, `chain_length`, `hops_attempted`, `served_by_hop`, `skipped_hops*`, `exclusions`, `complexity_source`, `degrade_reason`. It was 0% before the flip. |
| 2026-09-26 | "we have to know the models from the chain and then the model run for the Hermes request" | **DONE.** `served_by_hop` + `provider`/`model` on the same row; `model` on 100% of post-flip proxy rows. |
| 2026-09-25 | "build the ui, the stats for nerds … search the data, the charts, the flow, the rows" | **DONE.** `:9092/ui` "data command center", 8 panels incl. request flow and ledger search; `/proxy/stats` aggregates. |
| 2026-10-03 | "does the UI let you browse models with up/down, latency, tokens" | **NOT DONE** — TR-258 (join proven: registry × health × averages → up/down, latency, tokens, price). |

## Route the traffic through the router

| date | ask (verbatim, trimmed) | state now |
|---|---|---|
| 2026-09-23 | "allow it to be a proxy in front of hermes and other ai harnesses and services" | **DONE.** Proxy fronts the gateway (`ROUTER_PROXY_UPSTREAM`); OpenAI surface verified; a third-party Hermes (remote box) was served through it. |
| 2026-09-25 | "create the tasks to close the gaps so we can route traffic this way" | **DONE 2026-10-03.** Scheduler flipped: `.gateway.url` = `http://127.0.0.1:9391`. |
| 2026-09-24 | "the scheduler calls are supposed to go to hermes but you're in the middle, you look at the prompt" | **HALF.** Calls go through the router now; the prompt is still mostly not read (see above). |
| 2026-09-25 | "have you tested yourself … make sure you're not just with a pinned list but auto adjusting" | **PARTLY.** Fallback ladder exercised (mean 1.4 hops, 100% of hop-attempting rows served); ranking still pinned to price. |
| 2026-10-03 | "point this scheduler into the local task router and let's see" | **DONE**, tracked on TR-263 with a live watcher. Early numbers: 7 refusals, latency p50 11.4s (direct 2.2s), max 180.08s. |

## Remote / offload to bunkers

| date | ask (verbatim, trimmed) | state now |
|---|---|---|
| 2026-09-19..10-03 (17 messages) | "offloading work onto bunkers … the task router will be remote on each bunker and will forward the request to the Hermes that is going to actually execute the work" | **NOT DONE.** Local topology proven end to end (router in front, Hermes behind, router chooses lane). Per-bunker deployment is the same shape with the address moved; related work in flight: SCHED-GAP-1720 (receive path), SCHED-GAP-1723 (per-agent gateway + `API_SERVER_KEY`), TR-236 (crier ingress). |

---

## The alignment statement

Four behaviours the owner has asked for repeatedly are still not in place, and each has been reported at
some point as done or spec'd:

1. **Cost per task decides the rank** (asked 09-16, restated 09-17, 09-30, 10-03). Ranking is price.
2. **The prompt determines complexity** (asked 09-19, 09-23 ×2, 09-24, 09-28, 10-03). Measured today: ~10%.
3. **Raw complexities are data, not a profile** (asked 10-03). The board cannot express them.
4. **The measured bands join to a task** (implied by 1 and 2). The join does not exist.

What the flip changed is the *precondition*: routed rows now carry chain + complexity + cost together, so
the raw material for all four exists for the first time. That is progress on the owner's ask — not a
separate feature, and not a discovery.

Standing rule adopted here: quote the owner's ask, with its date, in the row that closes it.
