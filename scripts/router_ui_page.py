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
  <select id="series_group"><option value="total">all traffic</option><option value="band">by band</option><option value="lane">by lane</option></select>
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
      <h2>over time <span class="dim" id="series_window"></span></h2>
      <div class="body" id="series"><span class="skel">loading…</span></div>
      <div class="note" id="series_note">—</div>
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
/* a raw epoch in a table is unreadable; local time is what you debug with */
const ts = (v) => (v === null || v === undefined || isNaN(Number(v))) ? '<span class="dim">—</span>'
  : new Date(Number(v) * 1000).toLocaleString(undefined, {month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit', second:'2-digit'});
/* the router records a request that had no hop under the pseudo-lane none/none; say so rather than
   letting it read as a real lane in a traffic view */
const lane = (p, m) => (!p || p === 'none') ? '<span class="warn">(no hop)</span>' : esc(p + '/' + m);
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
    const w = (d.windows || {})[win+'h'] || {};
    const groups = Object.values(w.groups || {});
    if(!groups.length){
      $('#traffic').innerHTML = '<span class="dim">no proxied traffic in this window</span>';
      $('#traffic_note').textContent = 'scanned '+num(w.rows_scanned,0)+' ledger row(s) · '
        + num(w.proxy_rows_matched,0)+' proxied row(s) · 0 inside the '+win+'h window · '
        + 'computed '+num(d.age_s,1)+'s ago (ttl '+num(d.ttl_s,0)+'s)';
      $('#traffic_note').className = 'note';
      return;
    }
    groups.sort((a,b) => (b.samples||0) - (a.samples||0));
    $('#traffic').innerHTML = '<table><thead><tr><th>lane</th><th>band</th><th class="num">n</th>'
      + '<th class="num">cost/task</th><th class="num">priced</th><th class="num">wall s</th>'
      + '<th class="num">success</th><th>failures</th></tr></thead><tbody>'
      + groups.slice(0,40).map(r => {
          const cost = (r.cost_usd_per_task === null || r.cost_usd_per_task === undefined)
            ? '<span class="dim" title="'+esc(r.cost_reason||'')+'">no priced sample</span>'
            : num(r.cost_usd_per_task, 6);
          const fails = Object.entries(r.failure_reasons || {}).map(([k,v]) => esc(k)+'×'+v).join(' ') || '<span class="dim">—</span>';
          const band = r.band ? esc(String(r.band).slice(0,16)) : '<span class="dim">'+(esc(r.band_source)||'unknown')+'</span>';
          return '<tr><td>'+lane(r.provider, r.model)+'</td><td>'+band+'</td>'
            + '<td class="num">'+esc(r.samples)+'</td><td class="num">'+cost+'</td>'
            + '<td class="num dim">'+(r.cost_samples===null||r.cost_samples===undefined?'—':esc(r.cost_samples))+'</td>'
            + '<td class="num dim">'+(r.wall_time_s_per_task===null||r.wall_time_s_per_task===undefined?'—':num(r.wall_time_s_per_task,0))+'</td>'
            + '<td class="num">'+(r.success_rate===null||r.success_rate===undefined ? '<span class="dim">unknown</span>' : num(r.success_rate*100,1)+'%')+'</td>'
            + '<td class="dim">'+fails+'</td></tr>';
        }).join('') + '</tbody></table>';
    $('#traffic_note').textContent = 'window '+win+'h · '+num(w.rows_in_window,0)+' proxied row(s) in window · '
      + num(w.proxy_rows_matched,0)+' of '+num(w.rows_scanned,0)+' scanned · computed '+num(d.age_s,1)+'s ago (ttl '+num(d.ttl_s,0)+'s)'
      + ' · cost rows disclose priced samples';
    $('#traffic_note').className = 'note';
  }catch(e){ $('#traffic').innerHTML = '<span class="bad">traffic unavailable</span>'; $('#traffic_note').className='note bad'; $('#traffic_note').textContent = e.message; }
}

/* ---- traffic over time (TR-152) ---------------------------------------- */
const BAR = (n, max) => {
  if(!max || !n) return '<span class="dim">·</span>';
  const chars = '▁▂▃▄▅▆▇█';
  const i = Math.max(0, Math.min(chars.length-1, Math.round((n/max)*(chars.length-1))));
  return '<span class="ok">'+chars[i].repeat(Math.max(1, Math.min(12, Math.ceil(n/max*12))))+'</span>';
};
async function series(){
  const win = $('#win').value;
  const grouping = $('#series_group') ? $('#series_group').value : 'total';
  $('#series_window').textContent = 'hourly · window '+win+'h · by '+grouping;
  try{
    const d = await j('/api/ui/series?bucket=hour&window_h='+win+'&group='+grouping);
    const buckets = d.buckets || [];
    if(!buckets.length){ $('#series').innerHTML = '<span class="dim">no buckets in this window</span>'; }
    else {
      const max = Math.max(...buckets.map(b => b.requests || 0), 1);
      $('#series').innerHTML = '<table><thead><tr><th>hour</th><th>key</th><th>req</th><th></th>'
        + '<th class="num">served</th><th class="num">failed</th><th class="num">cost</th><th class="num">priced</th></tr></thead><tbody>'
        + buckets.slice(-40).map(b => {
            const cost = (b.cost_usd === null || b.cost_usd === undefined)
              ? '<span class="dim" title="'+esc(b.cost_reason||'')+'">no priced sample</span>' : num(b.cost_usd, 4);
            return '<tr><td class="dim">'+new Date(b.start_ts*1000).toLocaleString(undefined,{month:'2-digit',day:'2-digit',hour:'2-digit'})+'</td>'
              + '<td class="dim">'+esc(b.key||'all')+'</td><td class="num">'+esc(b.requests)+'</td><td>'+BAR(b.requests, max)+'</td>'
              + '<td class="num '+(b.served?'ok':'dim')+'">'+esc(b.served)+'</td>'
              + '<td class="num '+(b.failed?'bad':'dim')+'">'+esc(b.failed)+'</td>'
              + '<td class="num">'+cost+'</td><td class="num dim">'+esc(b.cost_samples)+'</td></tr>';
          }).join('') + '</tbody></table>';
    }
    $('#series_note').textContent = d.rows_in_window + ' row(s) in the '+d.window_h+'h window · '
      + d.rows_scanned + ' store row(s) scanned (' + (d.scan_window||'') + ') · every bucket carries its own sample and priced count';
  }catch(e){ $('#series').innerHTML = '<span class="bad">series unavailable</span>'; $('#series_note').textContent = e.message; }
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
        + list.map(r => '<tr><td>'+esc(r.id)+'</td><td>'+esc(r.status)+'</td><td>'+esc(String(r.title||'').slice(0,64))+'</td><td class="dim">'+esc(String(r.updated_at||r.completed_at||'').replace('T',' ').slice(0,19))+'</td></tr>').join('')
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
  const p = new URLSearchParams({limit:'50', order:'recent'});
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
            return '<tr data-i="'+i+'"><td class="dim">'+ts(r.ts)+'</td><td class="dim">'+esc(r.source_system)+'</td>'
              + '<td>'+lane(r.provider, r.model)+'</td><td class="dim">'+esc(String(r.complexity_sig||'—').slice(0,14))+'</td>'
              + '<td>'+ok+'</td><td class="num">'+esc(r.steps===undefined?'—':r.steps)+'</td>'
              + '<td class="num dim">'+esc((r.tokens_in||0)+'/'+(r.tokens_out||0))+'</td><td class="num">'+cost+'</td></tr>';
          }).join('') + '</tbody></table>'
      : '<span class="dim">no ledger row matched</span>';
    const n = $('#ledger_note');
    n.textContent = (d.scan_window||'')+' · scanned '+d.rows_scanned+' row(s) · matched '+d.total_matched+' · showing '+d.returned
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
  h += line('when', ts(r.ts));
  h += line('session', esc(r.session_id||'—'));
  h += line('lane', (r.provider && r.provider !== 'none') ? '<b>'+esc(r.provider+'/'+(r.model||''))+'</b>' : '<span class="warn">(no hop — nothing eligible after gating)</span>');
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
$('#win').addEventListener('change', function(){ traffic(); series(); });
if($('#series_group')) $('#series_group').addEventListener('change', series);

async function filters(){
  try{
    const p = await j('/providers');
    const list = (p.providers || p || []).map(x => x.id || x.provider || x).filter(Boolean);
    $('#f_provider').insertAdjacentHTML('beforeend', list.map(x => '<option>'+esc(x)+'</option>').join(''));
  }catch(e){}
  try{
    const d = await j('/proxy/stats?windows=168&grouping=model_band');
    const w = (d.windows || {})['168h'] || {};
    const bands = [...new Set(Object.values(w.groups || {}).map(r => r.band).filter(Boolean))];
    $('#f_band').insertAdjacentHTML('beforeend', bands.map(x => '<option>'+esc(x)+'</option>').join(''));
  }catch(e){}
}

function refresh(){ ledger(); board(); }
identity(); traffic(); series(); gates(); filters().then(refresh); refresh();
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


# ------------------------------------------------------------------ TR-152 series

def _series_float(query, name, default=None):
    v = (query or {}).get(name)
    if isinstance(v, list):
        v = v[0] if v else None
    try:
        return float(v) if v not in (None, '') else default
    except (TypeError, ValueError):
        return default


def series(query, store_path, now_s=None):
    """TR-152: traffic and cost over time, bucketed, by lane / by band / total.

    Every bucket carries its own sample count AND how many of its samples were priced, because a cost
    column built from unpriced rows would read as free. Buckets with no traffic are emitted as empty
    rather than omitted, so a quiet hour is visible as a quiet hour instead of a gap in the axis.
    The response names the window it read and how many rows it scanned.
    """
    def one(name, default=None):
        v = (query or {}).get(name)
        if isinstance(v, list):
            v = v[0] if v else None
        return v if v not in (None, '') else default

    bucket = (one('bucket') or 'hour').lower()
    if bucket not in ('hour', 'day'):
        bucket = 'hour'
    width = 3600.0 if bucket == 'hour' else 86400.0
    window_h = _series_float(query, 'window_h', 24.0)
    group = (one('group') or 'total').lower()
    scan_limit = int(_series_float(query, 'scan_limit', 200000) or 200000)
    window_s = max(1.0, window_h) * 3600.0

    import collections
    import time as _time
    # injectable clock: a fixed timestamp is how a caller (or a test) pins a window
    now = float(now_s) if now_s is not None else _time.time()
    cutoff = now - window_s
    # tail-scan: the newest scan_limit rows cover the requested window for any realistic window
    tail = collections.deque(maxlen=max(1, scan_limit))
    scanned = 0
    bad = 0
    try:
        with open(store_path, encoding='utf-8', errors='replace') as fh:
            for line in fh:
                if line.strip():
                    tail.append(line)
                    scanned += 1
    except OSError as e:
        return {'error': f'store unreadable: {e}', 'buckets': [], 'rows_scanned': 0}

    rows = []
    parse_failed = 0
    for line in tail:
        try:
            d = json.loads(line)
        except ValueError:
            parse_failed += 1
            continue
        ts = d.get('ts')
        if not isinstance(ts, (int, float)) or ts < cutoff:
            continue
        rows.append(d)

    keys = collections.defaultdict(lambda: {
        'requests': 0, 'served': 0, 'failed': 0, 'unknown': 0, 'lanes': set(),
        'tokens_in': 0, 'tokens_out': 0, 'cost': 0.0, 'cost_samples': 0,
        'hops': collections.Counter(), 'bands': collections.Counter()})
    for d in rows:
        b = int(d['ts'] // width) * width
        lane = f"{d.get('provider')}/{d.get('model')}"
        band = d.get('complexity_sig') or 'unknown'
        if group == 'band':
            k = band
        elif group == 'lane':
            k = lane
        else:
            k = 'all'
        g = keys[(b, k)]
        g['requests'] += 1
        if d.get('success') is True:
            g['served'] += 1
        elif d.get('success') is False:
            g['failed'] += 1
        else:
            g['unknown'] += 1
        g['lanes'].add(lane)
        g['tokens_in'] += int(d.get('tokens_in') or 0)
        g['tokens_out'] += int(d.get('tokens_out') or 0)
        g['bands'][band] += 1
        c = d.get('cost_usd')
        if isinstance(c, (int, float)):
            g['cost'] += float(c)
            g['cost_samples'] += 1
        hops = d.get('hops_attempted')
        if hops is None:
            hops = 'no-hops' if d.get('route_outcome') == 'no-hops' else 'unknown'
        g['hops'][str(hops)] += 1

    # emit every bucket in the window, including the quiet ones
    first = int(cutoff // width) * width
    buckets = []
    b = first
    while b <= int(now // width) * width:
        labels = sorted({k for (bb, k) in keys if bb == b})
        if not labels:
            buckets.append({'start_ts': b, 'group': group, 'requests': 0, 'served': 0, 'failed': 0,
                            'unknown': 0, 'lanes': 0, 'tokens_in': 0, 'tokens_out': 0,
                            'cost_usd': None, 'cost_samples': 0, 'hops': {}, 'note': 'no traffic in this bucket'})
        for k in labels:
            g = keys[(b, k)]
            buckets.append({'start_ts': b, 'group': group, 'key': k,
                            'requests': g['requests'], 'served': g['served'], 'failed': g['failed'],
                            'unknown': g['unknown'], 'lanes': len(g['lanes']),
                            'tokens_in': g['tokens_in'], 'tokens_out': g['tokens_out'],
                            'cost_usd': (round(g['cost'], 8) if g['cost_samples'] else None),
                            'cost_samples': g['cost_samples'],
                            'cost_reason': (None if g['cost_samples'] else 'no priced sample in this bucket'),
                            'hops': dict(g['hops'])})
        b += int(width)
    return {'bucket': bucket, 'bucket_s': width, 'window_h': window_h, 'group': group,
            'buckets': buckets, 'bucket_count': len(buckets),
            'rows_in_window': len(rows), 'rows_scanned': scanned, 'parse_failed': parse_failed,
            'scan_window': f'newest {scan_limit} of {scanned} store rows',
            'window_start_ts': cutoff, 'window_end_ts': now,
            'store': store_path}
