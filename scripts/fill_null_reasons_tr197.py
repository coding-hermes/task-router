#!/usr/bin/env python3
"""fill_null_reasons_tr197.py — TR-197: every null on a LIVE lane carries a reason.

Two mechanisms, both the sanctioned channels (never hand-edit data/tables/):
  1. FILLS from real sources already in the repo -> thin lifecycle overlay rows
     appended to data/lifecycle.jsonl (the seed-merged, provenance-carrying
     channel used by fix_overlay_wipes_tr198.py). The next reseed merges them
     and the tail-sync rewrites data/tables/models.jsonl.
  2. STAMPS for nulls with no honest source -> data/null_reasons.jsonl
     (a sidecar OUTSIDE tables/ so the tail-sync never rewrites it), keyed
     (provider, model, field) with a reason from the FINITE AC2 vocabulary:
       disabled / archived / retired-by-date / not-published-by-provider /
       unpriced-pending-sticker / no-sample-yet / unknown-by-design.
     scripts/data_null_census.py consumes this file to classify those nulls
     MEANINGFUL.

Fill sources (all in-repo, verified 2026-10-01):
  - context_limit: registry twin consensus (same model on other providers),
    model_catalog.jsonl rows (models.dev sync), and the opencode-go mirror
    (opencode-go-2 is the same reseller surface, 2nd account per
    providers.jsonl).
  - api_type: provider-uniform recorded values (every recorded lane of the
    provider uses one value), the clinepass writer convention
    (router_clinepass.py emits 'openai-chat'), lane-family consensus
    (neuralwatt *-flex = responses), and the opencode-go mirror.
  - public sticker fields on commandcode claude-sonnet-5-5 / gpt-6.1-sol: the
    row's own price_evidence quotes the models.dev sticker numbers.
  - data_class 'unknown': the registry's explicit-unknown token (used by the
    xkiro provider_import when the data-handling class is unverified). Bane
    doctrine: unknown != missing.
  - archive false: lanes that are demonstrably live (no valid_to, not
    disabled) but predate the full-schema writers.

Usage:
  python3 scripts/fill_null_reasons_tr197.py            # dry run (default)
  python3 scripts/fill_null_reasons_tr197.py --apply    # append overlay + sidecar

Idempotent: lanes already targeted in data/lifecycle.jsonl (for fills) or
data/null_reasons.jsonl (for stamps) are skipped, so a re-run writes nothing
new. R4 (no anonymous dates): every overlay row carries lifecycle_source +
lifecycle_checked_at.
"""

import argparse
import collections
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TABLES = os.path.join(REPO, "data", "tables")
LC_PATH = os.path.join(REPO, "data", "lifecycle.jsonl")
STAMP_PATH = os.path.join(REPO, "data", "null_reasons.jsonl")
TODAY = "2026-10-01"


def load(name):
    out = []
    with open(os.path.join(TABLES, name), errors="replace") as f:
        for line in f:
            if line.strip():
                out.append(json.loads(line))
    return out


def is_live(r):
    return not r.get("archive") and not r.get("disabled") and not r.get("valid_to")


MODELS = load("models.jsonl")
BY_LANE = {(r["provider"], r["model"]): r for r in MODELS}
LIVE = {k: r for k, r in BY_LANE.items() if is_live(r)}

# ---------------------------------------------------------------------------
# context_limit fills: (provider, model) -> (value, evidence)
# ---------------------------------------------------------------------------
_ctx_fills = {}


def _ctx_twin(model, value, note):
    _ctx_fills[("clinepass", model)] = (
        value,
        "sibling-lane consensus: %s carries context_limit=%s on the twin "
        "registry lanes (same model, other providers%s)" % (model, value, note),
    )


_ctx_twin("deepseek-v4-flash", 1000000, "; official-surface value, resellers agree")
_ctx_twin("deepseek-v4-pro", 1000000, "; official-surface value, resellers agree")
_ctx_twin("glm-5.3", 1000000, "; official zai-glm + resellers agree")
_ctx_twin("kimi-k2.7-code", 262144, "; 5 resellers agree")
_ctx_twin("kimi-k3", 1048576, "; fireworks/ollama/opencode-go agree")
_ctx_twin("mimo-v2.5", 1000000, "; opencode-go twins")
_ctx_twin("mimo-v2.5-pro", 1048576, "; crof + opencode-go twins")
_ctx_twin("qwen3.7-plus", 1000000, "; opencode-go twins")
_ctx_twin("qwen3.8-max", 1000000, "; opencode-go twins")

for model, val in (
    ("gpt-oss-120b", 131072),
    ("nemotron-3-5-lightning-30b-a3b", 262144),
    ("nemotron-3-ultra", 262144),
    ("qwen3-8-max", 262144),
):
    _ctx_fills[("fireworks-ai", model)] = (
        val,
        "models.dev fireworks-ai section (fetched 2026-10-01): %s ctx=%d"
        % (
            "nemotron-lightning-3p5-30b-a3b"
            if "lightning" in model
            else (
                "nemotron-3-ultra-nvfp4"
                if model == "nemotron-3-ultra"
                else ("qwen3p8-max" if model == "qwen3-8-max" else model)
            ),
            val,
        ),
    )
_ctx_fills[("fireworks-ai", "deepseek-v4-flash-0731")] = (
    1000000,
    "sibling-lane consensus: deepseek-v4-flash (the 0731 snapshot) carries "
    "context_limit=1000000 on deepseek/crof/opencode-go twins",
)
_ctx_fills[("fireworks-ai", "deepseek-v4-pro-0813")] = (
    1000000,
    "sibling-lane consensus: deepseek-v4-pro carries context_limit=1000000 "
    "on deepseek/crof/opencode-go twins",
)
_ctx_fills[("fireworks-ai", "qwen3-7-plus")] = (
    1000000,
    "sibling-lane consensus: qwen3.7-plus (fireworks writes dots as dashes, "
    "same convention as nemotron-3-5 = 3.5) carries context_limit=1000000 "
    "on the opencode-go twins",
)

_ctx_fills[("kimi-for-coding", "kimi-k2.7-code")] = (
    262144,
    "sibling-lane consensus: kimi-k2.7-code carries context_limit=262144 on "
    "crof/ollama-cloud/opencode-go/opencode-go-2 (supersedes the old "
    'model_notes "unknown" row)',
)
for model in ("Qwen/Qwen3.6-35B-A3B", "moonshotai/Kimi-K2.7-Code"):
    _ctx_fills[("neuralwatt", model)] = (
        131056 if "Qwen" in model else 262144,
        "model_catalog.jsonl (provider, model) row — models.dev sync — records "
        "context_window=%d" % (131056 if "Qwen" in model else 262144),
    )

OG2_CTX = {
    "gpt-6-luna": 1050000,
    "grok-4.7": 500000,
    "longcat-2.5-preview-free": 1000000,
    "mimo-v2.6-flash": 1048576,
    "mimo-v2.6-pro": 1048576,
    "muse-spark-1.3-contributor": 1048576,
    "space-bunny-free": 1048576,
}
for model, val in OG2_CTX.items():
    _ctx_fills[("opencode-go-2", model)] = (
        val,
        "opencode-go (the SAME reseller surface, 2nd account per "
        "providers.jsonl) records context_limit=%d for this model" % val,
    )

# ---------------------------------------------------------------------------
# api_type fills: (provider, model) -> (value, evidence)
# ---------------------------------------------------------------------------
_api_fills = {}
UNIFORM = {
    "clinepass": (
        "openai-chat",
        24,
        "router_clinepass.py writes api_type=openai-chat for every "
        "clinepass lane (writer convention; 2 retired rows recorded it)",
    ),
    "openrouter": ("openai-chat", 256, None),
    "xkiro": ("chat_completions", 43, None),
    "xkiro-2": ("chat_completions", 45, None),
    "commandcode": ("responses", 41, None),
    "commandcode-2": ("responses", 41, None),
}
_api_fills[("groq", "qwen/qwen3.8-27b")] = (
    "responses",
    "provider-uniform recorded value: groq records api_type=responses "
    "on 2/2 of its other lanes",
)
_api_fills[("neuralwatt", "glm-5.3-flex")] = (
    "responses",
    "lane-family consensus: all 5 recorded neuralwatt *-flex lanes "
    "(glm-5.2-short/fast-flex family) use api_type=responses",
)
for model in OG2_CTX:
    if model == "muse-spark-1.3-contributor":
        continue  # mirror is None too -> stamp, not fill
    _api_fills[("opencode-go-2", model)] = (
        "openai-chat",
        "opencode-go (the SAME reseller surface, 2nd account per "
        "providers.jsonl) records api_type=openai-chat for this model",
    )


def _uniform_api_fills():
    out = {}
    for r in MODELS:
        if (not is_live(r)) or r.get("api_type") is not None:
            continue
        key = (r["provider"], r["model"])
        if key in _api_fills:
            continue
        u = UNIFORM.get(r["provider"])
        if u:
            val, n, note = u
            ev = (
                "provider-uniform recorded value: %d recorded %s lanes all use "
                "api_type=%s" % (n, r["provider"], val)
            )
            if note:
                ev += " — " + note
            out[key] = (val, ev)
    return out


_api_fills.update(_uniform_api_fills())

# ---------------------------------------------------------------------------
# public sticker fills from the row's OWN evidence (commandcode pass-throughs)
# ---------------------------------------------------------------------------
_sticker_fills = {
    ("commandcode", "claude-sonnet-5-5"): {
        "public_price": 2.0,
        "public_in_per_m": 2.0,
        "public_out_per_m": 10.0,
        "public_cache_read_per_m": 0.2,
        "evidence": "the row price_evidence quotes the models.dev "
        "anthropic/claude-sonnet-5-5 sticker 2.0/10.0 cache 0.2 "
        "(1:1 PAYG pass-through, same no-markup method as the 09-13 "
        "commandcode pricing); normalized_price already 2.0",
    },
    ("commandcode", "gpt-6.1-sol"): {
        "public_price": 2.0,
        "public_in_per_m": 2.0,
        "public_out_per_m": 10.0,
        "public_cache_read_per_m": 0.1,
        "evidence": "the row price_evidence quotes the models.dev openai/gpt-6.1-sol "
        "sticker 2.0/10.0 cache 0.1 (matches openai-codex catalog "
        "2/10); normalized_price already 2.0",
    },
}

# ---------------------------------------------------------------------------
# data_class + archive fills
# ---------------------------------------------------------------------------
_dc_targets = [k for k, r in LIVE.items() if r.get("data_class") is None]
_arch_targets = [k for k, r in LIVE.items() if r.get("archive") is None]

# ---------------------------------------------------------------------------
# stamps: (provider, model, field) -> (reason, evidence)
# ---------------------------------------------------------------------------
STAMPS = collections.OrderedDict()


def _stamp(prov, model, fields, reason, evidence):
    for f in fields:
        STAMPS[(prov, model, f)] = (reason, evidence)


for r in MODELS:
    if not is_live(r):
        continue
    prov, mod = r["provider"], r["model"]
    # context_limit / api_type with no honest source -> no-sample-yet
    # (per-field checks: a lane filling api_type can still need a ctx stamp)
    if r.get("context_limit") is None and (prov, mod) not in _ctx_fills:
        _stamp(
            prov,
            mod,
            ["context_limit"],
            "no-sample-yet",
            "no catalog row, no registry twin with a recorded value, and no "
            "provider doc — research pending (writers never guess)",
        )
    if r.get("api_type") is None and (prov, mod) not in _api_fills:
        _stamp(
            prov,
            mod,
            ["api_type"],
            "no-sample-yet",
            "no catalog row, no registry twin with a recorded value, and no "
            "provider doc — research pending (writers never guess)",
        )
    # price fields
    ev = str(r.get("price_evidence") or "")
    if prov == "openrouter" and mod in (
        "openrouter/auto",
        "openrouter/bodybuilder",
        "openrouter/fusion",
        "openrouter/pareto-code",
    ):
        _stamp(
            prov,
            mod,
            ["normalized_price", "public_price", "public_in_per_m", "public_out_per_m"],
            "unknown-by-design",
            "provider-side meta-route (router picks the backing model at request "
            "time) — BY_DESIGN in router_lifecycle.py; can never carry a "
            "per-model price (TR-076)",
        )
    elif r.get("public_price") is None and "window-cost-pending" in ev:
        _stamp(
            prov,
            mod,
            ["public_price", "public_in_per_m", "public_out_per_m"],
            "unpriced-pending-sticker",
            "TR-070 window-cost-pending story already on the row: zero-price SKU "
            "with no billable window until the vendor or a reseller consensus "
            "publishes a rate",
        )
    if r.get("normalized_price") is None:
        if "no-sticker-match" in ev or "provider_import" in ev:
            _stamp(
                prov,
                mod,
                ["normalized_price"],
                "not-published-by-provider",
                "carrier catalog lists the SKU with no pricing block / preset has "
                "no sticker entry / no models.dev section (TR-198 "
                "upstream-unpriceable class); re-check when the vendor publishes",
            )
        elif "window-cost-pending" in ev:
            _stamp(
                prov,
                mod,
                ["normalized_price"],
                "unpriced-pending-sticker",
                "metered window cost pending a measured sample (TR-070); "
                "normalized stays NULL, never 0",
            )
        elif prov == "kimi-for-coding":
            _stamp(
                prov,
                mod,
                ["normalized_price"],
                "unpriced-pending-sticker",
                "plan-offset subscription lane: router_pricing MANUAL_FORMULA row "
                '("$0 list, plan economics unproven"); stamp when the plan meter '
                "is measured",
            )
        elif prov == "openai-codex" and r.get("public_price") is not None:
            _stamp(
                prov,
                mod,
                ["normalized_price"],
                "unpriced-pending-sticker",
                "no plan_terms row for openai-codex and no billing basis to "
                "normalize against; the models.dev sticker stays on public_* "
                "until the metered window is measured",
            )
    if r.get("public_price") is None and (
        "no-sticker-match" in ev or "provider_import" in ev
    ):
        _stamp(
            prov,
            mod,
            ["public_price", "public_in_per_m", "public_out_per_m"],
            "not-published-by-provider",
            "no sticker exists anywhere to publish (carrier catalog has no "
            "pricing block, preset has no sticker entry, no models.dev section) "
            "— the public trio stays NULL, never invented",
        )

# plan lanes whose sticker the vendor never published (xkiro cohere imports etc.)
for r in MODELS:
    if not is_live(r) or r.get("public_price") is not None:
        continue
    prov, mod = r["provider"], r["model"]
    if (prov, mod) in STAMPS or (prov == "openrouter"):
        continue
    ev = str(r.get("price_evidence") or "")
    if "window-cost-pending" in ev or "clinepass" in prov:
        continue  # already stamped above / pattern-classified
    if "provider_import" in ev:
        _stamp(
            prov,
            mod,
            ["public_price", "public_in_per_m", "public_out_per_m"],
            "not-published-by-provider",
            "plan-covered import (list sticker never published by the upstream "
            "vendor); effective economics already on normalized_price/evidence",
        )


# ---------------------------------------------------------------------------
# assemble overlay rows (one per lane, merged keys)
# ---------------------------------------------------------------------------
def overlay_rows():
    per_lane = collections.defaultdict(dict)
    for (prov, mod), (val, evidence) in _ctx_fills.items():
        r = LIVE.get((prov, mod))
        if not r or r.get("context_limit") is not None:
            continue
        per_lane[(prov, mod)]["context_limit"] = val
        per_lane[(prov, mod)]["_ev"] = "TR-197 null-census fill %s: %s" % (
            TODAY,
            evidence,
        )
    for (prov, mod), (val, evidence) in _api_fills.items():
        r = LIVE.get((prov, mod))
        if not r or r.get("api_type") is not None:
            continue
        per_lane[(prov, mod)]["api_type"] = val
        per_lane[(prov, mod)]["_ev"] = "TR-197 null-census fill %s: %s" % (
            TODAY,
            evidence,
        )
    for (prov, mod), patch in _sticker_fills.items():
        r = LIVE.get((prov, mod))
        if not r:
            continue
        for k, v in patch.items():
            if k == "evidence":
                continue
            if r.get(k) is None:
                per_lane[(prov, mod)][k] = v
        per_lane[(prov, mod)]["_ev"] = "TR-197 null-census fill %s: %s" % (
            TODAY,
            patch["evidence"],
        )
    for key in _dc_targets:
        per_lane[key]["data_class"] = "unknown"
        per_lane[key]["_ev"] = (
            "TR-197 null-census fill %s: explicit unknown stamp (Bane doctrine: "
            "unknown != missing); matches the xkiro provider_import convention "
            'data_class="unknown" for an unverified data-handling class' % TODAY
        )
    for key in _arch_targets:
        per_lane[key]["archive"] = False
        per_lane[key]["_ev"] = (
            "TR-197 null-census fill %s: explicit false — lane is live (no valid_to, "
            "not disabled); NULL archive predates the full-schema writers" % TODAY
        )
    rows = []
    for (prov, mod), patch in sorted(per_lane.items()):
        rows.append(
            {
                "provider": prov,
                "model": mod,
                "lifecycle_source": patch["_ev"],
                "lifecycle_checked_at": TODAY,
                **{k: v for k, v in patch.items() if k != "_ev"},
            }
        )
    return rows


def stamp_rows():
    rows = []
    for (prov, mod, field), (reason, evidence) in STAMPS.items():
        r = LIVE.get((prov, mod))
        if not r or r.get(field) is not None:
            continue
        rows.append(
            {
                "provider": prov,
                "model": mod,
                "field": field,
                "reason": reason,
                "evidence": evidence,
                "ts": TODAY,
            }
        )
    return rows


def existing(path, keyf):
    out = set()
    if os.path.exists(path):
        for line in open(path, errors="replace"):
            if line.strip():
                out.add(keyf(json.loads(line)))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--apply", action="store_true", help="write the files (default dry run)"
    )
    args = ap.parse_args()

    ov = overlay_rows()
    st = stamp_rows()
    # Field-wise dedupe against the lane's LAST lifecycle row (the seed applies
    # overlays in file order, so the last row wins per key). A historical
    # full-schema row carrying context_limit=None does NOT count as done —
    # a null there is exactly what this repair fills.
    last_lc = {}
    if os.path.exists(LC_PATH):
        for line in open(LC_PATH, errors="replace"):
            if line.strip():
                r = json.loads(line)
                last_lc[(r.get("provider"), r.get("model"))] = r
    done_fields = {}
    for key, r in last_lc.items():
        done_fields[key] = {
            k
            for k, v in r.items()
            if k
            not in ("provider", "model", "lifecycle_source", "lifecycle_checked_at")
            and v is not None
        }
    done_st = existing(
        STAMP_PATH, lambda r: (r.get("provider"), r.get("model"), r.get("field"))
    )

    def _undone(row):
        key = (row["provider"], row["model"])
        have = done_fields.get(key, set())
        return any(
            k not in have
            for k in row
            if k
            not in ("provider", "model", "lifecycle_source", "lifecycle_checked_at")
        )

    ov_new = [r for r in ov if _undone(r)]
    st_new = [r for r in st if (r["provider"], r["model"], r["field"]) not in done_st]

    print(
        "overlay rows: %d (%d already in %s)"
        % (len(ov), len(ov) - len(ov_new), LC_PATH)
    )
    print(
        "stamp rows:   %d (%d already in %s)"
        % (len(st), len(st) - len(st_new), STAMP_PATH)
    )
    per_reason = collections.Counter(r["reason"] for r in st)
    print("stamps by reason: %s" % dict(per_reason))
    fieldcount = collections.Counter()
    for r in ov_new:
        for k in r:
            if k not in (
                "provider",
                "model",
                "lifecycle_source",
                "lifecycle_checked_at",
            ):
                fieldcount[k] += 1
    print("overlay fields: %s" % dict(fieldcount))

    if not args.apply:
        print("\nDRY RUN — rerun with --apply to write.")
        return 0
    if ov_new:
        with open(LC_PATH, "a") as f:
            for r in ov_new:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print("appended %d overlay rows -> %s" % (len(ov_new), LC_PATH))
    if st_new:
        with open(STAMP_PATH, "a") as f:
            for r in st_new:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print("wrote %d stamp rows -> %s" % (len(st_new), STAMP_PATH))
    return 0


if __name__ == "__main__":
    sys.exit(main())
