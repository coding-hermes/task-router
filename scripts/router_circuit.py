#!/usr/bin/env python3
"""router_circuit.py — circuit-breaker state for (provider, model) pairs.

Shared state file: ~/.hermes/model-router/circuit-state.json
Cooldowns: 1st failure 5m, then double per consecutive failure, cap 1h.
A pair with open_until in the future is excluded from chains (router_spawn.py).

Usage:
  router_circuit.py record-failure <provider> <model> [reason]
  router_circuit.py record-success <provider> <model>
  router_circuit.py status [provider]
  router_circuit.py clear <provider> <model> | --all
"""
import json, os, sys, datetime, time

STATE = os.path.expanduser('~/.hermes/model-router/circuit-state.json')
BASE_COOLDOWN_S = 300   # 5m
MAX_COOLDOWN_S = 3600   # 1h

def load():
    try:
        return json.load(open(STATE))
    except Exception:
        return {'version': 1, 'pairs': {}}

def save(st):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = STATE + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(st, f, indent=1)
    os.replace(tmp, STATE)

def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')

def record_failure(provider, model, reason=''):
    st = load()
    key = f'{provider}/{model}'
    c = st['pairs'].setdefault(key, {'failures': 0, 'open_until': None,
                                     'last_failure': None, 'reason': ''})
    c['failures'] = c.get('failures', 0) + 1
    cd = min(BASE_COOLDOWN_S * (2 ** (c['failures'] - 1)), MAX_COOLDOWN_S)
    c['open_until'] = (datetime.datetime.now(datetime.timezone.utc) +
                       datetime.timedelta(seconds=cd)).isoformat(timespec='seconds')
    c['last_failure'] = now_iso()
    if reason:
        c['reason'] = reason
    save(st)
    print(f'OPEN {key} — {c["failures"]} consecutive failures, cooldown {cd}s, open until {c["open_until"]}')
    return 0

def record_success(provider, model):
    st = load()
    key = f'{provider}/{model}'
    if key in st['pairs']:
        prev = st['pairs'].pop(key)
        print(f'CLOSED {key} — was open until {prev.get("open_until")} ({prev.get("failures")} failures)')
        save(st)
    else:
        print(f'{key} already closed')
    return 0

def status(provider=None):
    st = load()
    now = now_iso()
    pairs = st['pairs']
    if provider:
        pairs = {k: v for k, v in pairs.items() if k.startswith(provider + '/')}
    if not pairs:
        print('no open/recorded circuits')
        return 0
    for key, c in sorted(pairs.items()):
        state = 'OPEN' if c.get('open_until') and c['open_until'] > now else 'cooling'
        print(f'{key:<42} {state:<8} failures={c.get("failures", 0)} open_until={c.get("open_until")} {c.get("reason", "")}')
    return 0

def clear(provider=None, model=None, all_=False):
    st = load()
    if all_:
        st['pairs'] = {}
        save(st)
        print('cleared all circuits')
        return 0
    key = f'{provider}/{model}'
    if key in st['pairs']:
        del st['pairs'][key]
        save(st)
        print(f'cleared {key}')
    else:
        print(f'{key} not recorded')
    return 0

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    cmd = sys.argv[1]
    if cmd == 'record-failure' and len(sys.argv) >= 4:
        sys.exit(record_failure(sys.argv[2], sys.argv[3], ' '.join(sys.argv[4:])))
    elif cmd == 'record-success' and len(sys.argv) >= 4:
        sys.exit(record_success(sys.argv[2], sys.argv[3]))
    elif cmd == 'status':
        sys.exit(status(sys.argv[2] if len(sys.argv) > 2 else None))
    elif cmd == 'clear':
        if len(sys.argv) >= 4:
            sys.exit(clear(sys.argv[2], sys.argv[3]))
        elif len(sys.argv) == 3 and sys.argv[2] == '--all':
            sys.exit(clear(all_=True))
    print(__doc__)
    sys.exit(2)
