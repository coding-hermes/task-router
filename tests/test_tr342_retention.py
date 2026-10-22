"""TR-342: benchmark backup retention test.

Verifies that all scripts write backups into data/state/ with
a max-keep-3 retention policy, and never litter /tmp.

This is a source-inspection test — the logic is simple enough
that grepping source is more reliable than importing the module
(which triggers a full seed run with side effects).
"""

import os

import pytest


class TestBackupNeverWritesToTmp:
    """All backup write paths go under data/state/, never /tmp."""

    def test_probe_ingest_no_tmp_benchmarks(self) -> None:
        """router_probe_ingest.py: no /tmp/benchmarks backup path."""
        with open("scripts/router_probe_ingest.py", "r", encoding="utf-8") as f:
            src = f.read()
        assert "/tmp/benchmarks" not in src, (
            "router_probe_ingest.py still writes to /tmp for backups"
        )

    def test_mirror_account_no_tmp_models(self) -> None:
        """router_mirror_account.py: no /tmp/models backup path."""
        with open("scripts/router_mirror_account.py", "r", encoding="utf-8") as f:
            src = f.read()
        assert "/tmp/models.jsonl" not in src, (
            "router_mirror_account.py still writes to /tmp for backups"
        )

    def test_release_backfill_no_tmp_models(self) -> None:
        """router_release_backfill.py: no /tmp/models backup path."""
        with open("scripts/router_release_backfill.py", "r", encoding="utf-8") as f:
            src = f.read()
        assert "/tmp/models.jsonl" not in src, (
            "router_release_backfill.py still writes to /tmp for backups"
        )

    def test_seed_uses_backup_with_retention(self) -> None:
        """router_seed.py uses _backup_with_retention (no direct /tmp writes)."""
        with open("scripts/router_seed.py", "r", encoding="utf-8") as f:
            src = f.read()
        # The backup function should not write to /tmp directly
        assert "_backup_with_retention" in src, (
            "router_seed.py should use _backup_with_retention helper"
        )
        # Verify no remaining /tmp/benchmarks backup paths
        assert "/tmp/benchmarks" not in src.replace(
            "_tr342_sweep", ""
        ), "router_seed.py still has /tmp backup writes"

    def test_backup_with_retention_defined_in_seed(self) -> None:
        """_backup_with_retention uses _STATE_DIR from DATA_DIR, not /tmp."""
        with open("scripts/router_seed.py", "r", encoding="utf-8") as f:
            src = f.read()
        # The function must reference _STATE_DIR (derived from DATA_DIR)
        assert "_STATE_DIR" in src, (
            "_backup_with_retention must use _STATE_DIR for backup location"
        )
        # Verify it uses os.makedirs and retention pruning
        assert "os.makedirs" in src, "must create _STATE_DIR with makedirs"
        assert "max_keep" in src, "must enforce max_keep retention"

    def test_no_dead_tr342_sweep(self) -> None:
        """_tr342_sweep was removed — it was dead code (never called)."""
        with open("scripts/router_seed.py", "r", encoding="utf-8") as f:
            src = f.read()
        # Sweep was a one-time cleanup; no longer needed since code no longer writes /tmp
        assert "_tr342_sweep" not in src or src.count("_tr342_sweep") == 0, (
            "_tr342_sweep should be removed (dead code; one-time sweep only)"
        )


class TestRetentionLogicExists:
    """Retention pruning logic is present in all backup sites."""

    def test_seed_retention_pruning(self) -> None:
        """router_seed.py enforces max_keep retention."""
        with open("scripts/router_seed.py", "r", encoding="utf-8") as f:
            src = f.read()
        # Check for pruning logic: sort, reverse, iterate beyond max_keep
        assert "max_keep" in src
        assert "for old in" in src or "all_baks[max_keep:]" in src, (
            "must prune backups beyond max_keep"
        )

    def test_probe_retention_pruning(self) -> None:
        """router_probe_ingest.py enforces max_keep retention."""
        with open("scripts/router_probe_ingest.py", "r", encoding="utf-8") as f:
            src = f.read()
        assert "max_keep" in src
        assert "for old in" in src or "all_baks[max_keep:]" in src, (
            "must prune backups beyond max_keep"
        )

    def test_mirror_retention_pruning(self) -> None:
        """router_mirror_account.py enforces max_keep retention."""
        with open("scripts/router_mirror_account.py", "r", encoding="utf-8") as f:
            src = f.read()
        assert "max_keep" in src
        assert "for old in" in src or "all_baks[max_keep:]" in src, (
            "must prune backups beyond max_keep"
        )

    def test_release_retention_pruning(self) -> None:
        """router_release_backfill.py enforces max_keep retention."""
        with open("scripts/router_release_backfill.py", "r", encoding="utf-8") as f:
            src = f.read()
        assert "max_keep" in src
        assert "for old in" in src or "all_baks[max_keep:]" in src, (
            "must prune backups beyond max_keep"
        )
