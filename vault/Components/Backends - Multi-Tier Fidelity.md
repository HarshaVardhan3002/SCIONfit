# Backends - Multi-Tier Fidelity Ladder (`scionarena.backends`)

> [analytical.py](file:///C:/Users/Von/Desktop/PAS_Research_desktop/PAS_Research_desktop/scionfit/src/scionarena/backends/analytical.py), [scionpathml.py](file:///C:/Users/Von/Desktop/PAS_Research_desktop/PAS_Research_desktop/scionfit/src/scionarena/backends/scionpathml.py), [dqnsim.py](file:///C:/Users/Von/Desktop/PAS_Research_desktop/PAS_Research_desktop/scionfit/src/scionarena/backends/dqnsim.py), [testbed.py](file:///C:/Users/Von/Desktop/PAS_Research_desktop/PAS_Research_desktop/scionfit/src/scionarena/backends/testbed.py)

---

## 📌 The Fidelity Ladder Concept
To eliminate the sim-to-real gap, the harness is designed so that **one scenario definition drives four tiers of fidelity**:

```
[Tier 0: Real Data Replay] ──► [Tier 1: Analytical BPR Engine]
                                            │
[Tier 3: Live SCION Hardware] ◄── [Tier 2: Control-Plane Simulator]
```

---

## 🪜 Backend Status Audit

| Tier | Module | Upstream Source | Status |
| :--- | :--- | :--- | :--- |
| **Tier 0** | `scionpathml.py` | [ScionPathML](https://arxiv.org/abs/2509.07154) (SCIONLab dataset) | ⚠️ **Stubbed**: Call signatures fixed, awaiting raw dataset export. |
| **Tier 1** | `analytical.py` | Built-in NumPy BPR Link Model | ✅ **Fully Functional & Validated**: Used across all conformance and demo runs. |
| **Tier 2** | `dqnsim.py` | [scion-dqn-sim](https://github.com/netsys-lab/scion-dqn-sim) | ⚠️ **Stubbed**: Type mappings in place; upstream repo needs pinning/vendoring. |
| **Tier 3** | `testbed.py` | [ietf-scion-testbed](https://github.com/netsys-lab/ietf-scion-testbed) (`linkd` REST) | ⚠️ **Stubbed**: REST API call mappings drafted; requires Proxmox testbed instance. |

---

## 🔒 Portability Invariant
A scenario file formatted per [ADR 0007](file:///C:/Users/Von/Desktop/PAS_Research_desktop/PAS_Research_desktop/scionfit/docs/adr/0007-scenario-schema.md) runs in Tier 1 for rapid algorithmic sweeps, and in Tier 3 on live Linux routers using identical disruption parameters.
