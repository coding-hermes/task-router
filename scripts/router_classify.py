#!/usr/bin/env python3
"""router_classify.py — TR-067 classifier hop (prompt as DATA, validated output).

Turns a task request into the complexity MATRIX the router can act on: the
per-category levels the task requires. Two hard rules from the spec:

R7  the prompt is a VERSIONED FILE (data/classifier/prompt-<v>.md), never a
    string in code, and every result records the prompt version + model used;
R10 the classifier is MEASURED like any other task (its own outcome row) and
    degrades VISIBLY — never to an uncontrolled default lane.

Validation is strict: unknown categories are rejected, levels clamped to the
vocabulary range, confidence parsed as 0..1. Anything unusable yields
matrix=None + reasons; callers decide the degrade (they have a declared
profile or the default profile).

The LLM call is INJECTABLE (`llm=callable(prompt, text) -> str`) so tests and
offline runs never touch the network.
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROMPT_DIR = os.path.join(REPO, 'data', 'classifier')
LEVEL_MIN, LEVEL_MAX = -5, 5
DEFAULT_PROMPT_VERSION = 'v1'


def prompt_path(version=DEFAULT_PROMPT_VERSION):
    return os.path.join(PROMPT_DIR, f'prompt-{version}.md')


def load_prompt(version=DEFAULT_PROMPT_VERSION):
    with open(prompt_path(version)) as f:
        return f.read()


def registry_categories(registry_path=None):
    """The category vocabulary — DATA-DRIVEN from the registry (union of
    task_profile_requirements), never a hardcoded list."""
    # DATA > CODE: the vocabulary is the registry's, never a list in this file.
    # The path was <repo>/registry.json, but the artefact is written to
    # <repo>/data/registry.json, so the lookup silently returned [] - and an
    # empty vocabulary makes validate_matrix PERMISSIVE (it can no longer reject
    # an unknown category) while making a bare {category: level} answer
    # unparseable (every key looks unknown). Candidates are tried in order;
    # ROUTING_REGISTRY still wins, and [] is returned only when none is readable.
    candidates = [registry_path] if registry_path else [
        os.environ.get('ROUTING_REGISTRY'),
        os.path.join(REPO, 'data', 'registry.json'),
        os.path.join(REPO, 'registry.json'),
    ]
    for path in [c for c in candidates if c]:
        try:
            with open(path) as f:
                tables = json.load(f).get('tables', {})
        except (OSError, ValueError):
            continue
        cats = sorted({r.get('category') for r in tables.get('task_profile_requirements') or []
                       if r.get('category')})
        if cats:
            return cats
    return []
    return sorted({r.get('category') for r in tables.get('task_profile_requirements') or []
                   if r.get('category')})


def _extract_json(raw):
    """Pull the first JSON object out of a model answer (tolerates fences and
    surrounding prose). Returns None when there is none."""
    if not isinstance(raw, str):
        return None
    fenced = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', raw, re.S)
    candidate = fenced.group(1) if fenced else None
    if candidate is None:
        start = raw.find('{')
        while start != -1:
            depth = 0
            for i in range(start, len(raw)):
                if raw[i] == '{':
                    depth += 1
                elif raw[i] == '}':
                    depth -= 1
                    if depth == 0:
                        candidate = raw[start:i + 1]
                        break
            if candidate:
                break
            start = raw.find('{', start + 1)
    if candidate is None:
        return None
    try:
        obj = json.loads(candidate)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def validate_matrix(obj, categories):
    """obj -> (matrix, confidence, problems). Unknown categories are REJECTED
    (reported), levels clamped, empty matrix allowed (a task may require
    nothing special)."""
    problems = []
    if not isinstance(obj, dict):
        return None, None, ['classifier output is not a JSON object']
    if 'categories' in obj:
        raw_cats = obj['categories']
    elif 'complexity' in obj:
        raw_cats = obj['complexity']
    else:
        # bare mapping is accepted ONLY when every key is a known category;
        # otherwise the object is a malformed answer, not an empty matrix
        keys = [k for k in obj if k not in ('confidence', 'reason', 'notes')]
        if keys and all(str(k).strip() in set(categories or []) for k in keys):
            raw_cats = {k: obj[k] for k in keys}
        else:
            return None, None, ['missing "categories" object (unrecognized keys: '
                                + ', '.join(str(k) for k in keys[:6]) + ')']
    if not isinstance(raw_cats, dict):
        return None, None, ['missing "categories" object']
    known = set(categories or [])
    matrix = {}
    for cat, lvl in raw_cats.items():
        if cat in ('confidence', 'reason', 'notes'):
            continue
        c = str(cat).strip()
        if known and c not in known:
            problems.append(f'unknown category {c!r} rejected')
            continue
        try:
            v = int(lvl)
        except (TypeError, ValueError):
            problems.append(f'category {c!r} level {lvl!r} is not an integer')
            continue
        if v < LEVEL_MIN or v > LEVEL_MAX:
            problems.append(f'category {c!r} level {v} clamped to [{LEVEL_MIN},{LEVEL_MAX}]')
            v = max(LEVEL_MIN, min(LEVEL_MAX, v))
        matrix[c] = v
    conf = obj.get('confidence')
    try:
        conf = None if conf is None else max(0.0, min(1.0, float(conf)))
    except (TypeError, ValueError):
        problems.append(f'confidence {conf!r} unparseable — recorded as null')
        conf = None
    return matrix, conf, problems


#: HTTP statuses worth a bounded retry: rate limiting and upstream hiccups —
#: never a 4xx from OUR payload (that is a bug, retrying it is waste).
_RETRYABLE_STATUS = (429, 500, 502, 503, 504)



#: The budget for one classification. A reasoning model needs room to think
#: before it answers, and an empty completion is reported as a rating failure -
#: so this is deliberately generous, bounded by the model's published ceiling
#: and never below 4096. Owner 2026-10-03: "make sure you raise this number".
CLASSIFY_MAX_TOKENS_DEFAULT = 16384


def _classify_budget():
    """Budget in tokens for one classification call (env > default)."""
    for name in ('ROUTER_CLASSIFY_MAX_TOKENS',):
        v = os.environ.get(name)
        if v:
            try:
                n = int(v)
                if n > 0:
                    return n
            except ValueError:
                pass
    return CLASSIFY_MAX_TOKENS_DEFAULT


def _structured_mode():
    """Which response_format rung to try: json_schema | json_object | none.

    ROUTER_CLASSIFY_STRUCTURED=auto (default) tries the best rung and steps down
    on the first rejection; 'off' disables structured output entirely (plain
    prompt + tolerant parse). The step-down happens in default_llm, so a lane
    that does not support a rung costs one call, not a correctness bug.
    """
    v = (os.environ.get('ROUTER_CLASSIFY_STRUCTURED') or 'auto').strip().lower()
    if v in ('off', 'none', 'false', '0'):
        return 'none'
    if v in ('json_schema', 'json_object'):
        return v
    # A rung rejected once is remembered for this process, so the ladder steps
    # down instead of paying a 400 on every call (DeepSeek takes json_object but
    # rejects json_schema).
    return os.environ.get('ROUTER_CLASSIFY_STRUCTURED_MODE') or 'json_schema'


def _matrix_schema(categories=None):
    """The complexity matrix as a JSON Schema: {category: signed level}.

    Keys are restricted to the registry's own categories when they are known
    (the registry is the authority for the list, not this file); levels are the
    signed -5..+5 scale. `additionalProperties` stays open so an unknown-but-real
    category is reported rather than rejected - validate_matrix() is the gate
    that decides what is admissible, and it is not made redundant here.
    """
    lvl = {'type': 'integer', 'minimum': -5, 'maximum': 5}
    cats = list(categories or [])
    if cats:
        return {'type': 'object', 'properties': {c: dict(lvl) for c in cats},
                'additionalProperties': lvl}
    return {'type': 'object', 'additionalProperties': lvl}


#: The classifier is a small structured extraction, so thinking is pure overhead
#: here: measured on the live endpoint with the same prompt, the production body
#: spent 122 of 187 completion tokens on reasoning (480 chars of it), while
#: reasoning_effort='none' answered in 61 tokens with zero reasoning and 35% less
#: wall time. Two mechanisms work on this endpoint and both are tried, best
#: first; whichever lands is recorded on the call meta so a provider change is
#: visible in the ledger rather than inferred from latency.
THINKING_LADDER = ({'reasoning_effort': 'none'}, {'thinking': {'type': 'disabled'}})


def _thinking_off():
    """The param that turns thinking off: {'reasoning_effort': 'none'} | {'thinking': {...}} | {}.

    ROUTER_CLASSIFY_THINKING=off (default) walks THINKING_LADDER; 'on' sends
    nothing (the model thinks, as it did before this existed); 'auto' is
    currently the same as 'on' and exists so a caller can be explicit.
    ROUTER_CLASSIFY_THINKING_MODE names a rung directly (measured, not guessed:
    reasoning_effort=minimal is NOT minimal on this endpoint - it burned 741
    reasoning tokens against the baseline's 122).
    """
    v = (os.environ.get('ROUTER_CLASSIFY_THINKING') or 'off').strip().lower()
    if v in ('on', 'auto', 'default', 'false', '0'):
        return {}
    forced = os.environ.get('ROUTER_CLASSIFY_THINKING_MODE')
    if forced == 'none':
        return dict(THINKING_LADDER[0])
    if forced == 'disabled':
        return dict(THINKING_LADDER[1])
    return dict(THINKING_LADDER[0])

def _classifier_lanes():
    """The configured classifier lanes, primary first.

    Lane 0: ROUTER_CLASSIFIER_BASE_URL/_MODEL/_KEY_ENV (the existing primary —
    unchanged). Lane 1+: ROUTER_CLASSIFIER_FALLBACK_BASE_URL/_MODEL/_KEY_ENV
    (+ _FALLBACK_TIMEOUT_S — a slow lane gets its own budget, never the
    primary's). Env/data-driven end to end; no provider names in code.
    """
    lanes = [{'base': os.environ.get('ROUTER_CLASSIFIER_BASE_URL'),
              'model': os.environ.get('ROUTER_CLASSIFIER_MODEL', 'glm-5.3-flash'),
              'key_env': os.environ.get('ROUTER_CLASSIFIER_KEY_ENV', ''),
              'key_value': os.environ.get('ROUTER_CLASSIFIER_KEY_VALUE'),
              'timeout': _env_float('ROUTER_CLASSIFIER_TIMEOUT_S', 60.0)}]
    fb_base = os.environ.get('ROUTER_CLASSIFIER_FALLBACK_BASE_URL')
    if fb_base:
        lanes.append({'base': fb_base,
                      'model': os.environ.get('ROUTER_CLASSIFIER_FALLBACK_MODEL', ''),
                      'key_env': os.environ.get('ROUTER_CLASSIFIER_FALLBACK_KEY_ENV', ''),
                      'key_value': os.environ.get('ROUTER_CLASSIFIER_FALLBACK_KEY_VALUE'),
                      'timeout': _env_float('ROUTER_CLASSIFIER_FALLBACK_TIMEOUT_S', 60.0)})
    return [lane for lane in lanes if lane['base']]


def _env_float(name, default):
    try:
        return float(os.environ.get(name, '') or default)
    except ValueError:
        return default


def _retry_budget():
    """Bounded retry count for the PRIMARY lane (ROUTER_CLASSIFIER_RETRIES,
    default 0 = one call, no retry). The fallback lane is tried ONCE, only
    after the primary is fully exhausted."""
    try:
        return max(0, int(os.environ.get('ROUTER_CLASSIFIER_RETRIES', '0')))
    except ValueError:
        return 0


def _backoff_delay(attempt):
    """Seconds to sleep before retry `attempt` (1-based): 1s, 2s, 4s… capped at
    8s. A classifier retry must not hold the proxied request hostage."""
    return min(2 ** (attempt - 1), 8)


def default_llm(prompt, text, timeout=60, timeout_s=None, categories=None):
    """OpenAI-shaped classifier call over the configured lanes.

    Primary lane first, with bounded retry + backoff on 429/5xx
    (ROUTER_CLASSIFIER_RETRIES); the fallback lane (ROUTER_CLASSIFIER_FALLBACK_*)
    is used only after the primary is exhausted. Each lane carries its own
    timeout budget (default 60 s, overridden by `timeout_s` for the primary
    and by ROUTER_CLASSIFIER_FALLBACK_TIMEOUT_S for the fallback). Raises on
    total failure — the caller degrades visibly.
    """
    lanes = _classifier_lanes()
    if not lanes:
        raise RuntimeError('ROUTER_CLASSIFIER_BASE_URL not configured')
    # max_tokens is a RATING-SUCCESS parameter, not a cost knob: at 700 the
    # configured reasoning model spent the budget thinking and answered with an
    # empty completion (no error), which is how 70 of 97 live requests in the
    # first hour of the flip landed on 'default'. Budget for the reasoning
    # preamble PLUS the small JSON answer. ROUTER_CLASSIFY_MAX_TOKENS wins.
    _payload = {
        'temperature': 0, 'max_tokens': _classify_budget(),
        'messages': [{'role': 'system', 'content': prompt},
                     {'role': 'user', 'content': text}],
    }
    # STRUCTURED OUTPUT (owner 2026-10-03). The classifier answers with a small
    # {category: level} object, so ask the API to guarantee the shape instead of
    # hoping prose contains JSON and parsing around it - the tolerant-parse
    # heuristic was covering for a missing contract. Ladder, best first, each
    # rung recorded so a row can say which one produced its answer:
    #   json_schema -> json_object -> none (plain prompt, tolerant parse).
    _mode = _structured_mode()
    _cats = categories if categories is not None else registry_categories()
    _think = _thinking_off()
    _payload.update(_think)
    if _mode == 'json_schema':
        _payload['response_format'] = {
            'type': 'json_schema',
            'json_schema': {'name': 'complexity_matrix', 'strict': False,
                            'schema': _matrix_schema(_cats)}}
    elif _mode == 'json_object':
        _payload['response_format'] = {'type': 'json_object'}
    body = json.dumps(_payload).encode()
    # Override the primary lane timeout if the caller supplied timeout_s
    if timeout_s is not None and lanes:
        lanes = [dict(lanes[0], timeout=float(timeout_s))] + list(lanes[1:])
    retries = _retry_budget()
    last_exc = None
    for lane_idx, lane in enumerate(lanes):
        attempts = (retries + 1) if lane_idx == 0 else 1
        for attempt in range(1, attempts + 1):
            if attempt > 1:
                time.sleep(_backoff_delay(attempt - 1))
            try:
                # Step-down ladder for REJECTED PARAMS (not lane failures). One
                # param per attempt, and the order is evidence-driven: on this
                # endpoint response_format=json_schema is the known rejection
                # (400) while reasoning_effort='none' is accepted, so the rung
                # that is known-bad goes first. A rejected param must never cost
                # the rating - the rating is the expensive outcome.
                try:
                    got = _call_lane(lane, body)
                except Exception as exc:  # noqa: BLE001
                    if not _is_rejection(exc):
                        raise
                    m = json.loads(body)
                    rf = (m.get('response_format') or {}).get('type')
                    if rf:
                        nxt = {'json_schema': 'json_object', 'json_object': None}.get(rf)
                        if nxt:
                            m['response_format'] = {'type': nxt}
                            os.environ['ROUTER_CLASSIFY_STRUCTURED_MODE'] = nxt
                        else:
                            m.pop('response_format', None)
                            os.environ['ROUTER_CLASSIFY_STRUCTURED_MODE'] = 'none'
                        print('classifier: response_format %r rejected (%s) - stepping down to %r'
                              % (rf, str(exc)[:100], nxt or 'a plain prompt'), file=sys.stderr)
                    elif m.get('reasoning_effort'):
                        m.pop('reasoning_effort', None)
                        m['thinking'] = {'type': 'disabled'}
                        os.environ['ROUTER_CLASSIFY_THINKING_MODE'] = 'disabled'
                        print('classifier: reasoning_effort rejected (%s) - trying thinking=disabled'
                              % str(exc)[:100], file=sys.stderr)
                    elif m.get('thinking'):
                        m.pop('thinking', None)
                        os.environ['ROUTER_CLASSIFY_THINKING'] = 'on'
                        print('classifier: thinking param rejected (%s) - sending no thinking param'
                              % str(exc)[:100], file=sys.stderr)
                    else:
                        raise
                    body = json.dumps(m).encode()
                    got = _call_lane(lane, body)
                if got or not isinstance(got, Raw):
                    return got
                # Empty completion on the first pass: the reasoning model likely
                # ate the budget. One bounded retry at 4x, then report honestly.
                if payload_budget := json.loads(body).get('max_tokens'):
                    retry_body = json.dumps(dict(json.loads(body),
                                                 max_tokens=payload_budget * 4)).encode()
                    retry = _call_lane(lane, retry_body)
                    if retry:
                        return retry
                    return Raw('', retried_at=payload_budget * 4, **getattr(retry, 'meta', {}))
                return got
            except urllib.error.HTTPError as exc:
                retryable = exc.code in _RETRYABLE_STATUS
                last_exc = exc
                if not retryable or attempt >= attempts:
                    if lane_idx < len(lanes) - 1 and retryable:
                        break   # fall to the next lane, do not raise yet
                    raise
            except Exception as exc:  # noqa: BLE001 — transport/timeout
                last_exc = exc
                if attempt >= attempts:
                    if lane_idx < len(lanes) - 1:
                        break
                    raise
    raise last_exc if last_exc else RuntimeError('no classifier lane attempted')


class Raw(str):
    """Classifier output that remembers HOW it was produced, without changing
    the plain-string contract its callers already rely on.

    Why this exists (measured 2026-10-03): the classifier ran on 97 of the first
    hour of flipped traffic and only 7 produced a usable matrix; the rest fell to
    'default' - a rating FAILURE, not an absent input. The failure was silent:
    max_tokens was 700 against a REASONING model, so the budget was spent
    thinking and the completion came back empty (or as reasoning_content only),
    with no error and no exception. This is the shape that turns a complexity
    contract into a fixed-profile router: the router cannot band what it cannot
    read, and nothing said why. The meta rides the string so a parse failure can
    name its own cause instead of reporting 'no JSON'. """
    def __new__(cls, value, **meta):
        o = super().__new__(cls, value or '')
        o.meta = meta
        return o


def _is_rejection(exc):
    """True when the endpoint refused the PARAM (4xx), not when the lane failed.

    A rejected parameter is a formatting problem to step down from; a 5xx or a
    timeout is a lane problem and belongs to the retry ladder above.
    """
    s = str(exc)
    return '400' in s or 'bad request' in s.lower() or 'unrecognized' in s.lower() \
        or 'unsupported' in s.lower()

def _call_lane(lane, body):
    """One POST to one classifier lane. `body` is the lane-agnostic request
    skeleton (messages/params); the model is per-lane data.

    Returns Raw: the content when the model produced one; when the budget was
    consumed by reasoning the string is EMPTY and meta says so ('truncated',
    'only_reasoning', 'chars_reasoning') - never silently a wrong answer."""
    payload = json.loads(body)
    payload['model'] = lane['model'] or payload.get('model', '')
    key = lane.get('key_value') or (
        os.environ.get(lane['key_env']) if lane['key_env'] else '')
    req = urllib.request.Request(
        lane['base'].rstrip('/') + '/chat/completions',
        data=json.dumps(payload).encode(),
        headers={'Content-Type': 'application/json',
                 'Authorization': f'Bearer {key}',
                 'User-Agent': 'task-router-classifier/1.0'})
    with urllib.request.urlopen(req, timeout=lane['timeout']) as resp:
        data = json.loads(resp.read())
    choice = (data.get('choices') or [{}])[0]
    msg = choice.get('message') or {}
    content = msg.get('content') or ''
    reasoning = msg.get('reasoning_content') or ''
    usage = data.get('usage') or {}
    finish = choice.get('finish_reason')
    meta = {'finish_reason': finish, 'max_tokens': payload.get('max_tokens'),
            'thinking': ('none' if payload.get('reasoning_effort') == 'none'
                         else 'disabled' if payload.get('thinking') else 'default'),
            'chars_content': len(content), 'chars_reasoning': len(reasoning),
            'reasoning_tokens': usage.get('completion_tokens_details', {}).get('reasoning_tokens')
                                if isinstance(usage.get('completion_tokens_details'), dict) else None,
            'model': payload.get('model')}
    if content:
        return Raw(content, **meta)
    # Empty completion. If the model reasoned instead, keep the reasoning as a
    # last-resort parse target but SAY that is what happened.
    if reasoning:
        return Raw(reasoning, only_reasoning=True, truncated=(finish == 'length'), **meta)
    return Raw('', empty=True, truncated=(finish == 'length'), **meta)


def classify(text, llm=None, version=DEFAULT_PROMPT_VERSION, categories=None, timeout_s=None):
    """text -> result dict. Never raises for classifier problems: the result
    carries matrix=None + reasons and the caller degrades (R10).

    timeout_s: optional float, overrides the primary lane timeout for this call
    (used by the startup self-check so it never blocks longer than requested).
    """
    out = {'prompt_version': version, 'model': os.environ.get('ROUTER_CLASSIFIER_MODEL'),
           'matrix': None, 'complexity_sig': None, 'confidence': None,
           'problems': [], 'raw': None}
    cats = categories if categories is not None else registry_categories()
    out['categories_known'] = len(cats)
    try:
        prompt = load_prompt(version)
    except OSError as exc:
        out['problems'].append(f'prompt {version} unreadable: {exc}')
        return out
    try:
        if llm:
            raw = llm(prompt, text)   # injected LLM: no timeout_s kwarg
        else:
            raw = default_llm(prompt, text, timeout_s=timeout_s, categories=cats)
    except Exception as exc:  # noqa: BLE001 — degrade, never crash the request
        out['problems'].append(f'classifier call failed: {str(exc)[:200]}')
        return out
    out['raw'] = (raw or '')[:2000]
    obj = _extract_json(raw)
    if obj is None:
        meta = getattr(raw, 'meta', {}) or {}
        hint = []
        if not str(raw or '').strip():
            hint.append('EMPTY completion')
        if meta.get('truncated'):
            hint.append('finish_reason=length')
        if meta.get('only_reasoning'):
            hint.append('only reasoning_content returned')
        if meta.get('reasoning_tokens'):
            hint.append(f"reasoning_tokens={meta['reasoning_tokens']}")
        # TR-237/R2.3: a rating failure must be diagnosable from the row alone -
        # 'no JSON object' with no cause is what let this stay invisible.
        out['problems'].append('no JSON object in classifier output'
                               + (f" ({', '.join(hint)})" if hint else ''))
        out['call_meta'] = meta
        return out
    out['call_meta'] = getattr(raw, 'meta', {}) or {}
    matrix, conf, problems = validate_matrix(obj, cats)
    out['problems'].extend(problems)
    out['confidence'] = conf
    if matrix is None:
        return out
    out['matrix'] = matrix
    try:
        sys.path.insert(0, os.path.join(REPO, 'scripts'))
        import router_outcomes as ro
        out['complexity_sig'] = ro.complexity_sig(matrix) if matrix else None
    except Exception:  # noqa: BLE001
        pass
    return out


def main():
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    ap.add_argument('--text')
    ap.add_argument('--file')
    ap.add_argument('--prompt-version', default=DEFAULT_PROMPT_VERSION)
    ap.add_argument('--list-categories', action='store_true')
    args = ap.parse_args()
    if args.list_categories:
        print(json.dumps(registry_categories(), indent=1))
        return 0
    text = args.text
    if args.file:
        text = open(args.file).read()
    if not text:
        print(json.dumps({'error': 'no --text/--file given'}, indent=1))
        return 0
    print(json.dumps(classify(text, version=args.prompt_version), indent=1))
    return 0


if __name__ == '__main__':
    sys.exit(main())
