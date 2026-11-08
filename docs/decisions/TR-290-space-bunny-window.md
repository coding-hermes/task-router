# ADR TR-290: The Space Bunny Promo Window Is Stamped Unknown-Ended, Not Re-Priced

**Status:** accepted
**Date:** 2026-11-08
**Owner:** task-router foreman
**Supersedes:** none
**Superseded by:** (none)

## Context

The Space Bunny lane reads `public_price 0.0` in `data/tables/models.jsonl` with no expiry
marker: the free window existed only in conversation, so when it ended nothing in the
registry would say why the price changed. The only evidence that the window ever existed is
an owner report on 2026-10-03 that the lane "is free and all for another day or so". No end
date was ever published; as of 2026-11-08 the window has lapsed by that report's own wording.
Five registry rows carry the 0.0 price (one live: `opencode-go/space-bunny-free`; four
disabled carriers), and `temporary_discounts.jsonl` mentioned space-bunny only inside
another model's note — the lane had no discount row of its own.

## Decision

Stamp the window, do not re-price:

1. `temporary_discounts.jsonl` gains one row for the live free lane
   (`opencode-go/space-bunny-free`): `discount_type free`, `value 1.0`,
   `valid_from 2026-10-03`, `valid_to null` **with a note** (end UNKNOWN — no invented
   date), source citing the owner report verbatim, recheck 2026-11-14.
2. Every space-bunny `models.jsonl` row priced 0.0 carries a `note` stating the window end
   is UNKNOWN and the lane is PAST its promo window as of 2026-11-08, so a closed window
   cannot read as still free. `public_price` is unchanged — this is a window stamp, not a
   re-price; the eventual price correction belongs to a live-measurement tick.

Unknown is written as unknown with a reason (R4/R7: no anonymous or invented dates).
`valid_to` stays null wherever no honest date exists; rows already retired 2026-10-23 with
provenance keep their dates.

---

**Verification:**

```bash
$ jq -r 'select(.valid_to==null and ((.note//"")|length==0)) | .model' data/tables/temporary_discounts.jsonl | wc -l
0

$ grep -c space-bunny data/tables/temporary_discounts.jsonl
2

$ jq -r 'select((.model//"")|test("space-bunny")) | select(.public_price==0.0) | [.provider,.model,(.note|test("PAST its promo window"))]|@tsv' data/tables/models.jsonl
# all five rows: true
```

ch:trace:TR-290:adr:data-stamp
