"""drivers/ — one module per host system that routes through the router proxy.

Design authority: docs/specs/SPEC-PROXY-DRIVERS.md (TR-071..TR-075).

A driver is a CONFIG SNIPPET plus a TELEMETRY READER. It never branches on
provider names and never teaches the router about the host system — the router
already owns lane pricing (TR-070), and selection logic that escapes into five
drivers drifts.

    id            slug, e.g. "hermes"
    caller_id()   the identity the host sends so proxied attempts are attributed
    config()      how a user points the host at the proxy (documented, not patched)
    row_for()     the telemetry reader: source DB -> TR-049 outcome rows
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from base import Driver, get_driver, list_drivers  # noqa: E402,F401
