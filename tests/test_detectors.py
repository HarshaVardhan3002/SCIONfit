"""M3: the detectors, tested against series whose answer is known by hand.

A detector is only worth what its false positives cost, so most of these are
negative: a constant, a ramp, a diurnal cycle and white noise must all score
low, or every model in M6 will be reported as oscillating.

The last group is the awkward one. It pins the finding in ADR 0010 -- peak
dominance cannot see aperiodic switching -- as a test rather than as prose, so
that if someone later "fixes" the index the failure tells them what they broke.
"""

from __future__ import annotations

import numpy as np
import pytest

from scionarena.instrument import (
    FAST_BAND_MIN,
    WARMUP,
    dominant_period,
    fast_swing,
    flap_rate,
    oscillation_index,
    spectrum,
)

N = 240
RNG = np.random.default_rng(11)


def alternating(amplitude: float = 0.4, n: int = N, base: float = 0.5) -> list[float]:
    """Period 2: the classic control-loop stampede, one step behind itself."""
    return [base + (amplitude if i % 2 else -amplitude) for i in range(n)]


def ramp(n: int = N) -> list[float]:
    return list(np.linspace(0.1, 0.9, n))


def diurnal(n: int = N, period: int = 120) -> list[float]:
    return list(0.5 + 0.3 * np.sin(2 * np.pi * np.arange(n) / period))


def noise(sigma: float = 0.02, n: int = N) -> list[float]:
    return list(0.5 + sigma * RNG.standard_normal(n))


# --------------------------------------------------------------------------
# amplitude


def test_fast_swing_recovers_the_amplitude_of_a_square_wave() -> None:
    """A square wave of +-0.4 has an RMS of 0.4. Nothing subtler is claimed."""
    assert fast_swing(alternating(0.4)) == pytest.approx(0.4, abs=1e-3)


def test_fast_swing_ignores_a_constant() -> None:
    assert fast_swing([0.7] * N) < 1e-12


def test_fast_swing_ignores_a_ramp() -> None:
    """Environment drift is not oscillation, however far the series travels."""
    assert fast_swing(ramp()) < 0.01


def test_fast_swing_ignores_a_diurnal_cycle() -> None:
    """A model correctly following a slow cycle must not be scored for it."""
    assert fast_swing(diurnal()) < 0.02


def test_fast_swing_is_small_for_sampling_noise() -> None:
    assert fast_swing(noise(0.02)) < 0.03


def test_fast_swing_separates_a_stampede_from_noise_by_an_order_of_magnitude() -> None:
    assert fast_swing(alternating(0.4)) > 10 * fast_swing(noise(0.02))


def test_a_short_series_is_not_called_oscillating() -> None:
    """Not observed long enough is different from calm, and returns the same 0.0.

    The distinction is the caller's to make from the series length; what the
    detector must not do is score a five-sample transient as a metronome.
    """
    assert fast_swing([0.0, 1.0, 0.0, 1.0]) == 0.0  # shorter than the warmup
    assert oscillation_index([0.0, 1.0, 0.0, 1.0]) == 0.0


# --------------------------------------------------------------------------
# periodicity


def test_peak_dominance_is_near_one_for_a_metronome() -> None:
    assert oscillation_index(alternating(0.4)) > 0.95


def test_peak_dominance_is_low_for_white_noise() -> None:
    """The floor: power spread over the band, so no bin owns much of it."""
    assert oscillation_index(noise()) < 0.15


def test_peak_dominance_ignores_the_low_band() -> None:
    """``FAST_BAND_MIN`` is the measure. A slow cycle scores as calm."""
    assert oscillation_index(diurnal(period=200)) < 0.2


def test_the_dominant_period_of_an_alternating_series_is_two() -> None:
    assert dominant_period(alternating()) == pytest.approx(2.0, abs=0.05)


def test_the_dominant_period_of_a_flat_series_is_infinite() -> None:
    assert dominant_period([0.3] * N) == float("inf")


def test_flap_rate_counts_reversals_not_movement() -> None:
    assert flap_rate(alternating(0.4)) > 0.9
    assert flap_rate(ramp()) == 0.0


def test_flap_rate_ignores_reversals_smaller_than_the_threshold() -> None:
    """Otherwise it reports the sampling noise of every calm run as flapping."""
    assert flap_rate(noise(0.001)) == 0.0


def test_the_spectrum_of_an_empty_series_is_empty() -> None:
    freq, power = spectrum([])
    assert freq.size == 0 and power.size == 0


# --------------------------------------------------------------------------
# ADR 0010: what peak dominance cannot see


def aperiodic_switching(n: int = N, hold: int = 3) -> list[float]:
    """Violent switching between two states at irregular intervals.

    What a herding model actually does once several scopes contend: it moves
    the whole population every time, but not on any fixed beat, because the
    thing it is chasing is being moved by everyone else.
    """
    rng = np.random.default_rng(3)
    out: list[float] = []
    state = 0.9
    while len(out) < n:
        out += [state] * int(rng.integers(1, hold + 1))
        state = 0.9 if state < 0.5 else 0.1
    return out[:n]


def test_peak_dominance_cannot_distinguish_aperiodic_switching_from_noise() -> None:
    """The negative finding in ADR 0010, as a test.

    If this ever fails because the index went up, the detector changed and the
    milestone's headline criterion should be revisited -- not because a failure
    here is bad, but because the reasoning recorded in the ADR was conditional
    on it.
    """
    violent = oscillation_index(aperiodic_switching())
    calm = oscillation_index(noise(0.02))
    assert violent < 0.5, f"peak dominance saw a metronome that is not there: {violent:.3f}"
    assert violent < 3 * calm, (
        f"peak dominance separated switching ({violent:.3f}) from noise ({calm:.3f}) "
        "by more than it did when ADR 0010 was written"
    )


def test_amplitude_does_distinguish_them() -> None:
    """The other half of the same finding, and the reason for the new detector."""
    assert fast_swing(aperiodic_switching()) > 10 * fast_swing(noise(0.02))


def test_the_warmup_is_discarded() -> None:
    """The opening transient is every model's, and scoring it flatters no one."""
    transient = [0.0, 1.0] * (WARMUP // 2) + [0.5] * N
    assert fast_swing(transient) < 0.01


def test_the_fast_band_edge_is_where_it_says_it_is() -> None:
    """A cycle just inside the band is seen; one just outside is not."""
    fast = 1.0 / (FAST_BAND_MIN * 2)  # comfortably inside
    slow = 1.0 / (FAST_BAND_MIN / 4)  # comfortably outside
    inside = [0.5 + 0.3 * np.sin(2 * np.pi * i / fast) for i in range(N)]
    outside = [0.5 + 0.3 * np.sin(2 * np.pi * i / slow) for i in range(N)]
    assert fast_swing(inside) > 0.15
    assert fast_swing(outside) < 0.05
