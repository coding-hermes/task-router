"""TR-101 — JEV as the second (cheap) complexity scorer.

Contracts pinned here (each one is a Bane requirement of 2026-09-20):
  1. JEV's answer is a SCORE on a documented scale (0..2, 1 = middle) and the
     scale is DATA (bands file), not code.
  2. score -> band -> MATRIX: the output is the same matrix shape the classifier
     returns, validated by the SAME validator, so both scorers cannot drift.
  3. FAIL-CLOSED and VISIBLE: no key / transport error / malformed answer /
     out-of-scale score each produce matrix=None (or a clamp warning) with the
     reason in `problems` — never a silent default lane.
  4. The old priority: a DECLARED profile still skips scoring entirely.
  5. Cost is recorded: every result carries the JEV usage cost (input cheap,
     output free) so the cheap-scorer claim stays measurable.
"""
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))

import router_jev  # noqa: E402

BANDS = [
    {"band": "low", "score_min": 0.0, "score_max": 0.6666666, "title": "trivial",
     "levels": {"code_gen": -3}, "note": "cheap lanes"},
    {"band": "mid", "score_min": 0.6666666, "score_max": 1.3333333, "title": "moderate",
     "levels": {"code_gen": -1, "test": 0}, "note": "mid lanes"},
    {"band": "high", "score_min": 1.3333333, "score_max": 2.0000001, "title": "hard",
     "levels": {"debug": 1, "test": 1}, "note": "strong lanes"},
]


def fake_http(score, confidence=0.8, cost=1.5e-05, status=200, body=None):
    def _call(url, payload, headers, timeout):
        if body is not None:
            return status, body
        return status, {"answers": {"hardness": {"type": "score", "score": score,
                                                 "probabilities": {"0": 0.1, "1": 0.2, "2": 0.7},
                                                 "confidence": confidence}},
                        "usage": {"cost": cost}, "model": "typesafe/jev-1.13-test"}
    return _call


# ------------------------------------------------------------- scale/data ---

def test_bands_are_data_and_covering():
    bands = router_jev.load_bands()
    assert bands, "band data file must not be empty"
    assert bands[0]["score_min"] == 0.0
    assert bands[-1]["score_max"] >= 2.0, "the 0..2 scale must be fully covered"


def test_band_boundaries_are_the_documented_middles():
    bands = BANDS
    assert router_jev.band_for(0.0, bands)["band"] == "low"
    assert router_jev.band_for(0.6, bands)["band"] == "low"
    assert router_jev.band_for(0.9, bands)["band"] == "mid"      # 1 is the middle
    assert router_jev.band_for(1.33, bands)["band"] == "mid"
    assert router_jev.band_for(1.5, bands)["band"] == "high"
    assert router_jev.band_for(2.0, bands)["band"] == "high"     # top closes


# ------------------------------------------------------ score -> matrix ----

def test_score_to_matrix_uses_band_levels():
    matrix, band, problems = router_jev.score_to_matrix(1.8, bands=BANDS)
    assert band["band"] == "high"
    assert matrix == {"debug": 1, "test": 1}
    assert problems == []


def test_matrix_is_validated_by_the_classifier_validator():
    """Both scorers must emit matrices the same validator accepts — otherwise
    chain selection would surprise on one path only."""
    import router_classify as rc
    matrix, _, _ = router_jev.score_to_matrix(0.5, bands=BANDS)
    ok, conf, problems = rc.validate_matrix({"categories": matrix}, rc.registry_categories())
    assert ok == matrix, problems


def test_unknown_category_in_band_data_is_rejected():
    bad = [{"band": "x", "score_min": 0.0, "score_max": 2.0, "title": "x",
            "levels": {"not_a_category": 3}, "note": ""}]
    matrix, band, problems = router_jev.score_to_matrix(1.0, bands=bad)
    assert band["band"] == "x"
    assert matrix == {}, "unknown categories must be dropped, not passed through"
    assert any("unknown category" in p for p in problems)


# ------------------------------------------------------------ fail-closed ---

def test_out_of_scale_score_clamps_with_a_problem():
    matrix, band, problems = router_jev.score_to_matrix(3.7, bands=BANDS)
    assert band["band"] == "high"
    assert matrix == {"debug": 1, "test": 1}
    assert any("outside the configured scale" in p for p in problems)


def test_no_score_is_a_problem_not_a_guess():
    matrix, band, problems = router_jev.score_to_matrix(None, bands=BANDS)
    assert matrix is None and band is None
    assert problems == ["no JEV score"]


def test_transport_error_is_reported_and_no_matrix():
    res = router_jev.classify("anything", http=fake_http(None, status=500, body="boom"), bands=BANDS)
    assert res["matrix"] is None
    assert res["score"] is None
    assert any("HTTP 500" in p for p in res["problems"])


def test_malformed_answer_is_reported_and_no_matrix():
    res = router_jev.classify("anything", http=fake_http(None, body={"answers": {"hardness": {"type": "score"}}}),
                              bands=BANDS)
    assert res["matrix"] is None
    assert any("malformed" in p for p in res["problems"])


def test_missing_key_is_visible(monkeypatch):
    monkeypatch.setattr(router_jev, "key_candidates", lambda: [])
    res = router_jev.classify("anything", bands=BANDS)
    assert res["matrix"] is None
    assert any("no JEV key" in p for p in res["problems"])


def test_key_failover_uses_the_next_key():
    """The fleet's key sets contain expired keys — a 401 on the first key must
    fall through to the next one, and the result must say WHICH key worked."""
    calls = []

    def http(url, payload, headers, timeout):
        calls.append(headers["Authorization"])
        if len(calls) == 1:
            return 401, "API key expired"
        return 200, {"answers": {"hardness": {"type": "score", "score": 1.0, "confidence": 0.5}},
                     "usage": {"cost": 1e-05}, "model": "typesafe/jev-1.13-test"}

    import router_jev as rj
    orig = rj.key_candidates
    rj.key_candidates = lambda: [("OR_JEV", "k1"), ("OPENROUTER_API_KEY", "k2")]
    try:
        res = rj.classify("x", http=http, bands=BANDS)
    finally:
        rj.key_candidates = orig
    assert len(calls) == 2, "must have failed over"
    assert res["score"] == 1.0
    assert res["key_name"] == "OPENROUTER_API_KEY"


# ------------------------------------------------------ result contract ----

def test_classify_result_shape_matches_the_classifier_peer():
    res = router_jev.classify("x", http=fake_http(1.9), bands=BANDS)
    for key in ("matrix", "complexity_sig", "confidence", "problems", "model"):
        assert key in res, key
    assert res["scorer"] == "jev"
    assert res["matrix"] == {"debug": 1, "test": 1}
    assert res["complexity_sig"], "signature lets stats key on the same matrix"
    assert res["cost"] == 1.5e-05, "the cheap-scorer claim must stay measurable"
    assert res["band"] == "high" and res["band_title"] == "hard"
    assert res["problems"] == []


def test_complexity_sig_matches_classifier_path_for_same_matrix():
    """Same matrix from either scorer must produce the SAME stats key, or
    outcome rows would split between scoring paths."""
    import router_outcomes as ro
    res = router_jev.classify("x", http=fake_http(1.9), bands=BANDS)
    assert res["complexity_sig"] == ro.complexity_sig(res["matrix"])


# ----------------------------------------------------------- proxy wiring ---

def test_declared_profile_still_wins_over_the_scorer(monkeypatch):
    """Priority contract: x-router-profile skips scoring entirely (contract
    face), with the JEV scorer selected — the request must not call JEV."""
    sys.path.insert(0, os.path.join(REPO, "scripts"))
    import router_server as rs
    called = {"jev": False}
    import router_jev as rj
    monkeypatch.setattr(rj, "classify", lambda *a, **k: called.__setitem__("jev", True) or {"matrix": None})
    src, payload = rs._proxy_requirements({"messages": [{"role": "user", "content": "hi"}]},
                                         {"x-router-profile": "P1_CODING", "x-router-scorer": "jev"}, "/v1/chat/completions")
    assert src == "declared"
    assert called["jev"] is False


def test_proxy_selects_jev_scorer_from_the_header(monkeypatch):
    import router_server as rs
    import router_jev as rj
    monkeypatch.setattr(rj, "classify", lambda text, **k: {
        "scorer": "jev", "matrix": {"debug": 1}, "complexity_sig": "sig",
        "confidence": 0.9, "score": 1.5, "band": "high", "model": "typesafe/jev-1.13",
        "problems": [], "cost": 1e-05})
    src, payload = rs._proxy_requirements({"messages": [{"role": "user", "content": "debug this"}]},
                                         {"x-router-scorer": "jev"}, "/v1/chat/completions")
    assert src == "jev"
    assert payload["matrix"] == {"debug": 1}
    assert payload["score"] == 1.5 and payload["band"] == "high"


def test_proxy_degrades_visibly_when_jev_returns_no_matrix(monkeypatch):
    import router_server as rs
    import router_jev as rj
    monkeypatch.setattr(rj, "classify", lambda text, **k: {
        "scorer": "jev", "matrix": None, "complexity_sig": None, "confidence": None,
        "score": None, "model": "typesafe/jev-1.13", "problems": ["all 1 JEV key(s) failed; last: HTTP 401"]})
    src, payload = rs._proxy_requirements({"messages": [{"role": "user", "content": "x"}]},
                                         {"x-router-scorer": "jev"}, "/v1/chat/completions")
    assert src == "default"
    assert any("JEV key" in p for p in payload["problems"])
    assert payload["profile_id"] == "P0_FORE"


def test_proxy_scorer_defaults_to_classifier(monkeypatch):
    import router_server as rs
    monkeypatch.delenv("ROUTER_SCORER", raising=False)
    import router_classify as rc
    monkeypatch.setattr(rc, "classify", lambda text, **k: {
        "matrix": {"code_gen": -2}, "complexity_sig": "s2", "confidence": 0.7,
        "prompt_version": "v1", "model": "glm-5.3-flash", "problems": []})
    src, payload = rs._proxy_requirements({"messages": [{"role": "user", "content": "x"}]},
                                         {}, "/v1/chat/completions")
    assert src == "classifier"
    assert payload["matrix"] == {"code_gen": -2}
