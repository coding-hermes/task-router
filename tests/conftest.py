"""Shared test configuration for task-router.

Why this file exists (TR-068): several tests drive the duckdb SEED pipeline in a
subprocess. Measured baseline on an idle box: ~11s. The fleet saturates this
machine routinely (loadavg in the hundreds), and under that load the same seed
stretches past a 120s per-call default — which surfaced as a full-suite-only
`subprocess.TimeoutExpired` and looked like flakiness or a state leak.

Rule: a subprocess budget must be sized for the WORST load the box actually
sees, not the idle case. Seed/duckdb pipelines get SEED_TIMEOUT; light JSON
tools (status, circuit, probefix) keep their tighter defaults.
"""

#: duckdb seed budget. 11s idle measured -> ~55x margin even at heavy load.
SEED_TIMEOUT = 600

#: Pipeline scripts that load the registry through duckdb.
SEED_PIPELINES = ("router_seed.py", "router_pricing.py")
