from __future__ import annotations

import ipaddress
import json
import os
import subprocess
import time
from pathlib import Path

import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# =========================
#   Files / persistent state
# =========================

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data")).resolve()
PEERS_FILE = DATA_DIR / "peers.json"
ALLOC_FILE = DATA_DIR / "allocations.json"

# =========================
#   WireGuard / join config
# =========================

WG_IFACE = os.environ.get("WG_IFACE", "wg0")
WG_NET = ipaddress.ip_network(os.environ.get("WG_NET", "10.50.0.0/24"))
WG_ENDPOINT = os.environ.get("WG_ENDPOINT", "")  # "public_ip:51820"
JOIN_TOKEN = os.environ.get("JOIN_TOKEN", "")

CONTROLLER_IP = ipaddress.ip_address(os.environ.get("CONTROLLER_IP", "10.50.0.1"))

# =========================
#   Node API / scheduler config
# =========================

NODE_API_PORT = int(os.environ.get("NODE_API_PORT", "8000"))

STATS_TTL = int(os.environ.get("STATS_TTL", "5"))  # seconds
MAX_CANDIDATES = int(os.environ.get("SCHED_MAX_CANDIDATES", "3"))  # try top-N nodes

NODE_PORTMAP_TOKENS = json.loads(os.environ.get("NODE_PORTMAP_TOKENS", "{}") or "{}")

# cache: node_id -> {"ts": epoch, "stats": {...}}
_stats_cache: dict = {}


# =========================
#   Helpers: files
# =========================

def sh(*args: str) -> str:
    return subprocess.check_output(args, text=True).strip()


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def save_json(path: Path, obj) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, sort_keys=True))


def load_peers() -> dict:
    return load_json(PEERS_FILE, {})


def save_peers(peers: dict) -> None:
    save_json(PEERS_FILE, peers)


def load_allocations() -> dict:
    return load_json(ALLOC_FILE, {})


def save_allocations(st: dict) -> None:
    save_json(ALLOC_FILE, st)


# =========================
#   Helpers: WireGuard
# =========================

def controller_pubkey() -> str:
    return sh("wg", "show", WG_IFACE, "public-key")


def pick_free_ip(peers: dict) -> str:
    used = {info.get("ip") for info in peers.values() if info.get("ip")}

    # reserve .1 for controller
    for ip in WG_NET.hosts():
        if ip == CONTROLLER_IP:
            continue
        ip_s = str(ip)
        if ip_s not in used:
            return ip_s
    raise RuntimeError("No free IPs in WG_NET")


def add_peer_to_wg(node_pubkey: str, node_ip: str) -> None:
    sh("wg", "set", WG_IFACE, "peer", node_pubkey, "allowed-ips", f"{node_ip}/32")


def sync_peers_to_wg() -> None:
    peers = load_peers()
    for node_id, info in peers.items():
        try:
            add_peer_to_wg(info["pubkey"], info["ip"])
        except Exception as e:
            print(f"[!] failed to sync peer {node_id}: {e}")


def wg_peers_status() -> dict:
    """
    Returns:
      { "<pubkey>": { "last_handshake": seconds_ago or None } }
    """
    out = subprocess.check_output(["wg", "show", WG_IFACE, "dump"], text=True).strip().splitlines()

    st = {}
    # line0: iface priv pub listen fwmark
    # peer lines: peer_pub preshared endpoint allowed_ips latest_handshake rx tx keepalive
    for line in out[1:]:
        parts = line.split("\t")
        if len(parts) < 8:
            continue

        peer_pub = parts[0]
        latest_handshake = int(parts[4])  # correct index is 4

        if latest_handshake == 0:
            st[peer_pub] = {"last_handshake": None}
        else:
            st[peer_pub] = {"last_handshake": int(time.time()) - latest_handshake}

    return st


# =========================
#   Helpers: Node API checks / stats / scheduling
# =========================

def check_node_api(ip: str, timeout: int = 3) -> bool:
    try:
        r = requests.get(f"http://{ip}:{NODE_API_PORT}/health", timeout=timeout)
        return r.status_code == 200
    except Exception:
        return False


def get_node_stats(node_id: str, ip: str, timeout: int = 3) -> dict | None:
    now = time.time()
    c = _stats_cache.get(node_id)
    if c and (now - c.get("ts", 0)) < STATS_TTL:
        return c.get("stats")

    try:
        r = requests.get(f"http://{ip}:{NODE_API_PORT}/stats", timeout=timeout)
        if r.status_code != 200:
            return None
        data = r.json()
        if not data.get("ok"):
            return None
        _stats_cache[node_id] = {"ts": now, "stats": data}
        return data
    except Exception:
        return None


def meets_requirements(stats: dict, req: dict) -> bool:
    ram_mb = int(req.get("ram_mb", 0))
    disk_gb = int(req.get("disk_gb", 0))
    cores = int(req.get("cores", 0))

    free_mb = int(stats.get("ram", {}).get("free_mb", 0))
    free_gb = int(stats.get("disk", {}).get("free_gb", 0))
    node_cores = int(stats.get("cpu", {}).get("cores", 0))

    if ram_mb and free_mb < ram_mb:
        return False
    if disk_gb and free_gb < disk_gb:
        return False
    if cores and node_cores < cores:
        return False
    return True


def node_score(stats: dict) -> float:
    free_mb = float(stats.get("ram", {}).get("free_mb", 0))
    free_gb = float(stats.get("disk", {}).get("free_gb", 0))
    load1 = float(stats.get("cpu", {}).get("load1", 9999))
    running = float(stats.get("vms", {}).get("running", 9999))

    # Bigger is better
    return (free_mb * 1.0) + (free_gb * 50.0) - (load1 * 200.0) - (running * 30.0)


def pick_candidates(peers: dict, wg_status: dict, req: dict) -> list[dict]:
    """
    Returns list of candidates sorted best->worst:
      [{"node_id":..., "ip":..., "stats":..., "score":...}, ...]
    """
    cands = []
    for node_id, info in peers.items():
        ip = info["ip"]
        pub = info["pubkey"]

        handshake = (wg_status.get(pub, {}) or {}).get("last_handshake")
        wireguard_up = handshake is not None and handshake < 120
        api_ok = check_node_api(ip)

        if not (wireguard_up and api_ok):
            continue

        st = get_node_stats(node_id, ip)
        if not st:
            continue

        if not meets_requirements(st, req):
            continue

        cands.append({
            "node_id": node_id,
            "ip": ip,
            "stats": st,
            "score": node_score(st),
        })

    cands.sort(key=lambda x: x["score"], reverse=True)
    return cands


def allocation_for_vm(name: str) -> dict | None:
    alloc = load_allocations()
    return (alloc.get("vms", {}) or {}).get(name)


def set_allocation(name: str, node_id: str, ip: str) -> None:
    alloc = load_allocations()
    alloc.setdefault("vms", {})[name] = {"node_id": node_id, "ip": ip, "ts": int(time.time())}
    save_allocations(alloc)


def node_url(ip: str, path: str) -> str:
    path = path.lstrip("/")
    return f"http://{ip}:{NODE_API_PORT}/{path}"


# =========================
#   Basic endpoints
# =========================

@app.get("/health")
def health():
    return jsonify({"ok": True})


@app.get("/wg/publickey")
def wg_pubkey():
    return jsonify({"ok": True, "public_key": controller_pubkey()})


# =========================
#   Join / peer management
# =========================

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


# =========================
#   Node proxy (explicit node_id)
# =========================

@app.route("/nodes/<node_id>/proxy/<path:path>", methods=["GET", "POST", "DELETE"])
def proxy(node_id: str, path: str):
    peers = load_peers()
    if node_id not in peers:
        return jsonify({"ok": False, "error": "unknown node"}), 404

    node_ip = peers[node_id]["ip"]
    url = node_url(node_ip, path)

    # add X-Portmap-Token only for /ports endpoints
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


# =========================
#   Nodes listing (health + wg handshake)
# =========================

@app.get("/nodes")
def list_nodes():
    peers = load_peers()
    wg_status = wg_peers_status()
    nodes = []

    for node_id, info in peers.items():
        ip = info["ip"]
        pub = info["pubkey"]

        handshake = (wg_status.get(pub, {}) or {}).get("last_handshake")
        wireguard_up = handshake is not None and handshake < 120
        api_ok = check_node_api(ip)

        nodes.append({
            "node_id": node_id,
            "ip": ip,
            "wireguard": {"connected": wireguard_up, "last_handshake_seconds": handshake},
            "api": {"reachable": api_ok},
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
            "wireguard": {"connected": wireguard_up, "last_handshake_seconds": handshake},
            "api": {"reachable": api_ok},
            "online": wireguard_up and api_ok,
        }
    })


# =========================
#   Scheduler API (the thing you asked for)
# =========================

@app.post("/vms")
def create_vm_scheduled():
    """
    Create VM without specifying node.
    Controller selects node via /stats and proxies POST /vms to that node.
    """
    body = request.get_json(force=True, silent=True) or {}

    # optional scheduling requirements:
    req = body.get("requirements", {}) or {}

    # payload for node-api (your node expects: name, memory_mib, vcpus, disk_size_gb, ...)
    vm_payload = body.get("vm", body)

    name = str(vm_payload.get("name", "")).strip()
    if not name:
        return jsonify({"ok": False, "error": "Missing field: name"}), 400

    peers = load_peers()
    wg_status = wg_peers_status()

    cands = pick_candidates(peers, wg_status, req)
    if not cands:
        return jsonify({"ok": False, "error": "no suitable nodes available"}), 503

    tried = []
    last_err = None

    for cand in cands[:MAX_CANDIDATES]:
        node_id = cand["node_id"]
        ip = cand["ip"]
        tried.append(node_id)

        try:
            r = requests.post(node_url(ip, "vms"), json=vm_payload, timeout=120)

            if 200 <= r.status_code < 300:
                set_allocation(name, node_id, ip)
                return (
                    r.text,
                    r.status_code,
                    {
                        "Content-Type": r.headers.get("Content-Type", "application/json"),
                        "X-Scheduled-Node": node_id,
                    },
                )

            last_err = f"node {node_id} returned {r.status_code}: {r.text[:300]}"
        except Exception as e:
            last_err = f"node {node_id} request failed: {e}"

    return jsonify({
        "ok": False,
        "error": "all candidates failed",
        "tried": tried,
        "last_error": last_err,
    }), 502


@app.get("/vms/<name>/where")
def vm_where(name: str):
    info = allocation_for_vm(name)
    if not info:
        return jsonify({"ok": False, "error": "unknown vm"}), 404
    return jsonify({"ok": True, "name": name, **info})


# =========================
#   Optional: VM operations without node_id
#   (uses allocations.json)
# =========================

def _proxy_to_allocated_vm(name: str, path: str, method: str):
    info = allocation_for_vm(name)
    if not info:
        return jsonify({"ok": False, "error": "unknown vm (no allocation)"}), 404

    ip = info["ip"]
    url = node_url(ip, path)

    try:
        if method == "GET":
            r = requests.get(url, params=request.args, timeout=120)
        elif method == "POST":
            r = requests.post(url, json=request.get_json(silent=True), params=request.args, timeout=120)
        else:
            r = requests.delete(url, params=request.args, timeout=120)

        return (r.text, r.status_code, {"Content-Type": r.headers.get("Content-Type", "application/json")})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502


@app.get("/vms/<name>/status")
def vm_status_auto(name: str):
    return _proxy_to_allocated_vm(name, f"vms/{name}/status", "GET")


@app.post("/vms/<name>/start")
def vm_start_auto(name: str):
    return _proxy_to_allocated_vm(name, f"vms/{name}/start", "POST")


@app.post("/vms/<name>/stop")
def vm_stop_auto(name: str):
    return _proxy_to_allocated_vm(name, f"vms/{name}/stop", "POST")


@app.delete("/vms/<name>")
def vm_delete_auto(name: str):
    # forward query args like ?delete_files=true
    return _proxy_to_allocated_vm(name, f"vms/{name}", "DELETE")


@app.get("/vms/<name>/ip")
def vm_ip_auto(name: str):
    # forward query args like ?timeout=120&network=default
    return _proxy_to_allocated_vm(name, f"vms/{name}/ip", "GET")


# =========================
#   Edge proxy (kept as in your original)
# =========================

EDGE_WG_IP = os.environ.get("EDGE_WG_IP", "").strip()  # 10.50.0.2
EDGE_AGENT_PORT = int(os.environ.get("EDGE_AGENT_PORT", "8081"))

@app.route("/edge/<path:path>", methods=["GET", "POST", "DELETE"])
def edge_proxy(path: str):
    if not EDGE_WG_IP:
        return jsonify({"ok": False, "error": "EDGE_WG_IP not set"}), 500

    url = f"http://{EDGE_WG_IP}:{EDGE_AGENT_PORT}/{path}"

    try:
        if request.method == "GET":
            r = requests.get(url, params=request.args, timeout=120)
        elif request.method == "POST":
            r = requests.post(url, json=request.get_json(silent=True), params=request.args, timeout=120)
        else:
            # NOTE: your original had "headers" variable that wasn't defined here
            r = requests.delete(url, params=request.args, timeout=120)

        return (r.text, r.status_code, {"Content-Type": r.headers.get("Content-Type", "application/json")})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502