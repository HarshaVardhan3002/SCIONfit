# Exposure - Tool Registry & Cost Accounting (`scionarena.exposure.tools`)

> [tools.py](file:///C:/Users/Von/Desktop/PAS_Research_desktop/PAS_Research_desktop/scionfit/src/scionarena/exposure/tools.py) & [budget.py](file:///C:/Users/Von/Desktop/PAS_Research_desktop/PAS_Research_desktop/scionfit/src/scionarena/exposure/budget.py)

---

## 📌 Role & Responsibilities
Defines JSON-Schema tools, charges realistic costs (bandwidth, probe units, wall-clock time), and enforces strict rate limits.

## 🛠️ Tool Registry Specification
| Tool Name | Purpose | Cost / Overheads | Rate Limit |
| :--- | :--- | :--- | :--- |
| `query_paths` | Discovers available paths between $(src, dst)$ | $2\text{ ms} + 0.1\text{ ms/path}$, $200\text{ bytes/path}$ | $2\text{ calls/sec}$ |
| `probe_path` (`echo`) | SCMP Echo round-trip latency probe | $1\text{ probe unit}$, $128\text{ bytes}$, $\approx \text{RTT}$ | $5\text{ calls/sec}$ |
| `probe_path` (`loss`) | Burst of 20 SCMP packets | $20\text{ probe units}$, $2.56\text{ KB}$, $\ge 0.5\text{ s}$ | $1\text{ call/sec}$ |
| `probe_path` (`bwtest`)| Active bandwidth saturation probe | $100\text{ probe units}$, $5\text{ MB}$, $2.0\text{ s}$ duration, **injects $50\text{ Mbps}$ load** | $1\text{ call/30 sec}$ |
| `fetch_history` | Retrieves historical telemetry events | $0.2\text{ ms/event}$, $64\text{ bytes/event}$ | Uncapped |
| `subscribe` | Subscribes to asynchronous telemetry streams | $5\text{ ms}$, $256\text{ bytes}$ | Uncapped |
| `publish_advisory` | Commits path advisory distribution | $1\text{ ms}$, $128\text{ bytes}$ | Cadence bound |

## 💰 The Refusal Cost Principle
When a tool call is refused (e.g. rate limit exceeded or invalid argument), the model is still charged `REFUSAL_COST = Cost(wall_clock_s=0.001, nbytes=64)`. This prevents models from probing the rate limiter for free state information.
