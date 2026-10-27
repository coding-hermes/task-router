import json
from collections import Counter
rows=[json.loads(l) for l in open('.coding-hermes/board/tasks.jsonl') if l.strip()]
print(Counter(r.get('status') for r in rows))
pend=[r for r in rows if r.get('status') in ('pending','open','todo','in_progress','blocked')]
for r in pend:
    print('---')
    for k in ('id','title','status','priority','created_at','updated_at','depends_on','assignee'):
        if k in r:
            print(f"{k}: {r[k]}")
    ac=r.get('acceptance_criteria') or r.get('acceptance') or ''
    print('AC:', str(ac)[:500])
