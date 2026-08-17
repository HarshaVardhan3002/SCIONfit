# Hard Invariants

> **The 7 Rules of `scionarena`**: Violating any of these destroys the scientific and engineering validity of the platform.

---

### Invariant 1: The harness never summarises for the model
* **Rule**: No automated feature engineering, rolling moving averages, or pre-aggregated statistics calculated on the model's behalf.
* **Why**: The model receives raw events. Memory management, context retention, and state estimation are essential ML properties under test.

### Invariant 2: Every tool call has a cost and the cost is charged
* **Rule**: Probing tools consume bandwidth, take wall-clock time, and consume finite probe quotas. Refused calls still cost computational budget.
* **Why**: Prevents models from bypassing learning by polling the entire network state infinitely at zero cost.

### Invariant 3: The network does not wait for the model
* **Rule**: Wall-clock time is simulated deterministically. If an advisory takes 800 ms of thinking time, it is applied 800 ms late against a network that has progressed.
* **Why**: Never pause the physical world while an AI computes. In live networking, stale decisions cause severe instability.

### Invariant 4: Bit-for-bit determinism from a seed
* **Rule**: Identical `(seed, model, version, scenario)` yields an identical Blake2b trace hash across runs and platforms.
* **Why**: True reproducibility is necessary for scientific benchmarking, regression testing, and verification.

### Invariant 5: The four front-ends share one substrate
* **Rule**: No bespoke simulation models per front-end.
* **Why**: A model trained in `gym`, checked in `conformance`, and evaluated in `bench` must run unchanged in `deploy`.

### Invariant 6: Probes and detectors must be able to fail
* **Rule**: A probe that nothing fails measures nothing. Every probe and detector must ship with a counter-model or synthetic positive that triggers a failure.
* **Why**: Protects against degenerate tests and benchmarks that grant false passes.

### Invariant 7: Strict downward dependency hierarchy
* **Rule**: $\text{core} \leftarrow \text{exposure} \leftarrow \{\text{conformance}, \text{bench}, \text{gym}, \text{agent}, \text{deploy}\}$.
* **Why**: Prevents architectural spaghetti, circular imports, and abstraction leakage.
