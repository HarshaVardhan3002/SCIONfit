"""Pathology detectors. Read the world's output; never read by the model.

First cut: oscillation. The rest of the taxonomy -- herding, context rot,
identity amnesia, staleness misuse -- lands in M4 and shares this module's
shape: a plain function over a series, no state, no dependency beyond numpy,
so that a detector can be run over a saved trace as easily as over a live run.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np
from numpy.typing import NDArray

__all__ = [
    "FAST_BAND_MAX_ROUNDS",
    "FAST_BAND_MIN",
    "WARMUP",
    "band_min",
    "warmup_samples",
    "oscillation_index",
    "fast_swing",
    "spectrum",
    "dominant_period",
    "flap_rate",
]

#: Frequencies below this, in cycles per sample, are not oscillation.
#:
#: **This constant is the measure.** Three different things move a load series:
#: the environment drifting and the model correctly following it (slow, low
#: frequency), hosts sampling independently (flat across the band, so no bin
#: dominates), and a control loop stampeding (one sharp peak). Only the third
#: is the pathology. An earlier version of this measure had no low-frequency
#: cut and scored a model that tracked a diurnal cycle exactly as badly as one
#: that flapped every other step, which made the number useless for the thing
#: it exists to decide. Do not remove the cut to "capture slow oscillation":
#: slow oscillation and environment tracking are not distinguishable in one
#: series, and a detector that cannot tell them apart should not report either.
FAST_BAND_MIN: float = 0.08

#: The same edge in units that survive a change of sample rate: a cycle slower
#: than this many decision rounds is not fast. ``FAST_BAND_MIN`` is this
#: reciprocal, and holds only for a series sampled once per round, which is what
#: M3 did and M4 no longer does -- see :func:`band_min` and ADR 0011.
#:
#: Twelve and a half rounds is where the edge has always been; what is new is
#: saying so in rounds. The number is bounded on both sides rather than chosen:
#: below about four rounds the band would exclude the slow herding a model can
#: sustain over several turns, and above about fifty it would start admitting
#: the diurnal cycle a model is *supposed* to follow. Anywhere in between
#: reports the same ranking, which is the test for a constant that is not tuned.
FAST_BAND_MAX_ROUNDS: float = 12.5

#: Samples discarded before measuring, for a series sampled once per decision
#: round. The loop starts from a uniform split and takes a few rounds to reach
#: whatever it is going to do; scoring the transient would credit every model
#: with the same initial swing. Off-cadence series convert with
#: :func:`warmup_samples`.
WARMUP: int = 40


def band_min(sample_s: float, decision_s: float) -> float:
    """The fast-band edge in cycles per sample, for a series sampled at ``sample_s``.

    Oscillation is a property of a control loop relative to *its own cadence* --
    the pathology is "flaps every other decision", not "flaps every 60 seconds"
    -- so the band is defined in rounds and converted to the series' axis here.
    Sample once per round and this returns ``FAST_BAND_MIN`` exactly, which is
    what keeps the M3 numbers comparable with everything measured after it.

    Sampling faster than the cadence widens the band in cycles-per-sample terms
    and narrows nothing: the fastest thing a model can do is still change its
    advice once per round, so no real signal lives above that, and the extra
    bins hold whatever the world was doing between the model's turns.
    """
    if not (sample_s > 0.0 and decision_s > 0.0):
        raise ValueError(f"both rates must be positive, got {sample_s=} {decision_s=}")
    return sample_s / (FAST_BAND_MAX_ROUNDS * decision_s)


def warmup_samples(sample_s: float, decision_s: float, *, rounds: int = WARMUP) -> int:
    """How many samples cover the opening ``rounds`` of transient."""
    if not (sample_s > 0.0 and decision_s > 0.0):
        raise ValueError(f"both rates must be positive, got {sample_s=} {decision_s=}")
    return int(math.ceil(rounds * decision_s / sample_s))


def spectrum(
    series: Sequence[float] | NDArray[np.float64],
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """Power spectrum of a de-meaned series, and the frequencies of its bins."""
    x = np.asarray(series, dtype=np.float64)
    if x.size == 0:
        return np.zeros(0), np.zeros(0)
    x = x - x.mean()
    power: NDArray[np.float64] = np.abs(np.fft.rfft(x)) ** 2
    freq = np.fft.rfftfreq(x.size).astype(np.float64)
    return freq, power


def oscillation_index(
    series: Sequence[float] | NDArray[np.float64],
    *,
    warmup: int = WARMUP,
    f_min: float = FAST_BAND_MIN,
) -> float:
    """Spectral peak dominance in the fast band. 0 is quiet, 1 is a metronome.

    Ported from the proposal's toy simulation, unchanged in substance: de-mean,
    take the power spectrum, keep the bins above ``f_min``, and report the
    largest as a fraction of that band's total power.

    Reading the number: independent sampling noise spreads power evenly over
    roughly ``n/2 * (1 - f_min)`` bins, so a well-behaved series scores about
    the reciprocal of that -- a few hundredths for a run of any length. A
    single dominant cycle scores near 1. The M3 thresholds, greedy above 0.5
    and stochastic below 0.15, sit either side of a gap that is two orders of
    magnitude wide, so neither is a tuned number.

    A series shorter than the warmup returns 0.0 rather than raising: a scope
    that only existed for ten steps has not been observed long enough to be
    called oscillating, and that is different from being calm.
    """
    x = np.asarray(series, dtype=np.float64)[warmup:]
    if x.size < 4:
        return 0.0
    freq, power = spectrum(x)
    band = power[freq >= f_min]
    total = float(band.sum())
    if total <= 0.0 or np.allclose(x, x[0]):
        return 0.0
    return float(band.max() / total)


def fast_swing(
    series: Sequence[float] | NDArray[np.float64],
    *,
    warmup: int = WARMUP,
    f_min: float = FAST_BAND_MIN,
) -> float:
    """How far the series moves fast, in the series' own units.

    The band-pass sibling of :func:`oscillation_index`: keep the same fast
    frequencies, throw away the phase question entirely, and report the RMS of
    what is left. On a utilisation series a value of 0.30 means the link swings
    by about thirty points of its capacity from one decision round to the next.

    It exists because peak dominance answers *is the movement periodic* and
    that turned out to be the wrong question at scale. See ADR 0010: with eight
    scopes deciding asynchronously, a herding model's flapping is violent but
    aperiodic, so its power is spread over the whole fast band and its peak
    dominance is indistinguishable from a calm model's. Amplitude separates the
    two by roughly an order of magnitude in the same runs where dominance
    separates them by nothing.

    Unlike the dominance index this is not bounded by one and is not
    dimensionless, so only compare it between series measured in the same
    units. That is the price of it being interpretable.

    The series is de-trended, not merely de-meaned, before the transform. A
    straight line is the one shape a finite Fourier basis cannot represent, so
    its energy leaks across every bin including the fast ones, and a link whose
    load rose steadily all run scored a tenth of a real stampede. Removing the
    trend is the same decision as the low-frequency cut, applied to the one
    frequency the cut cannot reach.
    """
    x = np.asarray(series, dtype=np.float64)[warmup:]
    if x.size < 4:
        return 0.0
    t = np.arange(x.size, dtype=np.float64)
    slope, intercept = np.polyfit(t, x, 1)
    spec = np.fft.rfft(x - (slope * t + intercept))
    spec[np.fft.rfftfreq(x.size) < f_min] = 0.0
    return float(np.fft.irfft(spec, n=x.size).std())


def dominant_period(
    series: Sequence[float] | NDArray[np.float64],
    *,
    warmup: int = WARMUP,
    f_min: float = FAST_BAND_MIN,
) -> float:
    """Samples per cycle of the peak the index found. ``inf`` if there is none.

    Reported alongside the index because the two together say what happened:
    an index of 0.9 at a period of 2 is a model chasing its own tail one step
    behind, which is the classic result and looks quite different in a figure
    from an index of 0.9 at a period of 30.
    """
    x = np.asarray(series, dtype=np.float64)[warmup:]
    if x.size < 4:
        return float("inf")
    freq, power = spectrum(x)
    mask = freq >= f_min
    if not mask.any() or power[mask].sum() <= 0.0:
        return float("inf")
    peak = float(freq[mask][int(np.argmax(power[mask]))])
    return float("inf") if peak <= 0.0 else 1.0 / peak


def flap_rate(
    series: Sequence[float] | NDArray[np.float64],
    *,
    warmup: int = WARMUP,
    threshold: float = 0.1,
) -> float:
    """Fraction of steps that reversed direction by more than ``threshold``.

    A blunt companion to the spectral measure, and useful precisely because it
    is blunt: it is computable by eye off the figure, so a reader who does not
    trust an FFT has something to check the index against.
    """
    x = np.asarray(series, dtype=np.float64)[warmup:]
    if x.size < 3:
        return 0.0
    d = np.diff(x)
    reversed_ = (d[:-1] * d[1:]) < 0.0
    large = np.abs(d[1:]) > threshold
    return float(np.mean(reversed_ & large))
