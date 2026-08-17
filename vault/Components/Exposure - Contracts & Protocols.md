# Exposure - Contracts & Protocols (`scionarena.exposure.contracts`)

> [contracts.py](file:///C:/Users/Von/Desktop/PAS_Research_desktop/PAS_Research_desktop/scionfit/src/scionarena/exposure/contracts.py)

---

## 📌 Role & Responsibilities
Provides the zero-dependency Python interface that any external model or agent imports. Has zero imports from `scionarena.core` or external ML libraries.

## 📜 Core Protocols
1. [`PathModel`](file:///C:/Users/Von/Desktop/PAS_Research_desktop/PAS_Research_desktop/scionfit/src/scionarena/exposure/contracts.py#L351):
   * `reset(topo: TopologySnapshot, seed: int)`: Clears per-episode state.
   * `observe(obs: Sequence[Observation], topo: TopologySnapshot)`: Ingests raw measurements.
   * `predict(topo, paths, horizon_s, demand)`: Predicts latency, throughput, and loss distributions.
   * `advise(topo, paths, sla, n_hosts)`: Emits probability distribution over paths.
2. [`ToolUsingModel`](file:///C:/Users/Von/Desktop/PAS_Research_desktop/PAS_Research_desktop/scionfit/src/scionarena/exposure/contracts.py#L450):
   * `act(session: SessionLike, deadline_s: float)`: Autonomous agent interaction.

## 📦 Data Contracts
* [`Capabilities`](file:///C:/Users/Von/Desktop/PAS_Research_desktop/PAS_Research_desktop/scionfit/src/scionarena/exposure/contracts.py#L308): Declares model features (`distributional`, `demand_conditioned`, `emits_assignment`, `staleness_aware`, `uses_tools`, etc.).
* [`Advisory`](file:///C:/Users/Von/Desktop/PAS_Research_desktop/PAS_Research_desktop/scionfit/src/scionarena/exposure/contracts.py#L228): Probability weights over paths, solver convergence stats, entropy.
* [`Dist`](file:///C:/Users/Von/Desktop/PAS_Research_desktop/PAS_Research_desktop/scionfit/src/scionarena/exposure/contracts.py#L158): Quantile distribution representation ($\ge 3$ ordered quantiles).
* [`Observation`](file:///C:/Users/Von/Desktop/PAS_Research_desktop/PAS_Research_desktop/scionfit/src/scionarena/exposure/contracts.py#L117): Telemetry point (`None` = unmeasured, distinct from 0.0).
