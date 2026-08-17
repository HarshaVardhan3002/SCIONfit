# Proposed Architecture (Target: Milestones M5 – M9)

> **The Vision**: A unified evaluation, training, and deployment platform for SCION AI Path Oracles across four fidelity tiers, supporting offline benchmark suites, reinforcement learning (Gymnasium), LLM agent pathology diagnostics, and live testbed deployment.

---

## 🏛️ Target Architecture Diagram (Full Vision)

```mermaid
flowchart TB
    subgraph ExternalConsumers["External Consumers & Model Authors"]
        GNNModel["GNN / ML Path Predictor"]
        RLPolicy["PPO / SAC Reinforcement Learning Agent"]
        LLMAgent["Autonomous LLM Recommendation Agent (Tool-Using)"]
        ProductionOracle["Production SCION Path Oracle Service"]
    end

    subgraph FourFrontends["Four Unified Front-Ends (Shared Substrate)"]
        ConformanceFE["conformance (M5)<br>• R1–R10 Behavioral Probes<br>• Rewritten R4 (Crypto Churn)<br>• AI-Behaviour Probes (A1-A4)"]
        BenchFE["bench (M6)<br>• Standardized Scenario Suite<br>• Axiomatic Scoring Engine<br>• Portable Leaderboards"]
        GymFE["gym (M7)<br>• Gymnasium Multi-Agent Env<br>• Flat & Dict Tool Action Spaces<br>• Vectorized Parallel Envs"]
        AgentFE["agent (M8)<br>• Autonomous LLM Loop<br>• Memory Retention / Rot Bench<br>• Context Distractor Injection"]
        DeployFE["deploy (M9)<br>• Production Daemon Shim<br>• SCION Daemon REST Interface<br>• Real Traffic Metric Dispatcher"]
    end

    subgraph ExposureLayer["Universal Exposure Layer (Seam B & D)"]
        Contracts["Standard Contracts (PathModel, ToolUsingModel, SessionLike)"]
        ToolAPI["Tool Registry & Cost Engine (SCMP, BWTest, History, Subscriptions)"]
        BudgetControl["Dynamic Budget & Rate-Limit Controller"]
        UnifiedSession["Universal Session (Episode & Stream Lifecycle)"]
    end

    subgraph InstrumentationEngine["Instrumentation & Diagnostic Engine (M4)"]
        EventStream["Append-Only Columnar Trace (Parquet/Arrow Event Log)"]
        MetricRegistry["Pluggable Metric Registry (Task, Operational, Behavioral)"]
        PathologyDetectors["Comprehensive Pathology Detectors:<br>• Oscillation Index & Fast Swing<br>• Herding Metric<br>• Identity Amnesia Detector<br>• Context Rot Curve<br>• Staleness Misuse<br>• Budget Exhaustion Behavior<br>• Topology Partition Overfit"]
        InteractiveTraceViewer["Post-Hoc Web Trace & Telemetry Viewer"]
    end

    subgraph CoreSubstrate["Unified Core Substrate Engine"]
        TopologyEngine["Topology Engine (2,000+ AS Scale, CSR Graph, Core/Edge ISDs)"]
        SegmentEngine["Segment & Beaconing Engine (Structural vs Crypto Identity)"]
        LinkDynamics["Calibrated Link Dynamics (Calibrated BPR, Directional Queueing)"]
        HostSim["Host Population Engine (Multi-Class SLAs, Defector Taxonomies)"]
        EventClock["Simulated Wall-Clock & Latency Event Queue"]
        ScenarioRunner["Universal Scenario Runner (YAML/JSON Multi-Tier Format)"]
    end

    subgraph FidelityLadder["Fidelity Ladder Backends (Seam A)"]
        T0["Tier 0: ScionPathML Dataset Replay (Real SCIONLab Data)"]
        T1["Tier 1: Analytical Calibrated Simulator (Sub-Millisecond Engine)"]
        T2["Tier 2: scion-dqn-sim Backend (BRITE Topologies + Packet Beaconing)"]
        T3["Tier 3: ietf-scion-testbed / linkd (Real 12-AS SCION Stack + Netem Shaping)"]
    end

    %% Wiring
    GNNModel --> ConformanceFE
    RLPolicy --> GymFE
    LLMAgent --> AgentFE
    ProductionOracle --> DeployFE
    GNNModel --> BenchFE

    ConformanceFE --> ExposureLayer
    BenchFE --> ExposureLayer
    GymFE --> ExposureLayer
    AgentFE --> ExposureLayer
    DeployFE --> ExposureLayer

    ExposureLayer --> CoreSubstrate
    ExposureLayer --> InstrumentationEngine
    CoreSubstrate --> InstrumentationEngine

    CoreSubstrate --> FidelityLadder

    InstrumentationEngine --> InteractiveTraceViewer
```

---

## 🎯 The 4 Front-Ends Built on One Substrate

The core thesis of `scionarena` is that **a model trained in `gym`, verified in `conformance`, and scored in `bench` must deploy into `deploy` without changing a single line of code**.

| Front-End | Purpose | Primary Evaluation Output |
| :--- | :--- | :--- |
| **`conformance`** (M5) | Verifies whether a model meets the structural prerequisites for closed-loop stability. | Report Card: `CONFORMANT`, `PARTIAL`, `OPEN-LOOP ONLY`, `MISDECLARED`. |
| **`bench`** (M6) | Reproducible benchmark suite across standardized network scenarios. | Composite benchmark score, QoE compliance, efficiency vs. capacity floor. |
| **`gym`** (M7) | High-performance Reinforcement Learning environment (Gymnasium compatible). | RL training trajectories, reward curves, multi-agent policy convergence. |
| **`agent`** (M8) | Evaluates LLM reasoning agents with costed tool calling. | Context degradation curve, token/cost efficiency, memory retention rate. |
| **`deploy`** (M9) | Production daemon driving real SCION infrastructure. | Real-time advisory broadcast via SCION control plane. |

---

## 🪜 The 4 Fidelity Ladder Tiers

A single unified scenario definition (JSON/YAML) drives all four tiers:

1. **Tier 0 (Replay)**: Replays real SCIONLab network traces from ScionPathML. Instant execution.
2. **Tier 1 (Analytical)**: Pure NumPy BPR queueing simulation. Thousands of episodes in milliseconds.
3. **Tier 2 (DQN Simulator)**: Uses `scion-dqn-sim` with BRITE topologies and simulated control-plane packet flooding.
4. **Tier 3 (Hardware Testbed)**: Connects to a live 12-AS SCION deployment via the `linkd` REST interface to apply real `tc netem/tbf` interface rate and delay shaping.
