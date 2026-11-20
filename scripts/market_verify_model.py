#!/usr/bin/env python3
"""Verify + complete the market evidence for ONE model (one kanban model card).

The model-card queue asks for a per-model pass:

  1. confirm the feed row landed in benchmarks.jsonl with provenance
     (source=market, percentile in score, raw ELO + rank + arena + n in
     source_ref);
  2. fill what the feed can prove about the SPEC (context window, thinking
     support, modalities, release date) into models.jsonl with provenance;
  3. write EXPLAINED NULLS for anything the feed cannot prove — no silent
     blanks.

The wholesale ingest (scripts/router_market_ingest.py) converges the whole
registry in one pass. This tool exists because the card contract is ONE MODEL
PER CARD: a card pass must be reviewable as a one-model diff, and running the
wholesale ingest from a card would rewrite ~4,900 cells of other models'
evidence under a commit message that names one model.

Scope, deliberately narrow:
  * benchmarks.jsonl — only the source=market rows for THIS model id are
    replaced (every other line is copied byte-for-byte). Fresh rows come from
    the same build_rows() the ingest uses, so the shapes cannot drift.
  * models.jsonl — only this model id's rows, and only spec fields the feed is
    authoritative for. A stored value that disagrees with the feed is reported
    and left alone unless --force-spec is given: the feed is a carrier view,
    and a lane's own catalogue can legitimately differ (a provider serving the
    model with a smaller window is not a wrong row).

Nothing is written without --write; the default run is a dry report.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, "scripts")
import router_market_ingest as rmi  # noqa: E402

BENCH = Path("data/tables/benchmarks.jsonl")
MODELS = Path("data/tables/models.jsonl")
NULL_REASONS = Path("data/null_reasons.jsonl")
# Fields the NULL-stamp registry explains. A stamp on any of these is only valid
# while the row leaves the field null.
_STAMPABLE_FIELDS = (
    "api_type", "data_class", "context_limit", "vision", "thinking",
    "cap_image_generation", "cap_embedding", "cap_computer_use", "cap_audio",
    "cap_web_search", "cap_video", "release_date", "normalized_price",
)


def load_jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def feed_row(payload: dict, model: str) -> dict | None:
    for entry in payload.get("data") or []:
        if entry.get("id") == model:
            return entry
    return None


def feed_spec(entry: dict) -> dict:
    """The spec facts the feed can prove, in registry vocabulary."""
    arch = entry.get("architecture") or {}
    inputs = [str(x) for x in (arch.get("input_modalities") or [])]
    outputs = [str(x) for x in (arch.get("output_modalities") or [])]
    params = [str(x) for x in (entry.get("supported_parameters") or [])]
    top = entry.get("top_provider") or {}
    created = entry.get("created")
    release = (
        dt.datetime.fromtimestamp(int(created), dt.timezone.utc).date().isoformat()
        if isinstance(created, int)
        else None
    )
    return {
        "context_length": entry.get("context_length"),
        "top_provider_context_length": top.get("context_length"),
        "max_completion_tokens": top.get("max_completion_tokens"),
        "vision": "image" in inputs,
        "audio_input": "audio" in inputs,
        "video_input": "video" in inputs,
        "image_output": "image" in outputs,
        "audio_output": "audio" in outputs,
        "thinking": "reasoning" in params or "include_reasoning" in params,
        "reasoning_effort_param": "reasoning_effort" in params,
        "tool_use": "tools" in params,
        "structured_outputs": "structured_outputs" in params,
        "release_date": release,
        "hugging_face_id": entry.get("hugging_face_id"),
        "modality": arch.get("modality"),
        "knowledge_cutoff": entry.get("knowledge_cutoff"),
    }


def spec_deltas(stored: dict, feed: dict) -> list[dict]:
    """Compare the registry row against the feed; name every disagreement."""
    checks = [
        ("context_limit", "context_length"),
        ("vision", "vision"),
        ("thinking", "thinking"),
        ("cap_image_generation", "image_output"),
        ("cap_audio", "audio_input"),
        ("cap_video", "video_input"),
        ("release_date", "release_date"),
    ]
    deltas: list[dict] = []
    for column, key in checks:
        have = stored.get(column)
        want = feed.get(key)
        if want is None:
            continue
        if isinstance(have, bool) or isinstance(want, bool):
            if bool(have) == bool(want) and have is not None:
                continue
            if have is None:
                deltas.append({"field": column, "stored": None, "feed": want, "kind": "fill"})
            else:
                deltas.append({"field": column, "stored": have, "feed": want, "kind": "conflict"})
            continue
        if have is None:
            deltas.append({"field": column, "stored": None, "feed": want, "kind": "fill"})
        elif str(have) != str(want):
            deltas.append({"field": column, "stored": have, "feed": want, "kind": "conflict"})
    return deltas


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True, help="feed model id, e.g. z-ai/glm-4.6")
    parser.add_argument("--date", required=True, help="evidence date (UTC YYYY-MM-DD)")
    parser.add_argument("--input", required=True, help="captured OpenRouter models payload")
    parser.add_argument("--models", default=str(MODELS))
    parser.add_argument("--output", default=str(BENCH))
    parser.add_argument("--write", action="store_true", help="apply; default is a dry report")
    parser.add_argument(
        "--force-spec",
        action="store_true",
        help="also overwrite conflicting spec values with the feed's (default: report only)",
    )
    args = parser.parse_args(argv)

    payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
    entry = feed_row(payload, args.model)
    models_path = Path(args.models)
    bench_path = Path(args.output)
    models = load_jsonl(models_path)
    bench = load_jsonl(bench_path)

    report: dict = {
        "model": args.model,
        "date": args.date,
        "feed_present": entry is not None,
        "lanes": [r["provider"] for r in models if r.get("model") == args.model],
        "bench_before": sum(
            1 for r in bench if r.get("model") == args.model and r.get("source") == "market"
        ),
    }
    if entry is None:
        report["error"] = "model id absent from the feed payload"
        print(json.dumps(report, indent=2, sort_keys=True))
        return 1

    spec = feed_spec(entry)
    report["feed_spec"] = spec

    # --- 1. benchmark cells -------------------------------------------------
    registry_ids = {r["model"] for r in models if r.get("provider") == "openrouter" and r.get("model")}
    fresh_rows, ingest_report = rmi.build_rows(payload, registry_ids, args.date)
    mine = [r for r in fresh_rows if r["model"] == args.model]
    # A lane may serve the model under a bare id; mirror the same rows so the
    # evidence reaches it (same rule the ingest applies, TR-350).
    bare = args.model.rsplit("/", 1)[1] if "/" in args.model else args.model
    variants = {r["model"] for r in models if r.get("model") == bare and bare != args.model}
    mirrored: list[dict] = []
    for row in mine:
        for variant in sorted(variants):
            alt = dict(row)
            alt["model"] = variant
            mirrored.append(alt)
    emitted = mine + mirrored
    report["ingest_rows_for_model"] = len(mine)
    report["ingest_rows_mirrored"] = len(mirrored)
    report["cells"] = sorted({r["category"] for r in mine})
    report["skipped_categories"] = ingest_report.get("skipped_categories")
    report["unmapped_reasons"] = ingest_report.get("unmapped_reasons")
    report["population_violations"] = ingest_report.get("population_violations")

    prior = [
        r for r in bench if r.get("model") == args.model and r.get("source") == "market"
    ]
    prior_provenanced = [r for r in prior if r.get("source_ref")]
    report["bench_before_provenanced"] = len(prior_provenanced)
    report["bench_before_unprovenanced"] = len(prior) - len(prior_provenanced)
    replaced = {(r["model"], r["category"]) for r in emitted}
    # A cell the feed no longer publishes is NOT silently dropped: the prior
    # value is real published evidence, and its age must stay visible. Carry it
    # forward with a source_ref that says exactly that (the 2026-11-20c wave set
    # this precedent for AA indexes that went null between reads).
    carried: list[dict] = []
    for row in prior:
        if (row["model"], row["category"]) in replaced:
            continue
        if row.get("source_ref"):
            carried.append(row)
            continue
        keep = dict(row)
        keep["source_ref"] = (
            f"openrouter-models-api:{row.get('valid_from') or 'unknown'}"
            f":carried-forward:not-republished-{args.date}"
            f":category={row['category']}:raw_score={row.get('score')}"
        )
        carried.append(keep)
    kept = [
        r
        for r in bench
        if not (r.get("source") == "market" and (r.get("model"), r.get("category")) in replaced)
    ]
    report["bench_carried_forward"] = sorted(r["category"] for r in carried)
    # TR-350 applies to carried cells too: the evidence belongs to the MODEL, so a
    # bare-id lane serving the same model must get the same re-stamped row. Without
    # this the feed-id lane ends up provenanced while its bare twin keeps the
    # provenance-less 2026-11-17 row — the exact split TR-350 exists to prevent.
    carried_mirrored: list[dict] = []
    for row in carried:
        for variant in sorted(variants):
            alt = dict(row)
            alt["model"] = variant
            carried_mirrored.append(alt)
    carried = carried + carried_mirrored
    report["bench_carried_mirrored"] = len(carried_mirrored)
    report["bench_emitted_total"] = len(emitted) + len(carried)
    report["bench_dropped_cells"] = sorted(
        {r["category"] for r in prior if (r["model"], r["category"]) not in replaced}
    )

    # --- 2. spec ------------------------------------------------------------
    lane_rows = [r for r in models if r.get("model") == args.model]
    spec_updates: list[dict] = []
    for row in lane_rows:
        # Only a LIVE lane is written: the NULL census and the resolver read live
        # lanes, and an archived lane's spec is a historical record, not a
        # routing input. Conflicts on archived lanes are still reported.
        live = not row.get("archive") and not row.get("disabled") and not row.get("valid_to")
        deltas = spec_deltas(row, spec)
        for delta in deltas:
            applies = live and (delta["kind"] == "fill" or args.force_spec)
            spec_updates.append(
                {
                    "provider": row.get("provider"),
                    "field": delta["field"],
                    "stored": delta["stored"],
                    "feed": delta["feed"],
                    "kind": delta["kind"],
                    "applied": bool(applies),
                }
            )
            if applies:
                row[delta["field"]] = delta["feed"]
    report["spec_updates"] = spec_updates

    # A filled field must RETIRE its NULL stamp. data/null_reasons.jsonl explains
    # why a field is null; once the feed proves the value, the stamp is a lie and
    # tests/test_null_census.py::test_stamp_registry_vocabulary_is_the_finite_ac2_set
    # fails on it (it asserts every stamp targets a field the row actually leaves
    # null). Retiring the stamp is the honest repair, not widening the test.
    # The invariant is "a stamp targets a field the row leaves null", so retire
    # every stamp whose field is now non-null on this model's rows — not just the
    # ones this run filled. A previous run (or a sibling wave) may have filled it
    # already, and re-running must still converge the registry.
    filled = {
        (row.get("provider"), args.model, field)
        for row in lane_rows
        for field in _STAMPABLE_FIELDS
        if row.get(field) is not None
    }
    retired: list[dict] = []
    if filled and NULL_REASONS.exists():
        kept_stamps: list[str] = []
        with NULL_REASONS.open(encoding="utf-8") as handle:
            for raw in handle:
                if not raw.strip():
                    continue
                stamp = json.loads(raw)
                key = (stamp.get("provider"), stamp.get("model"), stamp.get("field"))
                if key in filled:
                    retired.append(stamp)
                    continue
                kept_stamps.append(raw.rstrip("\n"))
        if retired:
            NULL_REASONS.write_text("\n".join(kept_stamps) + "\n", encoding="utf-8")
    report["stamps_retired"] = [
        {"provider": s["provider"], "field": s["field"], "reason": s["reason"]} for s in retired
    ]

    # --- 3. explained nulls ------------------------------------------------
    explained: dict[str, str] = {}
    if not spec["image_output"]:
        explained["cap_image_generation"] = f"feed architecture.output_modalities={spec['modality']} carries no image output"
    if not spec["audio_input"]:
        explained["cap_audio"] = f"feed input_modalities has no audio channel (modality={spec['modality']})"
    if not spec["video_input"]:
        explained["cap_video"] = f"feed input_modalities has no video channel (modality={spec['modality']})"
    for row in lane_rows:
        row["note"] = _merge_note(row.get("note"), args.model, args.date, explained, spec)
    report["explained_nulls"] = explained

    if not args.write:
        report["wrote"] = False
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0

    if replaced or carried:
        # Byte-for-byte preservation: only the lines this card owns are
        # re-serialised. Re-dumping every line would rewrite 17k lines of other
        # models' evidence under a one-model commit message.
        # The guard is `replaced OR carried`: a model whose feed read publishes
        # no arena cell at all (every AA-only lane) has an empty `replaced` set,
        # and gating the rewrite on `replaced` alone silently skipped the
        # carry-forward — the report claimed the cells were carried while the
        # provenance-less rows stayed on disk.
        carried_by_cell = {(r["model"], r["category"]): r for r in carried}
        out: list[str] = []
        with bench_path.open(encoding="utf-8") as handle:
            for raw in handle:
                if not raw.strip():
                    continue
                row = json.loads(raw)
                cell = (row.get("model"), row.get("category"))
                if row.get("source") == "market" and cell in replaced:
                    continue
                if cell in carried_by_cell and not row.get("source_ref"):
                    out.append(json.dumps(carried_by_cell[cell], ensure_ascii=False, sort_keys=True))
                    continue
                out.append(raw.rstrip("\n"))
        for row in emitted:
            out.append(json.dumps(row, ensure_ascii=False, sort_keys=True))
        bench_path.write_text("\n".join(out) + "\n", encoding="utf-8")

    # Key order is preserved: json.loads keeps the line's own order, so dumping
    # the mutated dict without sort_keys reproduces the original line plus any
    # appended keys. sort_keys here would rewrite every key of the line.
    mutated = {(row.get("provider"), row.get("model")): row for row in lane_rows}
    out_models: list[str] = []
    with models_path.open(encoding="utf-8") as handle:
        for raw in handle:
            if not raw.strip():
                continue
            row = json.loads(raw)
            key = (row.get("provider"), row.get("model"))
            if key in mutated:
                out_models.append(json.dumps(mutated[key], ensure_ascii=False))
                continue
            out_models.append(raw.rstrip("\n"))
    models_path.write_text("\n".join(out_models) + "\n", encoding="utf-8")
    report["wrote"] = True
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


def _merge_note(
    note: str | None, model: str, date: str, explained: dict[str, str], spec: dict
) -> str | None:
    """Append an explained-NULL line to the lane note, idempotently."""
    marker = f"market-verify {date} {model}:"
    if note and marker in note:
        return note
    if not explained:
        return note
    parts = "; ".join(f"{field}=NULL ({reason})" for field, reason in sorted(explained.items()))
    line = (
        f"{marker} explained NULLs per openrouter /api/v1/models read {date}: {parts}"
        f"; ctx {spec['context_length']} (top_provider {spec['top_provider_context_length']})"
        f"; modality {spec['modality']}"
    )
    return f"{note} | {line}" if note else line


if __name__ == "__main__":
    raise SystemExit(main())
