#!/usr/bin/env python3
"""model_data_commit_gate.py — COMMIT-TIME gate: refuse to commit partial model data.

WHY THIS EXISTS
---------------
Bane, twice, verbatim: "a commit hook should reject incomplete model data" and
"We can have this partial bullshit because fuck this up so many times already".
This repo has been burned repeatedly by a lane, tier or perf row landing with no
evidence behind it: the row looks like data, the resolver ranks on nothing, and
nobody notices until the usability census is already 300+ defects deep.

WHAT ALREADY EXISTED (and why it was not enough)
------------------------------------------------
  * .githooks/pre-push ran TR-362 INV4 (scripts/null_debt_invariants.py
    --inv4-diff) — a genuine, working gate, but it fires at PUSH time, only
    reads data/tables/models.jsonl, and only compares COMMITTED trees.
  * data/state/usability-baseline.json + tests/test_tr369_usability_gate.py —
    a real ratchet, but pytest/CI-only: it never blocks a local commit, and
    with 10 lanes of headroom under the baseline it lets a handful of new
    evidence-free lanes through untouched.
  * .git/hooks/pre-commit — the file exists, but this repo sets
    `core.hooksPath=.githooks` (scripts/install-hooks.sh), which makes
    .git/hooks/ completely inert. `git rev-parse --git-path hooks/pre-commit`
    answers `.githooks/pre-commit`, and that file did not exist.

So: no local commit was ever checked, and touching model_tier.jsonl or
model_perf.jsonl tripped nothing anywhere.

THE TWO RULES
-------------
(A) CAPABILITY EVIDENCE — for every models.jsonl lane that this commit ADDS, or
    flips from not-live to live, or strips measurement from while staying live:
    the lane must carry *some* real measurement, or a *stated* reason. Evidence
    is any of:
      - a non-null perf_* value on the models.jsonl row itself,
      - a model_tier.jsonl / model_perf.jsonl row for the model with a non-null
        `perf` (a raw measurement exists, whatever the resulting tier),
      - disabled=true WITH a non-empty disabled_reason (stated lane state),
      - a data/tables/model_notes.jsonl row for (provider, model) or
        ('*', model) with a non-empty note (documented exception),
      - EVERY perf_* field stamped for (provider, model) in
        data/null_reasons.jsonl with a TERMINAL reason (`unknown-by-design`,
        `not-published-by-provider`, `retired-by-date`). `no-sample-yet` is a
        deferral, not a disposition, and does NOT satisfy this: it is the exact
        blanket stamp that already covers ~22k unmeasured fields. A category
        that genuinely cannot be measured must say so on the record — it must
        never be given a fabricated number.
    Pre-existing live-but-unmeasured lanes are NOT re-litigated here — that is
    the ratchet's job (rule B), and ~1900 lanes predate any gate. Only this
    commit's deltas are judged.

(B) USABILITY RATCHET — the TR-369 defect count (live + priced + not a single
    category tier) may not rise versus data/state/usability-baseline.json, and
    may not rise versus the parent commit either. The parent ceiling is what
    closes TR-369's own blind spot: with ~10 lanes of headroom under the
    baseline today, adding one evidence-free lane while deleting another's tier
    rows would pass a baseline-only check untouched. The baseline is read from
    BOTH HEAD and the index and the LOWER of the two is enforced: a commit that
    "fixes" a regression by raising the baseline in the same diff is refused.
    The count is produced by scripts/router_usability_gate.classify() itself
    over the exact tables this commit will contain, so this gate's number is
    the gate's number — no second implementation to drift.

(C) EFFORT STAMPS (TR-398/TR-399) — every staged model_perf.jsonl or
    benchmarks.jsonl row must carry an explicit reasoning_effort from the
    closed vocabulary (default, off, low, medium, high, x-high, max — the
    live table plus the provider-alias map is
    scripts/capability_vocab.py). Refused: a missing/empty stamp (a
    measurement without an effort is incomplete — the row cannot be paired
    with the cost of the effort actually billed), an un-normalized provider
    alias ('xhigh' must be stored 'x-high'), and case-variants of a
    canonical spelling. A provider name with NO alias mapping is stored
    VERBATIM and accepted — the gate never guesses an unmapped name down to
    a lower level.

TRIGGERS
--------
The gate reads nothing (and returns in microseconds) unless the staged diff
touches one of:
    data/tables/models.jsonl        data/tables/model_tier.jsonl
    data/tables/model_perf.jsonl    data/tables/null_reasons.jsonl
    data/tables/model_notes.jsonl
Deletions count as touching (dropping tier rows is how the defect count rises).

BYPASS
------
    ALLOW_UNUSABLE_DATA=1 git commit ...
deliberate, disclosed exceptions only; the gate prints a loud warning and
proceeds. No gate in this repo is bypassed silently.

USAGE
-----
    python3 scripts/model_data_commit_gate.py --staged     # pre-commit (default)
    python3 scripts/model_data_commit_gate.py --explain    # full detail, same exit code
    python3 scripts/model_data_commit_gate.py --json       # machine-readable

Exit: 0 clean / 1 blocked / 2 internal error. Python stdlib only, no duckdb.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))

MODELS = "data/tables/models.jsonl"
TIERS = "data/tables/model_tier.jsonl"
PERFS = "data/tables/model_perf.jsonl"
BENCHES = "data/tables/benchmarks.jsonl"
STAMPS = "data/null_reasons.jsonl"  # the census's real stamp file (data_null_census.STAMP_PATH)
STAMPS_DERIVED = "data/tables/null_reasons.jsonl"  # seeded copy; a trigger, not the source
NOTES = "data/tables/model_notes.jsonl"
BASELINE = "data/state/usability-baseline.json"

TRIGGER_PATHS = (MODELS, TIERS, PERFS, BENCHES, STAMPS, STAMPS_DERIVED, NOTES)

BYPASS_ENV = "ALLOW_UNUSABLE_DATA"

#: Fallback AC2 vocabulary. The live table is data_null_census.REASONS; this
#: copy only exists so the gate still runs if that module cannot be imported.
_FALLBACK_REASONS = (
    "disabled",
    "archived",
    "retired-by-date",
    "not-published-by-provider",
    "unpriced-pending-sticker",
    "no-sample-yet",
    "unknown-by-design",
)

#: AC2 reasons that are a TERMINAL answer for a capability category. `no-sample-yet`
#: and `unpriced-pending-sticker` are deliberately absent: they are DEFERRALS ("we
#: will measure it later"), which is exactly the partial state this gate exists to
#: keep out of the tree. A new live lane must be measured, disabled, or declared
#: unmeasurable on the record.
TERMINAL_REASONS = (
    "unknown-by-design",
    "not-published-by-provider",
    "retired-by-date",
)

#: Fallback perf-category set, used only when no row carries a perf_* key.
#: The authoritative set is DERIVED from the rows (see perf_fields) because the
#: registry grows categories: TR-362's hard-coded ten of twenty-four silently
#: treated a perf_code_gen-only lane as unmeasured.
_FALLBACK_PERF = (
    "perf_agent_tick", "perf_long_doc", "perf_debug", "perf_schema",
    "perf_e2e_vision", "perf_review", "perf_delegation", "perf_guard",
    "perf_mock", "perf_reasoning",
)


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def _is_null(v):
    """Absent-key is NOT null; present-key with None/'' is (data_null_census rule)."""
    return v is None or v == ""


def _live(row):
    """Routable lane, matching data_null_census.is_live (TR-197 metric surface)."""
    return not row.get("archive") and not row.get("disabled") and not row.get("valid_to")


def _canon(row):
    return json.dumps(row, sort_keys=True, ensure_ascii=False)


def _key(row):
    return (row.get("provider"), row.get("model"))


def _key_of_stamp(row):
    return (row.get("provider"), row.get("model"), row.get("field"))


def _parse_jsonl(text):
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _git(args, repo, check=False):
    p = subprocess.run(
        ["git"] + args, capture_output=True, text=True, cwd=repo, timeout=120,
    )
    if check and p.returncode != 0:
        raise RuntimeError("git %s failed (rc=%d): %s"
                           % (" ".join(args), p.returncode, (p.stderr or "")[-400:]))
    return p


def _git_blob(repo, rev_path):
    """Content of rev:path, or None when the path is absent there."""
    p = _git(["show", rev_path], repo)
    if p.returncode != 0:
        return None
    return p.stdout


def _rows_at(repo, rev, path):
    """Rows of `path` at `rev` ('' = the index, 'HEAD', or any commit). [] if absent."""
    text = _git_blob(repo, "%s:%s" % (rev, path))
    return _parse_jsonl(text) if text is not None else []


# ---------------------------------------------------------------------------
# vocabularies
# ---------------------------------------------------------------------------
def ac2_reasons():
    try:
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        import data_null_census  # noqa: E402
        return tuple(data_null_census.REASONS)
    except Exception:
        return _FALLBACK_REASONS


def perf_fields(models_rows):
    """The required perf categories: every perf_* key any row carries.

    Derived, not hard-coded: the registry's category set is data, and a stale
    constant would either wave through or wrongly reject whole categories.
    """
    found = set()
    for r in models_rows:
        found.update(k for k in r if k.startswith("perf_"))
    return tuple(sorted(found)) or _FALLBACK_PERF


def load_stamps(rows, reasons):
    """(provider, model, field) -> reason, keeping only AC2-valid reasons."""
    out = {}
    bad = []
    for r in rows:
        reason = r.get("reason")
        if reason not in reasons:
            bad.append((r.get("provider"), r.get("model"), r.get("field"), reason))
            continue
        out[(r.get("provider"), r.get("model"), r.get("field"))] = reason
    return out, bad


def notes_index(rows):
    """(provider, model) and ('*', model) -> note text."""
    out = {}
    for r in rows:
        note = (r.get("note") or "").strip()
        if not note:
            continue
        out.setdefault((r.get("provider"), r.get("model")), note)
    return out


def measurement_index(tier_rows, perf_rows):
    """model -> True when a raw `perf` measurement exists for it, in any category."""
    measured = set()
    for r in list(tier_rows) + list(perf_rows):
        if not _is_null(r.get("perf")) and r.get("model"):
            measured.add(r.get("model"))
    return measured


# ---------------------------------------------------------------------------
# RULE A — capability evidence for this commit's new / newly-live lanes
# ---------------------------------------------------------------------------
def check_capability(models_head, models_base, tier_rows, perf_rows,
                     note_rows, stamp_rows, reasons=None, stamp_rows_base=None):
    """Violations for lanes this diff adds, flips live, or strips evidence from.

    A `changed` lane that was ALREADY live and ALREADY unmeasured is exempt:
    that is pre-existing debt (rule B's ratchet), not this commit's crime.
    Bad stamps are likewise scoped to rows this diff ADDS for a required perf
    field — pre-existing vocabulary debt (and non-perf stamps such as
    plan_entitlement) is not this commit's business.
    """
    reasons = reasons or ac2_reasons()
    fields = perf_fields(list(models_head) + list(models_base))
    stamps, bad_stamps = load_stamps(stamp_rows, reasons)
    notes = notes_index(note_rows)
    measured_models = measurement_index(tier_rows, perf_rows)

    base_stamp_keys = {(_key_of_stamp(r)) for r in (stamp_rows_base or [])}

    base_map = {_key(r): r for r in models_base}
    violations = []

    for row in models_head:
        key = _key(row)
        base_row = base_map.get(key)
        if base_row is not None and _canon(base_row) == _canon(row):
            continue  # untouched lane
        if not _live(row):
            continue  # disabled / archived / retired is already a stated state

        if base_row is None:
            change = "added"
        elif not _live(base_row):
            change = "newly-live"
        else:
            had_evidence = (any(not _is_null(base_row.get(f)) for f in fields)
                            or base_row.get("model") in measured_models)
            if had_evidence:
                change = "evidence-dropped"
            else:
                continue  # already-live, already-unmeasured: pre-existing debt

        # -- evidence gates ------------------------------------------------
        if any(not _is_null(row.get(f)) for f in fields):
            continue
        if row.get("model") in measured_models:
            continue
        if row.get("disabled") and row.get("disabled_reason"):
            continue
        if notes.get(key) or notes.get(("*", row.get("model"))):
            continue
        # A full stamp set only counts when every stamp is TERMINAL. A lane
        # blanket-stamped `no-sample-yet` is precisely the partial data Bane
        # keeps getting burned by: it looks dispositioned and is not.
        unstamped = [f for f in fields
                     if stamps.get((key[0], key[1], f)) not in TERMINAL_REASONS]
        if not unstamped:
            continue

        violations.append({
            "rule": "capability",
            "provider": key[0],
            "model": key[1],
            "change": change,
            "missing_fields": unstamped,
            "why": ("new live lane with no capability measurement and no stated reason"
                    if change == "added" else
                    "lane became live with no capability measurement and no stated reason"
                    if change == "newly-live" else
                    "lane lost every capability measurement while staying live"),
        })

    for prov, mod, field, reason in bad_stamps:
        if field not in fields:
            continue  # not a perf category; e.g. plan_entitlement is its own domain
        if ((prov, mod, field)) in base_stamp_keys:
            continue  # pre-existing vocabulary debt, not introduced here
        violations.append({
            "rule": "capability",
            "provider": prov,
            "model": mod,
            "change": "bad-stamp",
            "missing_fields": [field],
            "why": ("null_reasons.jsonl reason %r is outside the AC2 vocabulary "
                    "(data_null_census.REASONS) for field %r" % (reason, field)),
        })
    return violations


#: Fallback reasoning-effort vocabulary (TR-398). The live table is
#: capability_vocab.REASONING_EFFORTS / PROVIDER_EFFORT_ALIASES; these copies
#: exist only so the gate still runs if that module cannot be imported. The
#: gate refuses a row whose effort is MISSING (TR-399: a measurement without
#: an effort stamp is incomplete) and one carrying an UN-NORMALIZED provider
#' alias ('xhigh' must be stored 'x-high'). An unknown provider name with no
#: alias mapping is stored VERBATIM per the owner's never-downgrade rule —
#: the gate does not guess it down and does not refuse it.
_FALLBACK_EFFORTS = ("default", "off", "low", "medium", "high", "x-high", "max")
_FALLBACK_EFFORT_ALIASES = {
    "minimal": "low", "xhigh": "x-high", "max-thinking": "max",
    "maximum": "max", "none": "off",
}


def _effort_violation_why(value):
    """None when the stored effort stamp is valid; else the refusal why."""
    try:
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        import capability_vocab as _ev  # noqa: E402
        stored, status = _ev.normalize_reasoning_effort(value)
        canonical = tuple(_ev.REASONING_EFFORTS)
        stored_ok = _ev.is_stored_reasoning_effort(value)
    except Exception:
        stored, status = _effort_normalize_fallback(value)
        canonical = _FALLBACK_EFFORTS
        stored_ok = status == 'canonical' or status == 'verbatim'
        if status == 'verbatim' and str(value).strip().lower() in canonical:
            stored_ok = str(value).strip() == str(value).strip().lower()
    if not stored_ok and status == 'canonical':
        return ("reasoning_effort %r is a case-variant of the canonical "
                "spelling %r — store the normalized form" % (value, stored))
    if status == "missing":
        return ("reasoning_effort is MISSING/empty — TR-399: a measurement "
                "without an effort stamp is incomplete; store the effort the "
                "run actually used (vocabulary: %s)"
                % ", ".join(canonical))
    if status == "alias":
        return ("reasoning_effort %r is an un-normalized provider alias — "
                "store %r (the alias table must be applied at ingest, and "
                "must never DOWNGRADE a level)" % (value, stored))
    return None  # canonical or a verbatim provider name — valid


def _effort_normalize_fallback(value):
    if not isinstance(value, str) or not value.strip():
        return "", "missing"
    v = value.strip().lower()
    if v in _FALLBACK_EFFORTS:
        return v, "canonical"
    if v in _FALLBACK_EFFORT_ALIASES:
        return _FALLBACK_EFFORT_ALIASES[v], "alias"
    return value.strip(), "verbatim"


def check_efforts(perf_rows, bench_rows):
    """TR-398/TR-399 rule C: every staged perf/benchmark row carries a valid
    reasoning_effort stamp. Refused: missing/empty, un-normalized provider
    aliases, case-variants of the canonical spellings. Accepted: canonical
    values and verbatim unmapped provider names (never guessed down)."""
    violations = []
    for table, rows in ((PERFS, perf_rows), (BENCHES, bench_rows)):
        for i, r in enumerate(rows):
            why = _effort_violation_why(r.get("reasoning_effort"))
            if why:
                violations.append({
                    "rule": "effort",
                    "table": table,
                    "line": i + 1,
                    "model": r.get("model"),
                    "category": r.get("category"),
                    "why": why,
                })
    return violations


# ---------------------------------------------------------------------------
# RULE B — the usability ratchet
# ---------------------------------------------------------------------------
def _usability_gate_module():
    path = os.path.join(ROOT, "scripts", "router_usability_gate.py")
    spec = importlib.util.spec_from_file_location("_usability_gate_for_commit", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def defect_count(models_rows, tier_rows):
    """The TR-369 defect count for a given table pair, via the gate itself."""
    mod = _usability_gate_module()
    with tempfile.TemporaryDirectory(prefix="commit-gate-") as td:
        for name, rows in (("models.jsonl", models_rows), ("model_tier.jsonl", tier_rows)):
            with open(os.path.join(td, name), "w") as fh:
                for r in rows:
                    fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        mod.DATA = td  # classify() reads the module global at call time
        _usable, _explained, defects, _reasons = mod.classify()
    return len(defects), defects


def check_ratchet(head_defects, base_defects, baseline_head, baseline_base):
    """A ratchet violation dict, or None."""
    ceilings = [("baseline(index)", baseline_head), ("baseline(HEAD)", baseline_base)]
    limits = [(label, v) for label, v in ceilings if isinstance(v, int)]
    if base_defects is not None:
        limits.append(("parent commit", base_defects))
    if not limits:
        return None
    label, limit = min(limits, key=lambda kv: kv[1])
    if head_defects > limit:
        return {
            "rule": "ratchet",
            "head_defects": head_defects,
            "base_defects": base_defects,
            "baseline": baseline_head,
            "limit": limit,
            "limit_source": label,
            "why": "usability defects would rise to %d (ceiling %d from %s)"
                   % (head_defects, limit, label),
        }
    return None


# ---------------------------------------------------------------------------
# git plumbing
# ---------------------------------------------------------------------------
def staged_paths(repo):
    """Paths the index changes vs HEAD (deletions included). Empty on no-HEAD."""
    if _git(["rev-parse", "--verify", "HEAD"], repo).returncode != 0:
        return []
    p = _git(["diff", "--cached", "--name-only", "HEAD"], repo)
    if p.returncode != 0:
        return []
    return [ln.strip() for ln in p.stdout.splitlines() if ln.strip()]


def _baseline_value(repo, rev):
    text = _git_blob(repo, "%s:%s" % (rev, BASELINE))
    if text is None:
        return None
    try:
        v = json.loads(text).get("defects")
        return v if isinstance(v, int) else None
    except Exception:
        return None


def evaluate(repo):
    """Run all rules over the index. Returns a report dict."""
    paths = (MODELS, TIERS, PERFS, BENCHES, STAMPS, NOTES)
    head = {p: _rows_at(repo, "", p) for p in paths}
    base = {p: _rows_at(repo, "HEAD", p) for p in paths}

    violations = check_capability(
        head[MODELS], base[MODELS], head[TIERS], head[PERFS],
        head[NOTES], head[STAMPS], stamp_rows_base=base[STAMPS],
    )
    # TR-398/TR-399 rule C: the effort axis is validated on the staged rows.
    violations += check_efforts(head[PERFS], head[BENCHES])

    # TR-369's count, raw and unfiltered. Do NOT "excuse" stamped lanes here:
    # data/null_reasons.jsonl already carries a no-sample-yet stamp for every
    # perf field of every unmeasured lane (~22k rows), so any excuse-based
    # filter silently reduces the defect count to zero and disarms this rule.
    head_defects, detail = defect_count(head[MODELS], head[TIERS])
    base_defects, _ = defect_count(base[MODELS], base[TIERS])

    ratchet = check_ratchet(
        head_defects, base_defects,
        _baseline_value(repo, ""), _baseline_value(repo, "HEAD"),
    )
    if ratchet:
        violations.append(ratchet)

    return {
        "ok": not violations,
        "violations": violations,
        "head_defects": head_defects,
        "base_defects": base_defects,
        "new_defect_sample": [{"provider": d.get("provider"), "model": d.get("model")}
                              for d in detail[:255]],
    }


def _fmt_lane(v):
    provider = v.get("provider")
    model = v.get("model")
    return "%s/%s" % (provider, model)


def render(report):
    lines = []
    if report["ok"]:
        return None
    by_rule = {}
    for v in report["violations"]:
        by_rule.setdefault(v["rule"], []).append(v)

    lines.append("")
    lines.append("=" * 72)
    lines.append("COMMIT BLOCKED — incomplete model data")
    lines.append("=" * 72)

    cap = by_rule.get("capability") or []
    if cap:
        lanes = [v for v in cap if v.get("change") != "bad-stamp"]
        bad = [v for v in cap if v.get("change") == "bad-stamp"]
        if lanes:
            lines.append("  %d lane(s) with no capability measurement and no stated reason:"
                         % len(lanes))
            for v in lanes[:20]:
                lines.append("    • %-46s [%s]" % (_fmt_lane(v), v["change"]))
                lines.append("        %s" % v["why"])
            if len(lanes) > 20:
                lines.append("    … %d more" % (len(lanes) - 20))
        for v in bad[:10]:
            lines.append("    • %-46s [bad-stamp] %s" % (_fmt_lane(v), v["why"]))

    for v in (by_rule.get("effort") or [])[:10]:
        lines.append("  effort stamp (TR-398/TR-399): %s line %s (%s) — %s"
                     % (v.get("table"), v.get("line"), v.get("model"), v.get("why")))
    n_effort = len(by_rule.get("effort") or [])
    if n_effort > 10:
        lines.append("    … %d more" % (n_effort - 10))
    if n_effort:
        lines.append("    Every row must carry an explicit reasoning_effort from the")
        lines.append("    closed vocabulary (capability_vocab.REASONING_EFFORTS);")
        lines.append("    unmapped provider names are stored verbatim — never guessed down.")
    for v in by_rule.get("ratchet") or []:
        lines.append("  usability ratchet: %s" % v["why"])
        if v.get("base_defects") is not None:
            lines.append("    defects now %d, parent commit %d, baseline %s"
                         % (v["head_defects"], v["base_defects"], v.get("baseline")))
        lines.append("    a live+priced lane with no category tier is unusable to the"
                     " resolver, note or no note.")
        lines.append("    Clear it with a model_tier.jsonl row, or take the lane out of"
                     " service (disabled + disabled_reason).")
        lines.append("    Removing tier rows is how the count rises: evidence is not"
                     " tradeable against new lanes.")

    lines.append("")
    lines.append("  See the detail:   python3 scripts/model_data_commit_gate.py --explain")
    lines.append("  Fix, in order of preference:")
    lines.append("    1. measure the lane            (data/tables/model_tier.jsonl or model_perf.jsonl)")
    lines.append("    2. stamp every perf_* field    (data/tables/null_reasons.jsonl, reason from the")
    lines.append("                                    AC2 vocabulary — `unknown-by-design` is the honest")
    lines.append("                                    answer for a category that cannot be measured)")
    lines.append("    3. document a lane exception   (data/tables/model_notes.jsonl)")
    lines.append("    4. take the lane out of service (disabled=true + disabled_reason)")
    lines.append("")
    lines.append("  Deliberate, disclosed exception:")
    lines.append("    ALLOW_UNUSABLE_DATA=1 git commit ...")
    lines.append("  No gate in this repo is bypassed silently.")
    lines.append("")
    return "\n".join(line for line in lines if line is not None)


def main(argv=None):
    ap = argparse.ArgumentParser(description="commit-time gate for partial model data")
    ap.add_argument("--staged", action="store_true",
                    help="check the index against HEAD (default; what pre-commit does)")
    ap.add_argument("--scan-tree", action="store_true",
                    help="scan the WORKING TREE with no staging requirement — the same "
                         "rules as the commit gate, runnable any time (make scan)")
    ap.add_argument("--explain", action="store_true",
                    help="print full detail for every offending lane")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--repo", default=ROOT, help="repository root (default: this repo)")
    ap.add_argument("--force", action="store_true",
                    help="run even when the staged diff touches no model table (tests)")
    args = ap.parse_args(argv)

    repo = os.path.realpath(args.repo)

    if args.scan_tree:
        # Whole-tree scan: evaluate the COMMITTED tree (same data evaluate() uses)
        # but force-run regardless of what is staged, so `make scan` works any time.
        touched = list(TRIGGER_PATHS)
        try:
            report = evaluate(repo)
        except Exception as exc:
            print("%s: ERROR: scan failed to run: %s" % (os.path.basename(__file__), exc),
                  file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(report, indent=1, default=str))
        elif report["ok"]:
            print("%s: SCAN OK — %d usability defects (baseline %s)"
                  % (os.path.basename(__file__), report["head_defects"],
                     report.get("baseline_defects", "?")))
        else:
            print(render(report), file=sys.stderr if not args.explain else sys.stdout)
            if args.explain:
                print(json.dumps(report, indent=1, default=str))
        return 0 if report["ok"] else 1

    if not args.force:
        touched = [p for p in staged_paths(repo) if p in TRIGGER_PATHS]
        if not touched:
            return 0
    else:
        touched = list(TRIGGER_PATHS)

    if os.environ.get(BYPASS_ENV) == "1":
        print("%s: WARNING: %s=1 — the model-data commit gate is bypassed for this "
              "commit. Staged model tables: %s"
              % (os.path.basename(__file__), BYPASS_ENV, ", ".join(touched)),
              file=sys.stderr)
        return 0

    try:
        report = evaluate(repo)
    except Exception as exc:  # internal error must never look like a clean pass
        print("%s: ERROR: gate failed to run: %s" % (os.path.basename(__file__), exc),
              file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report, indent=1, default=str))
    elif report["ok"]:
        print("%s: OK — %d usability defects, model tables staged (%s)"
              % (os.path.basename(__file__), report["head_defects"], ", ".join(touched)),
              file=sys.stderr)
    else:
        msg = render(report)
        print(msg, file=sys.stderr if not args.explain else sys.stdout)
        if args.explain:
            print(json.dumps(report, indent=1, default=str))

    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
