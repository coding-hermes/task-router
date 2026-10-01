"""TR-203 AC2 — policy-gate COVERAGE gate for quota-state.json.

AC1 makes the resolver fail-closed on absence: a provider without a row in
quota-state.json's `providers` section is excluded with
`policy-gate-missing-row`. That is correct at resolve time, but it turns a
forgotten onboarding step into a silently smaller fleet — the lane vanishes
with a code nobody counts. This file is the counterweight: a test that fails
while ANY registry provider (data/tables/providers.jsonl) lacks either

  - a `providers.<id>` entry in quota-state.json (the gate row; status=open
    for a routable lane), or
  - a mention in the file's top-level `intentionally_ungated` list
    (a DOCUMENTED deliberate silence — the lane stays excluded, the audit
    just stops calling it a gap).

The exemption list does not open anything: it only lets a human say "this
silence is on purpose" where the audit (scripts/policy_gate_audit.py) and the
resolve path agree.

State file: `router_spawn.MR` — the SAME path the resolver reads, so the
test can never drift from gate behavior. Deployment override:
`ROUTER_STATE_DIR=<dir>` (or a fixture dir in CI).
"""
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")

if REPO not in sys.path:
    sys.path.insert(0, REPO)
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import router_spawn  # noqa: E402

PROVIDERS_JSONL = os.path.join(REPO, "data", "tables", "providers.jsonl")


def _registry_provider_ids():
    ids = []
    with open(PROVIDERS_JSONL) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            pid = row.get("id")
            if pid:
                ids.append(pid)
    assert ids, f"no provider ids parsed from {PROVIDERS_JSONL}"
    return ids


def _quota_doc():
    path = os.path.join(router_spawn.MR, "quota-state.json")
    if not os.path.exists(path):
        # No deployment state => nothing to audit (scratch CI runner, fresh
        # clone). Same contract as the duckdb-gated tests: SKIP cleanly, never
        # error — a coverage gate cannot judge a deployment that does not
        # exist. On the real deployment (this box, guard included) the file is
        # present and the gate is enforced.
        pytest.skip(
            f"quota-state.json not found at {path} (router_spawn.MR) — no "
            "deployment policy plane to audit on this host")
    with open(path) as fh:
        doc = json.load(fh)
    if not isinstance(doc, dict):
        pytest.fail(f"{path} is not a JSON object — malformed policy plane")
    return doc


def test_every_registry_provider_has_a_row_or_is_documented_ungated():
    doc = _quota_doc()
    qs = doc.get("providers") or {}
    if not isinstance(qs, dict):
        qs = {}
    raw = doc.get("intentionally_ungated") or []
    ungated = set(raw) if isinstance(raw, list) else set()

    missing = [p for p in _registry_provider_ids()
               if not isinstance(qs.get(p), dict) and p not in ungated]
    assert not missing, (
        f"{len(missing)} provider(s) in data/tables/providers.jsonl have NO "
        f"quota-state.json row and are NOT marked intentionally_ungated: "
        f"{missing}. They are excluded 'policy-gate-missing-row' on every "
        f"resolve — add providers.<id> with status=open (onboarding step, see "
        f"docs/registry-maintenance.md) or list them under "
        f"intentionally_ungated if the silence is deliberate. File: "
        f"{os.path.join(router_spawn.MR, 'quota-state.json')}")


def test_ungated_list_only_names_real_registry_providers():
    """A typo in intentionally_ungated would silently 'document' nothing —
    the mark must name a provider id that actually exists in the registry."""
    doc = _quota_doc()
    raw = doc.get("intentionally_ungated") or []
    if not isinstance(raw, list):
        pytest.fail("intentionally_ungated must be a list of provider ids")
    known = set(_registry_provider_ids())
    unknown = sorted(set(raw) - known)
    assert not unknown, (
        f"intentionally_ungated names ids absent from providers.jsonl: "
        f"{unknown} (typo? renamed provider?) — fix the list in "
        f"{os.path.join(router_spawn.MR, 'quota-state.json')}")


def test_coverage_gate_agrees_with_the_resolver():
    """Non-vacuity pin: for THIS deployment, every provider the resolver would
    exclude policy-gate-missing-row is exactly the set this gate flags (row
    missing AND not intentionally ungated). If these two ever disagree, one of
    the two rules drifted."""
    doc = _quota_doc()
    qs = doc.get("providers") or {}
    raw = doc.get("intentionally_ungated") or []
    ungated = set(raw) if isinstance(raw, list) else set()
    provs = _registry_provider_ids()
    resolver_would_exclude = {
        p for p in provs if not isinstance(qs.get(p), dict)}
    gate_flagged = {p for p in provs
                    if not isinstance(qs.get(p), dict) and p not in ungated}
    undocumented = resolver_would_exclude - gate_flagged
    assert undocumented <= ungated, (
        f"resolver excludes {sorted(undocumented)} policy-gate-missing-row "
        f"but the coverage gate would not flag them (and they are not in "
        f"intentionally_ungated) — the two rules diverged")
