# WireGuard Controller

Lightweight control plane for managing a private WireGuard network with dynamic node registration, health monitoring, and secure API proxying.

The controller automatically assigns VPN IP addresses to nodes, manages peers in real time, and provides an API to interact with nodes over the WireGuard network.

---

## Features

* Dynamic WireGuard peer registration via API
* Automatic IP allocation from private subnet
* Persistent peer storage
* Node health monitoring (WireGuard + API)
* Secure proxying to node APIs over VPN
* Edge agent proxy support
* Docker-ready deployment

---

## Architecture

```
                Internet
                    │
            ┌───────────────┐
            │   Controller   │
            │ WireGuard + API│
            └───────┬────────┘
                    │
            Private WG Network
                    │
       ┌────────────┼────────────┐
       │            │            │
     Node A       Node B       Node C
   Agent API    Agent API    Agent API
```

---

## How It Works

1. Node generates WireGuard keys.
2. Node sends join request to controller.
3. Controller:

   * validates token
   * assigns free IP
   * adds peer dynamically
   * returns connection parameters.
4. Node configures its WireGuard client.

No manual configuration required.

---

## API Endpoints

### Health Check

```
GET /health
```

---

### Get Controller Public Key

```
GET /wg/publickey
```

---

### Join Network

```
POST /join
Header: X-Join-Token
```

Request:

```json
{
  "node_id": "node-1",
  "node_pubkey": "..."
}
```

Response:

```json
{
  "node_ip": "10.50.0.2",
  "controller_pubkey": "...",
  "endpoint": "IP:51820"
}
```

---

### List Nodes

```
GET /nodes
```

---

### Get Node Details

```
GET /nodes/<node_id>
```

---

### Proxy Request to Node

```
/nodes/<node_id>/proxy/<path>
```

Example:

```
GET /nodes/node-1/proxy/health
```

---

### Edge Agent Proxy

```
/edge/<path>
```

Used for forwarding requests to an edge proxy agent.

---

## Environment Variables

| Variable        | Description                        |
| --------------- | ---------------------------------- |
| DATA_DIR        | Directory for persistent data      |
| WG_IFACE        | WireGuard interface name           |
| WG_ADDR         | Controller VPN address             |
| WG_NET          | WireGuard subnet                   |
| WG_ENDPOINT     | Public endpoint (IP:PORT)          |
| JOIN_TOKEN      | Secret token for node registration |
| NODE_API_PORT   | Node API port                      |
| EDGE_WG_IP      | Edge agent VPN IP                  |
| EDGE_AGENT_PORT | Edge agent API port                |

---

## Running with Docker

### Build

```
docker compose build
```

### Start

```
docker compose up -d
```

---

## Ports

| Port      | Purpose        |
| --------- | -------------- |
| 9000      | Controller API |
| 51820/udp | WireGuard      |

---

## Data Persistence

Peer data is stored in:

```
/data/peers.json
```

Example:

```json
{
  "node-1": {
    "ip": "10.50.0.2",
    "pubkey": "..."
  }
}
```

---

## Health Monitoring

Node status is determined by:

* WireGuard handshake activity
* Node API availability

A node is considered online only if both are reachable.

---

## Security

* Join requires shared secret token
* All traffic encrypted via WireGuard
* Nodes not publicly exposed
* Optional per-node API tokens

---

## Dependencies

* Python 3
* Flask
* Gunicorn
* WireGuard
* Docker

---

## Use Cases

* Private edge infrastructure
* Self-hosted VPN clusters
* Distributed proxy networks
* Secure remote compute nodes
* DevOps lab environments
