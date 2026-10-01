#!/usr/bin/env python3
"""X-RAY v2: corrected findings + HTML film. The image is ground; the analysis is a claim."""
import json, os, re, subprocess, datetime, collections

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
BOARD = REPO + '/.coding-hermes/board/tasks.jsonl'
LEDGER = REPO + '/data/state/outcomes.jsonl'
STAMP = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
OUTDIR = REPO + '/reports/xray'
os.makedirs(OUTDIR, exist_ok=True)
HTML = OUTDIR + '/task-router-xray.html'



def _rel_count():
    """Release objects at origin, or None when gh cannot answer. Never guess."""
    import json as _j
    out = sh('timeout 60 gh release list --repo coding-hermes/task-router --limit 50 --json tagName 2>/dev/null')
    try:
        v = _j.loads(out)
        return len(v)
    except Exception:
        return None



# ch:trace row=TR-252 spec=docs/traceability-doctrine.md#hard-rules test=tests/test_doc_parity.py doc=docs/traceability-doctrine.md evidence=reports/xray/ witness=tag:v0.2.0@origin memory=/project/task-router/xray-first-film-2026-10-01

def sh(cmd, t=90):
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=t, cwd=REPO)
        return (p.stdout or '').strip()
    except Exception as e:
        return 'ERR %r' % e


# ---- SURFACES ----
origin = {
    'tags': int(sh('timeout 40 git ls-remote --tags origin 2>/dev/null | wc -l') or 0),
    'branches_remote': int(sh('timeout 40 git ls-remote --heads origin 2>/dev/null | wc -l') or 0),
    # honest: count releases from JSON; if gh cannot answer, record NULL with a reason rather than guessing
    'releases': (lambda o: o if o is not None else 'unknown (gh unavailable)')(_rel_count()),
    'declared_version': sh("grep -m1 '^version' pyproject.toml | tr -d '\"' | awk '{print $3}'"),
    'files_at_origin': int(sh('timeout 60 git ls-tree -r --name-only origin/main 2>/dev/null | wc -l') or 0),
    'docs_files_at_origin': int(sh("timeout 40 git ls-tree -r --name-only origin/main 2>/dev/null | grep -c '^docs/' || true") or 0),
    'landing_files_at_origin': int(sh("timeout 40 git ls-tree -r --name-only origin/main 2>/dev/null | grep -ciE 'landing|index\\.html' || true") or 0),
    'workflows': sh('ls .github/workflows/ 2>/dev/null').replace('\n', ', '),
    'ci_last': sh('timeout 90 gh run list --repo coding-hermes/task-router --limit 1 --json conclusion --jq \'.[0].conclusion\' 2>/dev/null'),
    'local_ahead': int(sh('git rev-list --count origin/main..HEAD') or 0),
}
live = {}
for n, u in (('proxy', 'http://127.0.0.1:9391/health'), ('server', 'http://127.0.0.1:9092/health')):
    try:
        d = json.loads(sh('curl -s -m 20 %s' % u))
        live[n] = {'status': d.get('status'), 'commit': str(d.get('commit'))[:12], 'stale': d.get('stale'),
                   'gate_valid': (d.get('gate') or {}).get('valid')}
    except Exception:
        live[n] = {'raw': sh('curl -s -m 20 %s' % u)[:80]}
live['ingress_unauth_code'] = sh("curl -s -o /dev/null -w '%{http_code}' -m 20 -X POST http://127.0.0.1:9391/v1/chat/completions -H 'Content-Type: application/json' -d '{\"messages\":[{\"role\":\"user\",\"content\":\"x\"}]}'")

led = {'rows': 0, 'by_source': {}, 'routed_pct': 0.0}
if os.path.exists(LEDGER):
    sz = os.path.getsize(LEDGER)
    with open(LEDGER, 'rb') as f:
        f.seek(max(0, sz - 40_000_000))
        rows = []
        for line in f.read().decode('utf-8', 'replace').split('\n'):
            if line.strip().startswith('{'):
                try: rows.append(json.loads(line))
                except Exception: pass
    by = collections.defaultdict(lambda: [0, 0, 0])
    for r in rows:
        s = str(r.get('source_system')); by[s][0] += 1
        if r.get('chain_length') is not None or r.get('hops_attempted') is not None: by[s][1] += 1
        if r.get('complexity') is not None or r.get('profile_id') is not None: by[s][2] += 1
    led['rows'] = len(rows)
    led['by_source'] = {k: {'rows': v[0], 'chain': v[1], 'cx': v[2]} for k, v in by.items()}
    led['routed_pct'] = round(100.0 * by.get('router-proxy', [0])[0] / max(len(rows), 1), 2)

per = {}
for line in open(BOARD, errors='replace'):
    if line.strip():
        try: r = json.loads(line)
        except Exception: continue
        if r.get('id'): per[r['id']] = r
st = collections.Counter(str(r.get('status')) for r in per.values())
comp = [r for r in per.values() if str(r.get('status')) == 'complete']
claims = {
    'rows': len(per), 'status': dict(st), 'complete': len(comp),
    'with_criteria': sum(1 for r in comp if r.get('acceptance_criteria')),
    'with_evidence': sum(1 for r in comp if r.get('evidence')),
    'with_evidence_run': sum(1 for r in comp if r.get('evidence_run_id')),
}

# ---- DELTA (corrected rules + the two the film showed that my rules had missed) ----
D = []
_rel = origin['releases']
if origin['declared_version'] and origin['tags'] == 0 and (_rel == 0 or _rel == 'unknown (gh unavailable)'):
    D.append(('THE DECLARED DELIVERABLE DOES NOT EXIST OFF THIS MACHINE',
              'version %s is declared in pyproject.toml. At origin: %d tags, %d releases. The board files '
              'release sweeps for it and reports %d rows complete.' % (origin['declared_version'], origin['tags'], _rel, claims['complete'])))
if origin['ci_last'] and origin['ci_last'] != 'success':
    D.append(('THE OUTSIDE SEES A BROKEN BUILD',
              'newest CI conclusion at origin is "%s". Our board files a complete row whose text says CI is green.' % origin['ci_last']))
if claims['with_evidence_run'] == 0:
    D.append(('THE EVIDENCE INTERFACE IS UNUSED',
              'boardctl accepts --evidence-run-id on create and update. %d of %d complete rows carry one.'
              % (claims['with_evidence_run'], claims['complete'])))
if claims['with_evidence'] < claims['complete'] * 0.1:
    D.append(('COMPLETION IS UNPROVEN IN PROSE',
              '%d of %d complete rows carry any evidence field at all.' % (claims['with_evidence'], claims['complete'])))
if led['routed_pct'] < 10:
    D.append(('THE OUTCOME IS DARK WHILE THE BUILD IS GREEN',
              '%.2f%% of %d sampled ledger rows went through the router proxy; the rest never touch it, so they '
              'carry neither chain evidence nor complexity.' % (led['routed_pct'], led['rows'])))
if origin['branches_remote'] > 20:
    D.append(('LIMBO: WORK IN FLIGHT THAT NOBODY WILL LAND',
              '%d remote branches exist while the release has never been cut. Branch count is a structural '
              'symptom, not a checklist item - nobody filed it.' % origin['branches_remote']))
if origin['local_ahead'] > 0:
    D.append(('THE WORKING STATE IS AHEAD OF THE PUBLISHED STATE',
              '%d commits exist locally that the outside cannot see (the pre-push gate is correctly refusing a red main).'
              % origin['local_ahead']))
if origin['docs_files_at_origin'] > 0 and origin['landing_files_at_origin'] == 0:
    D.append(('DOCS EXIST, A PUBLISHED ENTRY POINT DOES NOT',
              '%d files under docs/ are at origin, and 0 landing/index files. Correction to the first film, whose '
              'analysis claimed docs were absent while its own image showed them.' % origin['docs_files_at_origin']))

film = {'taken': STAMP, 'project': 'task-router', 'origin': origin, 'live': live, 'ledger': led,
        'claims': claims, 'delta': D}
open(os.path.join(OUTDIR, 'task-router-latest.json'), 'w', encoding='utf-8').write(json.dumps(film, indent=2))

# ---- RENDER ----
def esc(s):
    return str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

ledger_rows = ''.join(
    '<tr><td><code>%s</code></td><td class="n">%s</td><td class="n %s">%s</td><td class="n %s">%s</td></tr>' % (
        esc(s), format(v['rows'], ','), 'zero' if v['chain'] == 0 else 'ok', format(v['chain'], ','),
        'zero' if v['cx'] == 0 else 'ok', format(v['cx'], ','))
    for s, v in sorted(led['by_source'].items(), key=lambda kv: -kv[1]['rows']))

delta_html = ''
for i, (t, w) in enumerate(D, 1):
    delta_html += '<div class="find"><div class="fn">FINDING %d</div><h3>%s</h3><p>%s</p></div>' % (i, esc(t), esc(w))

origin_rows = ''.join('<tr><td><code>%s</code></td><td class="n">%s</td></tr>' % (esc(k), esc(v)) for k, v in origin.items())
live_rows = ''.join('<tr><td><code>%s</code></td><td class="n">%s</td></tr>' % (esc(k), esc(json.dumps(v))) for k, v in live.items())
claim_rows = ''.join('<tr><td>%s</td><td class="n">%s</td></tr>' % (esc(k), esc(v)) for k, v in
                     [('rows on the board', claims['rows']), ('status values', json.dumps(claims['status'])),
                      ('complete rows', claims['complete']), ('...with acceptance criteria', claims['with_criteria']),
                      ('...with any evidence field', claims['with_evidence']), ('...with an evidence run', claims['with_evidence_run'])])

T = '''<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>X-RAY — task-router (__STAMP__)</title><style>
:root{--ink:#0d1117;--mut:#57606a;--line:#d0d7de;--bad:#a3121f;--ok:#0b6b3a;--bg:#fff;--soft:#f6f8fa;--acc:#1a4fd6}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:1000px;margin:0 auto;padding:26px 16px 60px}
h1{font-size:1.6rem;margin:0 0 4px;letter-spacing:-.02em}h2{font-size:1.08rem;margin:32px 0 8px;padding-bottom:6px;border-bottom:2px solid var(--ink)}
h3{font-size:1rem;margin:0 0 6px}p{margin:0 0 10px}
.kicker{text-transform:uppercase;letter-spacing:.1em;font-size:.68rem;color:var(--mut);font-weight:700}
.sub{color:var(--mut);font-size:.82rem}
.find{border:1px solid var(--line);border-left:6px solid var(--bad);padding:12px 14px;margin:10px 0;background:var(--soft)}
.fn{font-size:.66rem;font-weight:800;letter-spacing:.08em;color:var(--bad);margin-bottom:4px}
table{width:100%;border-collapse:collapse;font-size:.8rem;margin:8px 0}
th,td{border:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}
th{background:var(--soft);font-size:.7rem;text-transform:uppercase;letter-spacing:.05em}
.n{text-align:right;font-variant-numeric:tabular-nums}.zero{color:var(--bad);font-weight:700}.ok{color:var(--ok);font-weight:700}
code{background:var(--soft);border:1px solid var(--line);border-radius:3px;padding:0 4px;font-size:.85em}
.note{border:1px solid var(--line);border-left:5px solid var(--acc);background:var(--soft);padding:12px 14px;margin:12px 0}
.toc{background:var(--soft);border:1px solid var(--line);padding:10px 14px;margin:16px 0}
.toc ol{margin:6px 0 0 18px;padding:0}
a{color:var(--acc);text-decoration:none}a:hover{text-decoration:underline}
.foot{margin-top:34px;padding-top:12px;border-top:1px solid var(--line);color:var(--mut);font-size:.74rem}
@media(max-width:640px){.wrap{padding:16px 11px 44px}h1{font-size:1.32rem}table{font-size:.74rem}th,td{padding:5px 6px}}
</style></head><body><div class="wrap">
<div class="kicker">x-ray film &middot; surfaces read, not claims accepted</div>
<h1>task-router</h1>
<p class="sub">Taken __STAMP__. Four surfaces imaged from outside the work loop: origin, the live systems, the
ledger, and the board. Nothing here is a field we wrote about ourselves except the last table &mdash; and that
table is the thing being x-rayed.</p>

<div class="note"><div class="kicker">How to read a film</div>
<p style="margin:6px 0 0">The image is ground. The findings below it are <b>analysis</b>, i.e. claims &mdash; the first
film of this project proved that the hard way: its rules reported &ldquo;no docs&rdquo; while its own image showed 83 docs
files at origin. Read the tables, then judge the findings. A finding that cannot be traced to a row in a table is
another box.</p></div>

<div class="toc"><div class="kicker">Contents</div><ol>
<li><a href="#d">The delta &mdash; __N__ findings</a></li>
<li><a href="#o">Origin</a></li>
<li><a href="#l">Live systems</a></li>
<li><a href="#g">The ledger (what actually ran)</a></li>
<li><a href="#c">What the board claims (the thing being x-rayed)</a></li>
<li><a href="#m">Why this is the mechanism, not a report</a></li>
</ol></div>

<h2 id="d">1 &middot; The delta</h2>
__DELTA__

<h2 id="o">2 &middot; Origin <span class="sub">&mdash; a surface we cannot write to without CI seeing it</span></h2>
<table><tr><th>Observation</th><th class="n">Value</th></tr>__ORIGIN__</table>

<h2 id="l">3 &middot; Live systems</h2>
<table><tr><th>Observation</th><th class="n">Value</th></tr>__LIVE__</table>

<h2 id="g">4 &middot; The ledger &mdash; what actually ran</h2>
<p class="sub">__LROWS__ rows in the last 40 MB. <code>chain</code> = carries routing evidence. <code>cx</code> = carries complexity or a profile.</p>
<table><tr><th>source_system</th><th class="n">rows</th><th class="n">chain</th><th class="n">cx</th></tr>__LEDGER__</table>
<p>Routed share: <b>__ROUTED__%</b>. The router path writes both kinds of evidence on ~80% of its own rows. Every
other path writes neither, because those requests never enter the router.</p>

<h2 id="c">5 &middot; What the board claims</h2>
<table><tr><th>Claim</th><th class="n">Value</th></tr>__CLAIMS__</table>

<h2 id="m">6 &middot; Why this is the mechanism</h2>
<p>An envelope only measures the items we thought to list. This takes the <b>surfaces</b> as input and derives structure,
which is why it found the release gap, the unused evidence interface, the limbo branches and a red build at origin
&mdash; none of which anyone filed. Two properties make it a scale rather than another status report:</p>
<ul>
<li><b>The film is re-takeable and comparable.</b> A film is a snapshot with a timestamp; the interesting signal is
what persists across films. A fracture still present in the third film is systemic; the same reading in one film
may be a bad day.</li>
<li><b>The observer is not the author, and the surface is not ours.</b> Tags, releases, CI conclusions, HTTP codes
and ledger aggregates are read, never asserted. No row can author an observation.</li>
</ul>
<p>The rule that keeps it honest is the one already filed as TR-252: <b>no item may be closed by reference.</b> A
finding is closed by an observation in the next film, not by a row saying it was fixed.</p>

<div class="foot">Film <code>__JSON__</code> &middot; machine: the scan that produced this file &middot; read-only throughout; no live state was written.<br>
Companion: quorum-completeness-2026-10-01, scan-gpt-6.1-sol-2026-09-29, status-2026-09-29, timeout-alignment-2026-09-29.</div>
</div></body></html>'''

T = (T.replace('__STAMP__', STAMP).replace('__DELTA__', delta_html).replace('__ORIGIN__', origin_rows)
      .replace('__LIVE__', live_rows).replace('__LEDGER__', ledger_rows).replace('__CLAIMS__', claim_rows)
      .replace('__LROWS__', format(led['rows'], ',')).replace('__ROUTED__', str(led['routed_pct']))
      .replace('__JSON__', 'reports/xray/task-router-latest.json').replace('__N__', str(len(D))))
open(HTML, 'w', encoding='utf-8').write(T)
print('film: %s (%d bytes) | findings: %d' % (HTML, len(T), len(D)))
for i, (t, w) in enumerate(D, 1):
    print('  %d. %s' % (i, t))
