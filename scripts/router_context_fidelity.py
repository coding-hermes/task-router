#!/usr/bin/env python3
"""Measure whether a router lane preserves the tail of a large prompt.

The probe deliberately checks two independent facts: the upstream's reported
input-token meter and a marker placed near the end of the request.  A successful
HTTP status or an echoed model id is not evidence that the prompt arrived.
Results are stored outside the registry in ``context-fidelity-state.json`` so
resolver decisions remain scoped to the measured provider/model lane.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_PROMPT_CHARS = 12_000
DEFAULT_TOLERANCE_TOKENS = 128
DEFAULT_TIMEOUT_S = 30.0
STATE_FILENAME = "context-fidelity-state.json"


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def estimate_prompt_tokens(text: str) -> int:
    """Return a deterministic lower-bound estimate without a tokenizer package."""
    return max(1, math.ceil(len(text) / 4))


def make_probe_prompt(prompt_chars: int = DEFAULT_PROMPT_CHARS) -> dict[str, Any]:
    """Build a large prompt with a uniquely addressable tail marker."""
    if prompt_chars < 2_000:
        raise ValueError("prompt_chars must be at least 2000")
    needle = "CONTEXT_FIDELITY_NEEDLE_7F4A9C2E"
    instruction = (
        "\n\nRead the complete message. If the exact marker is present, reply "
        f"with only {needle}.\n"
    )
    filler_len = max(0, prompt_chars - len(needle) - len(instruction))
    filler = ("This is context-fidelity filler; preserve it exactly. " *
              ((filler_len // 52) + 1))[:filler_len]
    text = filler + needle + instruction
    return {
        "text": text,
        "needle": needle,
        "needle_offset": text.index(needle),
        "expected_prompt_tokens": estimate_prompt_tokens(text),
    }


def _endpoint(base_url: str) -> str:
    base = base_url.rstrip("/")
    return base if base.endswith("/chat/completions") else base + "/chat/completions"


def _response_text(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    choices = payload.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        message = choices[0].get("message")
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            return message["content"]
        if isinstance(choices[0].get("text"), str):
            return choices[0]["text"]
    response = payload.get("response")
    if isinstance(response, dict):
        output_text = response.get("output_text")
        if isinstance(output_text, str):
            return output_text
    return ""


def _reported_input_tokens(payload: Any) -> int | None:
    if not isinstance(payload, dict):
        return None
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return None
    for key in ("prompt_tokens", "input_tokens"):
        value = usage.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
            return int(value)
    return None


def judge_response(payload: Any, prompt: dict[str, Any],
                   tolerance_tokens: int = DEFAULT_TOLERANCE_TOKENS) -> dict[str, Any]:
    """Classify a decoded successful response without trusting HTTP/model echo."""
    expected = int(prompt["expected_prompt_tokens"])
    reported = _reported_input_tokens(payload)
    text = _response_text(payload)
    needle_found = prompt["needle"] in text
    result: dict[str, Any] = {
        "status": "UNKNOWN",
        "expected_prompt_tokens": expected,
        "reported_prompt_tokens": reported,
        "tolerance_tokens": tolerance_tokens,
        "needle": prompt["needle"],
        "needle_offset": prompt["needle_offset"],
        "needle_found": needle_found,
        "model": payload.get("model") if isinstance(payload, dict) else None,
        "measured_at": utc_now(),
    }
    if reported is None:
        result["status"] = "UNKNOWN"
        result["reason"] = "successful response omitted usage.prompt_tokens/input_tokens"
        return result
    deficit = expected - reported
    result["deficit_tokens"] = max(0, deficit)
    if deficit > tolerance_tokens or not needle_found:
        result["status"] = "TRUNCATED"
        reasons = []
        if deficit > tolerance_tokens:
            reasons.append(f"reported input {reported} < expected {expected} by {deficit}")
        if not needle_found:
            reasons.append("tail needle absent from model response")
        result["reason"] = "; ".join(reasons)
    else:
        result["status"] = "OK"
        result["reason"] = "reported input and tail needle are consistent"
    return result


def probe_lane(base_url: str, provider: str, model: str, *, api_key: str | None = None,
               headers: dict[str, str] | None = None,
               prompt_chars: int = DEFAULT_PROMPT_CHARS,
               tolerance_tokens: int = DEFAULT_TOLERANCE_TOKENS,
               timeout_s: float = DEFAULT_TIMEOUT_S) -> dict[str, Any]:
    """Probe one OpenAI-compatible lane and return a durable evidence record."""
    prompt = make_probe_prompt(prompt_chars)
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt["text"]}],
        "max_tokens": 64,
        "temperature": 0,
    }
    request_headers = {"Content-Type": "application/json"}
    if api_key:
        request_headers["Authorization"] = f"Bearer {api_key}"
    request_headers.update(headers or {})
    record: dict[str, Any] = {
        "provider": provider,
        "model": model,
        "status": "FAILED",
        "measured_at": utc_now(),
        "endpoint": _endpoint(base_url),
        "expected_prompt_tokens": prompt["expected_prompt_tokens"],
        "needle_offset": prompt["needle_offset"],
    }
    request = urllib.request.Request(
        _endpoint(base_url), data=json.dumps(body).encode(), headers=request_headers,
        method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            raw = response.read()
            status_code = response.status
    except urllib.error.HTTPError as exc:
        record["reason"] = f"HTTP {exc.code}"
        return record
    except Exception as exc:  # noqa: BLE001 — probe must not crash the resolver
        record["reason"] = f"{type(exc).__name__}: {exc}"
        return record
    record["http_status"] = status_code
    if status_code != 200:
        record["reason"] = f"HTTP {status_code}"
        return record
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError) as exc:
        record["reason"] = f"non-JSON response: {exc}"
        return record
    judged = judge_response(payload, prompt, tolerance_tokens)
    record.update(judged)
    return record


def state_path(state_dir: str | os.PathLike[str] | None = None) -> Path:
    root = Path(state_dir or os.environ.get(
        "ROUTER_STATE_DIR", Path.home() / ".hermes" / "model-router"))
    return root / STATE_FILENAME


def load_state(state_dir: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    path = state_path(state_dir)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"updated": None, "lanes": {}}
    if not isinstance(value, dict) or not isinstance(value.get("lanes"), dict):
        return {"updated": None, "lanes": {}}
    return value


def record_key(provider: str, model: str) -> str:
    return f"{provider}/{model}"


def save_result(result: dict[str, Any], state_dir: str | os.PathLike[str] | None = None) -> Path:
    """Merge one lane result atomically; unrelated lane evidence survives."""
    path = state_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    state = load_state(state_dir)
    state.setdefault("lanes", {})[record_key(result["provider"], result["model"])] = result
    state["updated"] = utc_now()
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    return path


def main(argv: Any = None) -> int:
    parser = argparse.ArgumentParser(description="Probe one router lane for prompt fidelity")
    parser.add_argument("--url", required=True, help="OpenAI-compatible base URL, normally ending in /v1")
    parser.add_argument("--provider", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--state-dir", default=None)
    parser.add_argument("--api-key-env", default=None)
    parser.add_argument("--prompt-chars", type=int, default=DEFAULT_PROMPT_CHARS)
    parser.add_argument("--tolerance-tokens", type=int, default=DEFAULT_TOLERANCE_TOKENS)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S)
    args = parser.parse_args(argv)
    key = os.environ.get(args.api_key_env) if args.api_key_env else None
    result = probe_lane(args.url, args.provider, args.model, api_key=key,
                        prompt_chars=args.prompt_chars,
                        tolerance_tokens=args.tolerance_tokens,
                        timeout_s=args.timeout)
    path = save_result(result, args.state_dir)
    print(json.dumps({"result": result, "state_path": str(path)}, sort_keys=True))
    return 0 if result["status"] == "OK" else 1


if __name__ == "__main__":
    raise SystemExit(main())
