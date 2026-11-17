PROBE ROOT: /home/kara/.hermes/stand-in/docs/task-router/probe-d12
TIP: bd51394a630d9fe475bbe9f7b6b5115b189270cf
DATE: 2026-11-17T04:26:05-05:00

=============== P1: the doctrine's verification claims ===============
$ sed -n '104,113p' docs/traceability-doctrine.md
<!-- ch:trace row=TR-253 spec=docs/traceability-doctrine.md wave=task-router-2026-10-01#trace doc=~/.hermes/skills/coding-hermes-traceability/SKILL.md evidence=reports/tr253-prompt-load.txt witness=gateway:state.db@sessions-20-post-change -->

## Verification

- One recursive grep finds every marker.
- A validator refuses: a marker without `row`; a reference-closure; a memory field as sole citation; a verdict
  without a bound commit; a commit not reachable from the published tip.
- A marker's `witness` must appear in the most recent out-of-band scan. If it does not, the claim is stale
  regardless of what the row says.


--- the doctrine says a row body also carries the marker ('Where markers go') ---
$ sed -n '76,84p' docs/traceability-doctrine.md
- **Change note** — a `## Trace` section in the merge or pull-request description.

## Commit footer

```
ch:trace row=TR-251 spec=docs/traceability-doctrine.md#hard-rules test=tests/test_doc_parity.py::test_x evidence=reports/xray/
```

## One call, four surfaces

=============== P2: what the validator's walker actually opens ===============
$ sed -n '30,56p' scripts/trace_context.py
TEXT_EXT = {'.py', '.go', '.rs', '.ts', '.tsx', '.js', '.jsx', '.md', '.yaml', '.yml', '.toml',
            '.json', '.jsonl', '.sh', '.txt', '.sql', '.c', '.h', '.java', '.rb', '.swift', '.kt'}
REQUIRED = ('row',)
BOUND = ('verdict', 'commit')


def walk(roots: Any, max_bytes: int=2_000_000):
    for root in roots:
        root = os.path.abspath(os.path.expanduser(root))
        if os.path.isfile(root):
            yield root
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith('.')]
            for fn in filenames:
                if os.path.splitext(fn)[1] not in TEXT_EXT:
                    continue
                p = os.path.join(dirpath, fn)
                try:
                    if os.path.getsize(p) > max_bytes:
                        continue
                except OSError:
                    continue
                yield p


def parse_line(line: Any) -> dict | None:

--- measured coverage: markers per top-level surface (tracked HEAD) ---
.coding-hermes/    239 ch:trace lines
docs/              16 ch:trace lines
tests/             4 ch:trace lines
scripts/           11 ch:trace lines
reports/           1 ch:trace lines

--- the board alone ---
$ git grep -c -F 'ch:trace' HEAD -- .coding-hermes/board/tasks.jsonl
HEAD:.coding-hermes/board/tasks.jsonl:117
$ git grep -c -F 'ch:trace' HEAD -- .coding-hermes/board/events.jsonl
HEAD:.coding-hermes/board/events.jsonl:75

--- what the walker yields, executed against this tree ---
$ python3 /home/kara/.hermes/stand-in/docs/task-router/trace_cov.py /home/kara/.hermes/stand-in/docs/task-router/probe-d12
walker yielded files: 1051
ch:trace lines inside walked files: 32
markers parse_line() accepted      : 29
lines the regex did not match      : 3
--- where the unmatched lines live (top dir+ext) ---
  docs/.md                       2
  scripts/.py                    1
ch:trace lines in ALL text files (dot-dirs + oversize included): 161
--- files the walker never sees (dot-dirs / oversize) ---
  oversize:data/null_reasons.jsonl
  oversize:data/tables/benchmarks.jsonl
  oversize:data/tables/models.jsonl
  oversize:snapshots/2026-10-03/models.jsonl
  oversize:.coding-hermes/board/tasks.jsonl

--- the validator's own numbers on the same tree ---
$ PYTHONPATH=. /home/kara/task-router/.venv/bin/python scripts/trace_context.py --validate-only --roots . | grep "files scanned"
files scanned: 1051 | markers matched: 26 | invalid: 8

--- and the markers it can never reach (dot-directories are skipped before size/type checks) ---
$ grep -n "dirnames\[:\] = " scripts/trace_context.py
43:            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith('.')]

=============== END ===============
