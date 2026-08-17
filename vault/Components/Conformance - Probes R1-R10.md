# Conformance - Probes R1–R10 (`scionarena.conformance`)

> [conformance.py](file:///C:/Users/Von/Desktop/PAS_Research_desktop/PAS_Research_desktop/scionfit/src/scionarena/conformance/probes/conformance.py) & [report.py](file:///C:/Users/Von/Desktop/PAS_Research_desktop/PAS_Research_desktop/scionfit/src/scionarena/conformance/report.py)

---

## 📌 Role & Responsibilities
Executes 10 controlled behavioral probes against candidate models to verify structural readiness for closed-loop deployment.

## 📋 The Ten Probes Summary

| Probe | Requirement Tested | Behavioral Intervention & Verification Mechanism |
| :--- | :--- | :--- |
| **R1** | Operates on a graph | Degrades one shared link; verifies predictions move only for paths using it and not for disjoint paths. |
| **R2** | Composition on unseen path | Constructs a valid path from known observed interfaces; verifies predictions match composed link ground truth. |
| **R3** | Missing is not zero | Delivers an unmeasured metric (`None`) vs. an explicit measured `0.0`; verifies outputs differ. |
| **R4** | Identity & Churn | Re-signs path segments with fresh crypto signatures; checks that history is not wiped out. |
| **R5** | Distributional output | Checks for $\ge 3$ ordered quantiles per metric rather than point estimates. |
| **R6** | Demand conditioning | Compares predictions under uniform vs. 95%-concentrated traffic; identical predictions indicate demand-blindness. |
| **R7** | Monotone in load | Sweeps offered traffic upward; checks that predicted path cost never decreases. |
| **R8** | Assignment, not ranking | Verifies `advise()` returns a distribution with entropy rather than a brittle one-hot argmax. |
| **R9** | Self-consistency | Re-asks the model under the load induced by its own advisory; checks that the answer does not diverge. |
| **R10** | Staleness awareness | Withholds telemetry for 600 s; checks that confidence decays and advisory flattens towards uniform. |

---

## ⚖️ The Declaration Asymmetry Rule
* Declaring `capability = False` honestly $\rightarrow$ **`DECLARED_ABSENT`** (not penalized; routes model to `OPEN-LOOP ONLY`).
* Declaring `capability = True` and failing probe $\rightarrow$ **`FALSE_CLAIM`** (harsh failure; flags model as `MISDECLARED`).
