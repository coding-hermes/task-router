import json
from collections import Counter
rows=[json.loads(l) for l in open('.coding-hermes/board/tasks.jsonl') if l.strip()]
last={}
for r in rows:
    rid=r.get('id')
    if rid in last:
        last[rid].append(r)
    else:
        last[rid]=[r]
print('total lines:', len(rows), 'unique ids:', len(last))
print('dup ids:', {k:len(v) for k,v in last.items() if len(v)>1})
cur=[v[-1] for v in last.values()]
print(Counter(r.get('status') for r in cur))
pend=[r for r in cur if r.get('status') in ('pending','open','todo','in_progress','blocked')]
order={'P0':0,'P1':1,'P2':2,'P3':3,'P4':4,'P5':5}
pend.sort(key=lambda r:(order.get(r.get('priority'),9), r.get('created_at') or ''))
for r in pend:
    print('===', r.get('priority'), r.get('status'), r.get('id'), '|', (r.get('title') or '')[:110])
    print('   created:', r.get('created_at'), 'updated:', r.get('updated_at'), 'depends_on:', r.get('depends_on'))
