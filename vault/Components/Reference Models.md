# Reference Models (`scionarena.reference.models`)

> [models.py](file:///C:/Users/Von/Desktop/PAS_Research_desktop/PAS_Research_desktop/scionfit/src/scionarena/reference/models.py)

---

## 📌 Built-in Reference Models

### 1. [`EMAOracle`](file:///C:/Users/Von/Desktop/PAS_Research_desktop/PAS_Research_desktop/scionfit/src/scionarena/reference/models.py#L54) (The Incumbent Path Oracle)
* **Design**: Exponential moving average (EMA) of metrics per interface, path metric as minimum over interfaces, greedy argmax advisory.
* **Capabilities**: `distributional=False`, `demand_conditioned=False`, `emits_assignment=False`.
* **Verdict**: `OPEN-LOOP ONLY`. Causes severe closed-loop herd oscillations.

### 2. [`MinRTTGreedy`](file:///C:/Users/Von/Desktop/PAS_Research_desktop/PAS_Research_desktop/scionfit/src/scionarena/reference/models.py#L149) (Greedy Latency Baseline)
* **Design**: Lowest predicted RTT wins 100% of traffic.
* **Capabilities**: Point estimate, greedy one-hot assignment.
* **Verdict**: `OPEN-LOOP ONLY`. High-frequency route flapping.

### 3. [`CapacityProportional`](file:///C:/Users/Von/Desktop/PAS_Research_desktop/PAS_Research_desktop/scionfit/src/scionarena/reference/models.py#L171) (Static Stable Baseline)
* **Design**: Splits traffic statically according to beacon-declared link capacity. Ignores telemetry entirely.
* **Verdict**: `OPEN-LOOP ONLY` (fails R1 and R3), but perfectly stable with zero oscillation.

### 4. [`ReferenceStochastic`](file:///C:/Users/Von/Desktop/PAS_Research_desktop/PAS_Research_desktop/scionfit/src/scionarena/reference/models.py#L236) (Fully Conformant 3-Block Design)
* **Design**:
  * *Block 1 (State Estimator)*: Running mean and variance per interface.
  * *Block 2 (Demand Response)*: Monotone load model.
  * *Block 3 (Assignment Operator)*: Entropy-regularized softmax advisory solved via Method of Successive Averages (MSA), with temperature decaying with telemetry age.
* **Capabilities**: All capabilities `True`.
* **Verdict**: `CONFORMANT` (passes all 10 probes).
