"""A gradient-boosted regressor that trains itself inside the loop.

The fifth architecture, and the first shipped model whose compute is worth
charging for. Everything else here decides in microseconds; this one fits trees,
and since ADR 0021 the fit lands on the simulated clock rather than being free.

**Why it trains inside the loop.** The harness evaluates trained models and does
not train them, and there is no artefact to ship: a tree ensemble for *this*
network has to be fitted on *this* network. A deployed one would be refitted
periodically from its own telemetry, which is exactly what this does. The
harness is not training it -- the model is training itself, on its own
observations, and paying for it out of its own decision slot. That is the
honest version of a classical ML baseline in a closed loop, and the cost showing
up in ``decision_p95_s`` is the finding, not a nuisance.

**Why the features are structural.** Nothing here is keyed on a path identity.
Every feature is computed from the snapshot the model was handed plus what it
has seen on that path, so a path built from interfaces that appeared one second
ago produces a row like any other. That is what earns
``handles_unseen_interfaces`` and ``composes_unseen_paths``, and it is also the
defence against the failure mode probe R4 exists for: a model keyed on an
identifier that changes when a segment is re-signed discards its own history
every refresh cycle while appearing to work.

``scikit-learn`` is behind the ``[trees]`` extra and imported inside the
methods, so the package installs and every other front-end runs without it.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Mapping, Sequence
from typing import Any

from ..exposure.contracts import (
    SLA,
    Advisory,
    Capabilities,
    Demand,
    Dist,
    Observation,
    PathRef,
    Prediction,
    TopologySnapshot,
)

__all__ = ["GradientBoosted", "MissingTrees", "EXTRA_HINT"]

EXTRA_HINT = "install the extra:  pip install 'scionarena[trees]'"

#: The quantiles fitted when ``distributional`` is on. Three, because two would
#: give an interval with no centre and five triples the fit cost for a
#: resolution the accuracy family does not read at this horizon count.
QUANTILES = (0.1, 0.5, 0.9)

#: Nominal coverage the outer pair is aimed at, and what ``coverage`` is scored
#: against. Stated here so the two cannot drift apart silently.
NOMINAL = 0.8

#: Rows kept. An hour-long run at the realistic tier would otherwise grow an
#: unbounded training set, and a tree fitted on the whole run is no longer an
#: online model -- it is one that has seen the future of its own episode.
DEFAULT_MEMORY = 4096

#: Rows needed before the first fit. Below this the ensemble memorises the
#: handful of paths it has seen, which looks like skill and is not.
MIN_ROWS = 48


class MissingTrees(RuntimeError):
    """Raised at construction rather than mid-sweep, with what to do about it."""


class GradientBoosted:
    """Histogram gradient boosting over structural path features, refitted online.

    One model per quantile when ``distributional`` is on, which triples the fit
    cost. That is deliberate and is the point of measuring it: emitting an
    interval is not free, and the operational family is where the price shows.
    """

    #: Column order. Written down because the adaptor pattern's whole lesson is
    #: that *something* has to, and here that something is the model itself.
    FEATURES = (
        "hop_count",
        "declared_latency_ms",
        "min_declared_bw",
        "age_s",
        "last_seen_ms",
        "was_observed",
        "horizon_s",
    )

    def __init__(
        self,
        *,
        distributional: bool = True,
        refit_every: int = 16,
        memory: int = DEFAULT_MEMORY,
        max_iter: int = 60,
        seed: int = 0,
    ) -> None:
        try:
            import sklearn  # noqa: F401
        except ImportError as exc:  # pragma: no cover -- exercised by the extra test
            raise MissingTrees(
                f"GradientBoosted needs scikit-learn, which is not installed. {EXTRA_HINT}"
            ) from exc

        self.distributional = bool(distributional)
        self.refit_every = max(1, int(refit_every))
        self.memory = max(MIN_ROWS, int(memory))
        self.max_iter = max(1, int(max_iter))
        self.seed = int(seed)

        self.capabilities = Capabilities(
            name="GradientBoosted",
            architecture="gbdt",
            version="0.1.0",
            authors="scionarena reference",
            distributional=self.distributional,
            # It sees no demand signal and says so. Conditioning would need the
            # offered load as a feature, and Q2 has not established that a
            # deployed node can observe it.
            demand_conditioned=False,
            monotone_in_demand=False,
            emits_assignment=True,
            handles_unseen_interfaces=True,
            composes_unseen_paths=True,
            # Every row carries the age of the evidence behind it, so a stale
            # path is a different point in feature space rather than the same
            # point asserted with equal confidence.
            staleness_aware=True,
            reports_confidence=True,
            stateful=True,
            notes=(
                "Histogram gradient boosting on structural path features, refitted from its "
                "own observations every refit_every rounds. Fits inside the decision slot, so "
                "the refit is charged as decision latency under think='measured'."
            ),
        )
        self._reset_state()

    # ------------------------------------------------------------- lifecycle

    def _reset_state(self) -> None:
        #: (features, target) rows, newest last, bounded.
        self._rows: deque[tuple[list[float], float]] = deque(maxlen=self.memory)
        #: Last observation per path: (t, latency_ms).
        self._last: dict[str, tuple[float, float]] = {}
        self._bw: dict[str, float] = {}
        self._loss: dict[str, float] = {}
        self._fitted: dict[float, Any] = {}
        self._rounds = 0
        self._since_fit = 0
        self._t = 0.0
        #: Residual spread of the last fit, the fallback interval when the model
        #: is a point estimator. Kept because an interval of zero width would
        #: score coverage 0.0 and look like over-confidence rather than absence.
        self._resid = 0.0

    def reset(self, topo: TopologySnapshot, seed: int = 0) -> None:
        self.seed = int(seed)
        self._reset_state()
        self._t = topo.t

    # ------------------------------------------------------------- ingestion

    def observe(self, obs: Sequence[Observation], topo: TopologySnapshot) -> None:
        """Turn measurements into training rows, then refit on a cadence.

        A row is formed only when there is a *previous* observation for the same
        path: the features describe what was known then, the horizon is how long
        ago that was, and the target is what actually happened. Without the
        pair there is no supervised example, only a reading.
        """
        self._t = max(self._t, topo.t)
        for o in obs:
            if o.throughput_mbps is not None:
                self._bw[o.path_id] = o.throughput_mbps
            if o.loss is not None:
                self._loss[o.path_id] = o.loss
            if o.latency_ms is None:  # not measured, which is not zero
                continue
            previous = self._last.get(o.path_id)
            self._last[o.path_id] = (o.t, o.latency_ms)
            if previous is None:
                continue
            t0, y0 = previous
            gap = max(0.0, o.t - t0)
            if gap <= 0.0:
                continue
            path = next((p for p in topo.paths if p.path_id == o.path_id), None)
            if path is None:
                continue
            self._rows.append(
                (self._row(path, topo, horizon_s=gap, at=(t0, y0)), float(o.latency_ms))
            )

        self._rounds += 1
        self._since_fit += 1
        if self._since_fit >= self.refit_every and len(self._rows) >= MIN_ROWS:
            self._fit()
            self._since_fit = 0

    # -------------------------------------------------------------- features

    def _row(
        self,
        path: PathRef,
        topo: TopologySnapshot,
        *,
        horizon_s: float,
        at: tuple[float, float] | None = None,
    ) -> list[float]:
        """One path as a flat vector, from the snapshot and nothing identity-keyed."""
        seen = [topo.interfaces[i] for i in path.interfaces if i in topo.interfaces]
        declared = sum(i.declared_latency_ms or 10.0 for i in seen)
        bandwidth = min((i.declared_bw_mbps or 100.0 for i in seen), default=100.0)

        memo = at if at is not None else self._last.get(path.path_id)
        if memo is None:
            # No evidence. The declared figure stands in, and the flag says so
            # rather than letting a zero pass for a measurement (probe R3).
            age, last, observed = 0.0, declared, 0.0
        else:
            t0, y0 = memo
            age, last, observed = max(0.0, self._t - t0), y0, 1.0
        return [
            float(path.hop_count),
            float(declared),
            float(bandwidth),
            float(age),
            float(last),
            observed,
            float(horizon_s),
        ]

    # ------------------------------------------------------------- the fit

    def _fit(self) -> None:
        import numpy as np
        from sklearn.ensemble import HistGradientBoostingRegressor

        rows = list(self._rows)
        features = np.asarray([r for r, _ in rows], dtype=float)
        targets = np.asarray([y for _, y in rows], dtype=float)

        fitted: dict[float, Any] = {}
        quantiles = QUANTILES if self.distributional else (0.5,)
        for q in quantiles:
            model = HistGradientBoostingRegressor(
                loss="quantile",
                quantile=q,
                max_iter=self.max_iter,
                random_state=self.seed,
                early_stopping=False,
            )
            model.fit(features, targets)
            fitted[q] = model
        self._fitted = fitted

        median = fitted[0.5].predict(features)
        self._resid = float(np.percentile(np.abs(targets - median), 80)) if len(targets) else 0.0

    # ------------------------------------------------------------ prediction

    def predict(
        self,
        topo: TopologySnapshot,
        paths: Sequence[PathRef],
        horizon_s: float = 0.0,
        demand: Demand | None = None,
    ) -> Mapping[str, Prediction]:
        """Score every path handed over. ``demand`` is ignored, and declared so."""
        if not paths:
            return {}
        if not self._fitted:
            return {p.path_id: self._prior(p, topo) for p in paths}

        import numpy as np

        features = np.asarray([self._row(p, topo, horizon_s=horizon_s) for p in paths], dtype=float)
        predicted = {q: model.predict(features) for q, model in self._fitted.items()}
        centre = predicted[0.5]

        out: dict[str, Prediction] = {}
        for index, path in enumerate(paths):
            mid = max(0.0, float(centre[index]))
            if self.distributional and len(predicted) > 1:
                # Sorted, because quantile regressors are fitted independently
                # and nothing makes them monotone. A crossed interval fails R5
                # and makes every coverage figure meaningless.
                values = sorted(max(0.0, float(predicted[q][index])) for q in QUANTILES)
                quantiles = dict(zip(QUANTILES, values, strict=True))
                spread = values[-1] - values[0]
            else:
                half = max(1.0, self._resid)
                quantiles = None
                spread = 2 * half
            out[path.path_id] = Prediction(
                latency_ms=Dist(mean=mid, quantiles=quantiles),
                throughput_mbps=Dist.point_estimate(
                    self._bw.get(path.path_id, float(features[index][2]))
                ),
                loss=Dist.point_estimate(self._loss.get(path.path_id, 0.001)),
                # Narrow relative to the level means confident. Bounded so a
                # degenerate fit cannot report certainty.
                confidence=max(0.0, min(1.0, 1.0 / (1.0 + spread / max(1.0, mid)))),
            )
        return out

    def _prior(self, path: PathRef, topo: TopologySnapshot) -> Prediction:
        """Before the first fit: the beacon's own numbers, widely bracketed.

        Not a refusal. A model with nothing learned yet still has to advise, and
        pretending to certainty here is what the interval is for.
        """
        row = self._row(path, topo, horizon_s=0.0)
        declared = row[1]
        quantiles = (
            {0.1: declared * 0.5, 0.5: declared, 0.9: declared * 2.0}
            if self.distributional
            else None
        )
        return Prediction(
            latency_ms=Dist(mean=declared, quantiles=quantiles),
            throughput_mbps=Dist.point_estimate(row[2]),
            loss=Dist.point_estimate(0.001),
            confidence=0.1,
        )

    # ------------------------------------------------------------ assignment

    def advise(
        self,
        topo: TopologySnapshot,
        paths: Sequence[PathRef],
        sla: SLA,
        n_hosts: int = 1,
    ) -> Advisory:
        """Softmax over predicted cost, scaled by the spread it actually sees.

        Scaled rather than absolute: path costs here run over three orders of
        magnitude, and a fixed temperature against a spread of hundreds returns
        weights of 1e-40 -- a ranking in everything but arithmetic, which is
        what ``emits_assignment`` would then be falsely claiming.
        """
        if not paths:
            return Advisory(weights={}, reason="nothing offered")
        predicted = self.predict(topo, paths, horizon_s=0.0, demand=None)
        costs = {p.path_id: predicted[p.path_id].cost() for p in paths}

        floor = min(costs.values())
        spread = max(costs.values()) - floor
        scale = max(1e-6, 0.25 * spread) if spread > 0 else 1.0
        weights = {k: math.exp(-min(50.0, (v - floor) / scale)) for k, v in costs.items()}
        total = sum(weights.values()) or 1.0
        return Advisory(
            weights={k: v / total for k, v in weights.items()},
            confidence=None if not self._fitted else 0.5,
            reason=(
                f"gbdt over {len(paths)} paths, {len(self._rows)} rows, "
                f"{'fitted' if self._fitted else 'prior'}"
            ),
        )
