#!/usr/bin/env python3
"""Perf probe (dogfood tick 2026-11-24): profile `router spawn P1_CODING
--format json` with cProfile, isolated to the consumer checkout with a scratch
state dir. Prints the top cumulative functions so the hot path has file:lines,
mirroring the PERF discipline: numbers first, no eyeballing."""
import cProfile, io, os, pstats, runpy, sys

os.chdir("/home/kara/.hermes/cache/scratch/tr-dogfood-20261124a/consumer")
os.environ.setdefault("ROUTER_STATE_DIR", "/home/kara/.hermes/cache/scratch/tr-dogfood-20261124a/tf-state")

sys.argv = ["router_spawn.py", "P1_CODING", "--format", "json"]
prof = cProfile.Profile()
buf = io.StringIO()
_stdout, sys.stdout = sys.stdout, buf
try:
    prof.enable()
    runpy.run_path("scripts/router_spawn.py", run_name="__main__")
finally:
    prof.disable()
    sys.stdout = _stdout
st = pstats.Stats(prof, stream=buf)
st.sort_stats("cumulative").print_stats(28)
out = st.stream.getvalue() if hasattr(st, "stream") else buf.getvalue()
print(out)
