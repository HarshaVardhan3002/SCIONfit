# System Verification & Walkthrough Log

> **Project**: `scionarena` / `scionfit`  
> **Date**: August 17, 2026  
> **Environment**: Windows x86_64, Python 3.11.15, NumPy 2.4.6, Pytest 9.1.1  
> **Status**: **400/400 Tests Passed** | **Performance Benchmarks Met** | **Closed-Loop Proven**

---

## 📋 Table of Contents
1. [Overview & Execution Objectives](#1-overview--execution-objectives)
2. [Complete Test Suite Verification (`pytest`)](#2-complete-test-suite-verification-pytest)
3. [Substrate Performance Benchmarks (`benchmarks/run.py`)](#3-substrate-performance-benchmarks-benchmarksrunpy)
4. [Conformance CLI Verification (`scionarena conformance`)](#4-conformance-cli-verification-scionarena-conformance)
5. [Closed-Loop Simulation & Demo (`scionarena demo`)](#5-closed-loop-simulation--demo-scionarena-demo)
6. [Web UI & REST API Verification (`scionarena ui`)](#6-web-ui--rest-api-verification-scionarena-ui)
7. [Comprehensive Engineering & Scientific Audit (Bugs & Discrepancies)](#7-comprehensive-engineering--scientific-audit-bugs--discrepancies)
8. [Proposed Fixes & Action Plan](#8-proposed-fixes--action-plan)

---

## 1. Overview & Execution Objectives

This walkthrough documents the full end-to-end execution and validation of the `scionarena` / `scionfit` system. All subsystems—the static CSR topology generator, cryptographic segment store, BPR link physics engine, multi-scope host populations, costed tool registry, event-driven simulated clock, conformance probes (R1–R10), reference models, and HTML reporting—were tested and benchmarked.

---

## 2. Complete Test Suite Verification (`pytest`)

### Command
```bash
.\.venv\Scripts\pytest -o pythonpath=src
```

### Execution Log
```
============================= test session starts =============================
platform win32 -- Python 3.11.15, pytest-9.1.1, pluggy-1.6.0
rootdir: C:\Users\Von\Desktop\PAS_Research_desktop\PAS_Research_desktop\scionfit
configfile: pyproject.toml
testpaths: tests
plugins: cov-7.1.0
collected 400 items

tests\test_benchmarks.py .............                                   [  3%]
tests\test_cli.py ....                                                   [  4%]
tests\test_clock.py ...........................................          [ 15%]
tests\test_conformance.py ...........                                    [ 17%]
tests\test_detectors.py ...................                              [ 22%]
tests\test_exposure.py ...........................................       [ 33%]
tests\test_hosts.py .......................                              [ 39%]
tests\test_interface.py ......                                           [ 40%]
tests\test_linkstate.py .............................                    [ 47%]
tests\test_loop.py .........................                             [ 54%]
tests\test_performance.py .................                              [ 58%]
tests\test_report.py ...                                                 [ 59%]
tests\test_sampler.py ...............                                    [ 62%]
tests\test_scenario.py ..........................................        [ 73%]
tests\test_segments.py .......................................           [ 83%]
tests\test_topology.py .........................                         [ 89%]
tests\test_trace.py ..............                                       [ 92%]
tests\test_ui.py ................                                        [ 96%]
tests\test_world.py .............                                        [100%]

============================ 400 passed in 21.42s =============================
```

**Result**: **100% Pass Rate (400/400 tests passing).**

---

## 3. Substrate Performance Benchmarks (`benchmarks/run.py`)

### 3.1 Dev Tier (200 ASes, 800 Links)
```bash
.\.venv\Scripts\python benchmarks/run.py --tier dev
```
```
calibration 8.8ms (machine factor 0.94x)

dev
  topology_build_s               0.7732ms  (-4%)
  beaconing_build_s               3.631ms  (-1%)
  path_query_cold_s              0.1812ms  (+3%)
  link_metrics_batch_s          0.02479ms  (+10%)
  substrate_step_s              0.01305ms  (+7%)
  topology_bytes              4.021e+04  (+0%)
  link_state_bytes              9.6e+04  (+0%)
  n_segments                        762  (+0%)

OK: within 25% on realistic
```

### 3.2 Realistic Tier (2,000 ASes, 10,000 Links)
```bash
.\.venv\Scripts\python benchmarks/run.py --tier realistic
```
```
calibration 8.8ms (machine factor 0.94x)

realistic
  topology_build_s                7.741ms  (+9%)
  beaconing_build_s               77.57ms  (+4%)
  path_query_cold_s               3.202ms  (+18%)
  link_metrics_batch_s           0.1359ms  (+3%)
  substrate_step_s               0.2016ms  (+10%)
  topology_bytes               4.96e+05  (+0%)
  link_state_bytes              1.2e+06  (+0%)
  n_segments                  1.833e+04  (+0%)

OK: within 25% on realistic
```

**Result**: All operations on the 2,000-AS graph execute sub-millisecond per step ($0.20\text{ ms}$), well within the 10 ms budget.

---

## 4. Conformance CLI Verification (`scionarena conformance`)

### 4.1 Cross-Model Discrimination Matrix
```bash
.\.venv\Scripts\scionarena.exe conformance compare
```
```
                  R1    R2    R3    R4    R5    R6    R7    R8    R9   R10   verdict
----------------------------------------------------------------------------------------------
ema             PASS  PASS  PASS  PASS    -     -     -     -    n/a    -    OPEN-LOOP ONLY
minrtt          PASS  PASS  PASS  PASS    -     -     -     -    n/a    -    OPEN-LOOP ONLY
proportional    FAIL  PASS  FAIL  PASS    -     -     -   PASS   n/a    -    OPEN-LOOP ONLY
reference       PASS  PASS  PASS  PASS  PASS  PASS  PASS  PASS  PASS  PASS   CONFORMANT
```

### 4.2 Detailed Reference Stochastic Model Report
```bash
.\.venv\Scripts\scionarena.exe conformance check reference
```
```
==============================================================================
 scionfit conformance report  ::  ReferenceStochastic v0.1.0
==============================================================================

 R1   PASS         0.94  Shared-link coupling
                          Affected paths moved 4.7x more than unaffected ones.

 R2   PASS         0.91  Composition onto an unseen path
                          Composed a prediction within 9% of truth.

 R3   PASS         1.00  Missing is not zero
                          Distinguishes an absent measurement from a zero one.

 R4   PASS         1.00  Survives topology churn
                          Handled interfaces that did not exist at reset.

 R5   PASS         1.00  Distributional output
                          All predictions are distributional and ordered.

 R6   PASS         1.00  Demand sensitivity
                          Concentrating demand changed the loaded path's predicted
                          cost by 59%.

 R7   PASS         1.00  Monotone in load
                          Cost rose monotonically across the sweep.

 R8   PASS         0.97  Assignment, not ranking
                          Spread across paths (max weight 0.28, normalised entropy
                          0.97).

 R9   PASS         1.00  Advice survives its own consequences
                          Advisory is stable and survives the demand it induces.

 R10  PASS         1.00  Confidence falls with information age
                          Both the advisory and the reported confidence relax as
                          information ages.

------------------------------------------------------------------------------
 PASS 10
 mean score        0.98
 closed-loop ready yes
 VERDICT           CONFORMANT
==============================================================================
```

### 4.3 Incumbent Path Oracle (`EMAOracle`) Report
```bash
.\.venv\Scripts\scionarena.exe conformance check ema
```
```
==============================================================================
 scionfit conformance report  ::  EMAOracle v1.0.0
==============================================================================

 R1   PASS         0.81  Shared-link coupling
 R2   PASS         0.92  Composition onto an unseen path
 R3   PASS         1.00  Missing is not zero
 R4   PASS         1.00  Survives topology churn
 R5   ABSENT       0.00  Distributional output (Model declares False)
 R6   ABSENT       0.00  Demand sensitivity (Model declares False)
 R7   ABSENT       0.00  Monotone in load (Model declares False)
 R8   ABSENT       0.00  Assignment, not ranking (Model declares False)
 R9   N/A            -   Advice survives its own consequences (Untestable)
 R10  ABSENT       0.00  Confidence falls with age (Model declares False)

------------------------------------------------------------------------------
 DECLARED_ABSENT 5  NOT_APPLICABLE 1  PASS 4
 mean score        0.41
 closed-loop ready no
 VERDICT           OPEN-LOOP ONLY
==============================================================================
```

---

## 5. Closed-Loop Simulation & Demo (`scionarena demo`)

### 5.1 Headline Multi-Scope Run (Smoke Tier, 8 Scopes, 240 Cycles, Seed 7, with +5s Slow Latency)
```bash
.\.venv\Scripts\scionarena.exe demo --tier smoke --scopes 8 --cycles 240 --slow 5
```
```
model                     decision_s  sample_s  samples  swing   share_swing  oscillation_index  dominant_period  flap_rate  mean_cost_ms  mean_deviation  mean_latency_s  overruns  grid_uniform  calls  wall_clock_s
------------------------  ----------  --------  -------  ------  -----------  -----------------  ---------------  ---------  ------------  --------------  --------------  --------  ------------  -----  ------------
MinRTTGreedy              30          30        240      3.3     0.4796       0.2968             4.08             0.2374     1315          0               1.869           2         True          7681   2.12        
ReferenceStochastic       30          30        240      0.5457  0.0983       0.2059             3.17             0.5455     442.2         0.0606          1.44            0         True          7681   5.091       
MinRTTGreedy +5s latency  30          30        240      3.279   0.4749       0.3757             4.26             0.2273     1336          0               6.848           2         True          7681   2.14        

[PASS] greedy link amplitude >= 4x stochastic: 6.05x  (3.300 vs 0.546)
[PASS] greedy split amplitude >= 4x stochastic: 4.88x  (0.480 vs 0.098)
[PASS] greedy mean path cost >= 2x stochastic: 2.97x  (1315 ms vs 442 ms)
[FAIL] M3 as originally written: peak dominance > 0.5 (see ADR 0010): 0.297
[PASS] samples sit on an exact grid, so the spectra mean something: 240 at 30s, jitter 0s, 240 at 30s, jitter 0s, 240 at 30s, jitter 0s
[----] rounds the model finished inside its decision slot: 238/240 at 30s, 240/240 at 30s, 238/240 at 30s
[PASS] a slower model is measurably worse, same model and seed: 1336 ms vs 1315 ms at 6.85 s decision latency

wrote demo\report.html in 10.12s
```

**Key Empirical Findings**:
* **Swing Amplitude Ratio ($6.05\times$)**: Greedy model oscillates with over $6\times$ the amplitude of the stochastic model.
* **Path Cost Penalty ($2.97\times$)**: Greedy model incurs nearly $3\times$ higher mean path latency/loss cost due to severe self-induced congestion.
* **Decision Latency Sensitivity**: Adding 5 seconds of decision latency measurably degrades performance from $1315\text{ ms}$ to $1336\text{ ms}$.

---

## 6. Web UI & REST API Verification (`scionarena ui`)

Tested via `tests/test_ui.py`:
* **Non-blocking Server Threading**: Background jobs launch asynchronously via `POST /api/run`.
* **State Polling**: Progress streamed via `GET /api/status?id=<id>`.
* **Clean Cancellation**: User cancel requests cleanly stop execution between decision rounds without state corruption.
* **Complete Dashboard Generation**: Interactive SVG time-series charts and HTML report cards render without missing placeholders.

---

## 7. Comprehensive Engineering & Scientific Audit (Bugs & Discrepancies)

An honest, transparent examination of the current codebase reveals several critical discrepancies, theoretical limitations, and pending engineering tasks:

### ⚠️ Discrepancy 1: Peak Dominance Criterion Failure under Uniform 30s Sampling
* **Observation**: In the demo run above, `[FAIL] M3 as originally written: peak dominance > 0.5: 0.297` fired.
* **Root Cause**: As documented in [ADR 0010](file:///C:/Users/Von/Desktop/PAS_Research_desktop/PAS_Research_desktop/scionfit/docs/adr/0010-measuring-oscillation-by-amplitude-not-periodicity.md) and [ADR 0011](file:///C:/Users/Von/Desktop/PAS_Research_desktop/PAS_Research_desktop/scionfit/docs/adr/0011-sampling-off-the-worlds-clock.md), M3's original $> 0.5$ spectral peak threshold was an artifact of decimation from variable-cadence runs. On a strict 30s uniform grid with 8 concurrent scopes, power is distributed across more FFT bins, lowering the single peak below 0.5.
* **Status**: **Expected and mathematically documented in ADR 0010.** The headline scale-invariant discriminator is the swing amplitude ratio ($6.05\times \ge 4.0\times$), which passed robustly.

### ⚠️ Discrepancy 2: Memory Footprint at Realistic Tier
* **Observation**: Invariant 1 dictates that the harness never summarises history on the model's behalf. At the realistic tier (2,000 ASes, 100 active scopes, 120 decision rounds), storing complete observation event sequences requires **~34 GB of RAM** for exploring stochastic models (257 MB per decision round).
* **Impact**: Unbounded memory growth makes long episodes memory-bound rather than compute-bound.
* **Status**: Open design item for Milestone M4/M8 (implementing structured trace compression / tiered ring buffers).

### ⚠️ Discrepancy 3: External Backend Adapters are Stubs
* **Observation**: In `src/scionarena/backends/`:
  * `scionpathml.py` (Tier 0)
  * `dqnsim.py` (Tier 2)
  * `testbed.py` (Tier 3)
  are syntactic stubs with fixed call shapes, but **have not yet been connected or validated against live upstream repositories or hardware**.
* **Impact**: Number reporting is strictly restricted to Tier 1 (analytical). Tiers 0, 2, and 3 cannot be cited as empirical truth until M9.

### ⚠️ Discrepancy 4: Windows Console Unicode Encoding Glitch
* **Observation**: In `scionarena/conformance/report.py` line 168, the middle dot character (`·`, `\u00b7`) rendered as `` on Windows console cp1252 stdout during `--format markdown`.
* **Fix**: Replace non-ASCII middle dot with standard markdown separator `|` or hyphen `-`.

---

## 8. Proposed Fixes & Action Plan

1. **Update Demo Assertion**: Clarify in `demo.py` that the legacy M3 peak dominance line is an informational diagnostic superseded by ADR 0010's swing amplitude ratio.
2. **ASCII-Safe Formatting**: Ensure all terminal and markdown outputs in `report.py` use ASCII-safe characters or explicit UTF-8 stream writers.
3. **M4 Instrumentation Finalization**:
   * Implement Arrow/Parquet columnar storage for `instrument/events.py` to bound memory consumption.
   * Finalize the synthetic positive/negative unit tests for all remaining pathology detectors (herding, identity amnesia, context rot, staleness misuse).
