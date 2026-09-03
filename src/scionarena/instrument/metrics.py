"""The metric registry. Five families, one bundle, no runner that knows the names.

A metric is a registered function of one :class:`MetricInput`. It returns a float
**or a mapping**, and a mapping is flattened into ``name.key`` -- which is how
"stratified, never as a single number" is enforced rather than requested: an
accuracy metric returns one value per horizon and has no way to return a mean
over them.

Nothing here can reach the substrate, the session or the model. ``instrument``
may not import ``exposure``, so :class:`MetricInput` is plain data assembled by
whoever ran the loop, and that is the property that makes these safe to run on a
result read back from disk months later.

There is no loss curve in here, and there is not meant to be: the harness
evaluates trained models, it does not train them.

See ADR 0017 for why ``predict`` had to start being called at all, and why regret
is measured against a bound rather than against the fixed point.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .detectors import band_min, fast_swing, flap_rate, oscillation_index, warmup_samples
from .sampler import Series

__all__ = [
    "FAMILIES",
    "Forecast",
    "Metric",
    "MetricInput",
    "REGISTRY",
    "compute",
    "metric",
]

#: The four families of Master Spec §28, in report order. A metric outside them
#: is refused at registration rather than silently filed under "other".
#:
#: ``recovery`` is not one of the spec's four and is deliberately last: it
#: scores what happened after a scheduled fault, and it is a family of its
#: own precisely so that nothing aggregates it together with the
#: steady-state numbers (ADR 0022). A model can be excellent in ordinary
#: flight and useless in the bad case, and an average over the two is the
#: number that would hide it.
FAMILIES = ("accuracy", "decision", "stability", "operational", "recovery")

#: Nominal coverage of the interval a distributional model is asked for, and the
#: step size of the adaptive-conformal update (§19.5). ``eta`` is the spec's own
#: order of magnitude; it trades how fast alpha chases a coverage failure against
#: how much it rattles on a run that is already calibrated.
NOMINAL = 0.8
ETA = 0.01


@dataclass(frozen=True, slots=True)
class Forecast:
    """One prediction, recorded when it was made rather than when it was scored.

    ``quantiles`` is empty for a point estimator, and the accuracy metrics that
    need a distribution return ``None`` for it rather than scoring the point as
    though it were one.
    """

    t: float
    src: str
    dst: str
    path_id: str
    horizon_s: float
    point: float
    quantiles: Mapping[float, float] = field(default_factory=dict)


@dataclass
class MetricInput:
    """Everything a metric may look at, and nothing else."""

    series: Series
    #: Per decision round: mean decision latency of the advisories it published.
    latency_s: Sequence[float] = ()
    #: What the model forecast, and when. Empty unless the run recorded them.
    forecasts: Sequence[Forecast] = ()
    cadence_s: float = 0.0
    sample_s: float = 0.0
    overruns: int = 0
    wall_clock_s: float = 0.0
    session: Mapping[str, Any] = field(default_factory=dict)
    hosts: Mapping[str, Any] = field(default_factory=dict)
    #: ``(simulated seconds, kind)`` for every event the scenario scheduled, as
    #: the substrate actually installed them rather than as they were written.
    #: Empty for a run with no bad day, which is why every recovery metric
    #: returns ``None`` there rather than a score (ADR 0022).
    events: Sequence[tuple[float, str]] = ()

    @property
    def band(self) -> dict[str, Any]:
        sample_s = self.sample_s or self.cadence_s or 1.0
        cadence_s = self.cadence_s or sample_s
        return {
            "warmup": warmup_samples(sample_s, cadence_s),
            "f_min": band_min(sample_s, cadence_s),
        }

    def truth_at(self, src: str, dst: str, path_id: str, t: float) -> float | None:
        """That path's true cost at the sample nearest ``t``, or ``None``.

        Nearest rather than interpolated: the grid is the world's own and a value
        between two samples is a value nothing measured.
        """
        track = self.series.path_cost.get((src, dst, path_id))
        times = self.series.times
        if not track or not times:
            return None
        index = int(np.argmin(np.abs(np.asarray(times) - t)))
        if abs(times[index] - t) > self.series.interval_s:
            return None
        value = track[index]
        return None if math.isnan(value) else float(value)

    def scored(self) -> list[tuple[Forecast, float]]:
        """Forecasts paired with what actually happened at their horizon.

        A forecast whose horizon falls past the end of the run has nothing to be
        scored against and is dropped rather than scored against the last sample,
        which would make every long horizon look like a short one.
        """
        out: list[tuple[Forecast, float]] = []
        for f in self.forecasts:
            truth = self.truth_at(f.src, f.dst, f.path_id, f.t + f.horizon_s)
            if truth is not None:
                out.append((f, truth))
        return out


@dataclass(frozen=True, slots=True)
class Metric:
    name: str
    family: str
    doc: str
    fn: Callable[[MetricInput], float | Mapping[str, float] | None]
    #: Which direction is better, or ``None`` for a signed quantity where
    #: neither is -- a coverage gap of +0.4 is as wrong as one of -0.4, and a
    #: report that sorted it as "lower is better" would rank the most
    #: over-confident model top. Carried because guessing from the name is how
    #: "coverage" gets sorted backwards.
    higher_is_better: bool | None = False
    #: A *count*, not a score: how many observations the metrics of this family
    #: were computed over. Registered through the same decorator so it lands on
    #: every cell without the runner learning its name, and flagged so a report
    #: renders it as a denominator rather than ranking models by it (ADR 0018).
    support: bool = False


REGISTRY: dict[str, Metric] = {}


def metric(
    name: str, family: str, *, higher_is_better: bool | None = False, support: bool = False
) -> Callable[[Callable[[MetricInput], Any]], Callable[[MetricInput], Any]]:
    """Register a metric. The runner never enumerates names.

    M4's acceptance criterion, and what lets the registry grow through Phases 5
    and 6 without the churn that welded the last four metrics into ``LoopResult``.
    """

    def wrap(fn: Callable[[MetricInput], Any]) -> Callable[[MetricInput], Any]:
        if family not in FAMILIES:
            raise ValueError(f"unknown family {family!r}; the families are {list(FAMILIES)}")
        if name in REGISTRY:
            raise ValueError(f"metric {name!r} is already registered by {REGISTRY[name].fn}")
        REGISTRY[name] = Metric(
            name=name,
            family=family,
            doc=(fn.__doc__ or "").strip().splitlines()[0] if fn.__doc__ else "",
            fn=fn,
            higher_is_better=higher_is_better,
            support=support,
        )
        return fn

    return wrap


def compute(
    data: MetricInput, *, families: Sequence[str] = (), names: Sequence[str] = ()
) -> dict[str, float | None]:
    """Every registered metric, flattened.

    A metric whose inputs are absent returns ``None`` and that ``None`` is kept.
    Zero would be a score, and a report cannot tell a model that scored zero from
    a run that never measured it.
    """
    out: dict[str, float | None] = {}
    for name, entry in sorted(REGISTRY.items()):
        if families and entry.family not in families:
            continue
        if names and name not in names:
            continue
        try:
            value = entry.fn(data)
        except Exception:  # noqa: BLE001 -- one broken metric must not lose the rest
            out[name] = None
            continue
        if value is None:
            out[name] = None
        elif isinstance(value, Mapping):
            for key, item in value.items():
                out[f"{name}.{key}"] = None if item is None else float(item)
        else:
            out[name] = float(value)
    return out


def _finite(values: Sequence[float]) -> np.ndarray:
    arr = np.asarray(list(values), dtype=np.float64)
    return arr[np.isfinite(arr)]


def _cost_pair(data: MetricInput) -> tuple[np.ndarray, np.ndarray]:
    """Realised and best-fixed cost, over the samples where *both* are finite.

    Jointly, not each on its own: filtering them separately leaves two arrays of
    different lengths that happen to subtract, and the difference is then between
    samples taken at different instants.
    """
    realised = np.asarray(list(data.series.mean_cost_ms), dtype=np.float64)
    best = np.asarray(list(data.series.best_cost_ms), dtype=np.float64)
    if realised.size == 0 or realised.size != best.size:
        return np.zeros(0), np.zeros(0)
    keep = np.isfinite(realised) & np.isfinite(best)
    return realised[keep], best[keep]


def _by_horizon(data: MetricInput) -> dict[str, list[tuple[Forecast, float]]]:
    """Scored forecasts, filed under the horizon they were made at."""
    out: dict[str, list[tuple[Forecast, float]]] = {}
    for pair in data.scored():
        out.setdefault(f"h{int(pair[0].horizon_s)}", []).append(pair)
    return out


# ==========================================================================
# accuracy -- one value per horizon, never a mean over them
# ==========================================================================


@metric("pinball", "accuracy")
def pinball(data: MetricInput) -> Mapping[str, float] | None:
    """Mean pinball loss over the predicted quantiles, per horizon."""
    strata = _by_horizon(data)
    if not strata:
        return None
    out: dict[str, float] = {}
    for horizon, pairs in strata.items():
        losses = [
            (tau * (truth - q) if truth >= q else (1.0 - tau) * (q - truth))
            for f, truth in pairs
            for tau, q in f.quantiles.items()
        ]
        if losses:
            out[horizon] = float(np.mean(losses))
    return out or None


@metric("crps", "accuracy")
def crps(data: MetricInput) -> Mapping[str, float] | None:
    """Continuous ranked probability score, approximated from the quantiles held.

    The quantile approximation rather than the integral: a model hands over a
    handful of quantiles and nothing else, so the integral would be over an
    interpolation this module invented. Twice the mean pinball loss over evenly
    spaced quantiles is the standard estimator and it is what the model actually
    told us.
    """
    strata = _by_horizon(data)
    if not strata:
        return None
    out: dict[str, float] = {}
    for horizon, pairs in strata.items():
        scores = []
        for f, truth in pairs:
            if len(f.quantiles) < 2:
                continue
            losses = [
                (tau * (truth - q) if truth >= q else (1.0 - tau) * (q - truth))
                for tau, q in f.quantiles.items()
            ]
            scores.append(2.0 * float(np.mean(losses)))
        if scores:
            out[horizon] = float(np.mean(scores))
    return out or None


@metric("coverage", "accuracy", higher_is_better=True)
def coverage(data: MetricInput) -> Mapping[str, float] | None:
    """Fraction of outcomes inside the model's own interval, per horizon.

    Against ``NOMINAL``, not against 1.0: an interval that always contains the
    truth is not well calibrated, it is uninformative, which is what
    ``interval_width`` is beside this for.
    """
    strata = _by_horizon(data)
    if not strata:
        return None
    out: dict[str, float] = {}
    for horizon, pairs in strata.items():
        hits: list[float] = []
        for f, truth in pairs:
            lo, hi = _lo(f), _hi(f)
            if lo is not None and hi is not None:
                hits.append(float(lo <= truth <= hi))
        if hits:
            out[horizon] = float(np.mean(hits))
    return out or None


@metric("coverage_gap", "accuracy", higher_is_better=None)
def coverage_gap(data: MetricInput) -> Mapping[str, float] | None:
    """Signed distance from nominal coverage. Negative is over-confident."""
    measured = coverage(data)
    if not measured:
        return None
    return {k: float(v) - NOMINAL for k, v in measured.items()}


@metric("interval_width", "accuracy")
def interval_width(data: MetricInput) -> Mapping[str, float] | None:
    """Mean width of the predicted interval, in ms. Read beside ``coverage``."""
    strata = _by_horizon(data)
    if not strata:
        return None
    out: dict[str, float] = {}
    for horizon, pairs in strata.items():
        widths: list[float] = []
        for f, _ in pairs:
            lo, hi = _lo(f), _hi(f)
            if lo is not None and hi is not None:
                widths.append(hi - lo)
        if widths:
            out[horizon] = float(np.mean(widths))
    return out or None


@metric("conformal_drift", "accuracy")
def conformal_drift(data: MetricInput) -> Mapping[str, float] | None:
    """How far adaptive conformal had to move alpha to hold nominal coverage.

    Master Spec §19.5's rule exactly: ``a_{t+1} = a_t + eta (target - hit)``.
    It assumes no exchangeability, which matters here specifically -- the
    observations are biased toward the paths the model recommended, so the
    sequence is not exchangeable by construction and a classical conformal
    guarantee would not hold.

    Reported beside ``coverage`` because a model can hold nominal coverage by
    widening without limit, and this is what shows it: a large drift means the
    intervals were being corrected the whole way through.
    """
    strata = _by_horizon(data)
    if not strata:
        return None
    target = 1.0 - NOMINAL
    out: dict[str, float] = {}
    for horizon, pairs in strata.items():
        alpha = target
        excursion = 0.0
        seen = 0
        for f, truth in sorted(pairs, key=lambda pair: pair[0].t):
            lo, hi = _lo(f), _hi(f)
            if lo is None or hi is None:
                continue
            inside = 1.0 if lo <= truth <= hi else 0.0
            alpha = alpha + ETA * (target - (1.0 - inside))
            excursion = max(excursion, abs(alpha - target))
            seen += 1
        if seen:
            out[horizon] = excursion
    return out or None


def _lo(f: Forecast) -> float | None:
    return _quantile(f, min(f.quantiles) if f.quantiles else None)


def _hi(f: Forecast) -> float | None:
    return _quantile(f, max(f.quantiles) if f.quantiles else None)


def _quantile(f: Forecast, tau: float | None) -> float | None:
    if tau is None or len(f.quantiles) < 2:
        return None
    return float(f.quantiles[tau])


# ==========================================================================
# decision quality -- the family that separates this from an open-loop benchmark
# ==========================================================================


@metric("regret_ms", "decision")
def regret_ms(data: MetricInput) -> float | None:
    """Mean cost above the best fixed path, per sample. **An upper bound.**

    A model can predict accurately and route badly, and this is the only number
    that sees it. Measured against the cheapest path as costs actually were,
    which is a lower bound on achievable cost -- moving the whole scope there
    would have raised it -- so the regret reported is an upper bound on true
    regret. ADR 0017 says why the fixed point is not solved here. The bound is
    the same for every model on the same world, so the comparison is sound even
    though the level is not exact.
    """
    realised, best = _cost_pair(data)
    warm = data.band["warmup"]
    if realised.size <= warm:
        return None
    return float(np.mean(realised[warm:] - best[warm:]))


@metric("regret_ratio", "decision")
def regret_ratio(data: MetricInput) -> float | None:
    """Realised cost over best-fixed-path cost. 1.0 is the bound, never below."""
    realised, best = _cost_pair(data)
    warm = data.band["warmup"]
    tail_best = best[warm:]
    if tail_best.size == 0 or float(tail_best.min()) <= 0.0:
        return None
    return float(np.mean(realised[warm:] / tail_best))


@metric("mean_cost_ms", "decision")
def mean_cost_ms(data: MetricInput) -> float | None:
    """Load-weighted mean path cost over the measured part of the run."""
    tail = _finite(data.series.mean_cost_ms)[data.band["warmup"] :]
    return float(np.mean(tail)) if tail.size else None


@metric("mean_deviation", "decision")
def mean_deviation(data: MetricInput) -> float | None:
    """Gap between what was asked for and what the population did.

    Sampling noise with a compliant population and something else without one.
    Not purely noise once any rung-3 knob is on (ADR 0015), so read it beside
    the axis the cell was run at.
    """
    values = _finite(data.series.deviation)
    return float(np.mean(values)) if values.size else None


# ==========================================================================
# stability
# ==========================================================================


@metric("swing", "stability")
def swing(data: MetricInput) -> float | None:
    """Fast-band amplitude on the worst link. The headline number (ADR 0010)."""
    values = [fast_swing(s, **data.band) for s in data.series.advised_load.values()]
    return max(values) if values else None


@metric("share_swing", "stability")
def share_swing(data: MetricInput) -> float | None:
    """The same amplitude on the realised split rather than on the link."""
    values = [fast_swing(s, **data.band) for s in data.series.path_share.values()]
    return max(values) if values else None


@metric("oscillation_index", "stability")
def oscillation(data: MetricInput) -> float | None:
    """Spectral peak dominance. Believe it at one scope; read ``swing`` otherwise."""
    values = [oscillation_index(s, **data.band) for s in data.series.advised_load.values()]
    return max(values) if values else None


@metric("flap_rate", "stability")
def flapping(data: MetricInput) -> float | None:
    """How often the busiest link's load reverses direction."""
    by_link = {i: fast_swing(s, **data.band) for i, s in data.series.advised_load.items()}
    if not by_link:
        return None
    worst = max(by_link, key=lambda k: by_link[k])
    return flap_rate(data.series.advised_load[worst], warmup=data.band["warmup"])


@metric("convergence_s", "stability")
def convergence_s(data: MetricInput) -> float | None:
    """Simulated seconds until the worst link stops moving, or ``inf``.

    The first sample after which the series stays inside a tenth of its own
    final level for the whole remainder. ``inf`` -- not a large number -- for a
    run that never settles, because "settled at sample 9,999" and "never
    settled" are different findings and a large number reads as the first.
    """
    by_link = {i: fast_swing(s, **data.band) for i, s in data.series.advised_load.items()}
    if not by_link or not data.series.times:
        return None
    worst = max(by_link, key=lambda k: by_link[k])
    values = np.asarray(data.series.advised_load[worst], dtype=np.float64)
    if values.size < 4 or not np.all(np.isfinite(values)):
        return None
    final = float(np.median(values[len(values) // 2 :]))
    band = max(0.1 * abs(final), 1e-3)
    settled = np.abs(values - final) <= band
    # Walk back from the end: the answer is where the unbroken tail begins.
    index = len(settled)
    while index > 0 and settled[index - 1]:
        index -= 1
    if index >= len(settled):
        return float("inf")
    return float(data.series.times[index] - data.series.times[0])


# ==========================================================================
# operational -- where an LLM will look dramatically unlike a regressor
# ==========================================================================


@metric("decision_p50_s", "operational")
def decision_p50_s(data: MetricInput) -> float | None:
    """Median decision latency, in simulated seconds."""
    values = _finite(data.latency_s)
    return float(np.median(values)) if values.size else None


@metric("decision_p95_s", "operational")
def decision_p95_s(data: MetricInput) -> float | None:
    """The tail, which is what misses a deadline. Reported beside the median
    because a model with a good median and a bad tail overruns."""
    values = _finite(data.latency_s)
    return float(np.percentile(values, 95)) if values.size else None


@metric("calls_per_decision", "operational")
def calls_per_decision(data: MetricInput) -> float | None:
    """Tool calls the model spent per decision round. Its information diet."""
    calls = data.session.get("calls")
    rounds = len(data.latency_s)
    return float(calls) / rounds if calls is not None and rounds else None


@metric("overruns", "operational")
def overruns(data: MetricInput) -> float | None:
    """Rounds whose turn took longer than the cadence it was given."""
    return float(data.overruns)


@metric("late_calls", "operational")
def late_calls(data: MetricInput) -> float | None:
    """Calls made after the model's deadline had passed."""
    by_tool = data.session.get("by_tool")
    if not isinstance(by_tool, Mapping):
        return None
    return float(sum(int(row.get("late", 0)) for row in by_tool.values()))


@metric("refusals", "operational")
def refusals(data: MetricInput) -> float | None:
    """Calls the budget or the rate limiter refused.

    What the model does when the budget is exhausted is the behaviour §28 asks
    about, and this is the count that says it happened at all.
    """
    by_tool = data.session.get("by_tool")
    if not isinstance(by_tool, Mapping):
        return None
    return float(sum(int(row.get("failed", 0)) for row in by_tool.values()))


@metric("wall_clock_s", "operational")
def wall_clock_s(data: MetricInput) -> float | None:
    """Real seconds the episode took. Not simulated: what it cost to run."""
    return float(data.wall_clock_s)


@metric("grid_uniform", "operational", higher_is_better=True)
def grid_uniform(data: MetricInput) -> float | None:
    """1.0 if every sample landed on the grid, and so the spectrum is real.

    Carried in the metric set rather than only on the report card because every
    stability number above is invalid without it and a result file read back
    later has no other way to find that out.
    """
    return float(data.series.uniform())


# ==========================================================================
# support -- how much was behind each family's numbers, so a report can say
# ==========================================================================
#
# Not scores. A report divides by these to decide how many digits a value has
# earned (ADR 0018), and the reason there are four rather than one is that the
# four families have four different denominators: a coverage figure taken from
# three repeats of a run that scored four forecasts each is not a figure from
# twelve observations of anything.


@metric("n_scored", "accuracy", higher_is_better=True, support=True)
def n_scored(data: MetricInput) -> Mapping[str, float] | None:
    """Forecasts that had a truth to be scored against, per horizon.

    Not the number made: a forecast whose horizon falls past the end of the run
    is dropped rather than scored against the last sample, so the long horizons
    are legitimately thinner than the short ones and the report has to be able
    to see by how much.
    """
    strata = _by_horizon(data)
    if not strata:
        return None
    return {horizon: float(len(pairs)) for horizon, pairs in strata.items()}


@metric("n_cost_samples", "decision", higher_is_better=True, support=True)
def n_cost_samples(data: MetricInput) -> float | None:
    """Samples where realised and best-fixed cost were both finite, after warmup.

    The warmup is subtracted because every metric in this family subtracts it.
    A support count that did not would report forty samples behind a regret of
    ``None``, which is the one thing a denominator must never do.
    """
    realised, _ = _cost_pair(data)
    return float(max(0, realised.size - int(data.band["warmup"])))


@metric("n_samples", "stability", higher_is_better=True, support=True)
def n_samples(data: MetricInput) -> float | None:
    """Samples the detectors had, after the warmup they discard.

    The warmup is subtracted because the stability metrics do not see it, and a
    denominator that counted it would say a run measured more than it did.
    """
    total = len(data.series)
    if total == 0:
        return None
    return float(max(0, total - int(data.band["warmup"])))


@metric("n_decisions", "operational", higher_is_better=True, support=True)
def n_decisions(data: MetricInput) -> float | None:
    """Decision rounds whose latency was recorded.

    The denominator under every percentile above it: ``decision_p95_s`` over
    three rounds is the slowest of three, and printing it to four decimals
    would say otherwise.
    """
    return float(len(data.latency_s))


# --------------------------------------------------------------------------
# recovery: the bad day, and only the bad day (ADR 0022)
#
# Everything above scores an episode as a whole, which is the right shape for a
# question about ordinary flight and the wrong shape for the question the
# product is actually for. An outage is thirty samples out of six hundred: a
# model that never comes back and a model that comes back in nine seconds land
# within noise of each other on every metric above, because the mean swallows
# the difference.
#
# These five read the same series against the scenario's own event timeline, and
# they are a family of their own so that nothing ever averages them together
# with the steady-state ones. That separation is the point; a combined score
# would be the number a reader quotes, and it would let a model that is
# excellent in the ordinary case and useless in the bad one look deployable.


#: Samples before the fault used to establish where the model was operating.
#: Enough that a single noisy sample cannot set the band, short enough that it
#: is still the same regime.
_BASELINE_SAMPLES = 10
#: How far above the pre-event mean still counts as "back", as a multiple of the
#: pre-event standard deviation, with a floor and a cap.
#:
#: The floor is so that a model whose pre-fault series is flat does not get an
#: impossibly tight band. The cap is the one that was measured rather than
#: chosen: a visibly oscillating model has a pre-fault standard deviation of the
#: same order as its mean, so an uncapped two-sigma band sits at three times the
#: operating cost, and a fault that *tripled* the cost of the network never
#: registered as a disturbance at all. The metric was silently useless for
#: exactly the models it is most needed for. Beyond half again, a band has
#: stopped being a band.
_BAND_SIGMA = 2.0
_BAND_FLOOR = 0.05
_BAND_CAP = 0.5
#: Consecutive in-band samples required. One sample inside the band on the way
#: through is a crossing, not a recovery.
_HOLD = 3
#: Events that are bookkeeping rather than weather. An advisory reaching the
#: hosts is the loop working, not the world going wrong.
_NOT_A_FAULT = frozenset({"advisory_apply"})


def _fault_at(data: MetricInput) -> float | None:
    """When the bad day started, or ``None`` if the run had no bad day."""
    times = [t for t, kind in data.events if kind not in _NOT_A_FAULT]
    return min(times) if times else None


@dataclass(frozen=True, slots=True)
class _Window:
    """One run's bad day, located on the sample grid."""

    #: When the first scheduled fault fired.
    at_s: float
    #: The last ``_BASELINE_SAMPLES`` finite samples before it.
    pre: np.ndarray
    #: Every sample from the fault onward.
    post: np.ndarray
    #: When ``post[0]`` was taken.
    post_t0: float
    #: Index into ``post`` of the first sample that left the band. Recovery is
    #: searched from here, never from the fault: the fault does not land on the
    #: sample that follows it, and a search that started at the fault would find
    #: the still-unaffected samples immediately and report a recovery of zero
    #: seconds for a model that had not yet been disturbed.
    onset: int
    #: The pre-fault band's ceiling.
    ceiling: float


def _recovery_window(data: MetricInput) -> _Window | None:
    """Where the bad day is on the grid, or ``None`` if there is nothing to say.

    Four separate "nothing to say" cases, all of which must return ``None``
    rather than a score: no fault was scheduled; the fault landed before there
    was enough series to set a baseline from; the series is empty; and -- the one
    that matters -- **the fault never disturbed this run**. A degrade that lands
    on links no driven scope was using is a real event that changed nothing here,
    and scoring it as a flawless recovery would hand a model credit for where the
    dice fell.

    The last case has a second cause worth naming, because it was measured on the
    smoke tier before this docstring existed: a model whose pre-fault cost was
    already swinging between 300 ms and 3,600 ms is not disturbed by a fault that
    doubles the mean, and the honest reading is that its own oscillation is wider
    than the weather. That is a finding about the model, and it belongs to the
    stability family rather than being smuggled into a recovery score.
    """
    at = _fault_at(data)
    if at is None:
        return None
    times = np.asarray(data.series.times, dtype=np.float64)
    cost = np.asarray(data.series.mean_cost_ms, dtype=np.float64)
    if times.size == 0 or cost.size != times.size:
        return None
    before = np.flatnonzero(times < at)
    after = np.flatnonzero(times >= at)
    if before.size < _BASELINE_SAMPLES or after.size < _HOLD:
        return None
    pre = cost[before[-_BASELINE_SAMPLES:]]
    pre = pre[np.isfinite(pre)]
    if pre.size < 2 or float(np.mean(pre)) <= 0.0:
        return None
    post = cost[after]
    ceiling = _band(pre)
    out = np.flatnonzero(np.isfinite(post) & (post > ceiling))
    if out.size == 0:
        return None
    return _Window(
        at_s=at,
        pre=pre,
        post=post,
        post_t0=float(times[after[0]]),
        onset=int(out[0]),
        ceiling=ceiling,
    )


def _band(pre: np.ndarray) -> float:
    mean = float(np.mean(pre))
    spread = _BAND_SIGMA * float(np.std(pre))
    return mean + min(max(spread, _BAND_FLOOR * abs(mean)), _BAND_CAP * abs(mean))


def _recovered_index(post: np.ndarray, ceiling: float) -> int | None:
    """First index of ``_HOLD`` consecutive in-band samples, or ``None``."""
    ok = np.isfinite(post) & (post <= ceiling)
    run = 0
    for index, good in enumerate(ok):
        run = run + 1 if good else 0
        if run >= _HOLD:
            return index - _HOLD + 1
    return None


@metric("recovered", "recovery", higher_is_better=True)
def recovered(data: MetricInput) -> float | None:
    """Did the cost return to its pre-fault band before the run ended.

    Separate from ``time_to_recover_s`` and not derivable from it, because a
    model that never recovered has no time to report and a ``None`` renders as
    *not measured*. That is exactly how a model which never came back would
    score like a model nobody watched.
    """
    window = _recovery_window(data)
    if window is None:
        return None
    return 1.0 if _recovered_index(window.post[window.onset :], window.ceiling) is not None else 0.0


@metric("time_to_recover_s", "recovery")
def time_to_recover_s(data: MetricInput) -> float | None:
    """Simulated seconds from the fault until the cost held its pre-fault band."""
    window = _recovery_window(data)
    if window is None:
        return None
    index = _recovered_index(window.post[window.onset :], window.ceiling)
    if index is None:
        return None
    interval = data.series.interval_s or 1.0
    at_recovery = window.post_t0 + (window.onset + index) * interval
    return max(0.0, at_recovery - window.at_s)


@metric("cost_during_recovery", "recovery")
def cost_during_recovery(data: MetricInput) -> float | None:
    """Mean fractional excess over the pre-fault level while the cost was away.

    A fraction rather than milliseconds so a smoke-tier run and a realistic one
    are on the same scale, and measured to the end of the run when there was no
    recovery -- charging a model for the whole time it stayed broken is the
    correct accounting, and it is what makes this readable beside ``recovered``.
    """
    window = _recovery_window(data)
    if window is None:
        return None
    mean = float(np.mean(window.pre))
    away = window.post[window.onset :]
    index = _recovered_index(away, window.ceiling)
    span = away[: index + _HOLD] if index is not None else away
    span = span[np.isfinite(span)]
    if span.size == 0:
        return None
    return float(np.mean(np.maximum(0.0, span - mean) / mean))


@metric("recovered_to", "recovery")
def recovered_to(data: MetricInput) -> float | None:
    """The operating point it settled at, over the one it started from.

    1.0 is back where it was; 1.4 is a network that is permanently forty per cent
    worse under this model, and a run reporting only ``recovered`` would call
    that a success. Measured over the last quarter of the post-fault series so a
    single late sample cannot set it.
    """
    window = _recovery_window(data)
    if window is None:
        return None
    post = window.post
    tail = post[max(1, len(post) * 3 // 4) :]
    tail = tail[np.isfinite(tail)]
    if tail.size == 0:
        return None
    return float(np.mean(tail) / np.mean(window.pre))


@metric("n_faults", "recovery", higher_is_better=True, support=True)
def n_faults(data: MetricInput) -> float | None:
    """Scheduled events this run had. Zero is a steady cell, not a missing figure."""
    return float(len([1 for _, kind in data.events if kind not in _NOT_A_FAULT]))
