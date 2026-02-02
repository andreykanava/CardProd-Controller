from __future__ import annotations

import ipaddress
import json
import os
import subprocess
from pathlib import Path
from flask import Flask, request, jsonify

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

    # already registered
    if node_id in peers:
        info = peers[node_id]
        # ensure peer exists in live wg (idempotent)
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

    try:
        if request.method == "GET":
            r = requests.get(url, params=request.args, timeout=120)
        elif request.method == "POST":
            r = requests.post(url, json=request.get_json(silent=True), params=request.args, timeout=120)
        else:
            r = requests.delete(url, params=request.args, timeout=120)

        return (r.text, r.status_code, {"Content-Type": r.headers.get("Content-Type", "application/json")})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502
