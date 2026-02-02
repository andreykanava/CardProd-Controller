#!/usr/bin/env bash
set -euo pipefail

DATA_DIR="${DATA_DIR:-/data}"
WG_IFACE="${WG_IFACE:-wg0}"
WG_ADDR="${WG_ADDR:-10.50.0.1/24}"
WG_LISTEN_PORT="${WG_LISTEN_PORT:-51820}"

mkdir -p "$DATA_DIR" /etc/wireguard

KEY_PRIV="$DATA_DIR/controller.key"
KEY_PUB="$DATA_DIR/controller.pub"

if [ ! -f "$KEY_PRIV" ]; then
  umask 077
  wg genkey | tee "$KEY_PRIV" | wg pubkey > "$KEY_PUB"
fi

PRIVKEY="$(cat "$KEY_PRIV")"

cat > /etc/wireguard/${WG_IFACE}.conf <<EOF
[Interface]
Address = ${WG_ADDR}
ListenPort = ${WG_LISTEN_PORT}
PrivateKey = ${PRIVKEY}
EOF

# поднимаем интерфейс
wg-quick down "${WG_IFACE}" >/dev/null 2>&1 || true
wg-quick up "${WG_IFACE}"

# если есть сохранённые peers — восстановим
PEERS_FILE="$DATA_DIR/peers.json"
if [ -f "$PEERS_FILE" ]; then
  python3 - <<'PY'
import json, os, subprocess
wg=os.environ.get("WG_IFACE","wg0")
peers=json.load(open(os.environ["PEERS_FILE"]))
for node_id, info in peers.items():
    subprocess.check_call(["wg","set",wg,"peer",info["pubkey"],"allowed-ips",f'{info["ip"]}/32'])
print("restored peers:", len(peers))
PY
fi

exec gunicorn -b 0.0.0.0:9000 \
  --workers 1 \
  --threads 8 \
  --timeout 180 \
  --access-logfile - \
  --error-logfile - \
  controller_app:app

