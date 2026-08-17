---
tags:
  - moc
  - index
  - scion
  - architecture
created: 2026-08-17
updated: 2026-08-17
type: index
---

# 🌐 scionarena & scionfit Knowledge Vault

> [!abstract] System Mission
> **Conformance and Closed-Loop Fit Checking for SCION Path-Selection Models**
> 
> *A high-performance research harness exposing realistic-scale SCION multi-path networks to autonomous AI recommendation nodes with raw telemetry streams, costed tools, simulated wall-clock latency, and quantitative measurement of network stability and AI operational pathologies.*

---

## 🎨 Interactive Obsidian Canvases
* 🗺️ **[[Architecture Map.canvas]]** — Interactive visual 2D node map of all system layers, entry points, and component connections.
* 🌲 **[[Decision Flow.canvas]]** — Visual whiteboard of the ADR decision tree, physical dynamics, and closed-loop feedback design.

---

## 🗺️ Map of Content (MOC)

### 🏗️ Architecture & System Design
* [[Current Architecture]] — The as-built architecture across M0–M4.
* [[Proposed Architecture]] — The target vision across M0–M9 (benchmarks, RL gym, agent harness, hardware deploy).
* [[Seams and Boundaries]] — The 4 strict architectural seams (A, B, C, D) isolating components.
* [[Hard Invariants]] — The 7 non-negotiable rules governing the system.

### 🧩 Subsystem & Component Deep-Dives
* **Substrate Engine (`core/`)**:
  * [[Core - Topology]] — Immutable AS/link graph, CSR sparse representation, relationship semantics.
  * [[Core - Segments & Identity]] — Beaconing abstraction, crypto churn vs structural identity (`structural_id` vs `segment_id`).
  * [[Core - Link State & Load Model]] — Monotone BPR link model with queueing tail, interface directions, and diurnal background traffic.
  * [[Core - Host Population & Traffic]] — Multi-scope host populations, independent multinomial sampling ($O(1/\sqrt{N})$), defector dynamics.
  * [[Core - Clock & Event Engine]] — Simulated wall-clock priority queue, non-blocking time, stopwatch latency measurement.
  * [[Core - Scenario Engine]] — Portable scenario descriptor schema and timeline event dispatch.
* **Exposure Boundary (`exposure/`)**:
  * [[Exposure - Contracts & Protocols]] — `PathModel`, `ToolUsingModel`, `SessionLike`, `Capabilities`, and plain Python types.
  * [[Exposure - Tool Registry & Cost Accounting]] — Costed, rate-limited JSON-schema tools and budget enforcement.
  * [[Exposure - Closed Loop]] — Multi-scope closed-loop driver connecting advice to traffic and back.
* **Instrumentation & Diagnostics (`instrument/`)**:
  * [[Instrument - Sampling & Detectors]] — Absolute world-clock sampling tap (ADR 0011), FFT spectral peak dominance, fast swing, flap rate.
  * [[Instrument - Reports & Dashboards]] — HTML trace viewer, terminal cards, JSON/Markdown reporting.
* **Front-ends & Reference Models**:
  * [[Conformance - Probes R1-R10]] — The 10 behavioral probes, declaration asymmetry, and report card verdicts.
  * [[Reference Models]] — Incumbent `EMAOracle`, `MinRTTGreedy`, `CapacityProportional`, and the conformant `ReferenceStochastic`.
  * [[Backends - Multi-Tier Fidelity]] — Tiers 0–3 (ScionPathML, Analytical, DQN-Sim, Testbed).
  * [[CLI & Web UI]] — `scionarena` CLI, `scionfit` alias, and zero-dependency web interface.

### 🧠 Design Decisions & Trade-Offs
* [[Decision Tree Graph]] — Comprehensive visual decision tree mapping all design choices and ADRs.
* [[ADR Summary]] — Complete summary of Architecture Decision Records (0001–0011).
* [[Crypto Churn vs Physical Static]] — Why physical links are static and crypto material churns (ADR 0002).
* [[BPR Congestion Tail vs MM1]] — The load-to-metrics modeling rationale (ADR 0006).
* [[Sampling on World Clock vs Decision Cadence]] — Why grid sampling must decouple from model execution time (ADR 0011).
* [[Oscillation Amplitude vs Spectral Dominance]] — Why swing ratio is the true scale-invariant metric (ADR 0010).

### 🚀 Roadmap, Verification & Evidence
* [[Milestone Roadmap M0-M9]] — Status and progress across all 10 project milestones.
* [[Open Questions & Assumptions]] — The critical unresolved questions (Q1–Q5) and provisional assumptions.
* `walkthrough.md` — Complete empirical execution log and discrepancies audit.

---

> [!tip] Quick CLI Commands
> ```bash
> # Run all 10 conformance probes against reference models
> scionarena conformance compare
> 
> # Closed-loop demonstration (smoke tier)
> scionarena demo --tier smoke --scopes 8 --cycles 240 --slow 5
> 
> # Check scale performance on realistic tier (2,000 ASes)
> python benchmarks/run.py --tier realistic
> 
> # Launch interactive local Web UI
> scionarena ui --port 8765
> ```
