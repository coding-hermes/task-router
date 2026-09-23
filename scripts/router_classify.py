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
    registry_path = registry_path or os.environ.get('ROUTING_REGISTRY') \
        or os.path.join(REPO, 'registry.json')
    try:
        with open(registry_path) as f:
            tables = json.load(f).get('tables', {})
    except (OSError, ValueError):
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


def default_llm(prompt, text, timeout=60):
    """OpenAI-shaped classifier call over the configured lanes.

    Primary lane first, with bounded retry + backoff on 429/5xx
    (ROUTER_CLASSIFIER_RETRIES); the fallback lane (ROUTER_CLASSIFIER_FALLBACK_*)
    is used only after the primary is exhausted. Each lane carries its own
    timeout budget. Raises on total failure — the caller degrades visibly.
    """
    lanes = _classifier_lanes()
    if not lanes:
        raise RuntimeError('ROUTER_CLASSIFIER_BASE_URL not configured')
    body = json.dumps({
        'temperature': 0, 'max_tokens': 700,
        'messages': [{'role': 'system', 'content': prompt},
                     {'role': 'user', 'content': text}],
    }).encode()
    retries = _retry_budget()
    last_exc = None
    for lane_idx, lane in enumerate(lanes):
        attempts = (retries + 1) if lane_idx == 0 else 1
        for attempt in range(1, attempts + 1):
            if attempt > 1:
                time.sleep(_backoff_delay(attempt - 1))
            try:
                return _call_lane(lane, body)
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


def _call_lane(lane, body):
    """One POST to one classifier lane. `body` is the lane-agnostic request
    skeleton (messages/params); the model is per-lane data."""
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
    msg = (data.get('choices') or [{}])[0].get('message') or {}
    return msg.get('content') or msg.get('reasoning_content') or ''


def classify(text, llm=None, version=DEFAULT_PROMPT_VERSION, categories=None):
    """text -> result dict. Never raises for classifier problems: the result
    carries matrix=None + reasons and the caller degrades (R10)."""
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
        raw = (llm or default_llm)(prompt, text)
    except Exception as exc:  # noqa: BLE001 — degrade, never crash the request
        out['problems'].append(f'classifier call failed: {str(exc)[:200]}')
        return out
    out['raw'] = (raw or '')[:2000]
    obj = _extract_json(raw)
    if obj is None:
        out['problems'].append('no JSON object in classifier output')
        return out
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
