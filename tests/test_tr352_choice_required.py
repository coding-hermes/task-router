"""TR-352: explicit unavailable model choices must fail closed."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts"))
import router_server as rsrv  # noqa: E402


def _resolved(*hops, **extra):
    return {"chain": [
        {"hop": i + 1, "provider": provider, "model": model}
        for i, (provider, model) in enumerate(hops)
    ], **extra}


def test_explicit_unavailable_model_returns_choice_required(monkeypatch):
    """An unavailable explicit request cannot fall through to the chain head."""
    monkeypatch.setattr(
        rsrv,
        "_subprocess_text",
        lambda *args, **kwargs: json.dumps(_resolved(("other", "available"))),
    )

    resolution = rsrv._proxy_chain({"requested_model": "missing-model"})

    assert resolution["choice_required"] is True
    assert resolution["error"] == "choice_required"
    assert resolution["missing_capability"] == "missing-model"
    assert resolution["requested_model"] == "missing-model"
    assert resolution["resolution"]["chain"][0]["model"] == "available"


def test_resolution_skip_evidence_is_preserved(monkeypatch):
    """Recommended missing entries may skip, but the resolver must say so."""
    payload = _resolved(
        ("p2", "next-model"),
        resolution_skips=[{
            "model": "missing-recommendation",
            "outcome": "skipped",
            "reason": "not confirmed active",
        }],
    )
    monkeypatch.setattr(rsrv, "_subprocess_text", lambda *args, **kwargs: json.dumps(payload))

    resolution = rsrv._proxy_chain({})

    assert resolution["resolution_skips"] == payload["resolution_skips"]
    assert resolution["chain"][0]["model"] == "next-model"


def test_proxy_returns_choice_required_without_calling_upstream(monkeypatch, tmp_path):
    """The HTTP-facing proxy refuses before any unrelated model is attempted."""
    monkeypatch.setenv("ROUTING_OUTCOMES_FILE", str(tmp_path / "outcomes.jsonl"))
    monkeypatch.setenv("ROUTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(rsrv, "_proxy_requirements", lambda *args: (
        "declared", {"profile_id": "P1_CODING", "matrix": None,
                      "problems": [], "requested_model": "missing-model"}))
    monkeypatch.setattr(rsrv, "_proxy_chain", lambda requirements, **kwargs: {
        "choice_required": True,
        "error": "choice_required",
        "requested_model": "missing-model",
        "missing_capability": "missing-model",
        "resolution": {"chain": [{"provider": "p2", "model": "unrelated"}]},
    })
    called = []

    def upstream(*args):
        called.append(args)
        return 200, {"choices": [{"message": {"content": "wrong lane"}}]}

    status, payload = rsrv.proxy_chat(
        "/v1/chat/completions", {"messages": []}, {}, upstream=upstream)

    assert status == 409
    assert payload["error"] == "choice_required"
    assert payload["missing_capability"] == "missing-model"
    assert called == []
