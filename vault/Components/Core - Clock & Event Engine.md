# Core - Clock & Event Engine (`scionarena.core.clock`)

> [clock.py](file:///C:/Users/Von/Desktop/PAS_Research_desktop/PAS_Research_desktop/scionfit/src/scionarena/core/clock.py)

---

## 📌 Role & Responsibilities
Provides deterministic simulated time progression and priority-queued discrete event dispatch.

## ⏱️ Design Principles
1. **Time Never Moves Backwards**: `Clock.advance_to(t)` strictly enforces $t \ge t_{\text{current}}$.
2. **Latency is Scheduled, Never Slept**: Computation time measured via [`Stopwatch`](file:///C:/Users/Von/Desktop/PAS_Research_desktop/PAS_Research_desktop/scionfit/src/scionarena/core/clock.py#L321) schedules consequences at $t + \Delta t_{\text{latency}}$. The substrate continues stepping during that interval.
3. **Events as Data**: Events are stored as immutable dataclasses (`at_s`, `kind`, `payload`, `seq`, `priority`), guaranteeing determinism and serializability.

## 🔢 Priority Ordering
* `PRIORITY_WORLD = 0`: Substrate ticks and physics updates.
* `PRIORITY_SCENARIO = 10`: Scheduled scenario perturbations.
* `PRIORITY_OBSERVE = 20`: Telemetry sampling and model observation taps.
