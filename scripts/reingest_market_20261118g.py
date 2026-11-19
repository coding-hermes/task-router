#!/usr/bin/env python3
"""Stamp provenance on the frozen 2026-11-17 market cells for
deepseek/deepseek-v4-flash-vision-exp.

Card t_model_deepseek_deepseek-v4-flash-vision-exp carries 2 source=market rows
(artificial_analysis_coding 0.65, artificial_analysis_agentic 0.475) with
source_ref=NULL: the stored 0-1 score cannot be traced back to the index and raw
score it was normalized from.

The 2026-11-18 feed publishes `benchmarks: null` for this id (223 of 480 ids
still carry an AA block, this one does not), so the two rows are FROZEN
2026-11-17 evidence. They are kept and stamped with the provenance that is
actually known — origin date, index name, the raw 0-100 score the 0-1 value was
normalized from, and an explicit not-republished marker — rather than dropped or
silently left blank.

Every other line of benchmarks.jsonl is copied byte-for-byte.
"""
import json
from pathlib import Path

DATE = "2026-11-18"
PRIOR = "2026-11-17"
BENCH = "data/tables/benchmarks.jsonl"
MODEL = "deepseek/deepseek-v4-flash-vision-exp"
AA_CATS = {
    "coding_index": "artificial_analysis_coding",
    "agentic_index": "artificial_analysis_agentic",
    "intelligence_index": "artificial_analysis_intelligence",
}


def main():
    out, stamped, already = [], 0, 0
    with open(BENCH, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                out.append(line)
                continue
            r = json.loads(line)
            if r.get("model") != MODEL or r.get("source") != "market":
                out.append(line)
                continue
            if r.get("source_ref"):
                already += 1
                out.append(line)
                continue
            index = next((k for k, v in AA_CATS.items() if v == r["category"]), None)
            if index is None:
                out.append(line)
                continue
            raw = round(float(r["score"]) * 100.0, 6)
            r["source_ref"] = (
                f"openrouter-models-api:{PRIOR}:index={index}:raw_score={raw}"
                f":not-republished-{DATE}"
            )
            stamped += 1
            out.append(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")
    with open(BENCH, "w", encoding="utf-8") as f:
        f.writelines(out)
    print(f"stamped {stamped} frozen AA rows; {already} already carried provenance")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
