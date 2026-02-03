from __future__ import annotations

import ipaddress
import json
import os
import subprocess
from pathlib import Path
from flask import Flask, request, jsonify
import time
import requests

app = Flask(__name__)

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data")).resolve()
PEERS_FILE = DATA_DIR / "peers.json"

WG_IFACE = os.environ.get("WG_IFACE", "wg0")
WG_NET = ipaddress.ip_network(os.environ.get("WG_NET", "10.50.0.0/24"))
WG_ENDPOINT = os.environ.get("WG_ENDPOINT", "")  # "public_ip:51820"
JOIN_TOKEN = os.environ.get("JOIN_TOKEN", "")

CONTROLLER_IP = ipaddress.ip_address(os.environ.get("CONTROLLER_IP", "10.50.0.1"))

def sh(*args: str) -> str:
    out = subprocess.check_output(args, text=True).strip()
    return out

def load_peers() -> dict:
    if not PEERS_FILE.exists():
        return {}
    return json.loads(PEERS_FILE.read_text())

def save_peers(peers: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PEERS_FILE.write_text(json.dumps(peers, indent=2, sort_keys=True))

def controller_pubkey() -> str:
    return sh("wg", "show", WG_IFACE, "public-key")

def pick_free_ip(peers: dict) -> str:
    used = set()
    for node_id, info in peers.items():
        used.add(info["ip"])

    # reserve .1 for controller
    for ip in WG_NET.hosts():
        if ip == CONTROLLER_IP:
            continue
        ip_s = str(ip)
        if ip_s not in used:
            return ip_s
    raise RuntimeError("No free IPs in WG_NET")

def add_peer_to_wg(node_pubkey: str, node_ip: str) -> None:
    # wg set wg0 peer <pub> allowed-ips <ip>/32
    sh("wg", "set", WG_IFACE, "peer", node_pubkey, "allowed-ips", f"{node_ip}/32")

def sync_peers_to_wg() -> None:
    peers = load_peers()
    for node_id, info in peers.items():
        try:
            add_peer_to_wg(info["pubkey"], info["ip"])
        except Exception as e:
            print(f"[!] failed to sync peer {node_id}: {e}")


@app.get("/health")
def health():
    return jsonify({"ok": True})

@app.get("/wg/publickey")
def wg_pubkey():
    return jsonify({"ok": True, "public_key": controller_pubkey()})

@app.post("/join")
def join():
    token = request.headers.get("X-Join-Token", "")
    if not JOIN_TOKEN or token != JOIN_TOKEN:
        return jsonify({"ok": False, "error": "forbidden"}), 403

    body = request.get_json(force=True, silent=True) or {}
    node_id = str(body.get("node_id", "")).strip()
    node_pubkey = str(body.get("node_pubkey", "")).strip()

    if not node_id or not node_pubkey:
        return jsonify({"ok": False, "error": "node_id and node_pubkey required"}), 400

    peers = load_peers()

    # registered already -> keep same IP, but allow key rotation
    if node_id in peers:
        info = peers[node_id]
        changed = False

        # key rotation support
        if info.get("pubkey") != node_pubkey:
            info["pubkey"] = node_pubkey
            changed = True

        # sanity: make sure IP exists
        if "ip" not in info or not info["ip"]:
            info["ip"] = pick_free_ip(peers)
            changed = True

        if changed:
            peers[node_id] = info
            save_peers(peers)

        # always ensure peer exists in live wg (idempotent)
        add_peer_to_wg(info["pubkey"], info["ip"])

        return jsonify({
            "ok": True,
            "node_id": node_id,
            "node_ip": info["ip"],
            "controller_pubkey": controller_pubkey(),
            "endpoint": WG_ENDPOINT,
            "allowed_ips": f"{CONTROLLER_IP}/32",
            "wg_net": str(WG_NET),
        })

    # new node -> assign ip + save
    node_ip = pick_free_ip(peers)
    peers[node_id] = {"ip": node_ip, "pubkey": node_pubkey}
    save_peers(peers)

    add_peer_to_wg(node_pubkey, node_ip)

    return jsonify({
        "ok": True,
        "node_id": node_id,
        "node_ip": node_ip,
        "controller_pubkey": controller_pubkey(),
        "endpoint": WG_ENDPOINT,
        "allowed_ips": f"{CONTROLLER_IP}/32",
        "wg_net": str(WG_NET),
    })


# простой прокси: controller дергает node-api по WG-IP
@app.route("/nodes/<node_id>/proxy/<path:path>", methods=["GET", "POST", "DELETE"])
def proxy(node_id: str, path: str):
    import requests

    peers = load_peers()
    if node_id not in peers:
        return jsonify({"ok": False, "error": "unknown node"}), 404

    node_ip = peers[node_id]["ip"]
    node_port = int(os.environ.get("NODE_API_PORT", "8000"))

    url = f"http://{node_ip}:{node_port}/{path}"

    # NEW: add X-Portmap-Token only for /ports endpoints
    headers = {}
    if path == "ports" or path.startswith("ports/"):
        tok = NODE_PORTMAP_TOKENS.get(node_id, "")
        if tok:
            headers["X-Portmap-Token"] = tok

    try:
        if request.method == "GET":
            r = requests.get(url, params=request.args, headers=headers, timeout=120)
        elif request.method == "POST":
            r = requests.post(url, json=request.get_json(silent=True), params=request.args, headers=headers, timeout=120)
        else:
            r = requests.delete(url, params=request.args, headers=headers, timeout=120)

        return (r.text, r.status_code, {"Content-Type": r.headers.get("Content-Type", "application/json")})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502


def wg_peers_status() -> dict:
    """
    Returns:
      { "<pubkey>": { "last_handshake": seconds_ago or None } }
    """
    out = subprocess.check_output(
        ["wg", "show", WG_IFACE, "dump"],
        text=True
    ).strip().splitlines()

    peers = {}

    # wg dump:
    # line0: iface priv pub listen fwmark
    # peer lines:
    # peer_pub preshared endpoint allowed_ips latest_handshake rx tx keepalive
    for line in out[1:]:
        parts = line.split("\t")
        if len(parts) < 8:
            continue

        peer_pub = parts[0]
        latest_handshake = int(parts[4])  # <-- ВОТ ТУТ был баг (не 5, а 4)

        if latest_handshake == 0:
            peers[peer_pub] = {"last_handshake": None}
        else:
            peers[peer_pub] = {"last_handshake": int(time.time()) - latest_handshake}

    return peers



def check_node_api(ip: str, timeout: int = 3) -> bool:
    try:
        r = requests.get(f"http://{ip}:{os.environ.get('NODE_API_PORT','8000')}/health",
                         timeout=timeout)
        return r.status_code == 200
    except Exception:
        return False



@app.get("/nodes")
def list_nodes():
    peers = load_peers()
    wg_status = wg_peers_status()

    nodes = []

    for node_id, info in peers.items():
        ip = info["ip"]
        pub = info["pubkey"]

        wg_info = wg_status.get(pub, {})
        handshake = wg_info.get("last_handshake")

        wireguard_up = handshake is not None and handshake < 120
        api_ok = check_node_api(ip)

        nodes.append({
            "node_id": node_id,
            "ip": ip,
            "wireguard": {
                "connected": wireguard_up,
                "last_handshake_seconds": handshake,
            },
            "api": {
                "reachable": api_ok,
            },
            "online": wireguard_up and api_ok,
        })

    return jsonify({"ok": True, "nodes": nodes})





@app.get("/nodes/<node_id>")
def get_node(node_id: str):
    peers = load_peers()
    if node_id not in peers:
        return jsonify({"ok": False, "error": "node not found"}), 404

    info = peers[node_id]
    ip = info["ip"]
    pub = info["pubkey"]

    wg_status = wg_peers_status().get(pub, {})
    handshake = wg_status.get("last_handshake")

    wireguard_up = handshake is not None and handshake < 120
    api_ok = check_node_api(ip)

    return jsonify({
        "ok": True,
        "node": {
            "node_id": node_id,
            "ip": ip,
            "wireguard": {
                "connected": wireguard_up,
                "last_handshake_seconds": handshake,
            },
            "api": {
                "reachable": api_ok,
            },
            "online": wireguard_up and api_ok,
        }
    })


EDGE_WG_IP = os.environ.get("EDGE_WG_IP", "").strip()          # 10.50.0.2
EDGE_AGENT_PORT = int(os.environ.get("EDGE_AGENT_PORT", "8081"))
EDGE_AGENT_TOKEN = os.environ.get("EDGE_AGENT_TOKEN", "").strip()
NODE_PORTMAP_TOKENS = json.loads(os.environ.get("NODE_PORTMAP_TOKENS", "{}") or "{}")


@app.route("/edge/<path:path>", methods=["GET", "POST", "DELETE"])
def edge_proxy(path: str):
    """
    Controller -> Proxy-Agent (FastAPI) proxy.
    Adds X-Agent-Token automatically.
    """
    if not EDGE_WG_IP:
        return jsonify({"ok": False, "error": "EDGE_WG_IP not set"}), 500
    if not EDGE_AGENT_TOKEN:
        return jsonify({"ok": False, "error": "EDGE_AGENT_TOKEN not set"}), 500

    url = f"http://{EDGE_WG_IP}:{EDGE_AGENT_PORT}/{path}"

    headers = {"X-Agent-Token": EDGE_AGENT_TOKEN}

    try:
        if request.method == "GET":
            r = requests.get(url, params=request.args, headers=headers, timeout=120)
        elif request.method == "POST":
            r = requests.post(
                url,
                json=request.get_json(silent=True),
                params=request.args,
                headers=headers,
                timeout=120,
            )
        else:
            r = requests.delete(url, params=request.args, headers=headers, timeout=120)

        return (r.text, r.status_code, {"Content-Type": r.headers.get("Content-Type", "application/json")})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502
