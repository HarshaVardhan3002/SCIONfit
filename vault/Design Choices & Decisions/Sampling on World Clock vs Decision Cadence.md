# Sampling on World Clock vs. Decision Cadence

> **Context**: How M4 solved the "sampling grid distortion" limitation inherited from M3 ([ADR 0011](file:///C:/Users/Von/Desktop/PAS_Research_desktop/PAS_Research_desktop/scionfit/docs/adr/0011-sampling-off-the-worlds-clock.md)).

---

## 🚫 The M3 Problem: Sampling per Decision Round
In M3, metric series were recorded **once per model decision round**.
* If a model took longer to compute (e.g. 50 s instead of 30 s), the simulation widened the time step between samples.
* **Consequence**: The resulting time series had uneven intervals or varied across models. When fed into FFT spectral estimators, folding power into fewer bins artificially inflated spectral peak dominance numbers.

## ✅ The M4 Solution: Substrate Tap on Absolute World Clock
In M4, time-series telemetry is extracted via an independent substrate tap at fixed simulated world-clock intervals:
$$t_k = k \cdot T_{\text{sample}}$$

```
World Time (s):   0    10   20   30   40   50   60   70   80   90
Sample Grid:      ▲    ▲    ▲    ▲    ▲    ▲    ▲    ▲    ▲    ▲
Model Decisions:  |────── Round 1 ──────|────── Round 2 ─────────|
```

### Key Advantages:
1. **Grid Uniformity**: The sampling grid is strictly uniform ($\text{jitter} = 0.0\text{ s}$), regardless of whether an AI model takes 1 ms or 100 s to compute.
2. **True Latency Accounting**: An overrunning model simply misses decision slots; the substrate continues to tick, traffic continues to evolve, and the network reflects unadvised state during delays.
3. **Valid Spectral Math**: FFT power spectral analysis yields valid, comparable frequency bins across all benchmark runs.
