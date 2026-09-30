"""TR-205 — every /api/v1/projects consumer must page past the first 200 lanes.

SCHED-GAP-1622 (scheduler commit c531082b, 2026-09-26) paginated
GET /api/v1/projects: default limit=200, max limit=500, response envelope
{"projects": [...], "total": N, "limit": L, "offset": O}. A bare GET
silently hides every lane past the first page — at the current fleet size
(~501 lanes) that is ~60% of the fleet invisible to fleet tooling, and an
--apply regen built on the truncated view REWRITES fleet.toml without the
hidden lanes' pins (this exact loss was proven live on 2026-09-27: the
regen dropped the role-lane pins alphabetically past the 200 cutoff).

Acceptance criteria under test:
  1. fleet-cooldown-policy.py follows ALL pages: limit=500, loop on the
     returned cursor (offset += len(rows)) until len(collected) >= total.
  2. every project-list call site goes through the paging helper (no bare
     /api/v1/projects GET left in the script).
  3. a mocked 3-page endpoint proves the caller collects all rows.
  4. the loop terminates on a degenerate server (stalled cursor / empty
     pages) — bounded pages, no unbounded retry.
"""

import importlib.util
import json
import os
import re
import unittest
import urllib.request
from unittest import mock

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                      "scripts", "fleet-cooldown-policy.py")

spec = importlib.util.spec_from_file_location("fleet_cooldown_policy_tr205", SCRIPT)
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)


SERVER_DEFAULT_LIMIT = 200  # scheduler internal/database/projects.go DefaultListProjectsLimit
SERVER_MAX_LIMIT = 500      # scheduler internal/database/projects.go MaxListProjectsLimit


def _page_of(rows, offset, total, page_size):
    """One server page in the exact SCHED-GAP-1622 wire shape."""
    window = rows[offset:offset + page_size]
    return {
        "projects": window,
        "total": total,
        "limit": min(page_size, SERVER_MAX_LIMIT),
        "offset": offset,
    }


class FakePaginatedProjectsAPI:
    """Stand-in for the scheduler's paginated /api/v1/projects endpoint.

    Enforces the server's real contract (internal/api/server_projects.go):
    limit clamped to [1, 500] (<=0 -> server default 200), offset < 0 -> 0.
    PAGE_SIZE is kept small so 3+ pages are cheap to exercise.
    """

    PAGE_SIZE = 5

    def __init__(self, n_lanes):
        self.rows = [
            {"name": f"{i:05d}", "enabled": True, "cooldown_s": 21600}
            for i in range(n_lanes)
        ]
        self.total = n_lanes
        self.requests = []  # every path the client asked for

    def handle(self, path):
        self.requests.append(path)
        assert path.startswith("/api/v1/projects"), path
        q = path.split("?", 1)[1] if "?" in path else ""
        limit = SERVER_DEFAULT_LIMIT
        offset = 0
        m = re.search(r"[?&]limit=(\d+)", q)
        if m:
            limit = int(m.group(1))
        m = re.search(r"[?&]offset=(\d+)", q)
        if m:
            offset = int(m.group(1))
        limit = max(1, min(limit, SERVER_MAX_LIMIT))
        offset = max(0, offset)
        # The fake serves its OWN page size (<= the requested limit): the
        # wire contract lets the server return fewer rows than asked, which
        # is what forces a real client to loop on actual page lengths.
        return _page_of(self.rows, offset, self.total, min(limit, self.PAGE_SIZE))

    def install(self, test):
        def fake_urlopen(req, timeout=None):
            url = req.full_url if isinstance(req, urllib.request.Request) else req
            path = url.split("pagination.test", 1)[1]
            body = json.dumps(self.handle(path)).encode()
            resp = mock.MagicMock()
            resp.__enter__ = lambda s: resp
            resp.__exit__ = lambda s, *a: None
            resp.read = lambda: body
            return resp

        p = mock.patch.object(urllib.request, "urlopen", fake_urlopen)
        p.start()
        test.addCleanup(p.stop)
        # The script hard-codes API='http://127.0.0.1:9090'; aim it at the
        # fake host so the path splitter above can slice the query off.
        hp = mock.patch.object(policy, "API", "http://pagination.test")
        hp.start()
        test.addCleanup(hp.stop)


PROJECTS_PATH_RE = re.compile(r"api_get\(\s*['\"]/api/v1/projects['\"]")


class TestTR205PaginationLoop(unittest.TestCase):
    """Criterion 3: a mocked 3-page endpoint must yield every row."""

    def setUp(self):
        self.api = FakePaginatedProjectsAPI(n_lanes=12)  # 3 pages at size 5
        self.api.install(self)

    def test_collects_all_rows_across_three_pages(self):
        rows = policy.api_get_all_projects()
        self.assertEqual(len(rows), 12)
        self.assertEqual({r["name"] for r in rows},
                         {f"{i:05d}" for i in range(12)})
        # and they came from the real page loop, not one lucky request:
        self.assertGreaterEqual(
            len(self.api.requests), 3,
            f"expected >=3 page requests, saw {self.api.requests}")

    def test_each_page_requests_limit_500(self):
        policy.api_get_all_projects()
        for path in self.api.requests:
            m = re.search(r"[?&]limit=(\d+)", path)
            self.assertIsNotNone(m, f"page request without ?limit=: {path}")
            self.assertEqual(int(m.group(1)), 500,
                             f"page request must use limit=500: {path}")

    def test_cursor_advances_by_page_len_until_total(self):
        policy.api_get_all_projects()
        offsets = []
        for path in self.api.requests:
            m = re.search(r"[?&]offset=(\d+)", path)
            offsets.append(int(m.group(1)) if m else 0)
        self.assertEqual(offsets, [0, 5, 10])

    def test_single_page_fleet_makes_exactly_one_request(self):
        small = FakePaginatedProjectsAPI(n_lanes=3)
        self.api.requests.clear()
        small.install(self)
        rows = policy.api_get_all_projects()
        self.assertEqual(len(rows), 3)
        self.assertEqual(len(small.requests), 1)


class TestTR205CallSites(unittest.TestCase):
    """Criterion 2: no bare /api/v1/projects GET may survive in the script."""

    def test_no_bare_projects_get_in_source(self):
        with open(SCRIPT) as f:
            src = f.read()
        bare = PROJECTS_PATH_RE.findall(src)
        self.assertEqual(
            bare, [],
            "bare api_get('/api/v1/projects') call site(s) present — they see "
            "only the first 200 lanes; route through api_get_all_projects()")

    def test_helper_exists_with_loop_semantics(self):
        self.assertTrue(callable(getattr(policy, "api_get_all_projects", None)),
                        "api_get_all_projects() helper missing")


class TestTR205ServerContractEdgeCases(unittest.TestCase):
    """The loop must survive the server's real clamps and degenerate pages."""

    def _with_pages(self, test, handler):
        def fake_urlopen(req, timeout=None):
            url = req.full_url if isinstance(req, urllib.request.Request) else req
            path = url.split("pagination.test", 1)[1]
            body = json.dumps(handler(path)).encode()
            resp = mock.MagicMock()
            resp.__enter__ = lambda s: resp
            resp.__exit__ = lambda s, *a: None
            resp.read = lambda: body
            return resp

        p = mock.patch.object(urllib.request, "urlopen", fake_urlopen)
        p.start()
        test.addCleanup(p.stop)
        hp = mock.patch.object(policy, "API", "http://pagination.test")
        hp.start()
        test.addCleanup(hp.stop)

    def test_loop_terminates_on_stalled_cursor(self):
        # Degenerate server: ignores ?offset= and hands out the SAME 2 rows
        # with a total far beyond anything it serves. offset += len(rows)
        # never closes the gap; the page cap must stop the loop, not spin.
        state = {"n": 0}

        def handler(path):
            state["n"] += 1
            return _page_of([{"name": "aaaaa"}, {"name": "aaaab"}],
                            offset=0, total=999999,
                            page_size=FakePaginatedProjectsAPI.PAGE_SIZE)

        self._with_pages(self, handler)
        rows = policy.api_get_all_projects()
        self.assertEqual(len(rows), 2 * state["n"])
        self.assertLessEqual(
            state["n"], policy.PROJECTS_MAX_PAGES,
            "pagination loop exceeded its page cap on a stalled cursor")

    def test_loop_terminates_on_empty_page(self):
        calls = {"n": 0}

        def handler(path):
            calls["n"] += 1
            return _page_of([], offset=0, total=500,
                            page_size=FakePaginatedProjectsAPI.PAGE_SIZE)

        self._with_pages(self, handler)
        self.assertEqual(policy.api_get_all_projects(), [])
        self.assertEqual(calls["n"], 1)

    def test_total_missing_still_returns_first_page(self):
        def handler(path):
            return {"projects": [{"name": "aaaaa"}]}  # legacy pre-pagination body

        self._with_pages(self, handler)
        rows = policy.api_get_all_projects()
        self.assertEqual([r["name"] for r in rows], ["aaaaa"])

    def test_auth_headers_reach_every_page_request(self):
        # SCHED-GAP-1602: page GETs ride api_get() and must carry the
        # operator credential header when one is configured.
        seen = []

        real_request = urllib.request.Request

        def fake_request(url, headers=None, **kw):
            seen.append((url, dict(headers or {})))
            return real_request(url, headers=headers or {}, **kw)

        def fake_urlopen(req, timeout=None):
            body = json.dumps({"projects": [], "total": 0,
                               "limit": 500, "offset": 0}).encode()
            resp = mock.MagicMock()
            resp.__enter__ = lambda s: resp
            resp.__exit__ = lambda s, *a: None
            resp.read = lambda: body
            return resp

        rp = mock.patch.object(urllib.request, "Request", fake_request)
        ru = mock.patch.object(urllib.request, "urlopen", fake_urlopen)
        rp.start()
        ru.start()
        self.addCleanup(rp.stop)
        self.addCleanup(ru.stop)

        with mock.patch.dict(os.environ, {"SCHEDULER_OPERATOR_TOKEN": "tok-tr205"}):
            policy.api_get_all_projects()

        self.assertTrue(seen, "no page requests were constructed")
        for url, headers in seen:
            self.assertIn("/api/v1/projects", url)
            self.assertEqual(headers.get("X-Operator-Token"), "tok-tr205")


if __name__ == "__main__":
    unittest.main()
