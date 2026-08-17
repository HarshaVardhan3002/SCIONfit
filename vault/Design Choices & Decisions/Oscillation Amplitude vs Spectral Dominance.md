# Oscillation Amplitude vs. Spectral Dominance

> **Context**: How to mathematically quantify network stampeding and herding ([ADR 0010](file:///C:/Users/Von/Desktop/PAS_Research_desktop/PAS_Research_desktop/scionfit/docs/adr/0010-measuring-oscillation-by-amplitude-not-periodicity.md)).

---

## 🔬 The Initial Metric: Spectral Peak Dominance
The initial proposal quantified oscillation via **Spectral Peak Dominance** in the fast frequency band ($f \ge 0.08\text{ cycles/sample}$):
$$\text{Index} = \frac{\max_{f \ge f_{\text{min}}} P(f)}{\sum_{f \ge f_{\text{min}}} P(f)}$$

The original target criterion was:
$$\text{Greedy Index} > 0.5, \quad \text{Stochastic Index} < 0.15$$

---

## ⚠️ Empirical Discovery: Why Dominance Fails Across Scales
Extensive testing across network scales (4, 8, 16, 24, 100 scopes) revealed:
1. **Sample Rate Dependency**: Dominance is proportional to the number of frequency bins $B$. For flat noise, dominance is $\sim 1/B$. Decimating a grid from 30 s to 120 s artificially inflates the metric twofold.
2. **Multi-Scope Flattening**: When 100 scopes share bottlenecks, multiple independent oscillatory frequencies mix, spreading power across multiple fast-band bins. The single highest peak falls below $0.5$ even though violent physical oscillations occur continuously.

---

## 🏆 The Scale-Invariant Headline: Fast-Band Swing Amplitude Ratio
[ADR 0010](file:///C:/Users/Von/Desktop/PAS_Research_desktop/PAS_Research_desktop/scionfit/docs/adr/0010-measuring-oscillation-by-amplitude-not-periodicity.md) established that the true, robust, scale-invariant discriminator is the **fast-band peak-to-peak swing amplitude ratio** between the candidate model and the reference stochastic model under identical scenarios:

$$\text{Amplitude Ratio} = \frac{\text{Swing}_{\text{greedy}}}{\text{Swing}_{\text{stochastic}}} \ge 4.0\times$$

$$\text{Mean Cost Ratio} = \frac{\text{Cost}_{\text{greedy}}}{\text{Cost}_{\text{stochastic}}} \ge 2.0\times$$

### Empirical Proof:
* At 4 scopes: Greedy swing 3.33 vs Stochastic 0.32 (**$10.5\times$ ratio**).
* At 8 scopes: Greedy swing 3.25 vs Stochastic 0.54 (**$6.0\times$ ratio**).
* At 24 scopes: Greedy swing 4.46 vs Stochastic 0.24 (**$18.7\times$ ratio**).

The swing amplitude ratio cleanly separates stable stochastic routing from unstable greedy routing across every scale.
