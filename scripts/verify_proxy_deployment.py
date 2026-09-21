#!/usr/bin/env python3
"""TR-067: live acceptance battery for the classified proxy deployment.

Run FROM the repo root while a proxy instance is up (recipe in
README.md, "Classified proxy deployment"). Checks, in order:

1. a realistic task through the proxy classifies it
   (_router.complexity_source == classifier),
2. exact-content parity on a constrained prompt: proxy vs direct-to-gateway
   return the same answer content (free-form prompts differ word-by-word
   across independent generations — parity is a content-forwarding
   property, not a generation-identity property),
3. the x-router-scorer: jev header is honored (complexity_source == jev),
4. the proxy serves the exact model it forwarded to (same-model check).

Auth: set API_SERVER_KEY (or pass it via ROUTER_VERIFY_KEY); the key is
read from env and never printed. Exit 0 only if every check passes.

Example:
    API_SERVER_KEY=... python3 scripts/verify_proxy_deployment.py
"""

import json
import os
import sys
import urllib.error
import urllib.request

PROXY = os.environ.get("ROUTER_VERIFY_PROXY", "http://127.0.0.1:9391")
GATEWAY = os.environ.get("ROUTER_VERIFY_GATEWAY", "http://127.0.0.1:8642")
KEY = os.environ.get("ROUTER_VERIFY_KEY") or os.environ.get("API_SERVER_KEY", "")

REAL = [{"role": "user",
         "content": "Write a Python function that inverts a binary tree and "
                    "briefly explain the recursion."}]
# Answer-constrained so proxy and direct answers are comparable exactly.
CONSTRAINED = [{"role": "user",
                "content": "Reply with exactly this one word and nothing "
                           "else: banana"}]


def post(url, body, headers):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, {"raw": raw[:300]}


def content_of(payload):
    return ((payload.get("choices") or [{}])[0].get("message") or {}).get("content") or ""


def main():
    if not KEY:
        print("FAIL: API key env (API_SERVER_KEY / ROUTER_VERIFY_KEY) not set")
        return 2
    auth = {"Authorization": f"Bearer {KEY}"}
    ok = True

    # 1. classifier source on live traffic
    st, p = post(PROXY + "/v1/chat/completions",
                 {"model": "task-router-auto", "messages": REAL,
                  "temperature": 0}, auth)
    meta = p.get("_router") or {}
    served = (meta.get("served_by") or {}).get("model")
    print(f"1. complexity_source={meta.get('complexity_source')} "
          f"served={served} status={st}")
    ok = ok and st == 200 and meta.get("complexity_source") == "classifier"

    # 2. exact-content parity (constrained prompt)
    st_a, pa = post(PROXY + "/v1/chat/completions",
                    {"model": "task-router-auto", "messages": CONSTRAINED,
                     "temperature": 0}, auth)
    st_b, pb = post(GATEWAY + "/v1/chat/completions",
                    {"model": (pa.get("_router") or {}).get("served_by", {})
                     .get("model"),
                     "messages": CONSTRAINED, "temperature": 0}, auth)
    same = content_of(pa).strip() == content_of(pb).strip()
    print(f"2. parity proxy={content_of(pa).strip()!r} "
          f"direct={content_of(pb).strip()!r} match={same}")
    ok = ok and same and st_a == 200 and st_b == 200

    # 3. jev scorer via header
    st_j, pj = post(PROXY + "/v1/chat/completions",
                    {"model": "task-router-auto", "messages": REAL,
                     "temperature": 0}, {**auth, "x-router-scorer": "jev"})
    jsource = (pj.get("_router") or {}).get("complexity_source")
    print(f"3. jev-header complexity_source={jsource} status={st_j}")
    ok = ok and st_j == 200 and jsource == "jev"

    # 4. same-model forwarding
    st_m, pm = post(PROXY + "/v1/chat/completions",
                    {"model": "task-router-auto", "messages": CONSTRAINED,
                     "temperature": 1}, auth)
    m1 = ((pm.get("_router") or {}).get("served_by") or {}).get("model")
    st_d, pd = post(GATEWAY + "/v1/chat/completions",
                    {"model": m1, "messages": CONSTRAINED,
                     "temperature": 1}, auth)
    print(f"4. same-model proxy={m1} direct={pd.get('model')} "
          f"match={m1 == pd.get('model')}")
    ok = ok and st_m == 200 and st_d == 200 and m1 == pd.get("model")

    print("VERDICT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
