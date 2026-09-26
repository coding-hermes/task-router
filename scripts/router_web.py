#!/usr/bin/env python3
"""TR-017 — local web UI for human settings changes + resolve preview.

Stdlib-only http.server. Serves a single-page dark UI (inline HTML, no CDN,
no build step) with two panes:
  * PREVIEW  — pick a profile (or project), resolve via the REAL
               scripts/router_spawn.py subprocess, render the chain.
  * SETTINGS — providers / profiles / discounts tables, edited through the
               same edit-gated path as the API server
               (ROUTER_EDIT_API_KEY / X-API-Key header). Read-only mode
               when no key is configured.

Fail-open: never imports router_server.py; never re-implements resolve.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PY = sys.executable
SPAWN = REPO / "scripts" / "router_spawn.py"

EDIT_KEY_ENV = "ROUTER_EDIT_API_KEY"

# TR-150: the data layer (ledger search / series / flow / board). Imported
# fail-open on purpose — the existing preview + settings panes must keep working
# even if the data module is missing or broken on a given box.
try:
    import router_ui_data as ui_data  # noqa: E402
    UI_DATA_ERROR = None
except Exception as _exc:  # noqa: BLE001
    ui_data = None
    UI_DATA_ERROR = str(_exc)[:200]

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9093


# --------------------------------------------------------------------------- #
# JSONL surgical edit helpers (read-modify-append convention)
# --------------------------------------------------------------------------- #
def _jsonl_read(path):
    rows = []
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _jsonl_write_atomic(path, rows):
    """Write rows to <path>.tmp then os.replace — byte-preserving of the
    rows list, atomic on the filesystem. A backup copy is taken first."""
    backup = path.with_suffix(path.suffix + f".bak-{int(time.time() * 1000)}")
    if path.exists():
        backup.write_bytes(path.read_bytes())
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    os.replace(tmp, path)
    return backup


def jsonl_upsert(path, key_fields, key_values, updates, insert_if_missing=True):
    """Read raw lines, update ONLY the row matching key_fields==key_values,
    byte-preserve everything else. Returns (changed: bool, backup_path)."""
    if not path.exists():
        raise FileNotFoundError(path)
    rows = _jsonl_read(path)
    matched = False
    for row in rows:
        if all(str(row.get(k)) == str(key_values[k]) for k in key_fields):
            matched = True
            row.update(updates)
            break
    if not matched:
        if not insert_if_missing:
            return False, None
        new_row = dict(key_values)
        new_row.update(updates)
        rows.append(new_row)
    backup = _jsonl_write_atomic(path, rows)
    return True, backup


def jsonl_patch_fields(path, key_fields, key_values, updates):
    """Update only the listed fields on the matching row (no insert)."""
    return jsonl_upsert(path, key_fields, key_values, updates,
                        insert_if_missing=False)


# --------------------------------------------------------------------------- #
# Data access
# --------------------------------------------------------------------------- #
class Store:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.providers = self.data_dir / "providers.jsonl"
        self.task_profiles = self.data_dir / "task_profiles.jsonl"
        self.profile_reqs = self.data_dir / "task_profile_requirements.jsonl"
        self.discounts = self.data_dir / "temporary_discounts.jsonl"

    def list_profiles(self):
        out = []
        for row in _jsonl_read(self.task_profiles):
            out.append({"id": row.get("id"), "title": row.get("title")})
        return out

    def settings_providers(self):
        rows = _jsonl_read(self.providers)
        for r in rows:
            r.setdefault("key_env", "")
        return rows

    def settings_profiles(self):
        reqs = {}
        for row in _jsonl_read(self.profile_reqs):
            reqs.setdefault(row.get("task_id"), {})[row.get("category")] = row.get("level")
        out = []
        for row in _jsonl_read(self.task_profiles):
            pid = row.get("id")
            out.append({
                "id": pid,
                "title": row.get("title"),
                "requirements": reqs.get(pid, {}),
            })
        return out

    def settings_discounts(self):
        return _jsonl_read(self.discounts)


# --------------------------------------------------------------------------- #
# Preview (subprocess the real resolve engine — preview == reality)
# --------------------------------------------------------------------------- #
def resolve_preview(store, profile=None, project=None,
                    tokens_in=1_000_000, tokens_out=1_000_000):
    args = [PY, str(SPAWN), "--format", "json"]
    if project:
        args.append(str(project))
    elif profile:
        args += ["--profile", str(profile)]
    else:
        return {"ok": False, "error": "need profile or project"}

    env = dict(os.environ)
    env["ROUTING_DATA_DIR"] = str(store.data_dir)
    env["ROUTING_REGISTRY"] = str(REPO / "registry.json")
    try:
        proc = subprocess.run(args, capture_output=True, text=True,
                              cwd=str(REPO), env=env, timeout=120)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "resolve timed out"}
    if proc.returncode != 0:
        return {"ok": False, "error": proc.stderr.strip()[:400] or "resolve failed"}

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"ok": False, "error": "resolve produced non-JSON output"}

    def _cost(hop):
        i, o = hop.get("in_per_m"), hop.get("out_per_m")
        if i is not None and o is not None:
            return (tokens_in / 1e6) * i + (tokens_out / 1e6) * o
        usd = hop.get("usd_1m")
        if usd is not None:
            return ((tokens_in + tokens_out) / 2 / 1e6) * usd
        return None

    hops = []
    for hop in data.get("chain", []):
        h = dict(hop)
        h["est_cost_usd"] = _cost(hop)
        hops.append(h)

    return {
        "ok": True,
        "resolve": data,
        "hops": hops,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "exclusions": data.get("exclusions", []),
        "gate_reasons": data.get("gate_reasons", []),
        "head": data.get("head"),
    }


# --------------------------------------------------------------------------- #
# HTTP handler
# --------------------------------------------------------------------------- #
class RouterWebHandler(BaseHTTPRequestHandler):
    # configured by make_server
    store = None
    mode = "read-only"
    edit_key = None
    registry = None

    server_version = "router-web/1.0"

    def log_message(self, *args):  # quiet
        pass

    # ---- helpers ---------------------------------------------------------- #
    def _send(self, code, payload, ctype="application/json"):
        # TR-150: a str payload is the HTML page — serve it verbatim. It used to fall
        # through json.dumps(), which wrapped the whole document in quotes and escaped
        # every newline, so the browser received one JSON string: the markup half-rendered
        # and the <script> body was unparseable. The UI was therefore never usable in a
        # browser even though every endpoint behind it answered correctly.
        if isinstance(payload, (bytes, bytearray)):
            body = payload
        elif isinstance(payload, str):
            body = payload.encode("utf-8")
        else:
            body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-API-Key")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, OPTIONS")
        self.end_headers()
        self.wfile.write(body)

    def _body_json(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode())

    def _write_gate(self):
        """Return None if allowed, else an (code, msg) tuple for write ops."""
        if self.mode != "edit":
            return (403, {"error": "read-only mode — edits disabled"})
        key = self.headers.get("X-API-Key", "")
        if not self.edit_key or key != self.edit_key:
            return (401, {"error": "unauthorized — missing/invalid X-API-Key"})
        return None

    def _q(self):
        from urllib.parse import urlparse, parse_qs
        u = urlparse(self.path)
        return u.path, parse_qs(u.query)

    # ---- routing ---------------------------------------------------------- #
    def do_OPTIONS(self):
        self._send(204, b"")

    def do_GET(self):
        path, q = self._q()
        if path in ("/", "/index.html"):
            self._send(200, PAGE_HTML, "text/html; charset=utf-8")
            return
        if path == "/api/status":
            self._send(200, {"mode": self.mode, "ready": True})
            return
        if path == "/api/profiles":
            self._send(200, {"profiles": self.store.list_profiles()})
            return
        if path == "/api/preview":
            profile = (q.get("profile") or [None])[0]
            project = (q.get("project") or [None])[0]
            try:
                tin = int((q.get("tokens_in") or [1000000])[0])
                tout = int((q.get("tokens_out") or [1000000])[0])
            except ValueError:
                tin, tout = 1_000_000, 1_000_000
            self._send(200, resolve_preview(self.store, profile, project, tin, tout))
            return
        if path.startswith("/api/ui/"):
            self._ui_data(path, q)
            return
        if path == "/api/settings/providers":
            self._send(200, {"rows": self.store.settings_providers()})
            return
        if path == "/api/settings/profiles":
            self._send(200, {"rows": self.store.settings_profiles()})
            return
        if path == "/api/settings/discounts":
            self._send(200, {"rows": self.store.settings_discounts()})
            return
        self._send(404, {"error": "not found"})

    def _ui_data(self, path, q):
        """TR-151..TR-156 read endpoints. Never raises into the socket."""
        if ui_data is None:
            self._send(503, {"error": "data layer unavailable", "reason": UI_DATA_ERROR})
            return

        def one(name, default=None):
            return (q.get(name) or [default])[0]

        try:
            if path == "/api/ui/ledger":
                self._send(200, ui_data.ledger_search(
                    q=one("q"), outcome=one("outcome"), source=one("source"),
                    provider=one("provider"), model=one("model"), band=one("band"),
                    since=one("since"), until=one("until"), sort=one("sort", "ts"),
                    desc=(one("desc", "1") not in ("0", "false", "no")),
                    limit=one("limit", 50), offset=one("offset", 0)))
                return
            if path == "/api/ui/series":
                self._send(200, ui_data.series(hours=one("hours", 24), bucket=one("bucket", "hour")))
                return
            if path == "/api/ui/flow":
                res = ui_data.flow(one("id"))
                code = int(res.pop("status", 200)) if isinstance(res.get("status"), int) else 200
                self._send(code, res)
                return
            if path == "/api/ui/board":
                self._send(200, ui_data.board(
                    search=one("q"), status=one("status"), priority=one("priority"),
                    owner=one("owner"), limit=one("limit", 100), offset=one("offset", 0)))
                return
        except Exception as exc:  # noqa: BLE001 — a bad query must not kill the UI
            self._send(500, {"error": "data query failed", "reason": str(exc)[:200]})
            return
        self._send(404, {"error": "not found"})

    def do_POST(self):
        self._write_dispatch("POST")

    def do_PUT(self):
        self._write_dispatch("PUT")

    def _write_dispatch(self, method):
        path, _ = self._q()
        gate = self._write_gate()
        if gate is not None:
            self._send(*gate)
            return
        try:
            body = self._body_json()
        except (json.JSONDecodeError, ValueError):
            self._send(400, {"error": "invalid JSON body"})
            return

        try:
            if path == "/api/settings/providers":
                self._edit_provider(body)
            elif path == "/api/settings/profiles/requirements":
                self._edit_profile_req(body)
            elif path == "/api/settings/discounts":
                self._edit_discount(body)
            else:
                self._send(404, {"error": "not found"})
        except Exception as exc:  # fail-open: report, never crash the server
            self._send(400, {"error": str(exc)[:300]})

    # ---- edits ------------------------------------------------------------- #
    def _edit_provider(self, body):
        pid = body.get("id")
        if not pid:
            self._send(400, {"error": "id required"})
            return
        updates = {}
        if "archive" in body:
            updates["archive"] = bool(body["archive"])
        if "key_env" in body:
            updates["key_env"] = str(body["key_env"])
        if not updates:
            self._send(400, {"error": "nothing to update"})
            return
        changed, backup = jsonl_patch_fields(
            self.store.providers, ["id"], {"id": pid}, updates)
        if not changed:
            self._send(404, {"error": f"provider {pid} not found"})
            return
        self._send(200, {"ok": True, "id": pid, "updates": updates,
                         "backup": str(backup)})

    def _edit_profile_req(self, body):
        tid = body.get("task_id")
        cat = body.get("category")
        if not tid or not cat:
            self._send(400, {"error": "task_id and category required"})
            return
        level = int(body["level"])
        changed, backup = jsonl_upsert(
            self.store.profile_reqs,
            ["task_id", "category"], {"task_id": tid, "category": cat},
            {"level": level})
        self._send(200, {"ok": True, "task_id": tid, "category": cat,
                         "level": level, "inserted": not changed,
                         "backup": str(backup)})

    def _edit_discount(self, body):
        prov = body.get("provider")
        model = body.get("model")
        if not prov or not model:
            self._send(400, {"error": "provider and model required"})
            return
        updates = {k: body[k] for k in
                   ("discount_type", "value", "valid_from", "valid_to",
                    "source", "note") if k in body}
        changed, backup = jsonl_upsert(
            self.store.discounts,
            ["provider", "model"], {"provider": prov, "model": model}, updates)
        self._send(200, {"ok": True, "provider": prov, "model": model,
                         "updates": updates, "inserted": not changed,
                         "backup": str(backup)})


# --------------------------------------------------------------------------- #
# Server bootstrap
# --------------------------------------------------------------------------- #
def make_server(host, port, mode, edit_key, data_dir=None):
    data_dir = data_dir or os.environ.get(
        "ROUTING_DATA_DIR", str(REPO / "data" / "tables"))
    RouterWebHandler.store = Store(data_dir)
    RouterWebHandler.mode = mode
    RouterWebHandler.edit_key = edit_key
    httpd = ThreadingHTTPServer((host, port), RouterWebHandler)
    return httpd


def main(argv=None):
    ap = argparse.ArgumentParser(description="Task Router web UI (TR-017)")
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--mode", choices=["read-only", "edit"],
                    default="read-only")
    ap.add_argument("--data-dir", default=None,
                    help="override ROUTING_DATA_DIR for tables")
    args = ap.parse_args(argv)

    # Fail closed: edit mode requires the edit key configured.
    edit_key = os.environ.get(EDIT_KEY_ENV)
    if args.mode == "edit" and not edit_key:
        sys.stderr.write(
            "FATAL: --mode edit requires ROUTER_EDIT_API_KEY to be set. "
            "Refusing to start (fail closed).\n")
        return 2

    httpd = make_server(args.host, args.port, args.mode, edit_key,
                        data_dir=args.data_dir)
    proto = "EDIT" if args.mode == "edit" else "READ-ONLY"
    sys.stderr.write(
        f"router-web [{proto}] on http://{args.host}:{args.port}\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


# --------------------------------------------------------------------------- #
# Inline single-page dark UI
# --------------------------------------------------------------------------- #
PAGE_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>task-router — settings + resolve preview</title>
<style>
  :root{
    --bg:#0d1117; --panel:#161b22; --panel2:#1c2330; --line:#30363d;
    --txt:#c9d1d9; --dim:#8b949e; --acc:#58a6ff; --ok:#3fb950; --warn:#d29922;
    --bad:#f85149; --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--txt);
    font:13px/1.5 var(--mono);padding:14px}
  h1{font-size:15px;margin:0 0 4px;color:#fff;letter-spacing:.5px}
  .sub{color:var(--dim);margin-bottom:12px}
  .mode{font-weight:bold}
  .mode.ro{color:var(--warn)} .mode.ed{color:var(--ok)}
  .grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}
  @media(max-width:980px){.grid{grid-template-columns:1fr}}
  .pane{background:var(--panel);border:1px solid var(--line);border-radius:8px;
    padding:12px}
  .pane h2{font-size:13px;margin:0 0 10px;color:var(--acc);
    text-transform:uppercase;letter-spacing:1px}
  label{color:var(--dim);display:block;margin:6px 0 2px}
  select,input,button,textarea{font:inherit;background:var(--panel2);
    color:var(--txt);border:1px solid var(--line);border-radius:5px;
    padding:6px 8px;width:100%}
  button{cursor:pointer;background:#21262d;width:auto;color:var(--acc);
    border-color:var(--acc)}
  button:hover{background:#2d333b}
  .row{display:flex;gap:8px;align-items:flex-end}
  .row>*{flex:1}
  table{width:100%;border-collapse:collapse;margin-top:8px;font-size:12px}
  th,td{text-align:left;padding:5px 7px;border-bottom:1px solid var(--line)}
  th{color:var(--dim);font-weight:normal;text-transform:uppercase;font-size:10px}
  tr:nth-child(even){background:#11161d}
  .tag{display:inline-block;padding:1px 6px;border-radius:4px;font-size:10px}
  .tag.on{background:#1b3a1b;color:var(--ok)}
  .tag.off{background:#3a1b1b;color:var(--bad)}
  .sec{margin-bottom:14px}
  .sec h3{font-size:12px;color:var(--acc);margin:0 0 6px;
    border-bottom:1px solid var(--line);padding-bottom:4px}
  .json{white-space:pre-wrap;max-height:240px;overflow:auto;background:#0a0e14;
    border:1px solid var(--line);border-radius:5px;padding:8px;color:var(--dim)}
  details{margin-top:8px}
  summary{cursor:pointer;color:var(--dim)}
  .mini{font-size:11px;color:var(--dim);margin-top:6px}
  .keybar{display:flex;gap:8px;align-items:center;margin-top:10px}
  .keybar input{flex:2}
  .ok{color:var(--ok)} .err{color:var(--bad)}
  .head{font-size:12px;color:var(--ok);margin:6px 0}
  .excl{color:var(--warn)}
</style>
</head>
<body>
<h1>task-router · settings + resolve preview</h1>
<div class="sub">mode: <span id="mode" class="mode">…</span>
  &nbsp;·&nbsp; resolve via live <code>router_spawn.py</code></div>

<div class="grid">
  <!-- PREVIEW -->
  <div class="pane">
    <h2>Preview — resolve chain</h2>
    <label>Profile</label>
    <select id="profile"></select>
    <label>or Project (overrides profile)</label>
    <input id="project" placeholder="e.g. my-project">
    <div class="row">
      <div><label>Input tokens</label><input id="tin" type="number" value="1000000"></div>
      <div><label>Output tokens</label><input id="tout" type="number" value="1000000"></div>
    </div>
    <div class="row" style="margin-top:8px">
      <button id="resolve">Resolve</button>
      <button id="resolveR" style="color:var(--dim)">Resolve (my-project)</button>
    </div>
    <div id="head" class="head"></div>
    <div id="previewErr" class="err"></div>
    <table id="hops"><thead><tr>
      <th>#</th><th>provider</th><th>model</th><th>$/M</th>
      <th>in</th><th>out</th><th>est $</th><th>class</th>
    </tr></thead><tbody></tbody></table>
    <div id="gates" class="mini"></div>
    <details><summary>raw resolve JSON</summary>
      <div id="raw" class="json"></div></details>
  </div>

  <!-- SETTINGS -->
  <div class="pane">
    <h2>Settings</h2>
    <div class="keybar">
      <input id="apikey" placeholder="edit key (X-API-Key)">
      <button id="savekey">remember key</button>
    </div>
    <div class="mini" id="editnote"></div>

    <div class="sec">
      <h3>Providers</h3>
      <table id="prov"><thead><tr>
        <th>id</th><th>plan</th><th>quota</th><th>enabled</th>
        <th>key env</th><th></th>
      </tr></thead><tbody></tbody></table>
    </div>

    <div class="sec">
      <h3>Profiles (requirements)</h3>
      <table id="prof"><thead><tr>
        <th>id</th><th>title</th><th>requirements</th><th></th>
      </tr></thead><tbody></tbody></table>
      <div class="mini">add/update a requirement level (cat=level, e.g. code_gen=-2)</div>
      <div class="row">
        <input id="pId" placeholder="profile id">
        <input id="pCat" placeholder="category">
        <input id="pLvl" type="number" placeholder="level">
        <button id="pSet">set</button>
      </div>
    </div>

    <div class="sec">
      <h3>Discounts</h3>
      <table id="disc"><thead><tr>
        <th>provider</th><th>model</th><th>type</th><th>value</th><th>to</th>
      </tr></thead><tbody></tbody></table>
      <div class="mini">upsert a discount row (provider+model key)</div>
      <div class="row">
        <input id="dProv" placeholder="provider">
        <input id="dModel" placeholder="model">
        <input id="dType" placeholder="type">
        <input id="dVal" placeholder="value">
        <button id="dSet">set</button>
      </div>
    </div>
  </div>
</div>


<!-- TR-150: DATA — the command center. Same page, same edit gate, no second UI. -->
<div class="pane" style="margin-top:14px">
  <h2>Data — ledger search <span id="dHonest" class="mini"></span></h2>
  <div class="row">
    <input id="dq" placeholder="free text: session id, model, provider, failure reason…">
    <input id="dOutcome" placeholder="outcome (served)">
    <input id="dSource" placeholder="rating source (classifier)">
    <input id="dProvider" placeholder="provider">
    <input id="dLimit" type="number" value="25" style="max-width:90px">
    <button id="dGo">search</button>
  </div>
  <div class="mini" id="dMeta">—</div>
  <table id="dTable"><thead><tr>
    <th>when</th><th>session</th><th>rating</th><th>lane</th><th>outcome</th>
    <th>hops</th><th>in</th><th>out</th><th>cost</th><th>wall</th>
  </tr></thead><tbody></tbody></table>
  <div class="row"><button id="dPrev">prev</button><button id="dNext">next</button>
    <span class="mini">click a row to trace that request end to end</span></div>
</div>

<div class="pane" style="margin-top:14px">
  <h2>Data — traffic &amp; cost</h2>
  <div class="row">
    <div><label>window (hours)</label><input id="tHours" type="number" value="168"></div>
    <div><label>bucket</label><select id="tBucket">
      <option value="hour">hour</option><option value="day">day</option></select></div>
    <button id="tGo">chart</button>
  </div>
  <div class="mini" id="tMeta">—</div>
  <div id="tChart" style="margin-top:8px"></div>
</div>

<div class="pane" style="margin-top:14px">
  <h2>Data — request flow <span class="mini">(why this lane, what else was tried)</span></h2>
  <div class="json" id="fOut">pick a row above, or paste an id:</div>
  <div class="row"><input id="fId" placeholder="router-proxy-… or a gateway session id">
    <button id="fGo">trace</button></div>
</div>

<div class="pane" style="margin-top:14px">
  <h2>Data — board <span class="mini" id="bHonest"></span></h2>
  <div class="row">
    <input id="bq" placeholder="search tasks: id, title, commit…">
    <input id="bStatus" placeholder="status (pending)">
    <button id="bGo">search</button>
  </div>
  <table id="bTable"><thead><tr>
    <th>id</th><th>title</th><th>status</th><th>pri</th><th>created</th><th>commit</th>
  </tr></thead><tbody></tbody></table>
</div>

<script>
const K = localStorage.getItem('rw_key') || '';
document.getElementById('apikey').value = K;
const note = (m,c)=>{const e=document.getElementById('editnote');e.textContent=m;e.className='mini '+(c||'');};

async function api(path,opts){
  opts=opts||{};
  const h={'Content-Type':'application/json'};
  const k=document.getElementById('apikey').value;
  if(k) h['X-API-Key']=k;
  const r=await fetch(path,{...opts,headers:h});
  let j=null; try{j=await r.json();}catch(e){}
  return {r,j};
}
function jget(p){return api(p);}

// ---- TR-150 DATA pane -------------------------------------------------------
let dOffset = 0, dLast = 0;
const esc = s => String(s==null?'':s).replace(/[&<>"]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const when = ts => ts ? new Date(ts*1000).toISOString().replace('T',' ').slice(0,19) : '';

async function dataSearch(reset){
  if(reset) dOffset = 0;
  const q = new URLSearchParams({
    limit: document.getElementById('dLimit').value || 25,
    offset: dOffset,
    q: document.getElementById('dq').value,
    outcome: document.getElementById('dOutcome').value,
    source: document.getElementById('dSource').value,
    provider: document.getElementById('dProvider').value,
  });
  const {r,j} = await jget('/api/ui/ledger?'+q.toString());
  if(!j){ note('ledger query failed','err'); return; }
  dLast = j.total_matched || 0;
  const tb = document.querySelector('#dTable tbody'); tb.innerHTML='';
  (j.rows||[]).forEach(row=>{
    const tr = document.createElement('tr');
    const cost = (row.cost_usd==null) ? '<span class="excl">unpriced</span>' : row.cost_usd;
    tr.innerHTML = `<td>${esc(when(row.ts))}</td><td>${esc(row.session_id||'')}</td>`
      +`<td>${esc(row.complexity_source||'')}</td>`
      +`<td>${esc((row.provider||'')+'/'+(row.model||''))}</td>`
      +`<td>${esc(row.route_outcome||'')}</td><td>${esc(row.steps==null?(row.hops_attempted||''):row.steps)}</td>`
      +`<td>${esc(row.tokens_in||0)}</td><td>${esc(row.tokens_out||0)}</td>`
      +`<td>${cost}</td><td>${esc(row.wall_time_s||'')}</td>`;
    tr.style.cursor='pointer';
    tr.onclick = ()=>{ document.getElementById('fId').value = row.session_id||''; showFlow(row.session_id); };
    tb.appendChild(tr);
  });
  // the honesty line: a search must never look complete when it was truncated
  document.getElementById('dMeta').textContent =
    `${dLast} matched · showing ${dOffset+1}-${dOffset+(j.returned||0)}`
    +` · scanned ${j.rows_scanned} rows`
    +(j.rows_malformed?` · ${j.rows_malformed} torn line(s) skipped`:'')
    +(j.truncated?' · MORE RESULTS EXIST':' · end of results')
    +(j.source_exists?'':' · SOURCE FILE MISSING');
}

async function dataTraffic(){
  const q = new URLSearchParams({hours:document.getElementById('tHours').value||168,
                                bucket:document.getElementById('tBucket').value||'hour'});
  const {j} = await jget('/api/ui/series?'+q.toString());
  if(!j){ note('series query failed','err'); return; }
  const s = j.series||[];
  const max = Math.max(1, ...s.map(b=>b.requests));
  const bars = s.map(b=>{
    const h = Math.round(b.requests/max*90);
    const frac = b.success_rate==null?0:b.success_rate;
    const col = frac>=0.99 ? 'var(--ok)' : (frac>=0.8 ? 'var(--warn)' : 'var(--bad)');
    return `<div title="${b.iso} · ${b.requests} req · ${b.unique_lanes} lanes · ok ${frac}"
      style="flex:1;display:flex;flex-direction:column;justify-content:flex-end;align-items:center;height:100px">
      <div style="width:100%;background:${col};height:${h}px;border-radius:2px 2px 0 0"></div>
      <div class="mini" style="font-size:9px">${esc(b.iso.slice(11,16))}</div></div>`;
  }).join('');
  document.getElementById('tChart').innerHTML =
    `<div style="display:flex;gap:2px;align-items:flex-end;height:112px">${bars}</div>`;
  document.getElementById('tMeta').textContent =
    `${j.requests_total} proxied requests · ${j.unique_lanes_total} distinct lanes`
    +(j.note?` · ${j.note}`:'');
}

async function dataBoard(){
  const q = new URLSearchParams({limit:200, q:document.getElementById('bq').value,
                                 status:document.getElementById('bStatus').value});
  const {j} = await jget('/api/ui/board?'+q.toString());
  if(!j){ note('board query failed','err'); return; }
  const tb = document.querySelector('#bTable tbody'); tb.innerHTML='';
  (j.rows||[]).forEach(row=>{
    const tr = document.createElement('tr');
    tr.innerHTML = `<td>${esc(row.id)}</td><td>${esc(row.title)}</td>`
      +`<td>${esc(row.status)}</td><td>${esc(row.priority||'')}</td>`
      +`<td>${esc(row.created_at||'')}</td><td>${esc(row.commit_hash||'')}</td>`;
    tb.appendChild(tr);
  });
  const dups = Object.keys(j.duplicate_ids||{});
  document.getElementById('bHonest').textContent =
    `${j.board_rows} rows · ${j.total_matched} match`
    +(dups.length?` · DUPLICATE IDS: ${dups.join(',')}`:' · no duplicate ids');
}

async function showFlow(id){
  if(!id) return;
  const {r,j} = await jget('/api/ui/flow?id='+encodeURIComponent(id));
  const el = document.getElementById('fOut');
  if(!j){ el.textContent = 'query failed'; return; }
  if(r.status===404){ el.textContent = 'not found: searched for '+j.searched_for
    +` across ${j.rows_scanned} ledger rows`; return; }
  const hop = (j.ladder||[]).map((h,i)=>
    `  ${i+1}. ${h.provider||'?'}/${h.model||'?'}  status=${h.status} outcome=${h.outcome||''}`
    +` latency=${h.latency_s!=null?h.latency_s+'s':''} cost=${h.cost_usd!=null?h.cost_usd:''}`
    +(h.strategy?` strategy=${h.strategy}`:'')).join(String.fromCharCode(10));
  const cust = j.hermes_session||{};
  el.textContent =
`id            ${j.id}
when          ${j.when}
outcome       ${j.route_outcome}${j.failure_reason?' / '+j.failure_reason:''}${j.degrade_reason?' (degraded: '+j.degrade_reason+')':''}
rating        ${(j.rating||{}).source}  profile=${(j.rating||{}).profile_id||'-'}  band=${(j.rating||{}).band||'-'}
levels        ${JSON.stringify((j.rating||{}).required_categories||{})}
served by     ${(j.served_by||{}).provider}/${(j.served_by||{}).model}
chain         steps=${(j.chain||{}).steps} hops tried=${(j.chain||{}).hops_attempted} max=${(j.chain||{}).max_hops} served_hop=${(j.chain||{}).served_by_hop}
tokens        in=${(j.tokens||{}).in} out=${(j.tokens||{}).out} cache_read=${(j.tokens||{}).cache_read} reasoning=${(j.tokens||{}).reasoning}
cost          ${(j.cost||{}).usd==null?('unpriced ('+(j.cost||{}).reason+')'):(j.cost||{}).usd}  basis=${(j.cost||{}).basis||'-'}
wall          ${j.wall_time_s}s
hermes record ${cust.found===true?'FOUND ('+cust.source+', '+cust.model+')':(cust.found===false?'MISSING in state.db':'n/a — '+cust.reason)}
hops
${hop||'  (no per-hop detail in this row)'}`;
}

document.getElementById('dGo').onclick = ()=>dataSearch(true);
document.getElementById('dNext').onclick = ()=>{ if(dOffset+(document.getElementById('dLimit').value|0) < dLast)
  { dOffset += (document.getElementById('dLimit').value|0); dataSearch(false); } };
document.getElementById('dPrev').onclick = ()=>{ dOffset = Math.max(0, dOffset-(document.getElementById('dLimit').value|0)); dataSearch(false); };
document.getElementById('tGo').onclick = ()=>dataTraffic();
document.getElementById('bGo').onclick = ()=>dataBoard();
document.getElementById('fGo').onclick = ()=>showFlow(document.getElementById('fId').value.trim());
window.addEventListener('load', ()=>{ dataSearch(true); dataTraffic(); dataBoard(); });


async function loadMeta(){
  const {r,j}=await jget('/api/status');
  const m=document.getElementById('mode');
  if(j&&j.mode==='edit'){m.textContent='EDIT';m.className='mode ed';
    note('edit mode — writes require the key', 'ok');}
  else{m.textContent='READ-ONLY';m.className='mode ro';
    note('read-only mode — edits return 403', '');}
  const pf=await jget('/api/profiles');
  const sel=document.getElementById('profile');
  (pf.j?.profiles||[]).forEach(p=>{
    const o=document.createElement('option');
    o.value=p.id; o.textContent=p.id+(p.title?' — '+p.title:'');
    sel.appendChild(o);
  });
}
async function loadSettings(){
  const [p,d,pr]=await Promise.all([
    jget('/api/settings/providers'),
    jget('/api/settings/discounts'),
    jget('/api/settings/profiles')]);
  const pt=document.querySelector('#prov tbody'); pt.innerHTML='';
  (p.j?.rows||[]).forEach(r=>{
    const tr=document.createElement('tr');
    const en=r.archive===false||r.archive===undefined;
    tr.innerHTML=`<td>${r.id}</td><td>${r.plan||''}</td><td>${r.quota_unit||''}</td>
      <td><span class="tag ${en?'on':'off'}">${en?'on':'arch'}</span></td>
      <td>${r.key_env||''}</td>`;
    const td=document.createElement('td');
    const b=document.createElement('button'); b.textContent='toggle';
    b.style.fontSize='10px';
    b.onclick=async()=>{
      const {r:rr,jj}=await api('/api/settings/providers',{method:'POST',
        body:JSON.stringify({id:r.id,archive:!en})});
      flash(rr,jj); loadSettings();
    };
    td.appendChild(b); tr.appendChild(td); pt.appendChild(tr);
  });
  const dt=document.querySelector('#disc tbody'); dt.innerHTML='';
  (d.j?.rows||[]).forEach(r=>{
    const tr=document.createElement('tr');
    tr.innerHTML=`<td>${r.provider}</td><td>${r.model}</td>
      <td>${r.discount_type||''}</td><td>${r.value}</td><td>${r.valid_to||'—'}</td>`;
    dt.appendChild(tr);
  });
  const prt=document.querySelector('#prof tbody'); prt.innerHTML='';
  (pr.j?.rows||[]).forEach(r=>{
    const tr=document.createElement('tr');
    const reqs=Object.entries(r.requirements||{}).map(([c,l])=>`${c}=${l}`).join(' ');
    tr.innerHTML=`<td>${r.id}</td><td>${r.title||''}</td>
      <td style="font-size:11px">${reqs||'—'}</td>`;
    const td=document.createElement('td');
    const b=document.createElement('button'); b.textContent='edit'; b.style.fontSize='10px';
    b.onclick=()=>{document.getElementById('pId').value=r.id;};
    td.appendChild(b); tr.appendChild(td); prt.appendChild(tr);
  });
}
function flash(rr,jj){
  if(rr<300) note('saved ✓ '+(jj&&jj.backup?'':'')+' (HTTP '+rr+')','ok');
  else note('HTTP '+rr+' '+(jj&&jj.error||''),'err');
}
async function doResolve(useProject){
  document.getElementById('previewErr').textContent='';
  document.getElementById('head').textContent='';
  const q=new URLSearchParams();
  if(useProject){q.set('project','my-project');}
  else{
    const p=document.getElementById('profile').value;
    const pr=document.getElementById('project').value.trim();
    if(pr) q.set('project',pr); else if(p) q.set('profile',p);
    q.set('tokens_in',document.getElementById('tin').value||1000000);
    q.set('tokens_out',document.getElementById('tout').value||1000000);
  }
  const {r,j}=await jget('/api/preview?'+q.toString());
  if(!j||!j.ok){document.getElementById('previewErr').textContent=
    'resolve failed: '+(j&&j.error||'no response'); return;}
  const h=j.head;
  if(h) document.getElementById('head').textContent=
    `head → ${h.provider}/${h.model}  $${h.usd_1m}/M`;
  const tb=document.querySelector('#hops tbody'); tb.innerHTML='';
  (j.hops||[]).forEach(x=>{
    const tr=document.createElement('tr');
    const cost=x.est_cost_usd==null?'':('$'+x.est_cost_usd.toFixed(4));
    const ip=x.in_per_m==null?'':x.in_per_m;
    const op=x.out_per_m==null?'':x.out_per_m;
    tr.innerHTML=`<td>${x.hop}</td><td>${x.provider}</td><td>${x.model}</td>
      <td>${x.usd_1m}</td><td>${ip}</td><td>${op}</td><td>${cost}</td>
      <td>${x.data_class||''}</td>`;
    tb.appendChild(tr);
  });
  document.getElementById('raw').textContent=JSON.stringify(j.resolve,null,2);
  const g=j.gate_reasons||[];
  document.getElementById('gates').innerHTML = g.length
    ? '<b>gate reasons ('+g.length+'):</b><br>'+g.map(s=>'• '+s).join('<br>')
    : 'no gate reasons';
}
document.getElementById('savekey').onclick=()=>{
  const k=document.getElementById('apikey').value;
  if(k) localStorage.setItem('rw_key',k);
  note('key stored locally for this session','ok');
};
document.getElementById('resolve').onclick=()=>doResolve(false);
document.getElementById('resolveR').onclick=()=>doResolve(true);
document.getElementById('pSet').onclick=async()=>{
  const id=document.getElementById('pId').value.trim();
  const cat=document.getElementById('pCat').value.trim();
  const lvl=parseInt(document.getElementById('pLvl').value,10);
  if(!id||!cat||isNaN(lvl)){note('need id, category, level','err');return;}
  const {r,jj}=await api('/api/settings/profiles/requirements',{method:'POST',
    body:JSON.stringify({task_id:id,category:cat,level:lvl})});
  flash(r,jj); loadSettings();
};
document.getElementById('dSet').onclick=async()=>{
  const prov=document.getElementById('dProv').value.trim();
  const model=document.getElementById('dModel').value.trim();
  const type=document.getElementById('dType').value.trim();
  const val=document.getElementById('dVal').value.trim();
  if(!prov||!model){note('need provider + model','err');return;}
  const body={provider:prov,model:model};
  if(type) body.discount_type=type;
  if(val!=='') body.value=parseFloat(val);
  const {r,jj}=await api('/api/settings/discounts',{method:'POST',
    body:JSON.stringify(body)});
  flash(r,jj); loadSettings();
};
loadMeta(); loadSettings();
</script>
</body>
</html>"""


if __name__ == "__main__":
    sys.exit(main())
