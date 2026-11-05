#!/usr/bin/env bash
# router_redis_up.sh — idempotent provisioning for the task-router Redis read plane.
#
# Spec: docs/specs/SPEC-REDIS-READ-PLANE.md §11.5 (R58/R59/R62/R67).
# Creates the instance if absent, verifies + reports drift if present, and never
# touches any other container. Run again freely; it is the repair path too.
#
#   ./scripts/router_redis_up.sh            # provision / verify
#   ./scripts/router_redis_up.sh --check    # verify only, non-zero on drift
#   ./scripts/router_redis_up.sh --print    # print the spec it would apply
set -uo pipefail

NAME="${ROUTING_REDIS_CONTAINER:-tr388-redis}"
PORT="${ROUTING_REDIS_PORT:-6391}"
HOST_BIND="${ROUTING_REDIS_BIND:-127.0.0.1}"
# Digest-pinned: a tag can be repointed upstream, and this image is shared with a
# product container on this host (redis:7-alpine). Refresh deliberately.
IMAGE="${ROUTING_REDIS_IMAGE:-redis@sha256:e7723ff73d963f5cc6d9c4643ea3d989527a402a319239054e9472a7fb9219a2}"
SECRET="${ROUTING_REDIS_PASSWORD_FILE:-$HOME/.hermes/secrets/router-redis.pass}"
LABEL_COMPONENT="task-router.component=redis-read-plane"
LABEL_SPEC="task-router.spec=SPEC-REDIS-READ-PLANE"
MEMORY="${ROUTING_REDIS_MEMORY:-320m}"
MAXMEMORY="${ROUTING_REDIS_MAXMEMORY:-256mb}"
MODE="up"
case "${1:-}" in
  --check) MODE="check" ;;
  --print) MODE="print" ;;
  "" ) ;;
  *) echo "usage: $0 [--check|--print]" >&2; exit 2 ;;
esac

die() { echo "router_redis_up: $*" >&2; exit 1; }
say() { echo "router_redis_up: $*"; }

# ---- guards ---------------------------------------------------------------
command -v docker >/dev/null 2>&1 || die "docker is required (R65: container only, no host package)"
case "$PORT" in
  6379|6380|16379) die "port $PORT belongs to a PRODUCT instance on this host (R43 denylist)" ;;
esac
# R67: the port answering is not proof it is ours.
owner_of_port() {
  docker ps --format '{{.Names}}\t{{.Ports}}' 2>/dev/null \
    | awk -v p="127.0.0.1:$PORT->" 'index($0,p){print $1; exit}'
}
existing_port_owner="$(owner_of_port)"
if [ -n "$existing_port_owner" ] && [ "$existing_port_owner" != "$NAME" ]; then
  die "127.0.0.1:$PORT is already published by container '$existing_port_owner' — refusing to adopt it (R67)"
fi

# ---- secret (R59) ---------------------------------------------------------
ensure_secret() {
  mkdir -p "$(dirname "$SECRET")"
  chmod 700 "$(dirname "$SECRET")" 2>/dev/null || true
  if [ ! -s "$SECRET" ]; then
    (umask 077; head -c 24 /dev/urandom | base64 | tr -d '\n=+/' > "$SECRET")
    say "generated AUTH secret at $SECRET (mode 600, never printed, never committed)"
  fi
  chmod 600 "$SECRET"
}

# ---- the command this script owns ----------------------------------------
run_args() {
  cat <<EOF
--name $NAME
--restart unless-stopped
-p $HOST_BIND:$PORT:6379
--memory $MEMORY
--memory-swap $MEMORY
--label $LABEL_COMPONENT
--label $LABEL_SPEC
--log-opt max-size=10m
--log-opt max-file=3
--health-cmd redis-cli -a "\$REDIS_PASSWORD" ping || exit 1
--health-interval 30s
--health-timeout 3s
--health-retries 3
-e REDIS_PASSWORD=<from $SECRET>
$IMAGE
redis-server --requirepass <from $SECRET> --bind 0.0.0.0 --protected-mode yes
  --save "" --appendonly no --maxmemory $MAXMEMORY --maxmemory-policy noeviction
EOF
}

if [ "$MODE" = "print" ]; then
  run_args
  exit 0
fi

# ---- status ---------------------------------------------------------------
present="$(docker inspect --format '{{.Name}}' "$NAME" 2>/dev/null | sed 's|^/||')"
if [ -n "$present" ]; then
  image="$(docker inspect --format '{{.Config.Image}}' "$NAME")"
  restart="$(docker inspect --format '{{.HostConfig.RestartPolicy.Name}}' "$NAME")"
  mem="$(docker inspect --format '{{.HostConfig.Memory}}' "$NAME")"
  health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$NAME")"
  say "present: image=$image restart=$restart memory=$mem health=$health"
  drift=0
  [ "$image" = "$IMAGE" ] || { say "DRIFT image $image != $IMAGE"; drift=1; }
  [ "$restart" = "unless-stopped" ] || { say "DRIFT restart $restart"; drift=1; }
  [ "$health" = "healthy" ] || { say "DRIFT health $health"; drift=1; }
  [ "$mem" != "0" ] || { say "DRIFT no memory limit (maxmemory does not bound RSS)"; drift=1; }
  if [ "$MODE" = "check" ]; then
    [ "$drift" = "0" ] && { say "OK"; exit 0; }
    exit 1
  fi
  if [ "$drift" = "1" ]; then
    say "recreating to match the spec (data is disposable, R6)"
    docker rm -f "$NAME" >/dev/null || die "could not remove $NAME"
  else
    say "no drift; nothing to do"
  fi
fi

if [ -z "$(docker inspect --format '{{.Name}}' "$NAME" 2>/dev/null)" ]; then
  ensure_secret
  pass="$(cat "$SECRET")"
  docker run -d --name "$NAME" --restart unless-stopped \
    -p "$HOST_BIND:$PORT:6379" \
    --memory "$MEMORY" --memory-swap "$MEMORY" \
    --label "$LABEL_COMPONENT" --label "$LABEL_SPEC" \
    --log-opt max-size=10m --log-opt max-file=3 \
    --health-cmd 'redis-cli -a "$REDIS_PASSWORD" ping || exit 1' \
    --health-interval 30s --health-timeout 3s --health-retries 3 \
    -e REDIS_PASSWORD="$pass" "$IMAGE" \
    sh -lc 'exec redis-server --requirepass "$REDIS_PASSWORD" --bind 0.0.0.0 \
            --protected-mode yes --save "" --appendonly no \
            --maxmemory '"$MAXMEMORY"' --maxmemory-policy noeviction' >/dev/null \
    || die "docker run failed"
  say "created $NAME on $HOST_BIND:$PORT"
fi

# ---- verify (never claim without probing) --------------------------------
for _ in $(seq 1 15); do
  state="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$NAME")"
  [ "$state" = "healthy" ] && break
  sleep 2
done
[ "$state" = "healthy" ] || die "container health is '$state' — run: docker logs $NAME"

python3 - "$PORT" "$SECRET" <<'PY' || die "AUTH/PING probe failed (secret drift? see R59 rotation)"
import socket, sys
port, secret_path = int(sys.argv[1]), sys.argv[2]
pw = open(secret_path).read().strip()

def cmd(s, *args):
    out = b'*%d\r\n' % len(args)
    for a in args:
        b = a.encode() if isinstance(a, str) else a
        out += b'$%d\r\n%s\r\n' % (len(b), b)
    s.sendall(out)
    return s.recv(65536)

s = socket.create_connection(('127.0.0.1', port), timeout=5)
assert cmd(s, 'AUTH', pw).startswith(b'+OK'), 'AUTH failed'
assert cmd(s, 'PING').strip() == b'+PONG', 'PING failed'
info = cmd(s, 'INFO', 'server').decode(errors='ignore')
assert 'run_id' in info, 'no run_id in INFO server'
s.close()
print(f'router_redis_up: AUTH ok, PING ok, run_id present (port {port})')
PY

say "next: python3 scripts/router_redis.py load && python3 scripts/router_redis.py status"
