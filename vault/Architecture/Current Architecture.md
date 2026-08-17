# Current Architecture (Milestones M0 – M4)

> Status: **Implemented & Verified** across 400 unit/integration tests and benchmarks.

---

## 🏛️ System Architecture Diagram (As-Built)

```mermaid
flowchart TB
    subgraph UI_CLI["Front-End Interfaces & Entry Points"]
        CLI["CLI (scionarena / scionfit)"]
        WebUI["Zero-Dependency Web UI (ThreadingHTTPServer)"]
        DemoRunner["Demo Orchestrator (run_demo)"]
    end

    subgraph ConformanceLayer["Conformance Layer (M0 / M5 Seed)"]
        ConfRunner["Conformance Runner"]
        Probes["Probes R1–R10 (Behavioral Interventions)"]
        ConfReport["Report Card Generator (Verdicts: CONFORMANT, OPEN-LOOP ONLY, etc.)"]
    end

    subgraph ModelsLayer["Reference Models & Clients (Exposure Consumers)"]
        EMA["EMAOracle (Incumbent Path Oracle)"]
        MinRTT["MinRTTGreedy"]
        CapProp["CapacityProportional (Static Baseline)"]
        RefStoch["ReferenceStochastic (Conformant 3-Block Design)"]
    end

    subgraph ExposureLayer["Exposure Boundary (Seam B & D)"]
        Contracts["Contracts: PathModel, PathRef, Obs, Advisory, Capabilities"]
        Session["Session (Single Episode Controller)"]
        ToolRegistry["Tool Registry (query_paths, probe_path, etc.)"]
        BudgetEngine["Budget & Cost Accounting (Rate Limits, Latency, Units)"]
        LoopDriver["Closed Loop Driver (run_loop, Multi-Scope Orchestration)"]
    end

    subgraph SubstrateLayer["Substrate Engine (Numpy-Backed Physics)"]
        Topology["Topology (CSR Graph, Static Links, AS/ISDs)"]
        SegmentStore["SegmentStore (Up/Core/Down Segments, Crypto Churn)"]
        LinkState["LinkState (BPR + Queue Tail, Interface Directions, Diurnal Load)"]
        Hosts["HostPopulation (Multinomial Sampling, Defector Hook)"]
        Clock["Clock & Stopwatch (Simulated Wall-Clock Event Queue)"]
        ScenarioEngine["Scenario & Substrate (Timeline Events: degrade, surge)"]
    end

    subgraph InstrumentationLayer["Instrumentation & Detectors (M4 Tap)"]
        Sampler["Sampler (Absolute World-Clock Tap - ADR 0011)"]
        Detectors["Pathology Detectors (FFT Spectral Peak, Fast Swing, Flap Rate)"]
        HTMLViewer["Self-Contained HTML Report Generator"]
    end

    subgraph BackendsLayer["Backend Adapters (Seam A)"]
        Analytical["Analytical Backend (Tier 1 - Built-in)"]
        ReplayStub["ScionPathML Replay (Tier 0 Stub)"]
        DqnSimStub["scion-dqn-sim (Tier 2 Stub)"]
        TestbedStub["ietf-scion-testbed (Tier 3 Stub via linkd)"]
    end

    %% Wiring
    CLI --> ConfRunner
    CLI --> DemoRunner
    CLI --> WebUI
    WebUI --> DemoRunner
    DemoRunner --> LoopDriver

    ConfRunner --> Probes
    Probes --> Contracts
    Probes --> Analytical
    ConfRunner --> ConfReport

    ModelsLayer -->|Implements| Contracts
    ModelsLayer -.->|Calls Tools| ToolRegistry

    LoopDriver --> Session
    Session --> ToolRegistry
    Session --> BudgetEngine
    Session --> ScenarioEngine
    LoopDriver --> ModelsLayer

    ScenarioEngine --> Clock
    ScenarioEngine --> Topology
    ScenarioEngine --> SegmentStore
    ScenarioEngine --> LinkState
    ScenarioEngine --> Hosts

    Sampler -->|Taps State| ScenarioEngine
    LoopDriver --> Sampler
    Sampler --> Detectors
    Detectors --> HTMLViewer
    LoopDriver --> HTMLViewer

    Analytical --> ScenarioEngine
```

---

## 🔍 Detailed Component Interactions

### 1. The Closed Loop Execution Sequence
1. **Clock Advance**: The [`Clock`](file:///C:/Users/Von/Desktop/PAS_Research_desktop/PAS_Research_desktop/scionfit/src/scionarena/core/clock.py) steps the substrate by $\Delta t$, applying background diurnal loads and timeline perturbations.
2. **Observation Generation**: Telemetry from host transmissions and scheduled probes is placed into the model's observation queue.
3. **Model Invocation & Stopwatch**: The model's `advise()` or `act()` method is executed while a [`Stopwatch`](file:///C:/Users/Von/Desktop/PAS_Research_desktop/PAS_Research_desktop/scionfit/src/scionarena/core/clock.py#L321) measures elapsed decision time $\tau_{\text{decision}}$.
4. **Delayed Advisory Application**: An `advisory_apply` event is scheduled at $t_{\text{now}} + \tau_{\text{decision}}$.
5. **Host Resampling**: When the event fires, the [`HostPopulation`](file:///C:/Users/Von/Desktop/PAS_Research_desktop/PAS_Research_desktop/scionfit/src/scionarena/core/hosts.py) draws paths via `numpy.random.multinomial`.
6. **Realised Load on Links**: Link offered load is recalculated by accumulating host traffic across path interfaces.
7. **BPR Metric Calculation**: [`LinkState`](file:///C:/Users/Von/Desktop/PAS_Research_desktop/PAS_Research_desktop/scionfit/src/scionarena/core/linkstate.py) updates latencies, packet losses, and queueing delays.
8. **World-Clock Sampling**: The [`Sampler`](file:///C:/Users/Von/Desktop/PAS_Research_desktop/PAS_Research_desktop/scionfit/src/scionarena/instrument/sampler.py) records metrics at fixed simulated world time $t = k \cdot T_{\text{sample}}$, completely independent of model overruns.

---

## 📊 Current Structural Properties

* **Zero Python Objects per Path in Hot Loop**: State is stored in 1D contiguous NumPy arrays.
* **Deterministic Trace Hashing**: State digests generated via Blake2b hashing of arrays and event logs.
* **Separation of Contracts**: Models import only plain Python contracts from `scionarena.exposure.contracts` without any direct substrate dependencies.
