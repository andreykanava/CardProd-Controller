#!/usr/bin/env bash
set -euo pipefail

DATA_DIR="${DATA_DIR:-/data}"
WG_IFACE="${WG_IFACE:-wg0}"
WG_ADDR="${WG_ADDR:-10.50.0.1/24}"
WG_LISTEN_PORT="${WG_LISTEN_PORT:-51820}"
WG_NET="${WG_NET:-10.50.0.0/24}"

# public endpoint like "PUBLIC_IP:51820" (used by nodes)
WG_ENDPOINT="${WG_ENDPOINT:-}"

# controller api port
API_PORT="${API_PORT:-9000}"

# peers file (optional, for your sync function)
PEERS_FILE="${PEERS_FILE:-$DATA_DIR/peers.json}"

mkdir -p "$DATA_DIR" /etc/wireguard

KEY_PRIV="$DATA_DIR/controller.key"
KEY_PUB="$DATA_DIR/controller.pub"

# ----- keys -----
if [ ! -f "$KEY_PRIV" ] || [ ! -f "$KEY_PUB" ]; then
  echo "[*] Generating controller WireGuard keypair..."
  umask 077
  wg genkey | tee "$KEY_PRIV" | wg pubkey > "$KEY_PUB"
fi

PRIVKEY="$(cat "$KEY_PRIV")"

# ----- wg config -----
cat > "/etc/wireguard/${WG_IFACE}.conf" <<EOF
[Interface]
Address = ${WG_ADDR}
ListenPort = ${WG_LISTEN_PORT}
PrivateKey = ${PRIVKEY}

# Peers are added dynamically via /join (and restored by your sync on start)
EOF

# ----- sysctls -----
# might fail in some container setups, so don't crash if it's locked
echo "[*] Enabling IPv4 forwarding..."
sysctl -w net.ipv4.ip_forward=1 >/dev/null 2>&1 || true

# ----- bring up wg -----
echo "[*] Bringing up WireGuard..."
wg-quick down "${WG_IFACE}" >/dev/null 2>&1 || true
wg-quick up "${WG_IFACE}"

echo "[*] wg show:"
wg show "${WG_IFACE}" || true

# ----- OPTIONAL: your sync peers -> wg0 -----
# Put your python call here AFTER wg is up.
# Example:
# python3 - <<'PY'
# from controller_app import sync_peers_to_wg
# sync_peers_to_wg()
# print("peers synced")
# PY

# ----- export for app -----
export DATA_DIR
export WG_IFACE
export WG_NET
export WG_ENDPOINT
export PEERS_FILE

echo "[*] Starting controller API on 0.0.0.0:${API_PORT}"
exec gunicorn -w 1 --threads 4 --timeout 120 \
  -b "0.0.0.0:${API_PORT}" \
  controller_app:app
