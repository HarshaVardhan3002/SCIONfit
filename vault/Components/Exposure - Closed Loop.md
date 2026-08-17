# Exposure - Closed Loop (`scionarena.exposure.loop`)

> [loop.py](file:///C:/Users/Von/Desktop/PAS_Research_desktop/PAS_Research_desktop/scionfit/src/scionarena/exposure/loop.py)

---

## 📌 Role & Responsibilities
Executes multi-scope closed-loop iterations, driving the full cycle:
$$\text{Advisory} \longrightarrow \text{Host Sampling} \longrightarrow \text{Offered Load} \longrightarrow \text{Link Dynamics} \longrightarrow \text{Telemetry} \longrightarrow \text{Advisory}$$

## ⚙️ Loop Configuration ([`LoopConfig`](file:///C:/Users/Von/Desktop/PAS_Research_desktop/PAS_Research_desktop/scionfit/src/scionarena/exposure/loop.py#L64))
* `cycles`: Number of decision rounds per episode (default: 240).
* `decision_s`: Allocated simulated seconds per round (default: 30.0 s).
* `sample_s`: Sample recording period on absolute world clock (ADR 0011).
* `target_load`: Traffic load as a fraction of spare bottleneck capacity (default: 0.90).
* `extra_latency_s`: Injected artificial decision delay for controlled latency sensitivity studies.
* `n_tracked`: Top contested interface bottlenecks tracked for spectral analysis.

## 📊 Result Metrics ([`LoopResult`](file:///C:/Users/Von/Desktop/PAS_Research_desktop/PAS_Research_desktop/scionfit/src/scionarena/exposure/loop.py#L118))
Records fast-band swing amplitude, share swing amplitude, FFT spectral peak dominance, flap rate, mean path cost, decision latency distribution, and deadline overrun counts.
