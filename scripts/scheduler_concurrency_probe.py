#!/usr/bin/env python3
"""TR-386: is the scheduler AWARE of its own concurrency, or is Hermes what stops it?

Bane's question (mid-loop criterion): does the scheduler know how many things it
has open, or does it just keep trying until the Hermes gateway limit refuses it?
And: the HTTP gateway path — does it use a semaphore correctly?

This script answers from evidence, not from reading one function. It does three
things that can each independently contradict the others:

  1. STATIC  — where the scheduler counts its own in-flight ticks
                (internal/scheduler/packer.go, namespace_gate.go).
  2. LIVE    — running/queued ticks now, against each namespace's max_concurrent.
  3. DENIAL  — how often the scheduler DEFERRED for its own reasons (cooldown,
                budget, cap, board_unowned, tasks_no_work) versus how often the
                GATEWAY refused it (429 "Too many concurrent runs"). If the
                gateway is the thing stopping it, there is no admission control —
                the limit is doing the scheduler's job.
  4. LOAD    — loadavg against core count, because 'the box is fine' is a claim
                somebody has to measure.

Read-only. Prints a verdict per question.
"""
import json
import os
import re
import shutil
import sqlite3
import subprocess

DB = os.path.expanduser('~/.hermes/coding-hermes/scheduler.db')
SRC = os.path.expanduser('~/coding-hermes-scheduler/coding-herms-scheduler/internal/scheduler')
GW = os.path.expanduser('~/.hermes/logs/gateway.log')


def q(sql, args=()):
    con = sqlite3.connect(f'file:{DB}?mode=ro', uri=True)
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


print('=' * 72)
print('1. STATIC — does the scheduler count its own in-flight ticks?')
print('=' * 72)
hits = 0
for fn in ('packer.go', 'packer_select.go', 'namespace_gate.go'):
    p = os.path.join(SRC, fn)
    if not os.path.exists(p):
        continue
    for i, line in enumerate(open(p), 1):
        low = line.lower()
        if ('max_concurrent' in low or 'maxconc' in low or 'nsrunning' in low) and \
           not line.lstrip().startswith('//'):
            print(f'  {fn}:{i}: {line.strip()[:110]}')
            hits += 1
            if hits >= 8:
                break
    if hits >= 8:
        break
print(f'  -> {hits} live code references to its own concurrency accounting')

print()
print('=' * 72)
print('2. LIVE — in-flight ticks against each namespace cap')
print('=' * 72)
caps = dict(q("SELECT id, max_concurrent FROM namespaces"))
running = q("""SELECT COALESCE(p.namespace_id,'(none)'), count(*) FROM ticks t
               LEFT JOIN projects p ON p.name=t.project_name
               WHERE t.status='running' GROUP BY 1""")
running = dict(running)
queued = q("""SELECT COALESCE(p.namespace_id,'(none)'), count(*) FROM ticks t
              LEFT JOIN projects p ON p.name=t.project_name
              WHERE t.status='queued' GROUP BY 1""")
queued = dict(queued)
print(f"{'namespace':<26}{'running':>9}{'queued':>8}{'cap':>6}{'headroom':>10}")
for ns in sorted(set(list(caps) + list(running)), key=lambda k: -running.get(k, 0)):
    cap = caps.get(ns, 0)
    r = running.get(ns, 0)
    print(f'{ns[:25]:<26}{r:>9}{queued.get(ns,0):>8}'
          f'{(cap if cap else "unlim"):>6}{(cap - r) if cap else "-":>10}')

print()
print('=' * 72)
print('3. DENIAL — who actually stops a spawn?')
print('=' * 72)
rows = q("""SELECT reason, count(*) c FROM deferrals
            WHERE created_at > datetime('now','-1 day')
            GROUP BY reason ORDER BY c DESC LIMIT 12""")
self_stops = {'cooldown', 'budget', 'cap', 'failed_cooldown', 'board_unowned',
              'tasks_no_work', 'tasks_deferred', 'ok'}
total = sum(c for _, c in rows)
self_total = sum(c for r, c in rows if r in self_stops)
for r, c in rows:
    kind = 'SCHEDULER' if r in self_stops else 'other'
    print(f'  {r:<22}{c:>9}  {kind}')
print(f'  -> {self_total}/{total} deferrals are the SCHEDULER refusing on its own terms'
      f' ({100*self_total/total:.1f}%)' if total else '  -> no deferrals')

gw429 = 0
if os.path.exists(GW):
    out = subprocess.run(['grep', '-ac', 'Too many concurrent runs', GW],
                         capture_output=True, text=True).stdout.strip()
    gw429 = int(out or 0)
print(f'  -> gateway 429 "Too many concurrent runs" in the whole log: {gw429}')
print('  VERDICT: ' + ('scheduler is ADMISSION-AWARE — the gateway is not the brake'
                       if gw429 == 0 and self_total > 0 else
                       'the GATEWAY limit is doing the braking — no real admission control'))

print()
print('=' * 72)
print('4. LOAD — is the box actually fine?')
print('=' * 72)
la = open('/proc/loadavg').read().split()
cores = os.cpu_count()
l1, l5, l15 = (float(x) for x in la[:3])
print(f'  loadavg {l1} {l5} {l15} on {cores} cores  -> {l1/cores:.2f} per core')
print('  VERDICT: ' + ('healthy' if l1 / cores < 0.7 else
                       'SATURATED — the load gate (12) is being exceeded; '
                       'find the consumer before adding work'))
top = subprocess.run(['ps', '-eo', 'pcpu,comm,args', '--sort=-pcpu'],
                     capture_output=True, text=True).stdout.splitlines()[:6]
print('  top CPU consumers:')
for line in top[1:]:
    print('   ', line.strip()[:120])
