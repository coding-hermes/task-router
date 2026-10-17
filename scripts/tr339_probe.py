#!/usr/bin/env python3
"""Ad-hoc 339 probe: tmp git repo, fake CI id refusal, verdict fallback arms."""
import json, os, subprocess, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts"))
import evidence_replay as r

d = tempfile.mkdtemp()
g = ["git", "-C", d, "-c", "user.email=a@b", "-c", "user.name=a"]
subprocess.run(["git", "init", "-q", d], check=True)
subprocess.run(g + ["commit", "--allow-empty", "-q", "--allow-empty-message", "-m", "x"], check=True)
subprocess.run(g + ["commit", "--allow-empty", "-q", "--allow-empty-message", "-m", "y"], check=True)

# 339-01: fabricated 9-digit id refuses (gh absent in this env test: monkey the search)
old_which = r.shutil.which
r.shutil.which = lambda p: None if p == "gh" else old_which(p)
try:
    r.resolve_run("000000001234", d)
    print("BUG: fabricated id passed")
except r.ReplayFailure as e:
    print("339-01 refused OK:", str(e)[:80])
r.shutil.which = old_which

# 339-02 arm 1: verdict+criteria+commit ancestor -> ok
head = subprocess.run(["git", "-C", d, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
rd = os.path.join(d, ".gitreins/history/2026-10-16", "abc12345"); os.makedirs(rd)
json.dump({"task_criteria": ["c1"], "passed": True, "commit": head}, open(os.path.join(rd, "verdict.json"), "w"))
k, p = r.resolve_run("abc12345", d)
assert k == "verdict"
res = r.verdict_fallback_replay(p, "abc12345", d)
print("339-02 fallback OK:", res["ok"], res["detail"][:50])

# arm 2: passed=False -> refusal
rd2 = os.path.join(d, ".gitreins/history/2026-10-16", "dff34567"); os.makedirs(rd2)
json.dump({"task_criteria": ["c"], "passed": False, "commit": head}, open(os.path.join(rd2, "verdict.json"), "w"))
k, p = r.resolve_run("dff34567", d)
try:
    r.verdict_fallback_replay(p, "dff34567", d)
    print("BUG: passed=false passed")
except r.ReplayFailure as e:
    print("339-02 passed=false refused OK:", str(e)[:60])

# arm 3: no criteria -> refusal
rd3 = os.path.join(d, ".gitreins/history/2026-10-16", "eee22334"); os.makedirs(rd3)
json.dump({"passed": True, "commit": head}, open(os.path.join(rd3, "verdict.json"), "w"))
k, p = r.resolve_run("eee22334", d)
try:
    r.verdict_fallback_replay(p, "eee22334", d)
    print("BUG: no-crit passed")
except r.ReplayFailure as e:
    print("no-criteria refused OK:", str(e)[:60])

# arm 4: real repo verdict fe0601db (passed=false, graded d0db679 which IS ancestor) -> 339-04 refusal
repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
k, p = r.resolve_run("fe0601db", repo)
try:
    r.verdict_fallback_replay(p, "fe0601db", repo)
    print("BUG: fe0601db passed")
except r.ReplayFailure as e:
    print("339-04 LIVE verdict refused OK:", str(e)[:90])

# arm 5: real repo run be479d90 (HAS command.json) -> replays the command
k, p = r.resolve_run("be479d90", repo)
cmd = r.load_command(p["dir"], "be479d90")
res = r.replay_command(cmd, "be479d90")
print("be479d90 command replay:", res["ok"], res["detail"][:70])

# fuzz: resolve_run never returns ci unverified end-to-end through replay_diff
fails, reps = r.replay_diff(None, None, repo)
print("replay_diff on real repo:", "failures=", fails, "replayed=", reps)
