"""TR-207 — quota L1 rung-1 header readback adapters (contract + honesty).

The quota-layers spec (docs/quota-layers-spec.md §3, §6, §7) makes rung 1 of
the readback ladder: response headers on calls we already make, read through
one adapter per header MECHANISM (never per provider), recognized names loaded
from the L0 table (readback_kind == "header" rows — nothing hard-coded), and
an EXPLICIT fall-through when no header answers (basis derived-from-ledger,
never a silent skip). This file pins the contract with RECORDED-RESPONSE
fixtures only — zero live calls, CI-safe (a source scan below proves it):

  - one fixture per mechanism: anthropic unified, groq x-ratelimit,
    sambanova day-window variant, fireworks limit-tokens-prompt variant,
    openai-codex percent style, retry-after-only 429;
  - case-insensitive lookup (real responses case headers arbitrarily);
  - unknown headers IGNORED — never guessed into an observation;
  - observation shape exactly per spec §3 (source_rung "header", exact header
    names in source_detail, observed_at ISO-8601 with tz);
  - honesty: a 0.42 utilization reading never fabricates a remaining-token
    number; unknown stays None, never 0; confidence travels with the number;
  - the fall-through names the next rungs and emits no observation.
"""

import ast
import datetime
import json
import os
import re
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import router_quota_readback as rq  # noqa: E402

NOW = datetime.datetime(2026, 10, 2, 12, 0, 0, tzinfo=datetime.timezone.utc)
FROZEN = NOW.isoformat()

# One fixture per MECHANISM — recorded-response shape, no live call.
ANTHROPIC_UNIFIED = {
    "anthropic-ratelimit-unified-5h-utilization": "0.42",
    "anthropic-ratelimit-unified-7d-utilization": "0.15",
    "anthropic-ratelimit-unified-status": "allowed",
    "anthropic-ratelimit-unified-reset": "2026-10-02T18:00:00Z",
}
GROQ_XRATELIMIT = {
    "x-ratelimit-limit-requests": "30",
    "x-ratelimit-remaining-requests": "27",
    "x-ratelimit-reset-requests": "2s",
    "x-ratelimit-limit-tokens": "8000",
    "x-ratelimit-remaining-tokens": "6500",
    "x-ratelimit-reset-tokens": "1m",
    "retry-after": "8",
}
SAMBANOVA_DAY = {
    "x-ratelimit-limit-requests-day": "1500",
    "x-ratelimit-remaining-requests-day": "1497",
    "x-ratelimit-reset-requests-day": "4h17m",
}
FIREWORKS_LIMIT_ONLY = {
    "x-ratelimit-limit-tokens-prompt": "21600000",
    "x-ratelimit-limit-tokens-cache-adjusted-prompt": "5400000",
    "x-ratelimit-limit-tokens-generated": "216000",
}
OPENAI_CODEX = {
    "x-codex-primary-used-percent": "37.5",
    "x-codex-primary-window-minutes": "300",
    "x-codex-primary-reset-at": "1791326400",
    "x-codex-secondary-used-percent": "11",
    "x-codex-secondary-window-minutes": "10080",
    "x-codex-secondary-reset-at": "1791931200",
    "x-codex-credits-has-credits": "true",
    "x-codex-credits-unlimited": "false",
    "x-codex-credits-balance": "120.5",
}
RETRY_AFTER_429 = {"retry-after": "45"}


def _read(headers, provider="groq", **kw):
    kw.setdefault("now", NOW)
    return rq.read_observation(provider, headers, **kw)


class NoLiveCalls(unittest.TestCase):
    """AC2: the new files must contain zero network entry points."""

    def test_no_network_imports_or_calls(self):
        # Grep imports/call-sites for every network entry point, skipping
        # matches inside comments/strings (header names contain the substring
        # "requests"). Docstrings and header constants live in those.
        pattern = re.compile(
            r"\b(requests|httpx|urllib\.request|urlopen|http\.client|socket)\b"
        )
        for path in ("scripts/router_quota_readback.py", "tests/test_quota_readback.py"):
            with open(os.path.join(REPO, path), encoding="utf-8") as fh:
                tree = ast.parse(fh.read())
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                elif isinstance(node, ast.Call):
                    names = [ast.dump(node.func) if isinstance(node.func, ast.Name) else ""]
                for name in names:
                    self.assertEqual(
                        pattern.findall(name),
                        [],
                        "%s references a network entry point: %s" % (path, name),
                    )


class CaseInsensitiveLookup(unittest.TestCase):
    def test_mixed_case_headers_are_recognized(self):
        scrambled = {
            "X-RateLimit-Remaining-Requests": "27",
            "X-RATELIMIT-LIMIT-REQUESTS": "30",
            "ANTHROPIC-RATELIMIT-UNIFIED-5H-UTILIZATION": "0.42",
            "X-Codex-Primary-Used-Percent": "10",
        }
        observations, next_rung = _read(scrambled)
        self.assertIsNone(next_rung)
        mechanisms = {o["source_detail"]["mechanism"] for o in observations}
        self.assertEqual(
            mechanisms, {"x-ratelimit", "anthropic-ratelimit-unified", "x-codex-percent"}
        )


class PerMechanismFixtures(unittest.TestCase):
    def test_anthropic_unified_utilization_stays_honest(self):
        observations, next_rung = _read(ANTHROPIC_UNIFIED, provider="xkiro")
        self.assertIsNone(next_rung)
        self.assertEqual(len(observations), 2)
        kinds = {o["window_kind"]: o for o in observations}
        self.assertEqual(set(kinds), {"rolling_5h", "rolling_daily"})
        for obs in kinds.values():
            # Utilization is a fraction used, not a remaining count — never
            # fabricate one (spec §6).
            self.assertIsNone(obs["remaining"])
            self.assertIsNone(obs["limit"])
            self.assertEqual(obs["unit"], "unknown")
            self.assertEqual(obs["confidence"], "partial")
            self.assertEqual(obs["source_detail"]["mechanism"], "anthropic-ratelimit-unified")
        self.assertEqual(
            kinds["rolling_5h"]["source_detail"]["utilization"],
            {"anthropic-ratelimit-unified-5h-utilization": "0.42"},
        )
        self.assertEqual(kinds["rolling_5h"]["source_detail"]["status"], "allowed")
        self.assertEqual(kinds["rolling_5h"]["source_detail"]["reset"], "2026-10-02T18:00:00Z")

    def test_groq_x_ratelimit_requests_and_tokens(self):
        observations, next_rung = _read(GROQ_XRATELIMIT)
        self.assertIsNone(next_rung)
        # requests window + tokens window + the fixture's own retry-after
        # (a real 429/200 response legitimately carries all three).
        self.assertEqual(len(observations), 3)
        by_unit = {o["unit"]: o for o in observations}
        self.assertEqual(set(by_unit), {"requests", "tokens_per_minute", "unknown"})
        requests = by_unit["requests"]
        self.assertEqual(requests["window_kind"], "per_minute")
        self.assertEqual(requests["remaining"], 27)
        self.assertEqual(requests["limit"], 30)
        self.assertEqual(requests["confidence"], "verified")
        tokens = by_unit["tokens_per_minute"]
        self.assertEqual(tokens["remaining"], 6500)
        self.assertEqual(tokens["limit"], 8000)
        self.assertEqual(tokens["confidence"], "verified")
        self.assertEqual(tokens["source_detail"]["reset_tokens"], "1m")
        throttle = by_unit["unknown"]
        self.assertEqual(throttle["source_detail"]["mechanism"], "retry-after")
        self.assertEqual(throttle["source_detail"]["retry_after_seconds"], 8)

    def test_sambanova_day_window_variant(self):
        observations, next_rung = _read(SAMBANOVA_DAY, provider="sambanova")
        self.assertIsNone(next_rung)
        self.assertEqual(len(observations), 1)
        obs = observations[0]
        self.assertEqual(obs["window_kind"], "rolling_daily")
        self.assertEqual(obs["remaining"], 1497)
        self.assertEqual(obs["limit"], 1500)
        self.assertEqual(obs["unit"], "requests")
        self.assertEqual(obs["confidence"], "verified")
        self.assertIn(
            "x-ratelimit-remaining-requests-day", obs["source_detail"]["headers"]
        )

    def test_fireworks_limit_only_family(self):
        observations, next_rung = _read(FIREWORKS_LIMIT_ONLY, provider="fireworks-ai")
        self.assertIsNone(next_rung)
        self.assertEqual(len(observations), 1)
        obs = observations[0]
        self.assertEqual(obs["window_kind"], "per_minute")
        self.assertIsNone(obs["remaining"])  # no remaining header is documented
        self.assertEqual(obs["limit"], 21600000)  # prompt-tier figure
        self.assertEqual(obs["confidence"], "partial")
        detail = obs["source_detail"]
        self.assertTrue(detail["limit_only_family"])
        self.assertEqual(detail["limit_tokens_cache_adjusted_prompt"], 5400000)
        self.assertEqual(detail["limit_tokens_generated"], 216000)

    def test_openai_codex_percent_style(self):
        observations, next_rung = _read(OPENAI_CODEX, provider="openai-codex")
        self.assertIsNone(next_rung)
        self.assertEqual(len(observations), 3)  # primary + secondary + credits
        windows = {o["window_kind"]: o for o in observations}
        primary = windows["rolling_5h"]
        self.assertEqual(primary["remaining"], 62.5)  # 100 - used
        self.assertIsNone(primary["limit"])  # percent-only: no absolute cap
        self.assertEqual(primary["unit"], "percent")
        self.assertEqual(primary["confidence"], "partial")
        self.assertEqual(primary["source_detail"]["used_percent"], 37.5)
        self.assertEqual(primary["source_detail"]["window_minutes"], 300)
        self.assertEqual(primary["source_detail"]["reset_at_raw"], "1791326400")
        self.assertEqual(
            primary["source_detail"]["resets_at"],
            datetime.datetime.fromtimestamp(1791326400, tz=datetime.timezone.utc).isoformat(),
        )
        secondary = windows["weekly"]
        self.assertEqual(secondary["remaining"], 89)
        self.assertEqual(
            secondary["source_detail"]["window_minutes"], 10080
        )
        credits = windows["pool"]
        self.assertEqual(credits["unit"], "credits")
        self.assertEqual(credits["remaining"], 120.5)
        self.assertEqual(credits["confidence"], "verified")
        self.assertFalse(credits["source_detail"]["unlimited"])

    def test_retry_after_only_429(self):
        observations, next_rung = _read(RETRY_AFTER_429)
        self.assertIsNone(next_rung)
        self.assertEqual(len(observations), 1)
        obs = observations[0]
        self.assertIsNone(obs["window_kind"])  # the throttled window is unnamed
        self.assertIsNone(obs["remaining"])
        self.assertIsNone(obs["limit"])
        self.assertEqual(obs["unit"], "unknown")
        self.assertEqual(obs["confidence"], "partial")
        self.assertEqual(obs["source_detail"]["retry_after_seconds"], 45)
        self.assertEqual(obs["source_detail"]["retry_after_raw"], "45")


class LadderAndFallThrough(unittest.TestCase):
    def test_fall_through_names_next_rungs_and_emits_nothing(self):
        observations, next_rung = _read(
            {"content-type": "application/json", "x-request-id": "abc"},
            provider="deepseek",
        )
        self.assertEqual(observations, [])
        self.assertIsInstance(next_rung, dict)
        self.assertTrue(next_rung["fall_through"])
        self.assertEqual(next_rung["reason"], "no-recognized-quota-headers")
        self.assertEqual(next_rung["basis"], "derived-from-ledger")
        self.assertTrue(
            any("rung-2" in r for r in next_rung["next_rungs"]),
            next_rung["next_rungs"],
        )
        self.assertTrue(
            any("rung-4" in r for r in next_rung["next_rungs"]),
            next_rung["next_rungs"],
        )

    def test_empty_headers_fall_through(self):
        observations, next_rung = _read({}, provider="zai-glm")
        self.assertEqual(observations, [])
        self.assertEqual(next_rung["basis"], "derived-from-ledger")
        self.assertEqual(next_rung["headers_present_unrecognized"], [])

    def test_unknown_headers_never_guessed_into_observations(self):
        observations, next_rung = _read(
            {"x-my-quota-left": "999", "x-fake-remaining": "42"}
        )
        self.assertEqual(observations, [])
        self.assertTrue(next_rung["fall_through"])
        self.assertEqual(
            sorted(next_rung["headers_present_unrecognized"]),
            ["x-fake-remaining", "x-my-quota-left"],
        )

    def test_unknown_header_alongside_known_ones_is_ignored(self):
        observations, next_rung = _read(
            {"x-my-quota-left": "999", "x-ratelimit-limit-requests": "30",
             "x-ratelimit-remaining-requests": "29"}
        )
        self.assertIsNone(next_rung)
        self.assertEqual(len(observations), 1)
        self.assertNotIn("x-my-quota-left", observations[0]["source_detail"]["headers"])


class KnownNamesFromTable(unittest.TestCase):
    def test_every_declared_header_row_name_is_recognized(self):
        known = rq.known_header_names()
        table = os.path.join(REPO, "data", "tables", "provider_quota.jsonl")
        with open(table, encoding="utf-8") as fh:
            rows = [json.loads(line) for line in fh if line.strip()]
        header_rows = [r for r in rows if r.get("readback_kind") == "header"]
        self.assertGreater(len(header_rows), 0)
        checked = 0
        for row in header_rows:
            exact = row.get("readback_exact") or ""
            text = (
                json.dumps(exact)
                if isinstance(exact, list)
                else str(exact)
            )
            for token in re.findall(r"[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)+", text):
                if (
                    "ratelimit" in token.lower()
                    or token.lower() == "retry-after"
                    or token.lower().startswith("x-codex-")
                ):
                    self.assertIn(
                        token.lower(), known, "table-declared header not recognized"
                    )
                    checked += 1
        self.assertGreater(checked, 20)

    def test_module_uses_table_not_hardcoded_provider_lists(self):
        # Header NAMES are quoted verbatim in the mechanism vocabularies
        # (that is the recorded-contract design); provider IDS must not be.
        # Scan code tokens only — comments and docstrings may name the rows
        # the names came from.
        src = os.path.join(REPO, "scripts", "router_quota_readback.py")
        with open(src, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        code_tokens = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                code_tokens.add(node.id.lower())
            elif isinstance(node, ast.arg):
                code_tokens.add(node.arg.lower())
        for provider in ("groq", "sambanova", "fireworks", "openai-codex", "xkiro", "deepseek"):
            self.assertNotIn(
                provider,
                code_tokens,
                "hard-coded provider id %r found in code identifiers" % provider,
            )


class ObservationShape(unittest.TestCase):
    def test_keys_exactly_per_spec(self):
        observations, next_rung = _read(GROQ_XRATELIMIT)
        self.assertIsNone(next_rung)
        for obs in observations:
            self.assertEqual(
                sorted(obs.keys()),
                sorted(rq.SPEC_OBSERVATION_KEYS),
            )
            self.assertEqual(obs["source_rung"], "header")
            self.assertEqual(obs["provider_id"], "groq")
            self.assertEqual(obs["account"], "default")
            self.assertIsInstance(obs["source_detail"], dict)
            self.assertIsInstance(obs["source_detail"]["headers"], list)
            self.assertTrue(obs["source_detail"]["headers"])

    def test_exact_header_names_recorded_as_seen(self):
        scrambled = {
            "X-RateLimit-Remaining-Tokens": "6500",
            "X-RateLimit-Limit-Tokens": "8000",
        }
        observations, _ = _read(scrambled)
        self.assertEqual(
            observations[0]["source_detail"]["headers"],
            ["X-RateLimit-Remaining-Tokens", "X-RateLimit-Limit-Tokens"],
        )

    def test_observed_at_iso_with_tz_and_injectable(self):
        observations, _ = _read(GROQ_XRATELIMIT)
        for obs in observations:
            stamp = datetime.datetime.fromisoformat(obs["observed_at"])
            self.assertIsNotNone(stamp.utcoffset())
        fixed = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
        obs, rung = rq.read_observation("p", GROQ_XRATELIMIT, now=fixed)
        for o in obs:
            self.assertEqual(o["observed_at"], "2026-01-01T00:00:00+00:00")

    def test_adapter_level_observed_at_injection(self):
        obs = rq.read_x_ratelimit(
            GROQ_XRATELIMIT, provider_id="p", observed_at="2026-05-05T05:05:05+00:00"
        )
        self.assertTrue(obs)
        for o in obs:
            self.assertEqual(o["observed_at"], "2026-05-05T05:05:05+00:00")

    def test_utilization_honesty_no_fabricated_remaining(self):
        observations, _ = _read(
            {"anthropic-ratelimit-unified-5h-utilization": "0.42"}, provider="xkiro"
        )
        self.assertEqual(len(observations), 1)
        obs = observations[0]
        self.assertIsNone(obs["remaining"], "a utilization fraction must not become a count")
        self.assertIsNone(obs["limit"])
        self.assertEqual(obs["source_detail"]["utilization"], {
            "anthropic-ratelimit-unified-5h-utilization": "0.42"
        })

    def test_unlimited_credits_never_read_as_zero(self):
        observations, _ = _read(
            {"x-codex-credits-unlimited": "true"}, provider="openai-codex"
        )
        self.assertEqual(len(observations), 1)
        self.assertIsNone(observations[0]["remaining"])
        self.assertEqual(observations[0]["source_detail"]["unlimited"], True)

    def test_no_remaining_and_no_limit_is_never_zero(self):
        for fixture in (FIREWORKS_LIMIT_ONLY, RETRY_AFTER_429):
            observations, _ = _read(fixture)
            for obs in observations:
                if obs["limit"] is None and obs["remaining"] is None:
                    self.assertNotEqual(obs["remaining"], 0)
                self.assertNotEqual(obs["confidence"], "verified")


class NonNumericTolerance(unittest.TestCase):
    def test_unparseable_numbers_land_as_unparsed_not_zero(self):
        observations, _ = _read(
            {"x-ratelimit-remaining-requests": "soon", "x-ratelimit-limit-requests": "30"}
        )
        self.assertEqual(len(observations), 1)
        obs = observations[0]
        self.assertIsNone(obs["remaining"])
        self.assertEqual(obs["source_detail"]["remaining_unparsed"], "soon")
        self.assertEqual(obs["limit"], 30)
        self.assertEqual(obs["confidence"], "partial")

    def test_non_numeric_retry_after_preserved_verbatim(self):
        observations, _ = _read({"retry-after": "Fri, 02 Oct 2026 12:01:00 GMT"})
        obs = observations[0]
        self.assertNotIn("retry_after_seconds", obs["source_detail"])
        self.assertEqual(
            obs["source_detail"]["retry_after_raw"], "Fri, 02 Oct 2026 12:01:00 GMT"
        )


class Cli(unittest.TestCase):
    def _run_cli(self, headers, *extra):
        proc = __import__("subprocess").run(
            [
                sys.executable,
                os.path.join(REPO, "scripts", "router_quota_readback.py"),
                *extra,
            ],
            input=json.dumps(headers),
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        return proc

    def test_cli_parses_recorded_headers_from_stdin(self):
        proc = self._run_cli(OPENAI_CODEX, "--provider", "openai-codex")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual(len(out["observations"]), 3)
        self.assertNotIn("fall_through", out)

    def test_cli_fall_through_exit_zero(self):
        proc = self._run_cli({"server": "nginx"})
        self.assertEqual(proc.returncode, 0)
        out = json.loads(proc.stdout)
        self.assertEqual(out["observations"], [])
        self.assertTrue(out["fall_through"]["fall_through"])

    def test_cli_bad_input_exit_2(self):
        proc = self._run_cli([1, 2, 3])
        self.assertEqual(proc.returncode, 2)


if __name__ == "__main__":
    unittest.main()
