#!/usr/bin/env bash
# Fail early if a host port AEOI needs is already taken by another program.
# Usage: check-ports.sh [infra|kafka|all]   (default: all; only that profile's ports are checked)
#
# How: we try to BIND 127.0.0.1:<port> - exactly what Docker does. This sees every listener,
# including ones owned by root/other users that `lsof` (without sudo) cannot see.
# macOS bash 3.2 compatible.
set -u
PROFILE="${1:-all}"
if [ -f .env ]; then set -a; . ./.env; set +a; fi
PG="${AEOI_PG_PORT:-5433}"; REDIS="${AEOI_REDIS_PORT:-6380}"; KAFKA="${AEOI_KAFKA_PORT:-9094}"
case "$PROFILE" in
  infra) PORTS="$PG:PG:postgres $REDIS:REDIS:redis" ;;
  *)     PORTS="$PG:PG:postgres $REDIS:REDIS:redis $KAFKA:KAFKA:kafka" ;;
esac

port_in_use() {  # exit 0 if 127.0.0.1:$1 cannot be bound
  python3 - "$1" <<'PY'
import socket, sys
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
try:
    s.bind(("127.0.0.1", int(sys.argv[1])))
except OSError:
    sys.exit(0)
finally:
    s.close()
sys.exit(1)
PY
}

ours_running() {  # our compose service already up -> it owns the port, fine
  [ -n "$(docker compose --profile infra --profile kafka ps -q --status running "$1" 2>/dev/null)" ]
}

fail=0
for entry in $PORTS; do
  port="${entry%%:*}"; rest="${entry#*:}"; key="${rest%%:*}"; svc="${rest#*:}"
  port_in_use "$port" || continue
  ours_running "$svc" && continue
  owner="$(lsof -nP -iTCP:"$port" -sTCP:LISTEN -Fc 2>/dev/null | sed -n 's/^c//p' | sort -u | tr '\n' ' ')"
  [ -z "$owner" ] && owner="a process owned by another user (see: sudo lsof -nP -iTCP:$port -sTCP:LISTEN) "
  echo "✘ port $port ($svc) is used by: $owner-> stop it, or set AEOI_${key}_PORT=<free port> in .env"
  fail=1
done
[ "$fail" -eq 0 ] && echo "✔ ports free for profile '$PROFILE' (pg=$PG redis=$REDIS kafka=$KAFKA)"
exit "$fail"
