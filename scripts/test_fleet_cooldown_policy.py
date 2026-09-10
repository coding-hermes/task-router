#!/usr/bin/env python3
"""Regression tests for fleet-cooldown-policy.py.

Two regression families:
  1) Namespace preservation (2026-08-27): write_fleet_pins() regenerated
     fleet.toml from the projects API only — [[namespaces]] blocks were
     NEVER emitted, so the coding-hermes default_prompt block and any
     namespace-level config were silently clobbered on every --apply run.
  2) Dynamic-model emission (2026-08-28, Bane): the regen baked
     `model = "deepseek-v4-flash"` / `provider = "deepseek-foreman"` into
     every project lacking an explicit pin — re-pinning the whole fleet to
     one hardcoded lane and SHADOWING the task-router's dynamic resolution
     at spawn. fleet.toml only carries a pin when the operator set one.

Run: ~/.hermes/venvs/board/bin/python3 test_fleet_cooldown_policy.py
"""
import importlib.util
import os
import sys
import tempfile
import tomllib
import unittest

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "fleet-cooldown-policy.py")

spec = importlib.util.spec_from_file_location("fleet_cooldown_policy", SCRIPT)
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)

# The script self-re-execs into the board venv when duckdb is missing.
# Under the board venv python (per the run line above) it imports cleanly.


def sample_namespaces():
    return [
        {
            "id": "coding-hermes",
            "weight": 100,
            "reserved": 70,
            "hard_cap": 100,
            "max_concurrent": 0,
            "enabled": True,
            "description": "Foreman fleet",
            "default_prompt": "You are the foreman.\nLoad skills at start.\nMulti-line\nbody.",
            "model_chain": "",
        },
        {
            "id": "duckbrain-sync",
            "weight": 5,
            "reserved": 1,
            "hard_cap": 100,
            "max_concurrent": 2,
            "enabled": True,
            "description": "DuckBrain namespace sync jobs (Bane 2026-08-27)",
            "default_prompt": "You are a DuckBrain sync tick.\nProtocol:\n1. test-write\n2. scan\n3. verify.",
            "model_chain": '["deepseek-v4-flash@deepseek"]',
        },
    ]


def sample_projects():
    return [
        {
            "name": "helix",
            "repo_url": "https://example.com/helix",
            "workdir": "/home/kara/helix",
            "weight": 10,
            "priority": 5,
            "cooldown_s": 7200,
            "model": "deepseek-v4-flash",
            "provider": "deepseek-foreman",
            "namespace_id": "coding-hermes",
            "enabled": True,
        },
        {
            "name": "blog-sync",
            "repo_url": "local:/home/kara/.hermes/sync-workdirs/blog-sync",
            "workdir": "/home/kara/.hermes/sync-workdirs/blog-sync",
            "weight": 1,
            "priority": 5,
            "cooldown_s": 21600,
            "model": "deepseek-v4-flash",
            "provider": "deepseek-foreman",
            "namespace_id": "duckbrain-sync",
            "deliver": "telegram:-1003310984808:87792",
            "enabled": True,
        },
    ]


class _TomlWriter:
    """Redirect write_fleet_pins' fleet.toml write to a temp file."""

    def __init__(self):
        tmp = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False,
                                          encoding="utf-8")
        self.tmp_path = tmp.name
        tmp.close()

    def render(self, projects, namespaces):
        import builtins

        real_path = os.path.expanduser("~/.hermes/fleet.toml")
        original_open = builtins.open

        def spy_open(path, mode="r", *a, **k):
            if os.path.expanduser(path) == real_path and "w" in mode:
                return original_open(self.tmp_path, mode, *a, **k)
            return original_open(path, mode, *a, **k)

        builtins.open = spy_open
        try:
            policy.write_fleet_pins(projects, namespaces=namespaces)
        finally:
            builtins.open = original_open
        with open(self.tmp_path, encoding="utf-8") as f:
            return f.read()


class NamespacePreservationTest(unittest.TestCase):
    def test_emits_namespace_blocks(self):
        """REGRESSION (2026-08-27): regen must emit [[namespaces]] blocks.

        Before the fix, write_fleet_pins produced zero namespaces — this
        clobbered the coding-hermes default_prompt and duckbrain-sync
        config. A namespace list passed in must appear verbatim in the
        generated TOML.
        """
        ns = sample_namespaces()
        text = _TomlWriter().render(sample_projects(), ns)
        data = tomllib.loads(text)
        got = {n["id"] for n in data.get("namespaces", [])}
        self.assertEqual(got, {"coding-hermes", "duckbrain-sync"},
                         "namespaces missing from regenerated fleet.toml")

    def test_namespace_fields_roundtrip(self):
        """Every namespace-level field survives the regen (prompt, chain, cap)."""
        ns = sample_namespaces()
        text = _TomlWriter().render(sample_projects(), ns)
        data = tomllib.loads(text)
        by_id = {n["id"]: n for n in data["namespaces"]}

        ch = by_id["coding-hermes"]
        self.assertEqual(ch["weight"], 100)
        self.assertEqual(ch["reserved"], 70)
        self.assertEqual(ch["hard_cap"], 100)
        self.assertEqual(ch["max_concurrent"], 0)
        self.assertEqual(ch["enabled"], True)
        self.assertIn("Load skills at start", ch["default_prompt"])

        ds = by_id["duckbrain-sync"]
        self.assertEqual(ds["max_concurrent"], 2, "max_concurrent must survive")
        self.assertEqual(ds["model_chain"], ["deepseek-v4-flash@deepseek"],
                         "model_chain must survive as a TOML array")
        self.assertIn("test-write", ds["default_prompt"])
        self.assertIn("Bane 2026-08-27", ds["description"])

    def test_default_prompt_multiline_roundtrip(self):
        """Multi-line prompts with quotes survive triple-single-quoted emission."""
        long_prompt = ("You are a DuckBrain sync tick.\n"
                       "Protocol step 1: test-write the rotating key.\n"
                       'Domains: "person" | "event" | "config".\n'
                       "Step 4: write /sync/last-run LAST.\n")
        ns = [{
            "id": "prompt-ns", "weight": 10, "reserved": 1, "hard_cap": 100,
            "max_concurrent": 1, "enabled": True, "description": "",
            "default_prompt": long_prompt, "model_chain": "",
        }]
        text = _TomlWriter().render(sample_projects(), ns)
        data = tomllib.loads(text)
        self.assertEqual(data["namespaces"][0]["default_prompt"], long_prompt)

    def test_regenerated_fleet_toml_parses_with_projects(self):
        """The full regen output (namespaces + projects) is valid TOML and
        projects keep their namespace_id wiring."""
        ns = sample_namespaces()
        text = _TomlWriter().render(sample_projects(), ns)
        data = tomllib.loads(text)
        self.assertEqual(len(data["projects"]), 2)
        by_name = {p["name"]: p for p in data["projects"]}
        self.assertEqual(by_name["blog-sync"]["namespace_id"], "duckbrain-sync")
        self.assertEqual(by_name["blog-sync"]["cooldown_s"], 21600)
        self.assertEqual(by_name["helix"]["namespace_id"], "coding-hermes")


class DynamicModelEmissionTest(unittest.TestCase):
    """REGRESSION (2026-08-28, Bane): the regen must NEVER bake a model
    default. write_fleet_pins used to emit `model = "deepseek-v4-flash"` /
    `provider = "deepseek-foreman"` for every project lacking an explicit
    pin — re-pinning the whole fleet to one hardcoded lane and SHADOWING
    the task-router's dynamic resolution at spawn. The task-router decides;
    fleet.toml only carries an explicit pin when the operator set one."""

    def test_unset_model_stays_unset(self):
        """A project with NO model/provider must get NO model/provider lines
        — the router resolves at spawn; a hardcoded default is the bug."""
        project = {
            "name": "blog-sync", "repo_url": "local:/x",
            "workdir": "/x", "weight": 1, "priority": 5,
            "cooldown_s": 21600, "namespace_id": "duckbrain-sync",
            "enabled": True,
        }
        text = _TomlWriter().render([project], None)
        data = tomllib.loads(text)
        p = data["projects"][0]
        self.assertNotIn("model", p, "regen baked a model default — dynamic "
                                     "resolution is shadowed (Bane 2026-08-28)")
        self.assertNotIn("provider", p,
                         "regen baked a provider default — dynamic "
                         "resolution is shadowed (Bane 2026-08-28)")

    def test_explicit_pin_is_preserved(self):
        """An EXPLICIT operator pin still round-trips (overrides are legal;
        defaults are not)."""
        project = {
            "name": "helix", "repo_url": "https://example.com/helix",
            "workdir": "/home/kara/helix", "weight": 10, "priority": 5,
            "cooldown_s": 7200, "model": "glm-5.3-flash",
            "provider": "zai-glm", "namespace_id": "coding-hermes",
            "enabled": True,
        }
        text = _TomlWriter().render([project], None)
        data = tomllib.loads(text)
        p = data["projects"][0]
        self.assertEqual(p["model"], "glm-5.3-flash")
        self.assertEqual(p["provider"], "zai-glm")


class AdaptiveCooldownArmingTest(unittest.TestCase):
    """SCHED-GAP-1 (Bane 2026-09-04, shipped 2026-09-09): the speed-control
    money lever. Foreman-lane projects must be ARMED in every regen —
    adaptive_cooldown=true with an EXPLICIT 8x ceiling (the scheduler
    defaults an absent ceiling to 604800s = 7 days, which is NOT 8x).
    Other lanes stay off: arming sync/qa/pm now would only mask upstream
    outages as slow ticks until the upstream-quiet signal ships."""

    def test_foreman_lane_armed_with_explicit_ceiling(self):
        project = {
            "name": "helix", "repo_url": "https://example.com/helix",
            "workdir": "/home/kara/helix", "weight": 10, "priority": 5,
            "cooldown_s": 7200, "namespace_id": "coding-hermes",
            "enabled": True,
        }
        p = tomllib.loads(_TomlWriter().render([project], None))["projects"][0]
        self.assertTrue(p.get("adaptive_cooldown"),
                        "foreman-lane project left unarmed — adaptive_cooldown pin absent")
        self.assertEqual(p.get("cooldown_ceiling_s"), 57600,
                         "ceiling must be explicit 8x cooldown (7200*8); "
                         "an absent key defaults to 604800 in the loader")

    def test_non_foreman_lane_stays_disarmed(self):
        for ns in ("duckbrain-sync", "qa", "pm", None):
            project = {
                "name": "blog-sync", "repo_url": "local:/x",
                "workdir": "/x", "weight": 1, "priority": 5,
                "cooldown_s": 21600, "enabled": True,
            }
            if ns:
                project["namespace_id"] = ns
            p = tomllib.loads(_TomlWriter().render([project], None))["projects"][0]
            self.assertFalse(p.get("adaptive_cooldown", False),
                             f"lane {ns!r} must stay disarmed until "
                             f"upstream-quiet signal ships")
            self.assertNotIn("cooldown_ceiling_s", p)


if __name__ == "__main__":
    unittest.main(verbosity=2)
