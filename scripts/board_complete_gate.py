#!/usr/bin/env python3
"""board_complete_gate.py — TR-249: the completion gate for the board write path.

Doctrine: docs/traceability-doctrine.md ("Completion gate (TR-249)") — a board
can reach "complete" while the deliverable it names does not exist anywhere.
The 2026-10-01 scan behind that doctrine found 222 complete rows, 8 carrying
any evidence field, 0 carrying an evidence run. This module makes that state
unreachable for NEW completions, without mass-editing history.

Gate contract (board row TR-249)
--------------------------------
1. A transition into a terminal status (`complete`, `done`) is REFUSED when
   `acceptance_criteria` is absent/empty — the refusal NAMES the field.
2. REFUSED when no evidence is attached. Evidence must name a MEASURED NUMBER
   (contains a digit), an ARTIFACT PATH (`/` or a file extension), or an
   EXPLICIT STATED-REASON NULL (`none:<reason>`, or the `evidence_none`
   field, or `witness=none:<reason>` per doctrine hard rule 3). Prose like
   "GREEN" or "done" is not evidence. A non-empty `witness` (external
   locator) is the strongest form and is accepted.
3. The gate applies at THE single transition point that writes
   status=complete: `evaluate()` / `gate_row()` / `append_completion()`
   (CLI: `append-completion`). Writer audit (every in-repo tasks.jsonl
   writer, grepped 2026-10-07):
     - scripts/board_complete_gate.py append-completion — THE gated writer.
     - scripts/router_modelsdev.py _file_gap_rows — appends NEW rows only,
       status `todo` (never terminal) → gate not applicable.
     - scripts/board_stall_check.py — rewrites rows but only to
       REQUEUE_STATUS (`pending`), never terminal → not applicable.
     - scripts/board_task_intake.py — appends filed rows with status
       `pending`; completions are only READ → not applicable.
     - scripts/board-merge-driver.py — resolves conflicts over existing
       lines, synthesizes no status → not applicable.
     - Out-of-repo writers (~/.hermes/scripts/board_append.py, foreman tick
       tooling) are outside this repo's write path; they should route
       completion writes through this module's evaluate()/append_completion().
4. Existing rows are NOT mass-edited. `backlog` is a read-only scan that
   emits a NAMED report of complete-but-unproven rows with per-row status.
5. `check --dry-run` replays a completion transition (use `--prev-status
   pending` for a historical row) and prints the refusal without writing.

Idempotence: re-appending a row whose PREVIOUS state was already terminal is
not a new transition and is not gated (history is append-only; the gate never
re-judges the past).

ch:trace row=TR-249 spec=docs/traceability-doctrine.md#hard-rules test=tests/test_board_complete_gate.py evidence=docs/completeness-backlog-2026-10-07.md

THE SCALE (board row TR-252) — external witness verification
-------------------------------------------------------------
TR-249 proves a row carries PAPERWORK (criteria + evidence text). THE SCALE
proves the deliverable was OBSERVED on a surface we do not write. Box-in-a-box
terminates only where a claim bottoms out in an observation that is not a
report and not ours to write.

1. ENVELOPES: docs/envelopes/<project>.jsonl — versioned, append-only,
   authored OUTSIDE the work loop (header line + item lines
   {id, statement, owner, witness{kind, locator, expect}}). Witness kinds:
   github_tag, github_release, http_get (content digest), path_at_origin,
   live_ingress_resolves, provider_usage_page, ledger_predicate.
2. ANTI-RECURSION: no item may be closed BY REFERENCE. A witness pointing at
   another row id, a verdict, or a report is REFUSED — the refusal names
   `witness`. An envelope item is satisfied by an observation, never a link.
3. OBSERVATIONS: recorded out-of-band by `record` (observer identity,
   locator, observed value, timestamp). The observer is never the row's
   author and never runs inside the tick loop; the gate REFUSES an
   observation whose observer IS the author, and one whose observed_at is
   not later than the row's claim.
4. COVERAGE: `coverage --project P` prints witnessed_items / envelope_items
   with the envelope version; a witness goes STALE when not re-observed
   within the envelope window and stops counting.
5. LIVE RE-OBSERVATION (fail-closed): for github_tag/github_release rows the
   gate re-runs the external probes at check time (`git ls-remote --tags`,
   `gh release list`); a release row may not close while both return empty.
   Unobservable = refused, never assumed.
6. SELF-REFUTATION: a terminal row whose own text states the non-completion
   (the RELEASE-005 "v0.2.0 NO-GO" / RELEASE-006 "release surface still
   absent" shape) is REFUSED.
7. SHRINK SIGN-OFF: an envelope diff that removes or changes an item cannot
   be accepted without an explicit signoff in the new header.

The SCALE checks apply ONLY to rows bound to an envelope item (the item's
`rows` list, or the row's `witness` naming `envelope:<project>#<item>` / an
inline observation dict). Unbound rows keep exact TR-249 behavior.

ch:trace row=TR-252 spec=TR-252 (THE SCALE) test=tests/test_board_complete_gate.py evidence=docs/envelopes/task-router.jsonl
"""
from __future__ import annotations
import argparse
from collections.abc import Iterator
from dataclasses import dataclass, field
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_BOARD = os.path.join(REPO, '.coding-hermes', 'board', 'tasks.jsonl')
DEFAULT_ENVELOPES = os.path.join(REPO, 'docs', 'envelopes')

#: Row statuses the gate treats as "completion" (board_stall_check.TERMINAL).
TERMINAL = ('complete', 'done')

ARTIFACT_EXT = re.compile(
    r'\.(md|json|jsonl|txt|log|csv|tsv|html?|xml|ya?ml|png|jpe?g|gif|svg|pdf'
    r'|py|sh|ts|tsx|js|go|rs|parquet|sqlite|db|ipynb)\b', re.I)


class CompletionGateError(ValueError):
    """A completion transition was refused (the message names the field)."""


# ----------------------------------------------------------------- criteria #

def criteria_state(row: dict) -> tuple[bool, str]:
    """(present, detail) for the acceptance_criteria field."""
    v = row.get('acceptance_criteria')
    if v is None:
        return False, 'missing field: acceptance_criteria'
    items = v if isinstance(v, list) else [v]
    if any(isinstance(i, str) and i.strip() for i in items):
        return True, 'acceptance_criteria present'
    return False, 'missing field: acceptance_criteria (present but empty)'


# ----------------------------------------------------------------- evidence #

def _evidence_item_ok(item: object) -> bool:
    """One evidence string: measured number | artifact path | stated NULL."""
    if not isinstance(item, str):
        return False
    t = item.strip()
    if not t:
        return False
    if t.lower().startswith('none:'):
        return bool(t[5:].strip())          # the reason must be stated
    if re.search(r'\d', t):                 # names a measured number
        return True
    if '/' in t or ARTIFACT_EXT.search(t):  # names an artifact path
        return True
    return False


def evidence_state(row: dict) -> tuple[bool, str]:
    """(present, detail) for the evidence requirement. Accepted fields, in
    order: `evidence` / `evidence_artifact` (str or list), `witness`
    (external locator; `none:<reason>` = stated-reason NULL), and
    `evidence_none` (explicit stated-reason NULL)."""
    for field in ('evidence', 'evidence_artifact'):
        v = row.get(field)
        items = v if isinstance(v, list) else [v]
        for it in items:
            if _evidence_item_ok(it):
                return True, f'{field}={it!r}'
        raw = row.get(field)
        if raw is not None and not any(_evidence_item_ok(i) for i in items):
            shown = raw if isinstance(raw, str) else json.dumps(raw)
            return False, (f'missing field: evidence ({field}={shown.strip()[:80]!r} '
                           'names no measured number, no artifact path, '
                           'and is not a stated-reason NULL)')
    w = row.get('witness')
    if isinstance(w, str) and w.strip():
        t = w.strip()
        if t.lower().startswith('none:'):
            if t[5:].strip():
                return True, f'witness={t!r} (stated-reason NULL)'
        else:
            return True, f'witness={t!r}'
    en = row.get('evidence_none')
    if isinstance(en, str) and en.strip():
        return True, f'evidence_none={en.strip()!r} (stated-reason NULL)'
    return False, 'missing field: evidence (no measured number, no artifact path, no stated-reason NULL)'


# ===================================================================== SCALE #
#  Board row TR-252: THE SCALE — external witness verification.
#
#  TR-249 (above) proves a row carries paperwork. THE SCALE proves the
#  deliverable was observed on a surface we do not write. Verification
#  terminates only where a claim bottoms out in an observation that is NOT a
#  report and NOT OURS TO WRITE.

class ScaleError(CompletionGateError):
    """A SCALE rule refused the completion (message names the rule/field)."""


# ------------------------------------------------------------ envelope model #

@dataclass
class Witness:
    """The external surface that settles an envelope item."""
    kind: str
    locator: str
    expect: str

    KINDS = ('github_tag', 'github_release', 'http_get', 'path_at_origin',
             'live_ingress_resolves', 'provider_usage_page',
             'ledger_predicate')


@dataclass
class EnvelopeItem:
    """One envelope item: a statement of done + its witness."""
    id: str
    statement: str
    owner: str
    witness: Witness
    rows: list = field(default_factory=list)
    window_days: int = 30


@dataclass
class Envelope:
    """A versioned, append-only envelope for one project."""
    project: str
    version: int
    author: str
    created_at: str
    items: list

    def item(self, item_id: str):
        for it in self.items:
            if it.id == item_id:
                return it
        return None


#: Envelope header = line 1; item lines follow, one JSON object each.
def parse_envelope(path: str) -> Envelope:
    with open(path, encoding='utf-8') as fh:
        lines = [ln.strip() for ln in fh if ln.strip()]
    if not lines:
        raise ScaleError(f'envelope {path}: empty (no header line)')
    header = json.loads(lines[0])
    items = []
    for n, ln in enumerate(lines[1:], 2):
        try:
            raw = json.loads(ln)
        except ValueError as e:
            raise ScaleError(f'envelope {path}: line {n} is not JSON: {e}')
        w = raw.get('witness') or {}
        if not isinstance(w, dict) or w.get('kind') not in Witness.KINDS:
            raise ScaleError(
                f'envelope {path}: item {raw.get("id")!r} line {n}: witness.kind '
                f'must be one of {", ".join(Witness.KINDS)}')
        for fld in ('locator', 'expect'):
            if not str(w.get(fld) or '').strip():
                raise ScaleError(
                    f'envelope {path}: item {raw.get("id")!r} line {n}: '
                    f'witness.{fld} is required (the observable and its '
                    'expected value must be stated, not implied)')
        for fld in ('id', 'statement', 'owner'):
            if not str(raw.get(fld) or '').strip():
                raise ScaleError(
                    f'envelope {path}: item line {n}: missing field: {fld}')
        items.append(EnvelopeItem(
            id=str(raw['id']), statement=str(raw['statement']),
            owner=str(raw['owner']), witness=Witness(
                kind=str(w['kind']), locator=str(w['locator']),
                expect=str(w['expect'])),
            rows=list(raw.get('rows') or []),
            window_days=int(raw.get('window_days', 30))))
    return Envelope(project=str(header.get('project') or
                               os.path.splitext(os.path.basename(path))[0]),
                    version=int(header.get('version', 1)),
                    author=str(header.get('author') or ''),
                    created_at=str(header.get('created_at') or ''),
                    items=items)


def envelope_path(project: str, envelopes_dir: str | None = None) -> str:
    d = envelopes_dir or DEFAULT_ENVELOPES
    return os.path.join(d, f'{project}.jsonl')


def load_envelope(project: str, envelopes_dir: str | None = None) -> Envelope:
    return parse_envelope(envelope_path(project, envelopes_dir))


# --------------------------------------------------- anti-recursion validator #

_ROW_ID = re.compile(r'\b[A-Za-z][A-Za-z0-9]*-[A-Za-z0-9][A-Za-z0-9.-]*\b')
_REF_CUES = re.compile(
    r'(?i)\b(row|task|verdict|report|per|see|as documented|as recorded|'
    r'same as|identical to|per the|refer|cf\.?|superseded|supersedes)\b')


def _reference_shaped(text: str) -> bool:
    """Does this text POINT at another artifact instead of naming an
    observation? Row ids (TR-…, RELEASE-…), .gitreins verdict paths,
    docs/reports, or ref cue words — a locator to open, not ground."""
    t = text.strip()
    if not t:
        return True
    if '.gitreins/' in t or 'verdict.json' in t or 'command.json' in t:
        return True
    if re.search(r'(?i)\bdocs?/[a-z0-9._/-]+\.(md|json|html?)\b', t):
        return True
    if _ROW_ID.search(t) and _REF_CUES.search(t):
        return True
    return False


def closure_mode(item: EnvelopeItem, evidence: str | None) -> str:
    """'witness' | 'reference' | 'none' for an item's closure evidence."""
    if not evidence or not evidence.strip():
        return 'none'
    return 'reference' if _reference_shaped(evidence) else 'witness'


def validate_closure(item: EnvelopeItem, evidence: str | None) -> None:
    """THE anti-recursion rule: REFUSE closure by reference. Raises
    ScaleError naming `witness` — the item may be satisfied only by an
    observation against its witness, never a link to another item/row/
    verdict/report."""
    mode = closure_mode(item, evidence)
    if mode == 'reference':
        raise ScaleError(
            f'{item.id}: closure by reference REFUSED (anti-recursion, TR-252): '
            f"the evidence {evidence.strip()[:80]!r} points at another item/"
            'row/verdict/report instead of an observation of the witness '
            f'({item.witness.kind} {item.witness.locator}). NO ITEM MAY BE '
            'CLOSED BY REFERENCE.')
    if mode == 'none':
        raise ScaleError(
            f'{item.id}: REFUSED — no observation recorded for the witness '
            f'({item.witness.kind} {item.witness.locator}); expected '
            f'{item.witness.expect!r}. A closure without an observation is '
            'another box.')


# ------------------------------------------------------------- observations #

def observations_path(project: str, envelopes_dir: str | None = None) -> str:
    d = envelopes_dir or DEFAULT_ENVELOPES
    return os.path.join(d, f'{project}.observations.jsonl')


def load_observations(project: str, envelopes_dir: str | None = None) -> list:
    """Every recorded observation ({item_id, observer, locator, observed,
    observed_at, note}) — appended by an OUT-OF-BAND observer (`record`).
    Never written by a tick loop, never by the row's author."""
    path = observations_path(project, envelopes_dir)
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding='utf-8') as fh:
        for ln in fh:
            ln = ln.strip()
            if not ln:
                continue
            try:
                out.append(json.loads(ln))
            except ValueError:
                continue
    return out


def record_observation(project: str, item_id: str, observer: str,
                       locator: str, observed: str, envelopes_dir: Any=None,
                       observed_at: str | None = None, note: str = '') -> dict:
    """Append ONE observation (flock-serialized, fsync'd). The observer is an
    out-of-band actor: a human, cron, or CI — never the row's author. The
    gate enforces observer != author and observed_at > claim at check time."""
    obs = {'item_id': str(item_id), 'observer': str(observer),
           'locator': str(locator), 'observed': str(observed),
           'observed_at': observed_at or _utcnow(), 'note': str(note)}
    append_jsonl(observations_path(project, envelopes_dir), obs)
    return obs


def _row_author(row: dict) -> str:
    for k in ('author', 'claimed_by', 'worker', 'owner'):
        v = row.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ''


def _ts(s: str) -> datetime | None:
    t = str(s or '').strip().replace('Z', '+00:00')
    try:
        d = datetime.fromisoformat(t)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def check_observation(item: EnvelopeItem, obs: dict, row: dict | None = None,
                      now: datetime | None = None) -> tuple[bool, str]:
    """One recorded observation vs THE SCALE's WHO/WHEN/WHERE:
    (ok, why). WHO: observer recorded and != the row's author. WHEN:
    observed_at strictly AFTER the row's claim timestamp (settlement —
    'unless you wait'). WHERE: locator names the item's witness surface."""
    observer = str(obs.get('observer') or '').strip()
    if not observer:
        return False, 'observation has no observer identity'
    if row is not None:
        author = _row_author(row)
        if author and observer.lower() == author.lower():
            return False, (f'witness is the author itself: observer '
                           f'{observer!r} == row author — an author cannot '
                           'witness its own claim')
    locator = str(obs.get('locator') or '').strip()
    if item.witness.locator not in locator:
        return False, (f'observation locator {locator!r} does not name the '
                       f'witness surface {item.witness.locator!r}')
    at = _ts(obs.get('observed_at'))
    if at is None:
        return False, 'observation has no parseable observed_at timestamp'
    if row is not None:
        claim = None
        for k in ('claimed_at', 'updated_at', 'updated', 'created_at',
                  'created'):
            v = row.get(k)
            if isinstance(v, str) and v.strip():
                claim = _ts(v)
                if claim is not None:
                    break
        if claim is not None and at <= claim:
            return False, (f'observed_at {obs.get("observed_at")} is not '
                           f'later than the row claim {claim.isoformat()} — '
                           'settlement requires observation AFTER the claim')
    return True, f'observed by {observer} at {obs.get("observed_at")}'


# ----------------------------------------------------------- live re-observe #

def _run(cmd: list, timeout: int = 30,
         cwd: str | None = None) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, cwd=cwd)
        return p.returncode, (p.stdout or '') + (p.stderr or '')
    except (OSError, subprocess.TimeoutExpired) as e:
        return 127, str(e)


def _git_available(workdir: str | None) -> bool:
    """True when workdir is a git checkout (a worktree's .git is a FILE —
    existence, not isdir)."""
    return bool(workdir) and os.path.exists(os.path.join(workdir, '.git'))


def _github_tag_probe(locator: str, workdir: str | None = None) -> tuple[bool, str]:
    """locator '<owner>/<repo>#<tag>' → does the tag exist AT origin?
    Fail-closed: unobservable == not witnessed, never assumed."""
    m = re.match(r'^([\w.-]+)/([\w.-]+)#(.+)$', locator.strip())
    if not m:
        return False, f'unparseable github_tag locator: {locator!r}'
    owner, repo, tag = m.groups()
    if _git_available(workdir):
        rc, out = _run(['git', 'ls-remote', '--tags', 'origin',
                        f'refs/tags/{tag}'], timeout=60, cwd=workdir)
        if rc == 0 and out.strip():
            return True, f'tag {tag!r} present at origin'
        if rc != 0:
            return False, f'git ls-remote failed (rc={rc}): {out.strip()[:120]}'
    rc, out = _run(['gh', 'api', f'repos/{owner}/{repo}/git/ref/tags/{tag}'],
                   timeout=60)
    if rc == 0:
        return True, f'tag {tag!r} present on github ({owner}/{repo})'
    return False, f'tag {tag!r} NOT found at origin (0 matching refs)'


def _github_release_probe(locator: str, workdir: str | None = None) -> tuple[bool, str]:
    """locator '<owner>/<repo>' (or '#<tag>') → does a release object exist?
    The TR-252 stop condition: a release row may not close while
    `git ls-remote --tags origin` AND `gh release list` both return empty."""
    loc = locator.strip()
    owner_repo = loc.split('#', 1)[0]
    want_tag = loc.split('#', 1)[1] if '#' in loc else None
    if _git_available(workdir):
        rc, out = _run(['git', 'ls-remote', '--tags', 'origin'],
                       timeout=60, cwd=workdir)
        if rc != 0:
            return False, f'git ls-remote --tags origin failed (rc={rc})'
        if not out.strip():
            return False, ('STOP CONDITION (TR-252): git ls-remote --tags '
                           'origin returns ZERO tags')
    rc, out = _run(['gh', 'release', 'list', '--repo', owner_repo,
                    '--limit', '50'], timeout=60)
    if rc != 0:
        return False, f'gh release list failed (rc={rc}): {out.strip()[:120]}'
    lines = [l for l in out.splitlines() if l.strip()]
    if not lines:
        return False, (f'STOP CONDITION (TR-252): gh release list '
                       f'--repo {owner_repo} returns ZERO releases')
    if want_tag:
        for l in lines:
            if want_tag in l:
                return True, f'release for {want_tag!r} present: {l.strip()[:80]}'
        return False, f'releases exist but none matches {want_tag!r}'
    return True, f'{len(lines)} release(s) present at {owner_repo}'


def _http_probe(locator: str, expect: str) -> tuple[bool, str]:
    """GET the locator; verify the EXPECT digest ('sha256:<hex>') or an
    expected substring. Fail-closed on any fetch error."""
    try:
        req = urllib.request.Request(locator, headers={'User-Agent':
                                                       'board-complete-gate'})
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read()
            status = resp.status
    except (urllib.error.URLError, OSError, ValueError) as e:
        return False, f'http_get {locator} failed: {e}'
    expect_s = expect.strip()
    if expect_s.lower().startswith('sha256:'):
        digest = hashlib.sha256(body).hexdigest()
        if digest == expect_s.split(':', 1)[1].strip().lower():
            return True, f'HTTP {status}, sha256 digest matches'
        return False, (f'HTTP {status}, digest MISMATCH: got '
                       f'sha256:{digest}, expected {expect_s}')
    if expect_s and expect_s in body.decode('utf-8', 'replace'):
        return True, f'HTTP {status}, expected content present'
    return False, f'HTTP {status}, expected content absent'


def _git_workdir_hint() -> str | None:
    """The repo checkout the probes should run in (REPO works for the main
    clone; in a worktree the caller passes workdir= explicitly)."""
    return REPO if os.path.exists(os.path.join(REPO, '.git')) else None


def _path_at_origin_probe(locator: str) -> tuple[bool, str]:
    """Is the path present in the tree AT origin (never the working tree)?
    Uses `git cat-file -e origin/main:<path>` — origin's tree, not ours."""
    rc, out = _run(['git', 'cat-file', '-e', f'origin/main:{locator}'],
                   cwd=_git_workdir_hint())
    if rc == 0:
        return True, f'{locator} present in the tree at origin/main'
    return False, f'{locator} NOT in the tree at origin/main (rc={rc})'


def probe_witness(item: EnvelopeItem, workdir: str | None = None) -> tuple[bool, str]:
    """LIVE re-observation of an item's witness surface (fail-closed).
    github_tag/github_release probe the real external state; http_get fetches
    and digests; path_at_origin reads origin's tree. provider_usage_page and
    ledger_predicate are deliberately NOT probeable from the work loop —
    they require the out-of-band observer (`record`) by design: an agent
    reading its own billing page or writing its own ledger read is the
    author witnessing itself."""
    k = item.witness.kind
    if k == 'github_tag':
        return _github_tag_probe(item.witness.locator, workdir)
    if k == 'github_release':
        return _github_release_probe(item.witness.locator, workdir)
    if k == 'http_get':
        return _http_probe(item.witness.locator, item.witness.expect)
    if k == 'path_at_origin':
        return _path_at_origin_probe(item.witness.locator)
    if k in ('live_ingress_resolves', 'provider_usage_page',
             'ledger_predicate'):
        return False, (f'witness kind {k} requires an OUT-OF-BAND observer '
                       '(record an observation with `record`; the work loop '
                       'cannot observe this surface for itself)')
    return False, f'unknown witness kind: {k}'


# -------------------------------------------------------------- item status #

def item_status(env: Envelope, item: EnvelopeItem, row: dict | None = None,
                observations: list | None = None, now: datetime | None = None,
                live: bool = False, workdir: str | None = None,
                stale_days: int = 30) -> tuple[str, str]:
    """('WITNESSED'|'STALE'|'NOT WITNESSED', why) for one envelope item.

    WITNESSED  — observed after the claim by a non-author AND re-observed
                 within the window (or, with live=True, the external surface
                 re-observed RIGHT NOW confirms the expected state).
    STALE      — was witnessed but not re-observed within window_days; an
                 item that was true and stopped being true stops counting.
    NOT WITNESSED — no qualifying observation (or the live probe fails).
    """
    now = now or datetime.now(timezone.utc)
    obs = [o for o in (observations or [])
           if str(o.get('item_id')) == item.id]
    good = []
    for o in obs:
        ok, why = check_observation(item, o, row)
        if ok:
            good.append((o, why))
    latest = max((_ts(o.get('observed_at')) for o, _ in good), default=None)
    if latest is not None:
        age_days = (now - latest).total_seconds() / 86400.0
        if age_days <= min(item.window_days, stale_days):
            return 'WITNESSED', good[-1][1]
        return 'STALE', (f'last qualifying observation '
                         f'{latest.date().isoformat()} is {age_days:.0f}d old '
                         f'(window {min(item.window_days, stale_days)}d) — '
                         're-observation required')
    if live:
        ok, why = probe_witness(item, workdir)
        return ('WITNESSED', f'live re-observation: {why}') if ok \
            else ('NOT WITNESSED', f'live probe: {why}')
    return ('NOT WITNESSED', 'no qualifying observation recorded '
            '(observer must be out-of-band and after the claim)')


# ------------------------------------------------- the RELEASE-005/006 shape #

#: Rows whose OWN text states the non-completion — filed complete anyway.
#: That is the box-in-a-box mechanism (a complete row proving not-done), so
#: the gate refuses the shape itself, by phrase.
SELF_REFUTATION = re.compile(
    r'(?i)('
    r'release surface (still |remains )?absent'
    r'|still absent'
    r'|remains unshipped'
    r'|\bv?0\.\d+(\.\d+)?\s+(is\s+)?no-?go\b'
    r'|\bno-?go\b[^.]{0,60}\b(unshipped|absent|not (yet )?(shipped|released))\b'
    r'|\brelease (row|surface)[^.]{0,60}\bnot (yet )?(shipped|released|published)\b'
    r')'
    r'(?![^.]{0,40}\bmust|should|will|plan|todo|next|pending\b)')


def self_refutation_state(row: dict) -> tuple[bool, str | None]:
    """(refused, matched_text) — a terminal row whose own text states the
    NON-completion is refused: you may not file non-completion as a
    completed task (the RELEASE-005 'v0.2.0 NO-GO' / RELEASE-006 'release
    surface still absent' shape)."""
    status = str(row.get('status') or '').strip().lower()
    if status not in TERMINAL:
        return False, None
    parts = [row.get('title'), row.get('worker_status')]
    detail = row.get('detail')
    if isinstance(detail, str):
        parts.append(detail)
    ev = row.get('evidence')
    if isinstance(ev, list):
        parts.extend(str(x) for x in ev)
    elif isinstance(ev, str):
        parts.append(ev)
    blob = ' '.join(str(v) for v in parts if v)
    m = SELF_REFUTATION.search(blob)
    if m:
        return True, m.group(0)[:80]
    return False, None


# ------------------------------------------------------- envelope-bound rows #

def _envelope_ref(row: dict) -> tuple[str, str] | None:
    """(project, item_id) if the row is bound to an envelope item:
    witness = 'envelope:<project>#<item>' or a witness dict {'envelope':…,
    'item':…}. Unbound rows → None (TR-249 semantics unchanged)."""
    w = row.get('witness')
    if isinstance(w, str):
        m = re.match(r'^envelope:([\w.-]+)#([\w.-]+)$', w.strip())
        if m:
            return m.group(1), m.group(2)
    if isinstance(w, dict):
        env, it = w.get('envelope'), w.get('item')
        if env and it:
            return str(env), str(it)
    return None


def scale_state_for_row(row: dict, envelopes_dir: str | None = None,
                        now: datetime | None = None,
                        live: bool = False,
                        workdir: str | None = None) -> list:
    """SCALE problems [] for one row. Non-empty = refused. Applies only to
    rows bound to an envelope item; everything else keeps TR-249 behavior."""
    bound = _envelope_ref(row)
    problems: list[str] = []
    # The RELEASE-005/006 shape is refused REGARDLESS of binding: filing
    # non-completion as complete is the mechanism THE SCALE exists to kill.
    refused, matched = self_refutation_state(row)
    if refused:
        problems.append(
            'self-refuting completion REFUSED (TR-252): the row states its '
            f'own non-completion ({matched!r}) — a complete row whose text '
            'says the deliverable is absent is box-in-a-box; close it as '
            'blocked/open instead')
    if bound is None:
        return problems
    project, item_id = bound
    try:
        env = load_envelope(project, envelopes_dir)
    except (OSError, ScaleError) as e:
        return problems + [f'witness envelope unavailable: {e}']
    item = env.item(item_id)
    if item is None:
        return problems + [
            f'witness envelope {project} v{env.version} has no item '
            f'{item_id!r} — check the envelope, do not widen it']
    for rid in item.rows:
        if str(row.get('id')) == str(rid):
            break
    else:
        if item.rows:
            problems.append(
                f'row {row.get("id")} is not in envelope item {item_id}.rows '
                f'({item.rows}) — bind it in the envelope (append-only), '
                'not in the row')
    status, _why = item_status(env, item, row,
                               load_observations(project, envelopes_dir),
                               now=now, live=live, workdir=workdir)
    if status != 'WITNESSED':
        problems.append(
            f'witness NOT satisfied: envelope {project} v{env.version} item '
            f'{item_id} is {status} ({_why}) — an item counts only when '
            'observed by an external witness, after the claim, by a '
            'non-author, and re-observed within the window')
    return problems


# ----------------------------------------------------------------- coverage #

def coverage(env: Envelope, observations: list, now: datetime | None = None,
             stale_days: int = 30) -> dict:
    """{witnessed, items, stale, not_witnessed, detail} — THE NUMBER:
    witnessed_items / envelope_items per project. A witness goes STALE when
    not re-observed within the window and stops counting."""
    now = now or datetime.now(timezone.utc)
    detail = []
    witnessed = stale = 0
    for it in env.items:
        st, why = item_status(env, it, None, observations, now=now,
                              stale_days=stale_days)
        detail.append((it.id, st, why))
        witnessed += st == 'WITNESSED'
        stale += st == 'STALE'
    return {'project': env.project, 'version': env.version,
            'witnessed': witnessed, 'items': len(env.items),
            'stale': stale, 'detail': detail}


# ------------------------------------------------------- envelope shrinkage #

def envelope_shrink_signoff(old: Envelope, new: Envelope) -> None:
    """An envelope diff that REMOVES or CHANGES an item cannot be accepted
    without an explicit signoff in the new header — appending items is free,
    shrinking the scope to claim completion is not."""
    if new.version <= old.version:
        raise ScaleError(
            f'envelope version must INCREASE on change ({old.version} -> '
            f'{new.version}); the envelope is append-only')
    old_ids = {it.id for it in old.items}
    new_ids = {it.id for it in new.items}
    removed = old_ids - new_ids
    changed = [it.id for it in old.items if it.id in new_ids and
               (it.statement, it.owner, it.witness.kind, it.witness.locator,
                it.witness.expect) !=
               ((n := new.item(it.id)).statement, n.owner, n.witness.kind,
                n.witness.locator, n.witness.expect)]
    if (removed or changed) and not str(new.author or '').strip():
        raise ScaleError(
            'envelope SHRINK REQUIRES SIGN-OFF (TR-252): items removed '
            f'{sorted(removed)}, items changed {sorted(changed)} — the new '
            'header must record an explicit signoff author')
    for it in old.items:
        n = new.item(it.id)
        if n and (it.statement, it.owner, it.witness.kind,
                  it.witness.locator, it.witness.expect) != \
                (n.statement, n.owner, n.witness.kind, n.witness.locator,
                 n.witness.expect) and \
                not str(new.author or '').strip():
            raise ScaleError(  # pragma: no cover - unreachable (changed list)
                'changed item without sign-off')


# --------------------------------------------------------------------- gate #

def evaluate(row: dict, prev_status: str | None = None,
             envelopes_dir: str | None = None, live: bool = False,
             workdir: str | None = None) -> list[str]:
    """Problems [] for a row write; non-empty = the transition is refused.

    Only a transition INTO a terminal status is gated. `prev_status` is the
    row's previous terminal-ness (None = unknown/new → treated as a
    transition); an already-terminal previous state is an idempotent
    re-append and passes un-gated. `envelopes_dir` overrides the SCALE
    envelope location (default: docs/envelopes/ in-repo). `live=True`
    re-observes externally-probeable witnesses RIGHT NOW (fail-closed)."""
    status = str(row.get('status') or '').strip().lower()
    if status not in TERMINAL:
        return []
    if prev_status is not None and str(prev_status).strip().lower() in TERMINAL:
        return []
    problems = []
    ok_ac, why_ac = criteria_state(row)
    if not ok_ac:
        problems.append(why_ac)
    ok_ev, why_ev = evidence_state(row)
    if not ok_ev:
        problems.append(why_ev)
    # TR-252 THE SCALE: envelope-bound rows must clear the witness checks;
    # the self-refuting shape is refused for any terminal row.
    problems.extend(scale_state_for_row(row, envelopes_dir=envelopes_dir,
                                        live=live, workdir=workdir))
    return problems


def refusal_message(row: dict, problems: list[str]) -> str:
    tid = row.get('id') or '<no-id>'
    return (f"{tid}: completion REFUSED by board_complete_gate (TR-249): "
            + '; '.join(problems))


def gate_row(row: dict, prev_status: str | None = None) -> dict:
    """Raise CompletionGateError if the completion transition is refused."""
    problems = evaluate(row, prev_status)
    if problems:
        raise CompletionGateError(refusal_message(row, problems))
    return row


# ------------------------------------------------------------ board helpers #

def iter_rows(path: str) -> Iterator[tuple[int, dict]]:
    """(line_no, row) for every parseable JSON line (tolerant, last wins)."""
    with open(path, encoding='utf-8') as fh:
        for n, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                yield n, row


def latest_by_id(path: str) -> dict[str, dict]:
    """id -> row (LAST occurrence wins — the board is append-only JSONL)."""
    latest = {}
    for _, row in iter_rows(path):
        tid = row.get('id')
        if tid:
            latest[str(tid)] = row
    return latest


def append_jsonl(path: str, row: dict) -> None:
    """Append ONE row, flock-serialized, fsync'd — same discipline as
    board_task_intake.append_jsonl (a crash must not tear a line)."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    line = json.dumps(row, sort_keys=True) + '\n'
    with open(path, 'a', encoding='utf-8') as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


# --------------------------------------------------------------- transition #

def append_completion(board_path: str, task_id: str, sets: dict | None,
                      dry_run: bool = False,
                      prev_status: str | None = None,
                      envelopes_dir: str | None = None) -> dict:
    """The ONE gated write of a completion transition. Returns a verdict dict;
    refuses (CompletionGateError) before any byte is written."""
    latest = latest_by_id(board_path)
    prev = latest.get(str(task_id))
    if prev is None:
        raise CompletionGateError(
            f'{task_id}: REFUSED — no row with this id on {board_path}')
    row = dict(prev)
    for k, v in (sets or {}).items():
        row[k] = v
    row['updated_at'] = _utcnow()
    effective_prev = (prev_status if prev_status is not None
                      else prev.get('status'))
    problems = evaluate(row, effective_prev, envelopes_dir=envelopes_dir)
    if problems:
        # Fail LOUD, before any byte is written — library callers get the
        # exception; the CLI catches it, prints the refusal, exits 2.
        raise CompletionGateError(refusal_message(row, problems))
    row['completion_gate'] = {'gate': 'board_complete_gate',
                              'row': 'TR-249+TR-252',
                              'at': _utcnow(), 'verdict': 'pass'}
    verdict = {'task_id': str(task_id), 'dry_run': bool(dry_run),
               'verdict': 'APPEND', 'problems': [], 'written': False,
               'row': row}
    if dry_run:
        return verdict
    append_jsonl(board_path, row)
    verdict['written'] = True
    return verdict


# ------------------------------------------------------------------ backlog #

def classify_row(row: dict) -> tuple[str, str]:
    """Backlog class for one terminal row (+ per-field detail)."""
    ok_ac, _ = criteria_state(row)
    ok_ev, why_ev = evidence_state(row)
    scale = scale_state_for_row(row)
    if scale:
        why = '; '.join(scale)[:160]
        missing = 'witness(scale)'
        if not ok_ac:
            missing = 'acceptance_criteria+' + missing
        if not ok_ev:
            missing = 'evidence+' + missing
        return missing, why
    if ok_ac and ok_ev:
        return 'proven', why_ev
    missing = []
    if not ok_ac:
        missing.append('acceptance_criteria')
    if not ok_ev:
        missing.append('evidence')
    return '+'.join(missing), why_ev


def build_backlog_report(board_path: str, date_stamp: str,
                         envelopes_dir: str | None = None
                         ) -> tuple[str, dict]:
    """(markdown, stats) — the NAMED backlog of complete-but-unproven rows.
    Read-only: no board byte is touched. TR-252 adds the witness axis: a
    terminal row bound to an envelope item whose witness is not WITNESSED
    counts as unwitnessed — complete-but-unproven, visible without reading
    every row."""
    latest = latest_by_id(board_path)
    terminal, unproven, proven_ids = [], [], []
    unwitnessed = 0
    for tid in sorted(latest):
        row = latest[tid]
        status = str(row.get('status') or '').strip().lower()
        if status not in TERMINAL:
            continue
        terminal.append(tid)
        cls, detail = classify_row(row)
        if 'witness(scale)' in cls:
            unwitnessed += 1
        if cls == 'proven':
            proven_ids.append(tid)
        else:
            unproven.append((tid, row, cls, detail))
    stats = {'terminal': len(terminal), 'proven': len(proven_ids),
             'unproven': len(unproven), 'unwitnessed': unwitnessed}
    by_cls = {}
    for _, _, cls, _ in unproven:
        by_cls[cls] = by_cls.get(cls, 0) + 1
    lines = [
        f'# Completeness backlog — complete-but-unproven rows ({date_stamp})',
        '',
        f'Generated by `scripts/board_complete_gate.py backlog` (gates '
        f'TR-249 + TR-252, doctrine docs/traceability-doctrine.md). '
        f'Read-only scan of '
        f'`.coding-hermes/board/tasks.jsonl` (last row per id wins); '
        f'**no row was edited** — this file is the named report the gate '
        f'requires before any remediation is scheduled.',
        '',
        '## Method',
        '',
        '- A row is *complete-but-unproven* when its latest status is '
        '`complete`/`done` AND the gate would refuse the transition today:',
        '  no non-empty `acceptance_criteria`, or no evidence naming a '
        'measured number, an artifact path, or an explicit stated-reason '
        'NULL (`evidence`/`evidence_artifact`/`witness`/`evidence_none`), '
        'OR a TR-252 witness failure (envelope-bound item not WITNESSED, '
        'closure by reference, or a self-refuting non-completion row).',
        '- Historical rows are reported, never rewritten (TR-249 req 4).',
        '',
        '## Totals',
        '',
        f'- terminal rows: **{stats["terminal"]}**',
        f'- proven (would pass the gate): **{stats["proven"]}**',
        f'- complete-but-unproven: **{stats["unproven"]}** '
        f'({", ".join(f"{k}: {v}" for k, v in sorted(by_cls.items())) or "none"})',
        f'- complete-but-unwitnessed (TR-252 witness failures): '
        f'**{stats["unwitnessed"]}**',
        '',
        '## Unproven rows (per-row status)',
        '',
        '| id | status | worker_status | missing | evidence detail |',
        '|---|---|---|---|---|',
    ]
    for tid, row, cls, detail in unproven:
        title = str(row.get('title') or '').replace('|', '\\|')[:60]
        detail_md = str(detail).replace('|', '\\|')[:100]
        status = row.get('status')
        worker_status = row.get('worker_status')
        lines.append(
            f'| {tid} | {status} | {worker_status} | {cls} | {detail_md} |')
    lines += [
        '',
        '## Proven rows',
        '',
        f'{stats["proven"]} rows carry criteria + acceptable evidence: '
        + (', '.join(proven_ids) if proven_ids else '(none)') + '',
        '',
    ]
    return '\n'.join(lines), stats


# ---------------------------------------------------------------------- CLI #

def _parse_set(pairs: list[str] | None) -> dict:
    sets = {}
    for pair in pairs or []:
        if '=' not in pair:
            raise SystemExit(f'--set expects key=value, got {pair!r}')
        k, v = pair.split('=', 1)
        try:
            sets[k] = json.loads(v)      # lists/numbers/null pass through
        except ValueError:
            sets[k] = v
    return sets


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description='TR-249 completion gate for .coding-hermes/board/tasks.jsonl')
    sub = ap.add_subparsers(dest='cmd', required=True)

    p_chk = sub.add_parser(
        'check', help='evaluate one row as a completion transition (exit 2 = refused)')
    p_chk.add_argument('--board', default=DEFAULT_BOARD)
    p_chk.add_argument('--task', help='board row id (latest row wins)')
    p_chk.add_argument('--row-file', help='evaluate a single JSON row file instead')
    p_chk.add_argument('--envelopes', default=None,
                       help='envelopes dir (default docs/envelopes/)')
    p_chk.add_argument('--live', action='store_true',
                       help='re-observe externally-probeable witnesses NOW')
    p_chk.add_argument('--prev-status', default=None,
                       help="previous status (e.g. pending) — replay a historical "
                            "row's transition; omit for a new/unknown prior state")
    p_chk.add_argument('--dry-run', action='store_true',
                       help='check never writes; the flag just stamps the output')

    p_app = sub.add_parser(
        'append-completion',
        help='THE gated write: merge --set fields into a row and append it')
    p_app.add_argument('--board', default=DEFAULT_BOARD)
    p_app.add_argument('--task', required=True)
    p_app.add_argument('--set', action='append', default=[],
                       help='key=value (value may be JSON); repeatable')
    p_app.add_argument('--prev-status', default=None)
    p_app.add_argument('--envelopes', default=None)
    p_app.add_argument('--dry-run', action='store_true',
                       help='print the verdict, write nothing')

    p_blg = sub.add_parser(
        'backlog', help='read-only scan: complete-but-unproven rows report')
    p_blg.add_argument('--board', default=DEFAULT_BOARD)
    p_blg.add_argument('--envelopes', default=None,
                       help='envelopes dir (default docs/envelopes/)')
    p_blg.add_argument('--out', default=None,
                       help='report path (default docs/completeness-backlog-<date>.md)')
    p_blg.add_argument('--dry-run', action='store_true',
                       help='print totals only, write no report file')

    # ------------------------------------------------- TR-252: THE SCALE -- #
    p_env = sub.add_parser(
        'envelope', help='validate + display an envelope (THE SCALE)')
    p_env.add_argument('project', help='project name (docs/envelopes/<p>.jsonl)')
    p_env.add_argument('--envelopes', default=None)
    p_env.add_argument('--shrink-from', default=None,
                       help='previous envelope version file; enforces the '
                            'shrink sign-off rule')

    p_cov = sub.add_parser(
        'coverage', help='witnessed_items / envelope_items per project')
    p_cov.add_argument('project', help='project name')
    p_cov.add_argument('--envelopes', default=None)
    p_cov.add_argument('--stale-days', type=int, default=30)
    p_cov.add_argument('--live', action='store_true',
                       help='re-observe externally-probeable witnesses NOW '
                            '(github tags/releases, http, path_at_origin)')

    p_rec = sub.add_parser(
        'record', help='record an out-of-band OBSERVATION (never the '
                       'tick loop, never the row author)')
    p_rec.add_argument('project', help='project name')
    p_rec.add_argument('--item', required=True, help='envelope item id')
    p_rec.add_argument('--observer', required=True,
                       help='who observed (identity recorded verbatim)')
    p_rec.add_argument('--locator', required=True,
                       help='the witness locator observed')
    p_rec.add_argument('--observed', required=True,
                       help='the observed value (not a claim)')
    p_rec.add_argument('--at', dest='observed_at', default=None,
                       help='observation time (default: now, UTC)')
    p_rec.add_argument('--note', default='')
    p_rec.add_argument('--envelopes', default=None)

    args = ap.parse_args(argv)

    if args.cmd == 'check':
        if args.row_file:
            with open(args.row_file, encoding='utf-8') as fh:
                row = json.load(fh)
        else:
            if not args.task:
                ap.error('check needs --task or --row-file')
            row = latest_by_id(args.board).get(str(args.task))
            if row is None:
                print(f'{args.task}: no row on {args.board}')
                return 1
        problems = evaluate(row, args.prev_status,
                            envelopes_dir=args.envelopes, live=args.live,
                            workdir=REPO)
        tag = 'DRY-RUN ' if args.dry_run else ''
        if problems:
            print(tag + refusal_message(row, problems))
            return 2
        print(tag + f"{row.get('id')}: completion would be ACCEPTED by "
                    f"board_complete_gate (TR-249)")
        return 0

    if args.cmd == 'append-completion':
        try:
            verdict = append_completion(args.board, args.task,
                                        _parse_set(args.set),
                                        dry_run=args.dry_run,
                                        prev_status=args.prev_status,
                                        envelopes_dir=args.envelopes)
        except CompletionGateError as e:
            print(str(e))
            return 2
        what = 'WOULD APPEND (dry-run)' if args.dry_run else 'APPENDED'
        print(f"{args.task}: {what} — completion accepted by the TR-249 gate")
        return 0

    if args.cmd == 'backlog':
        date_stamp = _utcnow()[:10]
        report, stats = build_backlog_report(
            args.board, date_stamp, envelopes_dir=args.envelopes)
        if args.dry_run:
            print(f"DRY-RUN backlog: terminal={stats['terminal']} "
                  f"proven={stats['proven']} unproven={stats['unproven']} "
                  f"unwitnessed={stats['unwitnessed']} "
                  '(no report written)')
            return 0
        out = args.out or os.path.join(
            REPO, 'docs', f'completeness-backlog-{date_stamp}.md')
        with open(out, 'w', encoding='utf-8') as fh:
            fh.write(report)
        print(f"backlog written: {out} — terminal={stats['terminal']} "
              f"proven={stats['proven']} unproven={stats['unproven']} "
              f"unwitnessed={stats['unwitnessed']}")
        return 0

    if args.cmd == 'envelope':
        env = load_envelope(args.project, args.envelopes)
        print(f"envelope {env.project} v{env.version} — author="
              f"{env.author or '?'} created={env.created_at or '?'} "
              f"items={len(env.items)}")
        for it in env.items:
            rows = f" rows={it.rows}" if it.rows else ''
            print(f"  {it.id}: {it.statement[:70]}")
            print(f"    witness: {it.witness.kind} {it.witness.locator} "
                  f"expect={it.witness.expect[:50]!r} "
                  f"window={it.window_days}d{rows}")
        if args.shrink_from:
            old = parse_envelope(args.shrink_from)
            envelope_shrink_signoff(old, env)
            print(f"shrink check vs {os.path.basename(args.shrink_from)}: "
                  "OK (removals/changes signed off, version increased)")
        return 0

    if args.cmd == 'coverage':
        env = load_envelope(args.project, args.envelopes)
        cov = coverage(env, load_observations(args.project, args.envelopes),
                       stale_days=args.stale_days)
        print(f"coverage {cov['project']} v{cov['version']}: "
              f"witnessed {cov['witnessed']}/{cov['items']} "
              f"(stale {cov['stale']})")
        for item_id, st, why in cov['detail']:
            print(f"  {item_id}: {st} — {why[:110]}")
        return 0

    if args.cmd == 'record':
        obs = record_observation(args.project, args.item, args.observer,
                                 args.locator, args.observed,
                                 envelopes_dir=args.envelopes,
                                 observed_at=args.observed_at, note=args.note)
        print(f"observation recorded: {args.project}/{args.item} by "
              f"{obs['observer']} at {obs['observed_at']}")
        print("note: observations are out-of-band; the gate still checks "
              "observer != author and observed_at > claim at close time")
        return 0

    return 1


if __name__ == '__main__':
    sys.exit(main())
