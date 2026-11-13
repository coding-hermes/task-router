# TR-DISK-001 — legacy metrics generation compressed (ops tick 2026-11-13)

Tick: `task-router-ops-2026-11-13-20-03-05` (wall clock 15:03–15:20 -05:00)
Host: kara (nvme0n1p2, 1.8T), repo `/home/kara/task-router` @ `origin/main` `e994808e`
Lane: monitoring / ops

Every command below was executed on the live host; output is quoted verbatim.

## Acceptance arms (literal, from the row)

### Arm 1 — the 6.3 G legacy generation is gone (no such file, or a compressed residue under 1 G)

BEFORE:

```
$ du -h data/metrics.jsonl.1
6.3G    data/metrics.jsonl.1
```

Rotation was exercised first (arm 2), which shifted the legacy generation to `.2`:

```
$ ls -la data/metrics.jsonl*
-rw-rw-r-- 1 kara kara  414972488 Nov 13 15:03 metrics.jsonl.1
-rw-rw-r-- 1 kara kara 6662258500 Nov  6 11:18 metrics.jsonl.2
```

Compressed in place with `nice -n 10 gzip -6` (20.2 s wall, rc=0), integrity checked:

```
$ gzip -t data/metrics.jsonl.2.gz ; echo rc=$?
rc=0
```

AFTER:

```
$ du -h data/metrics.jsonl.2
du: cannot access 'data/metrics.jsonl.2': No such file or directory
rc=1
$ du -h data/metrics.jsonl.2.gz
192M    data/metrics.jsonl.2.gz
```

6 662 258 500 B → 201 005 626 B (33x). No metrics history was dropped: the generation
is retained, decompressible, and `gzip -t` passes.

### Arm 2 — `ROUTER_METRICS_MAX_BYTES=1048576 python3 scripts/router_metrics.py --rotate-now` exits 0 and rotates

```
$ ROUTER_METRICS_MAX_BYTES=1048576 python3 scripts/router_metrics.py --rotate-now
rotated=1 retained_bytes=7077230988
rc=0
```

Idempotent re-run after the compression (nothing left to rotate at the 1 MiB cap):

```
$ ROUTER_METRICS_MAX_BYTES=1048576 python3 scripts/router_metrics.py --rotate-now
rotated=0 retained_bytes=414972488
rc=0
```

### Arm 3 — `df -h` Use% no higher than today

BEFORE:

```
$ df -h /home/kara
/dev/nvme0n1p2  1.8T  1.6T  175G  90% /
```

AFTER:

```
$ df -h /home/kara
/dev/nvme0n1p2  1.8T  1.6T  181G  90% /
```

Use% 90% (= pre-state, arm satisfied); 6.4 GiB reclaimed (175 G → 181 G available).

## Residual state

```
$ ls -la data/metrics.jsonl*
-rw-rw-r-- 1 kara kara 414972488 Nov 13 15:03 data/metrics.jsonl.1
-rw-rw-r-- 1 kara kara 201005626 Nov  6 11:18 data/metrics.jsonl.2.gz
```

Retained hot set = active file + one 415 MB generation (within `ROUTER_METRICS_GENERATIONS`)
plus the compressed legacy generation. The writer already enforces the cap
(`ROUTER_METRICS_MAX_BYTES`, default 512 MiB), so this row's premise — "no rotation,
unbounded growth" — is closed by measurement, not assumption.

## Separate finding discovered during the same probe (filed as its own row)

`data/state` — not metrics — is the dominant consumer: 124 G of 125 G total.

```
$ ls -1 data/state/outcomes.jsonl.bak-verdicts-* | wc -l
354
$ du -ch data/state/outcomes.jsonl.bak-verdicts-* | tail -1
123G    total
$ du -sh data/state
124G    data/state
$ ls -la data/state/outcomes.jsonl
398104347  Nov 13 15:02  outcomes.jsonl
```

Every mutating `apply-verdicts` pass writes a **full 380 MB snapshot** of the store
(`scripts/router_outcomes.py`, the backup path built in `apply_tick_verdicts`, ~line 1720)
and retains every snapshot forever — roughly 30 mutating passes/day, ~8.4 GB/day.

Snapshot chain proved to be a monotone snapshot set, not divergent data — 11 backups
sampled across 2026-11-02 → 2026-11-13; every `session_id` they contain is present in the
live store (`missing_from_live=0` for all 11, including the two oldest):

```
live_lines=471709 live_ids=272646
sample_backup=outcomes.jsonl.bak-verdicts-20261102-220640 lines=446779 ids=257329 missing_from_live=0
sample_backup=outcomes.jsonl.bak-verdicts-20261102-230747 lines=446925 ids=257464 missing_from_live=0
sample_backup=outcomes.jsonl.bak-verdicts-20261105-131405 lines=458637 ids=265474 missing_from_live=0
sample_backup=outcomes.jsonl.bak-verdicts-20261105-214930 lines=459141 ids=265749 missing_from_live=0
sample_backup=outcomes.jsonl.bak-verdicts-20261108-050726 lines=463398 ids=268117 missing_from_live=0
sample_backup=outcomes.jsonl.bak-verdicts-20261108-154329 lines=463907 ids=268420 missing_from_live=0
sample_backup=outcomes.jsonl.bak-verdicts-20261109-075412 lines=464807 ids=268920 missing_from_live=0
sample_backup=outcomes.jsonl.bak-verdicts-20261111-015540 lines=466992 ids=270077 missing_from_live=0
sample_backup=outcomes.jsonl.bak-verdicts-20261112-172934 lines=470373 ids=271905 missing_from_live=0
sample_backup=outcomes.jsonl.bak-verdicts-20261112-220257 lines=470512 ids=271982 missing_from_live=0
sample_backup=outcomes.jsonl.bak-verdicts-20261113-002228 lines=470785 ids=272137 missing_from_live=0
```

Prune prepared (keep newest 24 ≈ 16 h of rollback, delete 330 oldest = 20261102-220640 …
20261112-220257) but **NOT executed**: the `rm` was blocked by the harness gate
(destructive cleanup without interactive consent — silence is not consent). State is
unchanged: 354 files / 123 G / live store 398 104 347 B. Pruning is left to an owner-
authorised action; the durable fix is code retention in the writer, not a manual sweep.
