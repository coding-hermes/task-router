#!/usr/bin/env python3
"""router_learn.py — the task-router LEARNING LOOP (Bane 2026-08-27).

DuckBrain-backed memory that makes the data-quality cron (and any session)
smarter on every run:

  dump                 — print doctrine + provider understanding + recent
                         lessons (the cron's pre-run script injects this into
                         the agent's context so it ACTS with memory, not from
                         a blank slate)
  lesson <slug> <text> — append a lesson learned (mistake + correction +
                         consequence). Append-only: each call adds a row.
  doctrine <key> <text>— upsert a standing doctrine rule (idempotent: same
                         key+sha -> skip, avoid duplicate rows)
  provider <name> <text> — upsert provider-understanding entry (billing,
                         rules, data source; idempotent by sha of text)
  recent [n]           — print the n most recent lessons (default 10)

Writes go through the DuckBrain CLI (cd ~/duckbrain && node bin/duckbrain.js)
into the `task-router` namespace — git-versioned, S3-backed. The namespace
already carries tables/ + docs/; these keys are the LIVE LEARNING layer:
  /doctrine/<key>            domain=concept   (standing rules, immutable-ish)
  /providers/<name>          domain=concept   (billing/quirks/data sources)
  /lessons/<YYYY-MM-DD>-<slug> domain=event   (append-only lesson log)
"""
import argparse, hashlib, json, os, subprocess, sys, datetime

NS = "task-router"
DB = os.path.expanduser("~/duckbrain")
CLI = ["node", "bin/duckbrain.js"]
TODAY = datetime.date.today().strftime("%Y-%m-%d")


def _cli(*args):
    """Run duckbrain CLI; return (rc, stdout)."""
    p = subprocess.run(CLI + list(args), cwd=DB, capture_output=True, text=True,
                       timeout=120)
    return p.returncode, (p.stdout + p.stderr).strip()


def _verify(key, text):
    """Post-write verification: grep the just-written uuid in the ns partition."""
    rc, out = _cli("recall", "--namespace=" + NS, "--key=" + key)
    if "Found 1 memories" not in out and "Found" not in out:
        print(f"WARN: recall for {key} did not confirm — {out[:200]}")
    return rc


def _write(key, domain, text, attrs):
    attr = json.dumps(attrs)
    rc, out = _cli("remember", key, "--namespace=" + NS, "--domain=" + domain,
                   "--attr=" + attr, "--wait", "--content=" + text)
    if rc != 0 or "Remembered" not in out:
        print(f"ERROR: write failed for {key}: {out[:300]}")
        return 1
    print(out.splitlines()[-1][:160] if out else f"wrote {key}")
    _verify(key, text)
    return 0


def dump():
    """Print doctrine + providers + recent lessons (for cron context)."""
    print("=" * 60)
    print("TASK-ROUTER LEARNING MEMORY (duckbrain ns=task-router)")
    print("=" * 60)
    for scope in ("/doctrine/", "/providers/"):
        rc, out = _cli("list-keys", "--namespace=" + NS)
        # list-keys output may be verbose; fall back to recall-by-prefix via CLI
        keys = []
        for line in out.splitlines():
            line = line.strip().strip('",')
            if line.startswith(scope):
                keys.append(line)
        if not keys:
            rc2, flat = _cli("recall", "--namespace=" + NS, "--key=" + scope.rstrip("/"))
            # exact-key recall on a prefix path rarely works; try JSONL scan
            keys = _scan_keys(scope)
        for k in sorted(set(keys)):
            rc3, txt = _cli("recall", "--namespace=" + NS, "--key=" + k)
            # extract the most recent content line
            content = _extract_content(txt)
            if content:
                print(f"\n## {k}\n{content[:900]}")
    rc, recent = _cli("recall", "--namespace=" + NS, "--key=/lessons/latest")
    print("\n## RECENT LESSONS")
    for k in sorted(set(_scan_keys("/lessons/")))[-5:]:
        rc4, txt = _cli("recall", "--namespace=" + NS, "--key=" + k)
        c = _extract_content(txt)
        if c:
            print(f"\n- {k}: {c[:400]}")
    print("\n" + "=" * 60)


def _scan_keys(prefix):
    """Scan the ns JSONL partitions for keys under prefix (CLI list-keys is
    paginated/unreliable for prefix filters)."""
    found = set()
    root = os.path.join(DB, "namespaces", NS)
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            if not fn.endswith(".jsonl"):
                continue
            path = os.path.join(dirpath, fn)
            try:
                for line in open(path):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    if rec.get("action") == "tombstone":
                        continue
                    k = rec.get("key", "")
                    if k.startswith(prefix):
                        found.add(k)
            except Exception:
                continue
    return found


def _extract_content(recall_out):
    """Pull the embedding_text/content out of CLI recall output (best-effort)."""
    for marker in ("content: ", "embedding_text: ", '"content": "', '"embedding_text": "'):
        idx = recall_out.rfind(marker)
        if idx >= 0:
            return recall_out[idx + len(marker):].strip().strip('"')[:1200]
    return ""


def lesson(slug, text):
    key = f"/lessons/{TODAY}-{slug}"
    _write(key, "event", text, {"date": TODAY, "kind": "lesson"})


def doctrine(key, text):
    k = f"/doctrine/{key}"
    # idempotent: same text sha -> skip (avoid duplicate rows)
    sha = hashlib.sha256(text.encode()).hexdigest()[:12]
    existing = _scan_keys("/doctrine/")
    if k in existing:
        rc, out = _cli("recall", "--namespace=" + NS, "--key=" + k)
        if sha in out:
            print(f"skip {k} (unchanged)")
            return 0
    _write(k, "concept", text, {"date": TODAY, "sha": sha})


def provider(name, text):
    k = f"/providers/{name}"
    sha = hashlib.sha256(text.encode()).hexdigest()[:12]
    existing = _scan_keys("/providers/")
    if k in existing:
        rc, out = _cli("recall", "--namespace=" + NS, "--key=" + k)
        if sha in out:
            print(f"skip {k} (unchanged)")
            return 0
    _write(k, "concept", text, {"date": TODAY, "sha": sha})


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("dump")
    p_lesson = sub.add_parser("lesson")
    p_lesson.add_argument("slug")
    p_lesson.add_argument("text")
    p_doctrine = sub.add_parser("doctrine")
    p_doctrine.add_argument("key")
    p_doctrine.add_argument("text")
    p_prov = sub.add_parser("provider")
    p_prov.add_argument("name")
    p_prov.add_argument("text")
    args = ap.parse_args(argv)
    if args.cmd == "dump":
        dump()
    elif args.cmd == "lesson":
        lesson(args.slug, args.text)
    elif args.cmd == "doctrine":
        doctrine(args.key, args.text)
    elif args.cmd == "provider":
        provider(args.name, args.text)
    else:
        ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
