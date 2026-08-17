# BPR Congestion Tail vs. M/M/1 Queueing

> **Context**: How the substrate maps offered traffic load to observable link metrics ([ADR 0006](file:///C:/Users/Von/Desktop/PAS_Research_desktop/PAS_Research_desktop/scionfit/docs/adr/0006-link-model-form.md)).

---

## 📐 The Load-to-Metrics Mathematical Form

Let offered load on interface $i$ be $L_i$, usable capacity be $C_i$, and health multiplier be $h_i$.

1. **Utilisation ($u$)**:
   $$u = \min\left(\frac{L_i}{C_i \cdot h_i}, 0.995\right)$$

2. **Directional Latency**:
   $$\text{latency}(u) = \text{latency}_{\text{free}} \cdot \left(1 + \alpha u^\beta\right) + \text{queue\_ms} \cdot \frac{u^\gamma}{1 - u}$$

3. **Packet Loss Probability**:
   $$\text{loss}(u) = \text{loss}_{\text{base}} + \text{loss}_{\text{max}} \cdot \left[\max\left(0, \frac{u - \text{onset}}{1 - \text{onset}}\right)\right]^\delta$$

4. **Available Bandwidth**:
   $$\text{available\_bw} = \max(0, C_i \cdot h_i - L_i)$$

5. **Scalar Path Cost**:
   $$\text{cost} = \text{latency} + \text{loss\_penalty\_ms} \cdot \text{loss}$$

---

## ⚖️ Why BPR + Tail Beats M/M/1

| Criterion | M/M/1 Formula | BPR with Queueing Tail |
| :--- | :--- | :--- |
| **Formula** | $\text{delay} \propto \frac{1}{1 - u}$ | $\text{free\_flow}(1 + \alpha u^\beta) + \text{tail}$ |
| **Realistic Operational Provisioning** | ❌ Assumes infinite buffer; blows up unrealistically early. | ✅ Accurately models low latency up to target utilisation threshold. |
| **Buffer Knee Representation** | ❌ Smooth hyperbola with no sharp knee. | ✅ Sharp non-linear knee at high load via $\frac{u^\gamma}{1-u}$. |
| **Calibration Flexibility** | ❌ No free parameters to fit against real data. | ✅ Parameters $(\alpha, \beta, \gamma, \text{queue\_ms})$ fitted via least squares (`fit_bpr`). |
| **Monotonicity Guarantee** | ✅ Monotone. | ✅ **Strictly non-decreasing** (guaranteed by non-negative powers and positive constants). |

---

## 🔒 Hard Invariant: Cost Monotonicity
The function $C(u)$ is strictly non-decreasing in load. This is a mathematical prerequisite for **Probe R7** (Monotone in Load) and game-theoretic equilibrium stability.
