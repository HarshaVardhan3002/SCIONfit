# Open Questions & Domain Assumptions

> Reference: [docs/OPEN_QUESTIONS.md](file:///C:/Users/Von/Desktop/PAS_Research_desktop/PAS_Research_desktop/scionfit/docs/OPEN_QUESTIONS.md)

---

## ❓ Open Questions Audit

### 1. Q1: Is the SCION path fingerprint stable across segment re-signing?
* **Problem**: Determines whether identity amnesia is a real-world risk in SCION or a theoretical edge case.
* **Current Assumption**: Both `structural` and `crypto_bound` policies are supported; neither is hardcoded as default.

### 2. Q2: Is offered load per link observable or estimable in real SCION?
* **Problem**: Requirement R6 (Demand Conditioning) assumes models can observe or estimate link demand.
* **Current Assumption**: BPR load model is parameterized in `LinkParams` and will be calibrated against Tier 0 traces in M9.

### 3. Q3: What is the realistic scale for a deployed SCION ISD?
* **Problem**: Sets memory and computational performance budgets for the substrate hot loop.
* **Current Assumption**: `realistic` tier target is 2,000 ASes, 10,000 links, and 100–300 paths per scope.

### 4. Q4: Do we have access to Tier 3 hardware?
* **Problem**: Testing on live Linux routers via `linkd` requires a Proxmox cluster running `ietf-scion-testbed`.
* **Current Assumption**: Tier 3 adapter is drafted as a REST stub; validation will occur once hardware is provisioned.
