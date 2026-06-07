# SDN-Based Adaptive QoS Management in Wireless Networks

> **Course:** Wireless Network Protocols — Bursa Uludağ University, Dept. of Computer Engineering  
> **Author:** Sabir Süleymanlı · `502531019`

A working demonstration of **Software-Defined Networking (SDN)** applied to adaptive Quality of Service (QoS) management. The system continuously monitors traffic conditions and autonomously adjusts bandwidth policies — no manual intervention required.

---

## Overview

Traditional networks apply static QoS rules that cannot react to changing conditions. This project replaces that model with a feedback-driven controller: a **Ryu SDN controller** observes VoIP traffic health every few seconds and dynamically throttles competing Bulk TCP flows whenever VoIP quality degrades.

Three scenarios are implemented and can be compared side-by-side on a live dashboard:

| Scenario | Description |
|---|---|
| **Baseline** | No QoS rules. All traffic treated equally. |
| **Static QoS** | Fixed bandwidth cap on Bulk TCP, adjustable via dashboard slider. |
| **Adaptive QoS** | Controller monitors VoIP metrics and applies/releases limits automatically. |

---

## Architecture

```
┌─────────────────────────────────────────────────────┐
│  Dashboard (dashboard.html)                         │
│  Chart.js · REST polling · Scenario controls        │
└───────────────────┬─────────────────────────────────┘
                    │ HTTP (port 8080)
┌───────────────────▼─────────────────────────────────┐
│  Ryu SDN Controller (adaptive_controller.py)        │
│  OpenFlow 1.3 · Meter-based rate limiting · REST API│
└───────────────────┬─────────────────────────────────┘
                    │ OpenFlow (port 6633)
┌───────────────────▼─────────────────────────────────┐
│  Mininet Network (topology.py)                       │
│                                                     │
│  h1 (VoIP · UDP · 10.0.0.1) ──┐                    │
│  h2 (Video · UDP · 10.0.0.2) ─┼── s1 ── s2 ── h4  │
│  h3 (Bulk · TCP · 10.0.0.3) ──┘       (server)     │
│                                                     │
│  Link capacity: 10 Mbps · Delay: 5 ms               │
└─────────────────────────────────────────────────────┘
```

---

## File Structure

```
.
├── topology.py              # Mininet topology + measurement loop
├── adaptive_controller.py   # Ryu OpenFlow 1.3 controller + REST API
├── dashboard.html           # Live metrics dashboard (Chart.js)
└── README.md
```

---

## Requirements

Tested on **Ubuntu 20.04 / 22.04** (bare metal or VM).

```bash
# System packages
sudo apt update
sudo apt install -y mininet iperf3 python3-pip

# Ryu (pin eventlet for compatibility)
pip3 install ryu==4.34 eventlet==0.30.2 webob

# Verify
mn --version
ryu-manager --version
iperf3 --version
```

---

## Running the Demo

### Step 1 — Start the Ryu controller

```bash
ryu-manager adaptive_controller.py \
  --observe-links \
  --wsapi-port 8080
```

Wait for: `(XXXX) wsgi starting up on http://0.0.0.0:8080`

---

### Step 2 — Start the Mininet topology

Open a **second terminal** and choose a scenario:

```bash
# Scenario 1 — Baseline (no QoS)
sudo python3 topology.py --mode baseline

# Scenario 2 — Static QoS (Bulk capped at 2 Mbps)
sudo python3 topology.py --mode static --bulk-limit 2

# Scenario 3 — Adaptive QoS
sudo python3 topology.py --mode adaptive
```

---

### Step 3 — Open the dashboard

```bash
firefox dashboard.html
# or
chromium-browser dashboard.html
```

The dashboard auto-detects a live Ryu connection. If Mininet is not running it falls back to a built-in simulation so the UI is always functional for demo purposes.

---

## How It Works

### Measurement loop (`topology.py`)

Every 6 seconds, `measure_iperf()` runs `iperf3` against the server host and parses its JSON output:

- **UDP flows** → throughput (Mbps), jitter (ms), packet loss (%)
- **TCP flows** → throughput (Mbps), retransmit count
- **RTT** → `measure_ping()` sends 5 ICMP packets and returns average round-trip time

Results are broadcast over UDP to the dashboard (port 9999) and also queryable via the Ryu REST API.

### Adaptive decision algorithm (`topology.py › run_adaptive_qos`)

```
Every cycle:
  if VoIP loss > 2% OR VoIP throughput < 0.3 Mbps:
      bulk_limit = max(MIN, bulk_limit × 0.6)   # fast cut  (–40% per step)
      apply tc rate limit on h3
  elif VoIP is healthy AND limit was active:
      bulk_limit = min(MAX, bulk_limit × 1.3)   # slow release (+30% per step)
      apply tc rate limit on h3
```

The asymmetry is intentional: protecting VoIP requires a fast reaction, while releasing the limit gradually prevents oscillation.

### OpenFlow rules (`adaptive_controller.py`)

| Traffic | Match | Priority | Action |
|---|---|---|---|
| VoIP | UDP dst 5001 | 200 (high) | Forward |
| Video | UDP dst 5002 | 100 (medium) | Forward |
| Bulk TCP | ip_proto=6 | 10 (low) | Forward via Meter |

The **Meter** (`_add_meter`) uses `OFPMeterBandDrop`: packets exceeding the configured kbps rate are dropped at the switch level without involving the controller.

### REST API

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/qos/mode` | Current mode and Bulk limit |
| `POST` | `/qos/mode` | Switch scenario (`baseline` / `static` / `adaptive`) |
| `GET` | `/qos/stats` | Latest measured metrics |

```bash
# Switch to adaptive mode
curl -X POST http://127.0.0.1:8080/qos/mode \
  -H "Content-Type: application/json" \
  -d '{"mode": "adaptive"}'

# Apply a 1.5 Mbps static cap
curl -X POST http://127.0.0.1:8080/qos/mode \
  -d '{"mode": "static", "bulk_limit_mbps": 1.5}'
```

---

## Dashboard Features

- **Live throughput chart** — VoIP, Video, Bulk plotted in real time (last 40 samples)
- **Packet loss & RTT chart** — dual Y-axis (loss % left, RTT ms right)
- **Metric cards** — per-flow throughput, jitter, loss, retransmit count
- **Static QoS panel** — slider to set Bulk cap; sends `POST /qos/mode` on apply
- **Adaptive decision panel** — shows VoIP health status, current Bulk limit, and last autonomous action taken by the controller
- **Event log** — timestamped record of all constraint changes

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `mn: command not found` | `sudo apt install mininet` |
| Ryu import errors | `pip3 install ryu==4.34 eventlet==0.30.2` |
| Switch not connecting | Start Ryu **before** Mininet |
| All throughputs read 0 | `iperf3` not installed or server not running on h4 |
| `tc: permission denied` | Run topology with `sudo` |
| Dashboard shows no data | Check `curl http://127.0.0.1:8080/qos/stats` |
| `TypeError: tuple not callable` | Webob version mismatch — fix: `pip3 install webob==1.8.7` |

---

## References

- Kreutz, D. et al. *Software-Defined Networking: A Comprehensive Survey*. IEEE, 2015.
- Jain, R. & Paul, S. *Network Virtualization and SDN for Cloud Computing*. IEEE, 2013.
- Sezer, S. et al. *Are We Ready for SDN?* IEEE Communications Magazine, 2013.
- [Mininet Documentation](http://mininet.org)
- [Ryu SDN Framework](https://ryu.readthedocs.io)
- [OpenFlow 1.3 Specification](https://opennetworking.org/wp-content/uploads/2014/10/openflow-spec-v1.3.0.pdf)

---

## License

This project was developed for academic purposes as part of the Wireless Network Protocols course at Bursa Uludağ University. Free to use and adapt with attribution.
