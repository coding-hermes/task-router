#!/usr/bin/env python3
"""Small thread-safe state for bounded router shutdown and readiness."""
from __future__ import annotations

import hashlib
import os
import subprocess
import threading
import time
from pathlib import Path


class DrainController:
    """Tracks readiness and in-flight HTTP handlers during graceful restart."""

    def __init__(self):
        self._condition = threading.Condition()
        self._draining = False
        self._inflight = 0

    @property
    def ready(self):
        with self._condition:
            return not self._draining

    @property
    def draining(self):
        with self._condition:
            return self._draining

    @property
    def inflight(self):
        with self._condition:
            return self._inflight

    def request_started(self):
        with self._condition:
            self._inflight += 1
            return not self._draining

    def request_finished(self):
        with self._condition:
            if self._inflight <= 0:
                raise RuntimeError("request_finished without a matching start")
            self._inflight -= 1
            if self._inflight == 0:
                self._condition.notify_all()

    def begin_drain(self):
        with self._condition:
            self._draining = True
            if self._inflight == 0:
                self._condition.notify_all()

    def wait_for_idle(self, timeout_s):
        """Wait no longer than timeout_s; False means work remains in flight."""
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        with self._condition:
            while self._inflight:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    def snapshot(self):
        with self._condition:
            return {"ready": not self._draining, "draining": self._draining,
                    "inflight": self._inflight}


def runtime_identity(repo_root=None):
    """Identify the code loaded by a router process for deploy verification.

    Revision is diagnostic; source_digest fingerprints the actual on-disk Python
    source at startup, including uncommitted changes. A deploy verifier compares
    its expected digest with /health after restart, so it can prove which code
    vintage is answering rather than trusting systemd's declared ExecStart.
    """
    repo = Path(repo_root or Path(__file__).resolve().parent.parent).resolve()
    try:
        revision = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True, timeout=2,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        revision = "unknown"
    try:
        dirty = bool(subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain", "--", "scripts"],
            check=True, capture_output=True, text=True, timeout=2,
        ).stdout.strip())
    except (OSError, subprocess.SubprocessError):
        dirty = None
    digest = hashlib.sha256()
    script_dir = repo / "scripts"
    files = sorted(script_dir.glob("*.py")) if script_dir.is_dir() else []
    for path in files:
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(b"<unreadable>")
        digest.update(b"\0")
    return {"pid": os.getpid(), "source_revision": revision,
            "source_dirty": dirty, "source_digest": digest.hexdigest()}
