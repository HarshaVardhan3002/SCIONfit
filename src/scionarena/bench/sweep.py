"""The sweep engine. A matrix of cells, run in parallel, resumable.

This is the "automated stress-test sweep" the product promises: a user names
their model and gets it scored across every axis, beside the five baselines
§28 makes mandatory, in one command.

The default is **one axis at a time** (ADR 0016). The full cartesian product of
the six axes is 1,458 cells before any model or repeat is counted, and at the
``realistic`` tier a cell is minutes; a sweep that only offered the grid would
be run at the smoke tier and quoted as though it were the real one. Sixteen
cells answer "what does this axis do to this model", which is the question a
report asks anyway. ``mode="grid"`` is there for someone who wants an
interaction and knows what it costs.

Cells run in a process pool. That is partly for speed and partly because ADR
0014 found that a metric measured in a process the previous metric had aged is
not the metric it says it is; a cell in its own process cannot inherit the
allocator state of the cell before it.
"""

from __future__ import annotations

import itertools
import os
import traceback
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from pathlib import Path
from statistics import median
from typing import Any

from ..core.hosts import HostParams
from ..core.scenario import ProbeLimits, Scenario, TopologySpec
from ..core.trace import TraceHash
from ..exposure.loading import ModelLoadError, load_model, resolve
from ..exposure.loop import LoopConfig, busiest_scopes, resolve_drive, run_loop
from ..reference.baselines import MANDATORY_BASELINES
from .axes import AXES, baseline_cell, settings_for
from .results import REFUSED, CellResult, cell_id, cell_seed, load_results, write_result

__all__ = ["Cell", "SweepSpec", "plan", "run_cell", "run_sweep"]


def _target(spec: str) -> str:
    """The import path a spec names, with built-in aliases resolved.

    Falls back to the spec itself for anything ``resolve`` will not take, which
    keeps a malformed spec a *load* failure -- reported per cell, with the
    sentence ADR 0013 promises -- rather than one that kills the whole plan.
    """
    try:
        return resolve(spec)
    except ModelLoadError:
        return spec


@dataclass(frozen=True, slots=True)
class Cell:
    """One model, at one point of the matrix, on one repeat."""

    model: str
    axes: Mapping[str, str]
    repeat: int
    mandatory: bool = False
    #: Set only by a parity sweep, where one model is run both ways over one
    #: world. Empty means "whatever the suite says", which is every other cell.
    drive: str = ""

    def identity(self, suite: str) -> tuple[str, int]:
        """This cell's name, and the seed of the world it runs in.

        The two do not come from the same hash. The name carries the drive so a
        parity pair writes two files; the seed does not, so the pair shares one
        world. A pair over two worlds would measure the seed.
        """
        return cell_id(suite, self.model, self.axes, self.repeat, self.drive), cell_seed(
            suite, self.model, self.axes, self.repeat
        )

    def describe(self) -> str:
        moved = {k: v for k, v in self.axes.items() if v != AXES[k].values[0].label}
        where = ", ".join(f"{k}={v}" for k, v in sorted(moved.items())) or "baseline"
        # The drive only when a parity sweep set it, because otherwise a plan
        # under ``drive="both"`` prints every cell twice, identically, and the
        # one thing a reader needs from it -- that these are two runs -- is the
        # one thing it does not say.
        how = f" [{self.drive}]" if self.drive else ""
        return f"{self.model}{how} @ {where}"


@dataclass(frozen=True, slots=True)
class SweepSpec:
    """A suite: what to run, over what, how many times.

    Everything here is in the suite digest, so a suite that has been edited does
    not silently reuse the results of the suite before the edit. ``baseline`` is
    in it too, and load-bearingly: under one-at-a-time every reading is measured
    against the baseline cell, so if the baseline is unrepresentative then every
    reading is unrepresentative in the same direction.
    """

    name: str = "standard"
    #: Model specs, in the form ADR 0013 defines. The mandatory baselines are
    #: appended to whatever is here.
    models: tuple[str, ...] = ()
    tier: str = "dev"
    mode: str = "oat"
    #: Worlds per cell. Three rather than one, measured rather than chosen:
    #: ``EMAOracle`` at the dev tier, same cell, six seeds, returned
    #: 2.29 0.32 2.26 0.39 0.30 2.44 -- **bimodal**, with a standard deviation
    #: equal to its mean, while ``CapacityProportional`` on the same six
    #: returned 0.06 +- 0.02. The models are still separated at every seed, so
    #: the suite discriminates on one repeat; the *magnitude* is not estimable
    #: from one, and a benchmark meant to be quoted has to report a spread. That
    #: the spread is bimodal rather than Gaussian is itself a finding, and a
    #: report that showed only a mean would hide it.
    repeats: int = 3
    #: Decision rounds per cell. Not a free knob: the stability detectors
    #: take a warmup and then measure a *band*, so a short run reports a swing
    #: of exactly zero for a model that is visibly flapping at a hundred and
    #: twenty. Measured on the dev tier, same model, same cell: 40 rounds gave
    #: 0.00, 120 gave 2.79, 240 gave 2.77. Below about a hundred this suite
    #: silently measures nothing, which is worse than measuring it slowly.
    cycles: int = 120
    decision_s: float = 30.0
    scopes: int = 8
    n_tracked: int = 24
    step_s: float = 1.0
    identity_policy: str = "structural"
    #: Forecast horizons, in simulated seconds. §28 asks "does anything beat
    #: persistence at +60 s / +300 s", which is why those two beside the nowcast.
    horizons_s: tuple[float, ...] = (0.0, 60.0, 300.0)
    #: How to drive the models: ``auto``, ``fixed``, ``agentic`` or ``both``
    #: (ADR 0019). Distinct from ``mode``, which is the shape of the matrix.
    #:
    #: ``auto`` believes each model's ``uses_tools`` declaration, so a suite
    #: mixing agents and forecasters runs each the way its author meant. That is
    #: the right default and it is also the confounded comparison: the agent
    #: picks its own probes and the forecaster is handed ours. ``fixed`` runs
    #: every model on the same information diet, which is the only way to ask
    #: which architecture forecasts better rather than which probes better.
    #:
    #: ``both`` runs the parity pair: every cell twice, ``fixed`` and
    #: ``agentic``, over one world. It doubles the matrix, and what it buys is
    #: the only measurement of what choosing your own probes is *worth*. A model
    #: with no ``act`` records a refusal for its agentic half rather than
    #: quietly running the fixed cycle twice, and the refusal costs nothing
    #: because it happens before the world is built.
    drive: str = "auto"
    #: What a decision's own compute costs (ADR 0021): ``free``, ``fixed`` or
    #: ``measured``. ``free`` is the default and is what every recorded number
    #: predates this setting under; moving it earlier would invalidate the whole
    #: existing baseline set for a difference of microseconds. It is the setting
    #: to change once there is a model whose thinking is worth charging, and a
    #: ``measured`` suite does not reproduce across machines.
    think: str = "free"
    #: Simulated seconds per model call under ``think="fixed"``.
    think_s: float = 0.0
    #: Which axes to move. Empty means all of them.
    only: tuple[str, ...] = ()
    baseline: Mapping[str, str] = field(default_factory=baseline_cell)
    include_baselines: bool = True

    def __post_init__(self) -> None:
        if self.mode not in ("oat", "grid"):
            raise ValueError(f"mode must be 'oat' or 'grid', not {self.mode!r}")
        if self.think not in ("free", "fixed", "measured"):
            raise ValueError(f"think must be 'free', 'fixed' or 'measured', not {self.think!r}")
        if self.drive not in ("auto", "fixed", "agentic", "both"):
            raise ValueError(
                f"drive must be 'auto', 'fixed', 'agentic' or 'both', not {self.drive!r}"
            )
        for name in self.only:
            if name not in AXES:
                raise ValueError(f"unknown axis {name!r}; the axes are {sorted(AXES)}")
        for name, label in self.baseline.items():
            AXES[name].value(label)  # raises with the labels it does have
        if self.repeats < 1:
            raise ValueError("repeats must be at least 1")
        if not self.models and not self.include_baselines:
            raise ValueError("a sweep with no models and no baselines would measure nothing")

    @property
    def duration_s(self) -> float:
        """Long enough for every round, plus a tail the sampler can use."""
        return self.cycles * self.decision_s + 4.0 * self.decision_s

    @property
    def axis_names(self) -> tuple[str, ...]:
        return self.only or tuple(AXES)

    def all_models(self) -> list[tuple[str, bool]]:
        """``(spec, is_mandatory)``, user models first, baselines deduplicated in.

        §28's requirement is worth nothing if it can be switched off, so the
        baselines are appended rather than offered. ``include_baselines=False``
        exists for tests and says so.

        Deduplication is on the *resolved* import path rather than on the spec
        string, because ``ema`` and
        ``scionarena.reference.models:EMAOracle`` are the same model under two
        names (ADR 0013). Comparing the strings ran it twice, under two seeds,
        and printed it as two models with a fourfold difference between them.
        """
        out: list[tuple[str, bool]] = [(m, False) for m in dict.fromkeys(self.models)]
        if self.include_baselines:
            named = {_target(m) for m, _ in out}
            out += [(spec, True) for spec in MANDATORY_BASELINES if _target(spec) not in named]
        return out

    def digest(self) -> str:
        """What this suite *is*. A cell recorded under a different one is stale."""
        return (
            TraceHash(label="suite")
            .update(
                {
                    "name": self.name,
                    "tier": self.tier,
                    "mode": self.mode,
                    "drive": self.drive,
                    "think": self.think,
                    "think_s": self.think_s,
                    "cycles": self.cycles,
                    "decision_s": self.decision_s,
                    "scopes": self.scopes,
                    "step_s": self.step_s,
                    "identity_policy": self.identity_policy,
                    "horizons_s": list(self.horizons_s),
                    "baseline": dict(self.baseline),
                    "axes": [AXES[n].to_dict() for n in self.axis_names],
                }
            )
            .short(16)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "digest": self.digest(),
            "tier": self.tier,
            "mode": self.mode,
            "models": list(self.models),
            "repeats": self.repeats,
            "cycles": self.cycles,
            "decision_s": self.decision_s,
            "scopes": self.scopes,
            "baseline": dict(self.baseline),
            "axes": list(self.axis_names),
        }


def _points(spec: SweepSpec) -> Iterator[dict[str, str]]:
    """The matrix, without models or repeats."""
    base = dict(spec.baseline)
    if spec.mode == "grid":
        names = list(spec.axis_names)
        for combo in itertools.product(*(AXES[n].labels for n in names)):
            yield {**base, **dict(zip(names, combo, strict=True))}
        return
    yield dict(base)
    for name in spec.axis_names:
        for label in AXES[name].labels:
            if label != base[name]:
                yield {**base, name: label}


def plan(spec: SweepSpec) -> list[Cell]:
    """Every cell this suite will run, in a fixed order.

    Fixed so that a run interrupted and resumed does the remaining work in the
    same order, and so that two people reading a plan see the same list. It is
    not the execution order -- the pool decides that -- which is why a cell's
    seed comes from the cell rather than from its position here.
    """
    drives = ("fixed", "agentic") if spec.drive == "both" else ("",)
    cells: list[Cell] = []
    for model, mandatory in spec.all_models():
        for axes in _points(spec):
            for repeat in range(spec.repeats):
                for drive in drives:
                    cells.append(
                        Cell(
                            model=model,
                            axes=axes,
                            repeat=repeat,
                            mandatory=mandatory,
                            drive=drive,
                        )
                    )
    return cells


def _scenario(spec: SweepSpec, cell: Cell, seed: int) -> Scenario:
    hosts, _, probes = settings_for(cell.axes)
    return Scenario(
        name=f"{spec.name}/{'+'.join(f'{k}={v}' for k, v in sorted(cell.axes.items()))}",
        seed=seed,
        duration_s=spec.duration_s,
        step_s=spec.step_s,
        identity_policy=spec.identity_policy,
        topology=TopologySpec(tier=spec.tier),
        probe_limits=ProbeLimits(**probes),
        hosts=HostParams(**hosts),
    )


def _config(spec: SweepSpec, cell: Cell, seed: int) -> LoopConfig:
    _, loop, _ = settings_for(cell.axes)
    return replace(
        LoopConfig(
            cycles=spec.cycles,
            decision_s=spec.decision_s,
            n_tracked=spec.n_tracked,
            seed=seed,
            # Always on here, and off everywhere else (ADR 0017). §28 makes
            # accuracy mandatory, so a benchmark cell that did not ask the model
            # to predict anything could not report three of the four families.
            record_forecasts=True,
            horizons_s=spec.horizons_s,
            # ``both`` sets it per cell; every other setting is suite-wide.
            drive=cell.drive or spec.drive,
            think=spec.think,
            think_s=spec.think_s,
        ),
        **loop,
    )


def _declared(model: Any) -> dict[str, Any]:
    """What the model says about itself, as the report will print it.

    Booleans and the identifying strings, and nothing else: ``extra`` is a
    free-form dict a model author may put anything in, including something
    unserialisable, and a cell that failed to write because of it would lose a
    run over a field no report reads.

    ``architecture`` travels with them because it is the grouping key the
    two-level comparison needs, and it has to be recorded per cell for the same
    reason the tier is: the model object is gone by the time a report is built.
    """
    caps = getattr(model, "capabilities", None)
    if caps is None:
        return {}
    fields = vars(caps)
    out: dict[str, Any] = {k: v for k, v in fields.items() if isinstance(v, bool)}
    for key in ("name", "version", "authors", "notes", "architecture"):
        value = fields.get(key)
        if isinstance(value, str) and value:
            out[key] = value
    return out


def run_cell(spec: SweepSpec, cell: Cell) -> CellResult:
    """Run one cell to a result. Never raises for a model's own failure.

    A model that raises, a budget that starves, a scenario with no multi-path
    scope: each is written down with ``error`` set and the sweep carries on. A
    sweep that died on the first of those would throw away every cell that had
    already run, and the failure is itself a finding about the model.
    """
    ident, seed = cell.identity(spec.name)
    scenario = _scenario(spec, cell, seed)
    result = CellResult(
        cell_id=ident,
        suite=spec.name,
        suite_digest=spec.digest(),
        model=cell.model,
        label=cell.model,
        mandatory=cell.mandatory,
        axes=dict(cell.axes),
        repeat=cell.repeat,
        seed=seed,
        scenario=scenario.name,
        tier=spec.tier,
    )
    try:
        model = load_model(cell.model)
        result.label = getattr(model.capabilities, "name", cell.model)
        result.capabilities = _declared(model)
        # Before the world is built, so a model that cannot be driven this way
        # costs a refusal rather than a topology. Under ``drive="both"`` that is
        # the difference between a cheap "this one has no agentic half" and
        # rebuilding the realistic tier to find it out.
        config = _config(spec, cell, seed)
        try:
            result.drive = resolve_drive(model, config.drive)
            result.think = config.think
        except ValueError as exc:
            # Not applicable rather than broken, and the distinction is the
            # whole reason a parity sweep can include the five baselines at all.
            result.error = f"{REFUSED} {exc}"
            return result
        world = scenario.build()
        result.substrate_digest = world.digest()
        scopes = busiest_scopes(world, spec.scopes)
        if not scopes:
            raise RuntimeError(
                f"no scope in the {spec.tier} tier has two paths to choose between, "
                "so no model could differ from any other here"
            )
        loop = run_loop(model, scenario, scopes, config=config, world=world)
        result.drive = loop.drive
        result.think = loop.think
        # The report card is M3's fixed set and stays fixed so an old result
        # still renders; the registry is what a report reads (ADR 0017), and a
        # metric it could not measure is None rather than zero.
        result.metrics = {**loop.report(), **loop.metrics()}
        result.wall_clock_s = loop.wall_clock_s
    except Exception as exc:  # noqa: BLE001 -- a failed cell is a recorded result
        result.error = f"{type(exc).__name__}: {exc}"
        result.metrics = {"traceback": traceback.format_exc(limit=6)}
    return result


def _worker(payload: tuple[SweepSpec, Cell, str]) -> tuple[str, str | None]:
    """Pool entry point. Writes its own file so nothing large is pickled back."""
    spec, cell, directory = payload
    result = run_cell(spec, cell)
    write_result(Path(directory), result)
    return result.cell_id, result.error


def _finished(directory: Path, digest: str) -> set[str]:
    """Cells already on disk under *this* suite. Anything else is re-run."""
    if not directory.exists():
        return set()
    return {r.cell_id for r in load_results(directory) if r.suite_digest == digest and r.ok}


def run_sweep(
    spec: SweepSpec,
    directory: Path,
    *,
    workers: int | None = None,
    resume: bool = True,
    on_cell: Callable[[int, int, str, str | None], None] | None = None,
    cells: Iterable[Cell] | None = None,
) -> list[CellResult]:
    """Run a suite into a directory and return everything it now holds.

    ``on_cell(done, total, cell_id, error)`` is called as each cell lands, on
    this thread, and is the only way to watch a sweep that takes an hour.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    digest = spec.digest()
    todo = list(cells if cells is not None else plan(spec))
    if resume:
        done = _finished(directory, digest)
        todo = [c for c in todo if c.identity(spec.name)[0] not in done]

    total = len(todo)
    if total:
        limit = workers if workers is not None else min(len(todo), (os.cpu_count() or 2))
        if limit <= 1:
            for index, cell in enumerate(todo, start=1):
                result = run_cell(spec, cell)
                write_result(directory, result)
                if on_cell is not None:
                    on_cell(index, total, result.cell_id, result.error)
        else:
            payloads = [(spec, cell, str(directory)) for cell in todo]
            with ProcessPoolExecutor(max_workers=limit) as pool:
                futures = [pool.submit(_worker, p) for p in payloads]
                for index, future in enumerate(as_completed(futures), start=1):
                    ident, error = future.result()
                    if on_cell is not None:
                        on_cell(index, total, ident, error)
    return list(load_results(directory))


def summarise(results: Sequence[CellResult], metric: str = "swing") -> list[dict[str, Any]]:
    """One row per (model, axis value), repeats folded into a range.

    A range rather than a mean, because the headline metric is bimodal across
    seeds -- ``EMAOracle`` at the dev tier returned 2.29 0.32 2.26 0.39 0.30
    2.44 on six worlds of one cell -- and a mean of that is a number that
    describes none of the runs it came from.

    Deliberately thin. Scoring is Phase 3's metric registry; this is enough to
    read a sweep in a terminal and see whether it discriminated.
    """
    grouped: dict[tuple[str, str], list[CellResult]] = {}
    for r in results:
        moved = {k: v for k, v in r.axes.items() if v != AXES[k].values[0].label}
        where = ", ".join(f"{k}={v}" for k, v in sorted(moved.items())) or "baseline"
        grouped.setdefault((where, r.label), []).append(r)

    rows: list[dict[str, Any]] = []
    for (where, label), cells in grouped.items():
        values = [
            float(c.metrics[metric])
            for c in cells
            if c.ok and isinstance(c.metrics.get(metric), (int, float))
        ]
        errors = [c.error for c in cells if not c.ok]
        rows.append(
            {
                "model": label,
                "mandatory": cells[0].mandatory,
                "where": where,
                "n": len(cells),
                metric: median(values) if values else None,
                "min": min(values) if values else None,
                "max": max(values) if values else None,
                "error": errors[0] if errors else None,
            }
        )
    return sorted(rows, key=lambda row: (row["where"], row["model"]))
