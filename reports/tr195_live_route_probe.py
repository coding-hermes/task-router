"""TR-195 live-route probe: the UI surface vs the alerter, same store, same number."""
import json
import os
import subprocess
import sys

os.environ["ROUTING_OUTCOMES_FILE"] = "/home/kara/task-router/data/state/outcomes.jsonl"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import router_server as rs  # noqa: E402
import router_nohops as rn  # noqa: E402

app = rs.RouterApplication("read-only", None)
st, res = app.dispatch("GET", "/api/ui/nohops", query={"window_h": "168"})
print("route status:", st)
print("route: rate", res["rate"], "n", res["n"],
      f"({res['nohops']}/{res['resolves']})")
print("route threshold:", res["threshold"], "|", res["threshold_source"])
print("route breached:", res["breached"], "|", res["breach_reason"])
print("route blocking:", res["top_blocking_category"],
      res["top_blocking_category_count"], "| hours:", res["hour_count"],
      "| rows_scanned:", res["rows_scanned"])
print("baseline:", res["baseline_source"])

# the alerter's own verdict against the same store, same window
rows, meta = rn.load_store_rows(os.environ["ROUTING_OUTCOMES_FILE"])
result = rn.evaluate(rows, window_h=168.0)
p = result["payload"]
print("alerter: rate", p["rate"], "n", p["n"], "threshold", p["threshold"],
      "alert", p["alert"])
same = (res["rate"] == p["rate"] and res["n"] == p["n"]
        and res["threshold"] == p["threshold"]
        and res["hours"] == result["series"]["hours"])
print("UI-AND-ALERT-SAME-NUMBER:", same)
sys.exit(0 if same and st == 200 else 1)
