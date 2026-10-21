# Context Call — ONE CONTEXT CALL (`router context-call`)

> TR-254: Single read-only command that pulls from four surfaces and returns
> a unified, provenanced output. Each fragment labelled with
> `{surface, source_path_or_key, taken_at}`.

## Usage

```bash
# All four surfaces (default)
router context-call <workdir>

# Specific surfaces
router context-call <workdir> --surfaces code,specs,board

# JSON output
router context-call <workdir> --json

# Custom DuckBrain namespaces
router context-call <workdir> --db-namespaces task-router,routing

# Specify Hilo binary path
HILO_BIN=/path/to/hilo router context-call <workdir>
```

## Surfaces

| Surface | Source | Read-Only |
|---------|--------|-----------|
| `code` | Hilo graph (`hilo graph related` + `hilo graph understand`) | Yes |
| `db` | DuckBrain HTTP API (`localhost:3000`) | Yes |
| `specs` | `docs/specs/`, `specs/`, `docs/` | Yes |
| `board` | `.coding-hermes/board/tasks.jsonl` | Yes |

## Output

Each fragment carries provenance:

```
[code] source=scripts/router_spawn.py taken_at=2026-10-21T08:00:00Z
  related: <hilo graph output>
```

Contradictions between surfaces are surfaced explicitly (e.g., a board row with
a commit hash but pending status).

## Acceptance Criteria (TR-254)

- AC1: One command returns fragments from all four surfaces, each labelled.
- AC2: Hilo leg proven against a real fleet repo (not Hilo itself).
- AC3: DuckBrain fragments are CITED RECORDS ONLY, never paraphrased.
- AC4: Contradictions surfaced explicitly.
- AC5: Read-only against every surface — no file content modified.
- AC6: Cost and latency measured and stated.

## Limitations

- Hilo requires `hilo` on PATH (v0.3+). If absent, code surface returns
  version "unknown" and sample files list.
- DuckBrain requires an API key at `~/.duckbrain/<namespace>.token` or
  `DUCKBRAIN_API_KEY` env var. If absent, returns "no auth" fragment.
- Specs surface reads up to 10 files per directory (capped).
