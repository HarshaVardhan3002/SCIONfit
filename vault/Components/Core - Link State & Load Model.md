# Core - Link State & Load Model (`scionarena.core.linkstate`)

> [linkstate.py](file:///C:/Users/Von/Desktop/PAS_Research_desktop/PAS_Research_desktop/scionfit/src/scionarena/core/linkstate.py)

---

## 📌 Role & Responsibilities
Transforms offered traffic load into observable physical network metrics (directional latency, packet loss, available throughput) using a calibrated, monotonic BPR congestion function.

## 🎛️ Mathematical Model Parameters
Defined in [`LinkParams`](file:///C:/Users/Von/Desktop/PAS_Research_desktop/PAS_Research_desktop/scionfit/src/scionarena/core/linkstate.py#L62):
* `alpha`: 0.15 (BPR coefficient)
* `beta`: 4.0 (BPR polynomial exponent)
* `queue_ms`: 8.0 ms (Queueing tail buffer latency at saturation)
* `queue_exponent`: 2.0
* `loss_onset`: 0.85 (Utilisation threshold where packet drops begin)
* `loss_exponent`: 2.0
* `max_loss`: 0.40 (40% maximum drop rate)
* `base_loss`: $10^{-5}$ (Background optical/interface loss)
* `loss_penalty_ms`: 500.0 ms (Penalty multiplier for collapsing loss to scalar cost)

## 🌊 Background Traffic Dynamics
Defined in [`BackgroundParams`](file:///C:/Users/Von/Desktop/PAS_Research_desktop/PAS_Research_desktop/scionfit/src/scionarena/core/linkstate.py#L100):
* Exogenous, deterministic, unreadable background load folded into total interface load:
  $$L_{\text{bg}}(t) = \text{diurnal\_load}(t, \phi_i) + \text{noise}(t)$$
* Per-link diurnal phases $\phi_i$ are drawn from a normal distribution with spread `PHASE_SPREAD = 0.09` days (~2.1 hours) around a global timezone mean.
* Counter-based pseudo-random draws based on `(seed, link_id, time_bucket)`.
