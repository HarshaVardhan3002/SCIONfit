# Architecture Decision Records (ADRs) Summary

> A complete register of all 11 Architecture Decision Records in `docs/adr/`.

---

### [ADR 0001: Record Architecture Decisions](file:///C:/Users/Von/Desktop/PAS_Research_desktop/PAS_Research_desktop/scionfit/docs/adr/0001-record-architecture-decisions.md)
* **Status**: Accepted
* **Summary**: Standardizes ADR recording in `docs/adr/` before implementing any significant architectural change.

### [ADR 0002: Static Physical Links; Cryptographic Material Churns](file:///C:/Users/Von/Desktop/PAS_Research_desktop/PAS_Research_desktop/scionfit/docs/adr/0002-static-links-crypto-churn.md)
* **Status**: Accepted (Supersedes v0.1 churn model)
* **Summary**: Replaces incorrect hourly physical link churn with stable physical topologies and scheduled cryptographic segment beacon refreshes. Introduces dual identifiers to evaluate identity amnesia.

### [ADR 0003: Four Front-Ends, One Substrate](file:///C:/Users/Von/Desktop/PAS_Research_desktop/PAS_Research_desktop/scionfit/docs/adr/0003-four-frontends-one-substrate.md)
* **Status**: Accepted
* **Summary**: Mandates that `conformance`, `bench`, `gym`, and `deploy` share an identical substrate to guarantee model portability across evaluation, training, and production.

### [ADR 0004: Package Layout and Enforced Import Direction](file:///C:/Users/Von/Desktop/PAS_Research_desktop/PAS_Research_desktop/scionfit/docs/adr/0004-m0-package-layout-and-enforced-import-direction.md)
* **Status**: Accepted
* **Summary**: Restructures package into `scionarena.{core, exposure, backends, instrument, conformance, reference, bench, gym, agent, deploy}` with CI-enforced `import-linter` rules.

### [ADR 0005: Segment Identity and Bounded Path Composition](file:///C:/Users/Von/Desktop/PAS_Research_desktop/PAS_Research_desktop/scionfit/docs/adr/0005-segment-identity-and-bounded-path-composition.md)
* **Status**: Accepted
* **Summary**: Carries both `structural_id` (BLAKE2b hash of interface sequence) and `segment_id` (hash tied to signatures). Binds core segment search with beam width limits and LRU caching for lazy path materialization.

### [ADR 0006: The Load-to-Metrics Form](file:///C:/Users/Von/Desktop/PAS_Research_desktop/PAS_Research_desktop/scionfit/docs/adr/0006-link-model-form.md)
* **Status**: Accepted
* **Summary**: Adopts the BPR formulation combined with a nonlinear queueing tail. Ensures cost is strictly monotonically non-decreasing with load. Folds unreadable diurnal background traffic into interface states.

### [ADR 0007: The Scenario Schema](file:///C:/Users/Von/Desktop/PAS_Research_desktop/PAS_Research_desktop/scionfit/docs/adr/0007-scenario-schema.md)
* **Status**: Accepted
* **Summary**: Defines a portable JSON/YAML scenario format driving all 4 fidelity tiers with explicit parameters for topology, traffic, search limits, and scheduled timeline events.

### [ADR 0008: Tool Costs, Rate Limits, and the Exposure Boundary](file:///C:/Users/Von/Desktop/PAS_Research_desktop/PAS_Research_desktop/scionfit/docs/adr/0008-tool-costs-rate-limits-and-the-exposure-boundary.md)
* **Status**: Accepted
* **Summary**: Defines explicit costs, wall-clock latencies, and rate limits across tools (`query_paths`, `probe_path`, `fetch_history`, etc.). Probes incur real bandwidth and load penalties. Refused calls cost execution budget.

### [ADR 0009: Closing the Loop — Hosts, Delay, and Realised Load](file:///C:/Users/Von/Desktop/PAS_Research_desktop/PAS_Research_desktop/scionfit/docs/adr/0009-closing-the-loop-hosts-delay-and-realised-load.md)
* **Status**: Accepted
* **Summary**: Closes the loop via independent multinomial host sampling. Delays advisory application by model decision latency $\tau_{\text{decision}}$. Tracks defector fractions.

### [ADR 0010: Measuring Oscillation by Amplitude, Not Periodicity](file:///C:/Users/Von/Desktop/PAS_Research_desktop/PAS_Research_desktop/scionfit/docs/adr/0010-measuring-oscillation-by-amplitude-not-periodicity.md)
* **Status**: Accepted (Revises M3 acceptance criteria)
* **Summary**: Proves that raw FFT peak dominance ($> 0.5$) is scale- and sample-rate-dependent. Replaces it with the scale-invariant **fast-band peak-to-peak swing amplitude ratio** ($\ge 4\times$) between greedy and stochastic models.

### [ADR 0011: Sampling off the World's Clock](file:///C:/Users/Von/Desktop/PAS_Research_desktop/PAS_Research_desktop/scionfit/docs/adr/0011-sampling-off-the-worlds-clock.md)
* **Status**: Accepted (Resolves M3 cadence limitation)
* **Summary**: Decouples time-series sampling from model decision rounds. Samples directly from a substrate tap at fixed simulated world time $k \cdot T_{\text{sample}}$, guaranteeing uniform sampling grids even when models overrun deadlines.
