#!/usr/bin/env python3
"""Market cross-check wave 2026-11-17e — Mistral family (9 cards).

Cards: t_model_mistralai_ministral-3b-2512, t_model_mistralai_ministral-8b-2512,
t_model_mistralai_mistral-large-2512, t_model_mistralai_mistral-medium-3,
t_model_mistralai_mistral-medium-3-5, t_model_mistralai_mistral-medium-3-5:batch,
t_model_mistralai_mistral-medium-3.1, t_model_mistralai_mistral-small-2603,
t_model_mistralai_mistral-small-3.2-24b-instruct.

Line-level rewrite: only the targeted (provider, model) rows are re-serialised;
every other line is copied byte-for-byte.
"""
import json
import os

TABLES = "data/tables/models.jsonl"
DATE = "2026-11-17"
FEED = "carrier:openrouter /api/v1/models read 2026-11-17"
MD = "models.dev api.json read 2026-11-17"

# (provider, model) -> {column: new value}
UPDATES = {
    # Mistral Medium 3: the stored sticker was 0.4/2.0/cache 0.04 — a 10%
    # under-price against BOTH the live feed and models.dev (0.44/2.2/0.044).
    ("openrouter", "mistralai/mistral-medium-3"): {
        "normalized_price": 0.44, "public_in_per_m": 0.44, "public_out_per_m": 2.2,
        "public_cache_read_per_m": 0.044, "public_price": 0.5104,
    },
    # Mistral Medium 3.1: same stale 0.4/2.0 base, and this lane is LIVE
    # (valid_to null) so the under-price was ranking it against lanes it does
    # not beat.
    ("openrouter", "mistralai/mistral-medium-3.1"): {
        "normalized_price": 0.44, "public_in_per_m": 0.44, "public_out_per_m": 2.2,
        "public_cache_read_per_m": 0.044, "public_price": 0.5104,
    },
    # Mistral Small 3.2 24B: stored 0.09375/0.25 is exactly 1.25x the published
    # 0.075/0.2 (a stale conversion residue, not a carrier markup — the feed and
    # models.dev both publish 0.075/0.2 with no cache rate).
    ("openrouter", "mistralai/mistral-small-3.2-24b-instruct"): {
        "normalized_price": 0.075, "public_in_per_m": 0.075, "public_out_per_m": 0.2,
        "public_price": 0.08,
    },
    # Mistral Large 3 2512: vision was NULL on the openrouter lane while the feed
    # publishes text+image+file input, models.dev publishes text+image+pdf, and
    # both xkiro twins already carry vision=true.
    ("openrouter", "mistralai/mistral-large-2512"): {"vision": True},
    # Mistral Small 2603: release_date is a MODEL property; the openrouter twin
    # carries 2026-03-16 (feed created + models.dev release_date agree) while
    # both xkiro lanes were NULL.
    ("xkiro", "mistralai/mistral-small-2603"): {"release_date": "2026-03-16"},
    ("xkiro-2", "mistralai/mistral-small-2603"): {"release_date": "2026-03-16"},
    # Mistral Medium 3.5: 2026-04-27 was 3 days early. models.dev release_date,
    # the feed's created stamp and the publisher announcement all say 2026-04-30.
    ("openrouter", "mistralai/mistral-medium-3-5"): {"release_date": "2026-04-30"},
    ("openrouter", "mistralai/mistral-medium-3-5:batch"): {"release_date": "2026-04-30"},
    # xkiro spells the same model with a dot (mistralai/mistral-medium-3.5); its
    # release_date was NULL on both lanes.
    ("xkiro", "mistralai/mistral-medium-3.5"): {"release_date": "2026-04-30"},
    ("xkiro-2", "mistralai/mistral-medium-3.5"): {"release_date": "2026-04-30"},
}

EVIDENCE_SUFFIX = {
    ("openrouter", "mistralai/mistral-medium-3"): (
        " | " + DATE + " cross-check: stale at 0.4/2.0/cache 0.04 — 10% under-priced. "
        + MD + " (openrouter/mistralai/mistral-medium-3) and " + FEED
        + " both publish 0.44/2.2, cache-read 0.044, 131,072 ctx, text+image+pdf input. "
        "public_price re-blended 0.96*in+0.04*out = 0.5104 (the convention every other "
        "openrouter row follows). Lane is retired (valid_to 2026-09-26) but the sticker "
        "is still the record of what it cost."
    ),
    ("openrouter", "mistralai/mistral-medium-3.1"): (
        " | " + DATE + " cross-check: stale at 0.4/2.0/cache 0.04 — 10% under-priced on a "
        "LIVE lane. " + MD + " (openrouter/mistralai/mistral-medium-3.1) and " + FEED
        + " both publish 0.44/2.2, cache-read 0.044, 131,072 ctx, text+image+pdf input. "
        "public_price re-blended 0.96*in+0.04*out = 0.5104."
    ),
    ("openrouter", "mistralai/mistral-small-3.2-24b-instruct"): (
        " | " + DATE + " cross-check: stale at 0.09375/0.25 — exactly 1.25x the published "
        "sticker, a conversion residue rather than a carrier markup. " + MD
        + " (openrouter/mistralai/mistral-small-3.2-24b-instruct) and " + FEED
        + " both publish 0.075/0.2 with NO cache rate (cache-read stays NULL = not "
        "published, never 0), 256,000 ctx, text+image input. public_price re-blended "
        "0.96*in+0.04*out = 0.08."
    ),
    ("openrouter", "mistralai/mistral-large-2512"): (
        " | " + DATE + " cross-check (no price change): 0.5/1.5/cache 0.05, 262,144 ctx "
        "match " + FEED + " and " + MD + " exactly. vision was NULL on this lane while "
        "the feed publishes text+image+file input and models.dev publishes "
        "text+image+pdf; both xkiro twins already said vision=true. Filled."
    ),
    ("xkiro", "mistralai/mistral-small-2603"): (
        " | " + DATE + " cross-check (no price change): release_date filled 2026-03-16 — "
        "a MODEL property, not a lane property; the openrouter twin carries it and "
        + MD + " + " + FEED + " agree. xkiro publishes no release date of its own."
    ),
    ("xkiro-2", "mistralai/mistral-small-2603"): (
        " | " + DATE + " cross-check (no price change): release_date filled 2026-03-16 — "
        "a MODEL property, not a lane property; the openrouter twin carries it and "
        + MD + " + " + FEED + " agree. xkiro-2 publishes no release date of its own."
    ),
    ("openrouter", "mistralai/mistral-medium-3-5"): (
        " | " + DATE + " cross-check (no price change): 1.5/7.5, 262,144 ctx, "
        "text+image+pdf input, reasoning=true all match " + FEED + " and " + MD
        + "; no cache rate is published by either, so cache-read/-write stay NULL. "
        "release_date corrected 2026-04-27 -> 2026-04-30 (models.dev release_date, the "
        "feed's created stamp and the publisher announcement all say 2026-04-30)."
    ),
    ("openrouter", "mistralai/mistral-medium-3-5:batch"): (
        " | " + DATE + " cross-check (no price change): 0.75/3.75 is exactly half the "
        "1.5/7.5 base, matching " + FEED + " for the :batch id; 262,144 ctx. "
        "release_date corrected 2026-04-27 -> 2026-04-30 with the base lane."
    ),
    ("xkiro", "mistralai/mistral-medium-3.5"): (
        " | " + DATE + " cross-check (no price change): xkiro's dotted spelling of "
        "mistralai/mistral-medium-3-5 (1.5/7.5, 256,000 ctx, thinking+vision true). "
        "release_date filled 2026-04-30 — a MODEL property; xkiro publishes no release "
        "date of its own, " + MD + " and the publisher announcement both say 2026-04-30."
    ),
    ("xkiro-2", "mistralai/mistral-medium-3.5"): (
        " | " + DATE + " cross-check (no price change): xkiro-2's dotted spelling of "
        "mistralai/mistral-medium-3-5 (1.5/7.5, 256,000 ctx, thinking+vision true). "
        "release_date filled 2026-04-30 — a MODEL property; " + MD + " and the publisher "
        "announcement both say 2026-04-30."
    ),
}


def main():
    out = []
    touched = 0
    with open(TABLES) as f:
        for line in f:
            raw = line.rstrip("\n")
            if not raw.strip():
                out.append(line)
                continue
            row = json.loads(raw)
            key = (row.get("provider"), row.get("model"))
            if key in UPDATES:
                for col, val in UPDATES[key].items():
                    row[col] = val
                if key in EVIDENCE_SUFFIX:
                    row["price_evidence"] = (row.get("price_evidence") or "") + EVIDENCE_SUFFIX[key]
                out.append(json.dumps(row) + "\n")
                touched += 1
            else:
                out.append(line)
    assert touched == len(UPDATES), "expected %d rows, touched %d" % (len(UPDATES), touched)
    tmp = TABLES + ".tmp"
    with open(tmp, "w") as f:
        f.writelines(out)
    os.replace(tmp, TABLES)
    print("rewrote %d rows" % touched)


if __name__ == "__main__":
    main()
