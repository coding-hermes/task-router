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


def default_llm(prompt, text, timeout=60):
    """OpenAI-shaped classifier call. Configured entirely by env so the model
    is DATA: ROUTER_CLASSIFIER_BASE_URL / _MODEL / _KEY_ENV (+ _KEY_VALUE for
    tests). Raises on failure — the caller degrades visibly."""
    base = os.environ.get('ROUTER_CLASSIFIER_BASE_URL')
    if not base:
        raise RuntimeError('ROUTER_CLASSIFIER_BASE_URL not configured')
    model = os.environ.get('ROUTER_CLASSIFIER_MODEL', 'glm-5.3-flash')
    key_env = os.environ.get('ROUTER_CLASSIFIER_KEY_ENV', '')
    key = os.environ.get('ROUTER_CLASSIFIER_KEY_VALUE') or (
        os.environ.get(key_env) if key_env else '')
    body = json.dumps({
        'model': model, 'temperature': 0, 'max_tokens': 700,
        'messages': [{'role': 'system', 'content': prompt},
                     {'role': 'user', 'content': text}],
    }).encode()
    req = urllib.request.Request(base.rstrip('/') + '/chat/completions', data=body,
                                 headers={'Content-Type': 'application/json',
                                          'Authorization': f'Bearer {key}',
                                          'User-Agent': 'task-router-classifier/1.0'})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
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
