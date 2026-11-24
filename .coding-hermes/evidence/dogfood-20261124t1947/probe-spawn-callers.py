#!/usr/bin/env python3
"""Perf probe 2: same spawn profile, but print the CALLERS of json.loads so the
464k-parse hot path is attributed to file:line, plus the file sizes the
resolve actually opens (du on data/state + data home)."""
import cProfile, io, os, pstats, runpy, sys

os.chdir("/home/kara/.hermes/cache/scratch/tr-dogfood-20261124a/consumer")
os.environ.setdefault("ROUTER_STATE_DIR", "/home/kara/.hermes/cache/scratch/tr-dogfood-20261124a/tf-state")

sys.argv = ["router_spawn.py", "P1_CODING", "--format", "json"]
prof = cProfile.Profile()
buf = io.StringIO()
_stdout = sys.stdout
sys.stdout = buf
try:
    prof.enable()
    runpy.run_path("scripts/router_spawn.py", run_name="__main__")
finally:
    prof.disable()
    sys.stdout = _stdout
st = pstats.Stats(prof, stream=buf)
st.sort_stats("cumulative")
st.print_callers("json/decoder.py:340\\(decode\\)")
st.print_callers("json/__init__.py:304\\(loads\\)")
print(st.stream.getvalue())
