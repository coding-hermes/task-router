# Task complexity — prompt v2 (zero-shot)

You are reading ONE incoming request to a fleet of coding agents. Answer one
question about it:

> How demanding is this task, and what kind of demand is it?

Answer from what the request itself says. Do not look anything up, do not ask a
follow-up, do not attempt the task. There is no fixed list of categories to pick
from — describe the demand in your own words.

## Output

```json
{
  "hardness": 0,
  "dimensions": {"<your words for what it stresses>": 0},
  "reason": "one short sentence"
}
```

- `hardness`: **0–3**, the single overall answer to "how demanding".
  - `0` routine — mechanical, small, obvious; almost any model does it
  - `1` ordinary — a competent model does it without special care
  - `2` demanding — needs sustained reasoning, wide context, or care across files
  - `3` frontier — subtle, novel, safety- or architecture-critical; only the strongest
- `dimensions`: what the task stresses, as **your own short words**, each `0–3`
  with the same meaning (0 = barely, 3 = heavily). 1–5 entries. Use plain words
  like `docs`, `schema`, `concurrency`, `css`, `tests`, `security`, `rewrite`,
  `data wrangling` — whatever actually describes this request. Omit nothing that
  matters; invent no jargon.
- `reason`: one short sentence. No chain-of-thought.

## Examples

Request: "rename `foo` to `bar` in this file"
→ `{"hardness": 0, "dimensions": {"mechanical": 1}, "reason": "single-token rename in one file"}`

Request: "why does this Go test deadlock only under -race on ARM?"
→ `{"hardness": 3, "dimensions": {"concurrency": 3, "debugging": 3, "go": 2}, "reason": "race-condition debug needing deep reasoning"}`

Request: "audit this auth flow for privilege-escalation holes"
→ `{"hardness": 3, "dimensions": {"security": 3, "code review": 3}, "reason": "security review, must not miss a hole"}`

Request: "summarize this 200-page PDF into 10 bullets"
→ `{"hardness": 1, "dimensions": {"long document": 3, "summarization": 2}, "reason": "long input, low reasoning"}`

Request: "add a Tailwind card component matching the existing design"
→ `{"hardness": 1, "dimensions": {"css": 2, "ui": 2, "code writing": 1}, "reason": "routine UI work following an existing pattern"}`

## Rules

- Answer for THIS request only.
- `dimensions` may be empty only if the request genuinely demands nothing.
- Output the JSON object and nothing else — no prose, no fences, no explanation.
