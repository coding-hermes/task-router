#!/usr/bin/env python3
"""router_ui_page.py — the Data Command Center page (TR-150).

Served by the router itself at GET /ui. ONE self-contained document: every asset is inline, there is
no CDN and no third-party request at view time, and the only fetches it makes are same-origin API
calls. That is a contract, not a style choice — it is asserted in tests/test_ui_page.py, because a
page that pulls a stylesheet from the internet is not a tool you can debug from when the network is
the thing that is broken.

House rules this page follows, all of them from the row that asked for it:
  * every panel STATES THE WINDOW it scanned, and an empty or stale panel says so in words rather
    than rendering as a zero (a 0 that means "no data" is a lie the fleet has already been bitten by);
  * the ledger search reports rows_scanned / total_matched / truncation, so a result can never look
    complete when it was a window;
  * the first screen is a search box, and the keyboard drives it ( / focuses, Esc clears, arrows move,
    Enter opens the row );
  * read-only by construction: v1 exposes no write path at all.
"""
import json

#: The one page. Plain string; no templating, no build step, no external asset.
PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>task-router — data command center</title>
<style>
  :root{
    --bg:#0b0e11; --panel:#12171c; --panel2:#171d24; --line:#232c36; --fg:#d7e1ea;
    --dim:#7f8c9b; --accent:#5fd1a4; --warn:#e6b455; --bad:#e0705f; --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);font:12px/1.45 var(--mono)}
  a{color:var(--accent)}
  header{position:sticky;top:0;z-index:5;background:linear-gradient(#0b0e11,#0b0e11f2);border-bottom:1px solid var(--line);padding:8px 12px;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
  .brand{color:var(--accent);font-weight:700;letter-spacing:.04em}
  .id{color:var(--dim)}
  .id b{color:var(--fg);font-weight:600}
  .grow{flex:1}
  input,select{background:var(--panel2);border:1px solid var(--line);color:var(--fg);font:12px var(--mono);padding:6px 8px;border-radius:4px}
  input:focus,select:focus{outline:1px solid var(--accent);border-color:var(--accent)}
  #q{min-width:340px}
  main{display:grid;grid-template-columns:minmax(320px,1fr) minmax(420px,1.6fr);gap:10px;padding:10px}
  @media(max-width:1100px){main{grid-template-columns:1fr}}
  section{background:var(--panel);border:1px solid var(--line);border-radius:6px;overflow:hidden;min-width:0}
  section>h2{margin:0;padding:7px 10px;background:var(--panel2);border-bottom:1px solid var(--line);font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:var(--dim);display:flex;gap:8px;align-items:center}
  .body{padding:8px 10px;max-height:44vh;overflow:auto}
  .body.tall{max-height:56vh}
  table{border-collapse:collapse;width:100%}
  th,td{text-align:left;padding:3px 6px;border-bottom:1px solid #1b232c;white-space:nowrap}
  th{color:var(--dim);font-weight:600;position:sticky;top:0;background:var(--panel)}
  tbody tr{cursor:pointer}
  tbody tr:hover{background:#182028}
  tbody tr.sel{background:#1d2a33;outline:1px solid var(--accent)}
  .num{text-align:right}
  .tag{border:1px solid var(--line);border-radius:3px;padding:0 4px;color:var(--dim)}
  .ok{color:var(--accent)} .warn{color:var(--warn)} .bad{color:var(--bad)} .dim{color:var(--dim)}
  .note{padding:6px 10px;border-top:1px solid var(--line);color:var(--dim);background:#0f141a}
  .note.warn{color:var(--warn)} .note.bad{color:var(--bad)}
  pre{margin:0;white-space:pre-wrap;word-break:break-word;color:#cfe0ee}
  .kv{display:grid;grid-template-columns:150px 1fr;gap:2px 10px}
  .kv div:nth-child(odd){color:var(--dim)}
  footer{padding:8px 12px;color:var(--dim);border-top:1px solid var(--line);display:flex;gap:14px;flex-wrap:wrap}
  kbd{border:1px solid var(--line);border-bottom-width:2px;border-radius:3px;padding:0 4px;background:var(--panel2);color:var(--fg)}
  .skel{color:var(--dim)}
</style>
</head>
<body>
<header>
  <span class="brand">router://data-command-center</span>
  <span class="id" id="ident">identity…</span>
  <span class="grow"></span>
  <input id="q" placeholder="search ledger  ( / to focus · Esc clears · Enter opens )" autocomplete="off">
  <select id="f_provider"><option value="">provider…</option></select>
  <select id="f_band"><option value="">band…</option></select>
  <select id="f_outcome"><option value="">outcome…</option><option>success</option><option>failed</option></select>
  <select id="win"><option value="24">24h</option><option value="72">72h</option><option value="168">7d</option></select>
</header>

<main>
  <div style="display:grid;gap:10px;align-content:start">
    <section>
      <h2>traffic + cost <span class="dim" id="traffic_window"></span></h2>
      <div class="body" id="traffic"><span class="skel">loading…</span></div>
      <div class="note" id="traffic_note">—</div>
    </section>
    <section>
      <h2>gates <span class="dim">quota · health · circuit</span></h2>
      <div class="body" id="gates"><span class="skel">loading…</span></div>
      <div class="note" id="gates_note">—</div>
    </section>
    <section>
      <h2>board <span class="dim" id="board_window"></span></h2>
      <div class="body" id="board"><span class="skel">loading…</span></div>
      <div class="note" id="board_note">—</div>
    </section>
  </div>

  <div style="display:grid;gap:10px;align-content:start">
    <section>
      <h2>ledger <span class="dim" id="ledger_window"></span></h2>
      <div class="body tall" id="ledger"><span class="skel">loading…</span></div>
      <div class="note" id="ledger_note">—</div>
    </section>
    <section>
      <h2>request flow <span class="dim">rating → requirements → chain → hops → served lane → cost</span></h2>
      <div class="body tall" id="flow"><span class="skel">select a ledger row</span></div>
      <div class="note" id="flow_note">—</div>
    </section>
  </div>
</main>

<footer>
  <span>READ-ONLY: no write path exists on this page.</span>
  <span>panels state the window they scanned; empty or stale data says so instead of rendering 0.</span>
  <span><kbd>/</kbd> search <kbd>Esc</kbd> clear <kbd>↑</kbd><kbd>↓</kbd> move <kbd>Enter</kbd> open row</span>
</footer>

<script>
'use strict';
const $ = (s) => document.querySelector(s);
const esc = (v) => String(v === null || v === undefined ? '' : v).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const num = (v, d) => (v === null || v === undefined) ? '<span class="dim">null</span>' : Number(v).toFixed(d === undefined ? 2 : d);
let rows = [], sel = 0;

async function j(url){ const r = await fetch(url, {headers:{'accept':'application/json'}}); if(!r.ok) throw new Error(url+' -> '+r.status); return r.json(); }

/* ---- identity -------------------------------------------------------- */
async function identity(){
  try{
    const h = await j('/health');
    const c = h.code || {};
    $('#ident').innerHTML = 'commit <b>'+esc(String(c.repo_commit||h.commit||'?').slice(0,12))+'</b>'
      + ' · loaded <b>'+esc(String(c.loaded_commit||'?').slice(0,12))+'</b>'
      + (c.stale ? ' <span class="warn">stale</span>' : ' <span class="ok">current</span>')
      + (h.registry && h.registry.mtime ? ' · registry <span class="dim">'+esc(h.registry.mtime)+'</span>' : '');
  }catch(e){ $('#ident').innerHTML = '<span class="bad">identity unavailable: '+esc(e.message)+'</span>'; }
}

/* ---- traffic + cost by band ------------------------------------------ */
async function traffic(){
  const win = $('#win').value;
  $('#traffic_window').textContent = 'window '+win+'h';
  try{
    const d = await j('/proxy/stats?windows='+win+'&grouping=model_band');
    const list = (d.averages || d.rows || []);
    if(!list.length){ $('#traffic').innerHTML = '<span class="dim">no proxied traffic in this window</span>'; }
    else {
      $('#traffic').innerHTML = '<table><thead><tr><th>lane</th><th>band</th><th class="num">n</th>'
        + '<th class="num">cost/task</th><th class="num">tokens</th><th class="num">success</th></tr></thead><tbody>'
        + list.slice(0,40).map(r => {
            const cost = r['avg_cost_task_'+win+'h'];
            return '<tr><td>'+esc((r.provider||'')+'/'+(r.model||''))+'</td><td class="dim">'+esc(String(r.complexity_sig||'—').slice(0,18))+'</td>'
              + '<td class="num">'+esc(r.n_samples)+'</td>'
              + '<td class="num">'+(cost===null||cost===undefined ? '<span class="dim">no priced sample</span>' : num(cost,6))+'</td>'
              + '<td class="num">'+num(r['avg_tokens_total_'+win+'h'],0)+'</td>'
              + '<td class="num">'+(r.success_rate===null||r.success_rate===undefined ? '<span class="dim">unknown</span>' : num(r.success_rate*100,1)+'%')+'</td></tr>';
          }).join('') + '</tbody></table>';
    }
    $('#traffic_note').textContent = (d.note || 'source: /proxy/stats') + ' · samples per row, cost rows disclose priced samples';
  }catch(e){ $('#traffic').innerHTML = '<span class="bad">traffic unavailable</span>'; $('#traffic_note').className='note bad'; $('#traffic_note').textContent = e.message; }
}

/* ---- gates ----------------------------------------------------------- */
async function gates(){
  try{
    const c = await j('/circuit/status');
    const pairs = c.pairs || c || {};
    const keys = Object.keys(pairs);
    $('#gates').innerHTML = keys.length
      ? '<table><thead><tr><th>lane</th><th>class</th><th>until</th><th>reason</th></tr></thead><tbody>'
        + keys.map(k => { const v = pairs[k]||{};
            return '<tr><td>'+esc(k)+'</td><td>'+esc(v.class||'—')+'</td><td class="dim">'+esc(String(v.open_until||'').slice(0,19))+'</td><td class="dim">'+esc(String(v.reason||'').slice(0,40))+'</td></tr>'; }).join('')
        + '</tbody></table>'
      : '<span class="dim">no open circuit breakers</span>';
    $('#gates_note').textContent = 'circuit pairs: '+keys.length+' · source: /circuit/status';
  }catch(e){ $('#gates').innerHTML = '<span class="bad">gates unavailable</span>'; $('#gates_note').textContent = e.message; }
}

/* ---- board ------------------------------------------------------------ */
async function board(){
  const q = $('#q').value.trim();
  $('#board_window').textContent = q ? 'filter: '+q : 'all rows';
  try{
    const d = await j('/api/ui/board?limit=25'+(q ? '&q='+encodeURIComponent(q) : ''));
    const list = d.rows || [];
    $('#board').innerHTML = list.length
      ? '<table><thead><tr><th>id</th><th>status</th><th>title</th><th>updated</th></tr></thead><tbody>'
        + list.map(r => '<tr><td>'+esc(r.id)+'</td><td>'+esc(r.status)+'</td><td>'+esc(String(r.title||'').slice(0,64))+'</td><td class="dim">'+esc(String(r.updated_at||r.completed_at||'').slice(0,19))+'</td></tr>').join('')
        + '</tbody></table>'
      : '<span class="dim">no matching board rows</span>';
    $('#board_note').textContent = 'scanned '+d.rows_scanned+' of '+d.total_rows+' board row(s) · matched '+d.total_matched
      + (d.scan_truncated ? ' · SCAN TRUNCATED' : '');
    $('#board_note').className = 'note'+(d.scan_truncated?' warn':'');
  }catch(e){ $('#board').innerHTML = '<span class="bad">board unavailable</span>'; $('#board_note').textContent = e.message; }
}

/* ---- ledger search ---------------------------------------------------- */
async function ledger(){
  const q = $('#q').value.trim();
  const p = new URLSearchParams({limit:'50'});
  if(q) p.set('q', q);
  if($('#f_provider').value) p.set('provider', $('#f_provider').value);
  if($('#f_band').value) p.set('band', $('#f_band').value);
  if($('#f_outcome').value) p.set('outcome', $('#f_outcome').value);
  $('#ledger_window').textContent = 'filters: '+(q ? 'q='+q+' ' : '')+[...p.keys()].filter(k=>k!=='limit').length+' active';
  try{
    const d = await j('/api/ui/ledger?'+p.toString());
    rows = d.rows || []; sel = 0;
    $('#ledger').innerHTML = rows.length
      ? '<table><thead><tr><th>ts</th><th>source</th><th>lane</th><th>band</th><th>outcome</th>'
        + '<th class="num">steps</th><th class="num">in/out</th><th class="num">cost</th></tr></thead><tbody>'
        + rows.map((r,i) => {
            const ok = r.success === true ? '<span class="ok">ok</span>' : (r.success === false ? '<span class="bad">fail</span>' : '<span class="dim">'+(esc(r.route_outcome)||'unknown')+'</span>');
            const cost = (r.cost_usd === null || r.cost_usd === undefined)
              ? '<span class="dim" title="'+esc(r.price_basis||'')+'">no price</span>' : num(r.cost_usd, 6);
            return '<tr data-i="'+i+'"><td class="dim">'+esc(String(r.ts||'').slice(0,19))+'</td><td class="dim">'+esc(r.source_system)+'</td>'
              + '<td>'+esc((r.provider||'')+'/'+(r.model||''))+'</td><td class="dim">'+esc(String(r.complexity_sig||'—').slice(0,14))+'</td>'
              + '<td>'+ok+'</td><td class="num">'+esc(r.steps===undefined?'—':r.steps)+'</td>'
              + '<td class="num dim">'+esc((r.tokens_in||0)+'/'+(r.tokens_out||0))+'</td><td class="num">'+cost+'</td></tr>';
          }).join('') + '</tbody></table>'
      : '<span class="dim">no ledger row matched</span>';
    const n = $('#ledger_note');
    n.textContent = 'scanned '+d.rows_scanned+' row(s) · matched '+d.total_matched+' · showing '+d.returned
      + (d.scan_truncated ? ' · SCAN TRUNCATED at the scan limit' : '')
      + (d.truncated ? ' · more matches exist beyond this page' : '');
    n.className = 'note'+(d.truncated ? ' warn':'');
    document.querySelectorAll('#ledger tbody tr').forEach(tr => tr.addEventListener('click', () => { sel = +tr.dataset.i; paint(); flow(rows[sel]); }));
    if(rows.length) flow(rows[0]);
  }catch(e){ $('#ledger').innerHTML = '<span class="bad">ledger unavailable</span>'; $('#ledger_note').textContent = e.message; }
}

function paint(){
  document.querySelectorAll('#ledger tbody tr').forEach(tr => tr.classList.toggle('sel', +tr.dataset.i === sel));
}

/* ---- flow drill-down -------------------------------------------------- */
function flow(r){
  if(!r){ $('#flow').innerHTML = '<span class="dim">select a ledger row</span>'; return; }
  const ce = r.chain_evidence || {};
  const cl = r.classifier || r.classifier_evidence || {};
  const ses = r.session || {};
  const req = r.required_categories || (r.requirements && r.requirements.matrix) || null;
  const hops = (ce.hops || r.hops || []);
  const line = (k,v) => '<div>'+esc(k)+'</div><div>'+v+'</div>';
  let h = '<div class="kv">';
  h += line('session', esc(r.session_id||'—'));
  h += line('lane', '<b>'+esc((r.provider||'')+'/'+(r.model||''))+'</b>');
  h += line('outcome', r.success===true?'<span class="ok">success</span>':(r.success===false?'<span class="bad">'+(esc(r.failure_reason)||'failed')+'</span>':'<span class="dim">'+esc(r.route_outcome||'unknown')+'</span>'));
  h += line('cost', (r.cost_usd===null||r.cost_usd===undefined)?'<span class="dim">no price on this hop</span>':num(r.cost_usd,8)+' <span class="dim">'+(esc(r.price_basis)||'')+'</span>');
  h += line('rating', req ? esc(JSON.stringify(req)) : '<span class="dim">none recorded</span>');
  h += line('rating source', esc(r.complexity_source||'—'));
  h += line('classifier', esc(JSON.stringify(cl)));
  h += line('chain', esc((ce.chain_length||(ce.considered||0))||'—')+' considered · '+(ce.truncated?'<span class="warn">chain truncated</span>':'full')
      + ' · excluded '+esc((ce.excluded||ce.exclusions||0)));
  h += line('hops', esc(r.hops_attempted===undefined?'—':r.hops_attempted)+' of '+(esc(r.max_hops)||'—'));
  h += line('steps/tokens', esc(r.steps===undefined?'—':r.steps)+' steps · '+esc(r.tokens_in||0)+' in / '+esc(r.tokens_out||0)+' out'
      + (r.cache_read_tokens?(' · cache read '+esc(r.cache_read_tokens)):''));
  h += line('session stats', Object.keys(ses).length ? esc(JSON.stringify(ses)) : '<span class="dim">no session block on this row</span>');
  h += '</div>';
  if(hops.length){ h += '<div style="margin-top:6px"><span class="dim">hops attempted</span><table><thead><tr><th>#</th><th>lane</th><th>outcome</th><th>ms</th></tr></thead><tbody>'
      + hops.map(x=>'<tr><td>'+esc(x.hop||'')+'</td><td>'+esc((x.provider||'')+'/'+(x.model||''))+'</td><td>'+esc(x.outcome||x.reason||'')+'</td><td class="dim">'+esc(x.latency_ms||'')+'</td></tr>').join('') + '</tbody></table></div>'; }
  $('#flow').innerHTML = h;
  $('#flow_note').textContent = 'raw row · ' + JSON.stringify(r).length + ' bytes · keys: ' + Object.keys(r).length;
}

/* ---- keyboard -------------------------------------------------------- */
document.addEventListener('keydown', (e) => {
  if(e.key === '/' && document.activeElement !== $('#q')){ e.preventDefault(); $('#q').focus(); return; }
  if(e.key === 'Escape'){ $('#q').value=''; $('#f_provider').value=''; $('#f_band').value=''; $('#f_outcome').value=''; refresh(); return; }
  if(!rows.length) return;
  if(e.key === 'ArrowDown' || e.key === 'j'){ sel = Math.min(sel+1, rows.length-1); paint(); flow(rows[sel]); }
  if(e.key === 'ArrowUp' || e.key === 'k'){ sel = Math.max(sel-1, 0); paint(); flow(rows[sel]); }
  if(e.key === 'Enter'){ const tr = document.querySelector('#ledger tbody tr.sel'); if(tr) tr.scrollIntoView({block:'center'}); }
});

let t = null;
$('#q').addEventListener('input', () => { clearTimeout(t); t = setTimeout(refresh, 250); });
['#f_provider','#f_band','#f_outcome'].forEach(s => $(s).addEventListener('change', refresh));
$('#win').addEventListener('change', traffic);

async function filters(){
  try{
    const p = await j('/providers');
    const list = (p.providers || p || []).map(x => x.id || x.provider || x).filter(Boolean);
    $('#f_provider').insertAdjacentHTML('beforeend', list.map(x => '<option>'+esc(x)+'</option>').join(''));
  }catch(e){}
  try{
    const d = await j('/proxy/stats?windows=168&grouping=band');
    const bands = [...new Set((d.averages||[]).map(r => r.complexity_sig).filter(Boolean))];
    $('#f_band').insertAdjacentHTML('beforeend', bands.map(x => '<option>'+esc(x)+'</option>').join(''));
  }catch(e){}
}

function refresh(){ ledger(); board(); }
identity(); traffic(); gates(); filters().then(refresh); refresh();
setInterval(identity, 60000);
</script>
</body>
</html>
"""


def page_html():
    """The whole page. One string, no build step, nothing fetched from the network."""
    return PAGE


def board_search(query, path):
    """Search the board JSONL for the page's board panel.

    Same honesty contract as the ledger search: report how much was read, so a filtered view cannot
    imply it looked at the whole board when it stopped early.
    """
    def one(name, default=None):
        v = (query or {}).get(name)
        if isinstance(v, list):
            v = v[0] if v else None
        return v if v not in (None, '') else default

    def as_int(name, default):
        try:
            return int(str(one(name, default)).strip())
        except (TypeError, ValueError):
            return default

    q = (one('q') or '').lower()
    status = one('status')
    limit = max(1, min(as_int('limit', 25), 200))
    offset = max(0, as_int('offset', 0))
    total_rows = 0
    matched = 0
    page = []
    try:
        with open(path, encoding='utf-8', errors='replace') as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                total_rows += 1
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if status and d.get('status') != status:
                    continue
                if q:
                    hay = ' '.join(str(d.get(k) or '') for k in
                                   ('id', 'title', 'status', 'priority', 'reasoning',
                                    'foreman_note', 'files_changed')).lower()
                    if q not in hay:
                        continue
                matched += 1
                if offset <= matched - 1 < offset + limit:
                    page.append(d)
    except OSError as e:
        return {'error': f'board unreadable: {e}', 'rows': [], 'total_rows': 0,
                'total_matched': 0, 'rows_scanned': 0, 'scan_truncated': False}
    keep = ('id', 'title', 'status', 'priority', 'worker_status', 'created_at', 'updated_at',
            'completed_at', 'foreman_note')
    trimmed = [{k: d.get(k) for k in keep if k in d} for d in page]
    return {'rows': trimmed, 'returned': len(trimmed), 'total_rows': total_rows,
            'rows_scanned': total_rows, 'total_matched': matched,
            'scan_truncated': False, 'truncated': matched > offset + len(trimmed),
            'limit': limit, 'offset': offset, 'path': path}
