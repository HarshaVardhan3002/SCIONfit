# Crypto Churn vs. Physical Link Churn

> **Context**: One of the most important domain corrections between `scionfit` v0.1 and `scionarena` v0.2 / M1 (documented in [ADR 0002](file:///C:/Users/Von/Desktop/PAS_Research_desktop/PAS_Research_desktop/scionfit/docs/adr/0002-static-links-crypto-churn.md)).

---

## 🚫 The Old v0.1 Assumption
The early prototype assumed that inter-AS physical links appear and disappear on the timescale of an hour, and tested whether models could handle new interface IDs appearing.

## ✅ The Corrected SCION Reality
In real-world SCION deployments:
1. **Physical Inter-AS Topology is Static**: Fiber links, BGP/SCION peering sessions, and AS core hierarchies are long-lived and stable. Real topology alterations are rare and planned (modeled as explicit scheduled scenario events).
2. **Cryptographic Segment Material Churns Continuously**: Path segments (Up, Core, Down) are periodically re-beaconed and re-signed by AS control-plane servers. A refreshed segment describes the **exact same physical interfaces** but with new cryptographic signatures, generation counters, and expiration timestamps.

---

## 💥 The "Identity Amnesia" Failure Mode
If a model maintains per-path state (e.g. historical latency moving averages, Kalman filter parameters, confidence intervals) keyed on an identifier tied to the cryptographic material (`segment_id`), **the model will silently discard its entire historical memory on every beacon refresh cycle**.

Its predictions will reset to uninformative defaults without throwing errors or raising alarms.

```
Time t0: Path [AS1:1 -> AS2:2] (Signature Alpha) ──► Model stores state keyed on "Alpha"
Time t1: Beacon Re-signed (Signature Beta)        ──► Model looks up "Beta" -> Cache Miss!
         Result: State is wiped clean, model is amnesiac.
```

---

## 🛠️ The Dual Identifier Solution
To evaluate and prevent this failure, the substrate exposes two identifiers:
* `structural_id`: Blake2b digest of the ordered sequence of interface IDs. **Survives re-signing.**
* `segment_id`: Blake2b digest of `structural_id + generation + signature_id`. **Changes on refresh.**

The scenario config selects which ID `path_id` aliases via `identity_policy`:
```yaml
identity_policy: structural | crypto_bound
```

* **Probe R4**: Tests the model under both policies. A model keyed on `segment_id` fails under `crypto_bound`, exposing identity amnesia.
