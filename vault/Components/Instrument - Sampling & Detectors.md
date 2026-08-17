# Instrument - Sampling & Detectors (`scionarena.instrument`)

> [sampler.py](file:///C:/Users/Von/Desktop/PAS_Research_desktop/PAS_Research_desktop/scionfit/src/scionarena/instrument/sampler.py) & [detectors.py](file:///C:/Users/Von/Desktop/PAS_Research_desktop/PAS_Research_desktop/scionfit/src/scionarena/instrument/detectors.py)

---

## 📌 Role & Responsibilities
Samples ground-truth network metrics on absolute simulated world-clock intervals and applies mathematical pathology detectors without exposing any feedback to the model under test.

## 🛰️ Substrate Tap ([`Sampler`](file:///C:/Users/Von/Desktop/PAS_Research_desktop/PAS_Research_desktop/scionfit/src/scionarena/instrument/sampler.py#L112))
* Direct read-only tap into [`Substrate`](file:///C:/Users/Von/Desktop/PAS_Research_desktop/PAS_Research_desktop/scionfit/src/scionarena/core/scenario.py#L427).
* Emits a [`Series`](file:///C:/Users/Von/Desktop/PAS_Research_desktop/PAS_Research_desktop/scionfit/src/scionarena/instrument/sampler.py#L37) with guaranteed sample interval $\Delta t = T_{\text{sample}}$, recording load, utilisation, loss, and path allocations.

## 🔬 Mathematical Pathology Detectors

1. **Oscillation Index ([`oscillation_index`](file:///C:/Users/Von/Desktop/PAS_Research_desktop/PAS_Research_desktop/scionfit/src/scionarena/instrument/detectors.py#L104))**:
   * Demeans the series, computes the FFT power spectrum $P(f) = |\text{rfft}(x)|^2$, restricts to fast band $f \ge f_{\text{min}}$, and returns peak power share:
     $$\text{Index} = \frac{\max_{f \ge f_{\text{min}}} P(f)}{\sum_{f \ge f_{\text{min}}} P(f)}$$
2. **Fast Swing Amplitude ([`fast_swing`](file:///C:/Users/Von/Desktop/PAS_Research_desktop/PAS_Research_desktop/scionfit/src/scionarena/instrument/detectors.py#L131))**:
   * High-pass filters the series by subtracting a moving average of window width $W = \text{FAST\_BAND\_MAX\_ROUNDS} \cdot \frac{T_{\text{decision}}}{T_{\text{sample}}}$, and returns the 95th-percentile peak-to-peak swing amplitude.
3. **Flap Rate ([`flap_rate`](file:///C:/Users/Von/Desktop/PAS_Research_desktop/PAS_Research_desktop/scionfit/src/scionarena/instrument/detectors.py#L187))**:
   * Fraction of successive steps where the modal (top-weighted) path changes.
