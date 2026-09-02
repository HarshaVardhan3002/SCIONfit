"""The thirty-second check, before the hour-long one (ADR 0020).

An adaptor is the part of a submission most likely to be wrong, and until this
existed the cheapest way to find that out was a failed cell in a sweep -- an
hour in, attributed to the model, with a traceback from inside the driver.

Nothing here builds a substrate. The topology is a handful of dataclasses made
by hand, which is why the whole thing costs a second and why it runs on a
machine that could not hold the realistic tier.

**This is not conformance.** It checks a strict subset of what the probe suite
checks, cheaply, and every result carries one of three states rather than two:
observed-and-matched, observed-and-contradicted, or *unchecked*. The third is
the reason the module is worth having. A cheap check with two states has to
guess, and a pre-flight that guesses "pass" sends a broken adaptor into an hour
of compute with a tick behind it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .contracts import (
    SLA,
    Advisory,
    Demand,
    InterfaceAttrs,
    PathModel,
    PathRef,
    Prediction,
    TopologySnapshot,
)

__all__ = [
    "Check",
    "Precheck",
    "OK",
    "CONTRADICTED",
    "UNCHECKED",
    "BROKEN",
    "ONE_HOT",
    "synthetic_topology",
    "precheck",
    "check_model",
]

OK = "ok"
CONTRADICTED = "contradicted"
UNCHECKED = "unchecked"
#: The contract itself was not honoured -- a wrong return type, a raise where
#: the docstring promises tolerance. Distinct from ``CONTRADICTED``, which is
#: an honest model whose declaration is wrong: this one will not run at all.
BROKEN = "broken"

#: Above this, an advisory is a ranking whatever produced it. Not 1.0: a softmax
#: at a temperature small next to the cost spread returns a residue like 1e-40
#: on every other path, which is non-zero and means nothing.
ONE_HOT = 0.99

#: Declarations no pre-flight can establish without a substrate, and why. Named
#: rather than silently skipped: an unlisted check is one nobody decided about.
NEEDS_SUBSTRATE: Mapping[str, str] = {
    "staleness_aware": "needs telemetry that ages, which needs a world with a clock",
    "self_consistent": "needs the closed loop: what its own advice did to the load",
    "manages_own_memory": "needs a history long enough to outgrow, which needs a run",
    "respects_deadline": "needs a session with a deadline and a cost per call",
}


@dataclass(frozen=True, slots=True)
class Check:
    """One question, its answer, and what the answer was based on."""

    name: str
    state: str
    detail: str = ""

    @property
    def bad(self) -> bool:
        return self.state in (CONTRADICTED, BROKEN)


@dataclass
class Precheck:
    """What the pre-flight found. Counts are kept apart on purpose."""

    spec: str = ""
    name: str = ""
    architecture: str = ""
    checks: list[Check] = field(default_factory=list)
    load_error: str = ""

    def by_state(self, state: str) -> list[Check]:
        return [c for c in self.checks if c.state == state]

    @property
    def blocking(self) -> list[Check]:
        """What would make a sweep cell fail, as opposed to score badly."""
        return [c for c in self.checks if c.state == BROKEN]

    def summary(self) -> str:
        if self.load_error:
            return f"{self.spec}: did not load"
        counts = ", ".join(
            f"{len(self.by_state(s))} {s}" for s in (OK, CONTRADICTED, BROKEN, UNCHECKED)
        )
        return f"{self.name or self.spec}: {counts}"

    def lines(self) -> list[str]:
        """The whole report, as text, in the order a reader needs it."""
        if self.load_error:
            return [f"{self.spec} did not load:", f"  {self.load_error}"]
        out = [self.summary()]
        tag = self.architecture or "unrecorded"
        out.append(f"  architecture: {tag}")
        for state in (BROKEN, CONTRADICTED, OK, UNCHECKED):
            found = self.by_state(state)
            if not found:
                continue
            out.append(f"  {state}:")
            out += [f"    {c.name:<26} {c.detail}" for c in found]
        out.append("")
        out.append(
            "This is a pre-flight, not a verdict. It checks a subset of what the "
            "probes check, without a substrate. Run 'scionfit check' for the "
            "check that decides."
        )
        return out


# --------------------------------------------------------------------------
# a world made of five dataclasses
# --------------------------------------------------------------------------

SRC = "1-ff00:0:1"
DST = "1-ff00:0:9"


def synthetic_topology(*, extra: bool = False) -> TopologySnapshot:
    """Four paths between one pair, from interfaces named here and nowhere else.

    ``extra`` adds a path over an interface absent from the snapshot the model
    was reset with, which is how ``handles_unseen_interfaces`` is asked about
    without a world that churns.
    """
    ifaces = {
        f"if{i}": InterfaceAttrs(
            iface_id=f"if{i}",
            as_id=f"1-ff00:0:{i}",
            isd=1,
            link_type="core" if i % 2 else "parent_child",
            declared_bw_mbps=1000.0 * (i + 1),
            declared_latency_ms=5.0 * (i + 1),
            mtu=1400,
        )
        for i in range(1, 7)
    }
    paths = [
        PathRef("p1", SRC, DST, ("if1", "if2"), expiry_s=600.0, mtu=1400),
        PathRef("p2", SRC, DST, ("if3", "if4"), expiry_s=600.0, mtu=1400),
        PathRef("p3", SRC, DST, ("if1", "if4", "if5"), expiry_s=600.0, mtu=1400),
    ]
    if extra:
        ifaces["if99"] = InterfaceAttrs("if99", "1-ff00:0:99", 1, "peering", 100.0, 40.0, 1400)
        paths.append(PathRef("p4", SRC, DST, ("if99", "if6"), expiry_s=600.0, mtu=1400))
    return TopologySnapshot(t=0.0, interfaces=ifaces, paths=tuple(paths))


def _observations(topo: TopologySnapshot, t: float, scale: float) -> list[Any]:
    from .contracts import Observation

    return [
        Observation(
            t=t,
            path_id=p.path_id,
            latency_ms=10.0 * scale * (i + 1),
            throughput_mbps=100.0 / scale,
            loss=0.001 * scale,
            source="scmp",
        )
        for i, p in enumerate(topo.paths)
    ]


def _demand(paths: Sequence[PathRef], heavy: str | None = None) -> Demand:
    share = 1.0 / max(1, len(paths))
    weights = {p.path_id: share for p in paths}
    if heavy is not None and heavy in weights:
        weights = {k: (0.9 if k == heavy else 0.1 / max(1, len(paths) - 1)) for k in weights}
    return Demand(per_path=weights, n_hosts=100)


# --------------------------------------------------------------------------
# the checks
# --------------------------------------------------------------------------


def _declared(model: PathModel, flag: str) -> bool:
    return bool(getattr(model.capabilities, flag, False))


def _shape_checks(model: PathModel, topo: TopologySnapshot, out: list[Check]) -> bool:
    """The contract itself. Returns False when nothing further is worth asking."""
    paths = list(topo.paths)
    try:
        model.reset(topo, seed=0)
    except Exception as exc:  # noqa: BLE001 -- the whole point is to catch it here
        out.append(Check("reset", BROKEN, f"raised {type(exc).__name__}: {exc}"))
        return False
    out.append(Check("reset", OK, "accepted a snapshot and a seed"))

    try:
        model.observe([], topo)
        model.observe(_observations(topo, 1.0, 1.0), topo)
    except Exception as exc:  # noqa: BLE001
        out.append(Check("observe", BROKEN, f"raised {type(exc).__name__}: {exc}"))
        return False
    out.append(Check("observe", OK, "took an empty sequence and a full one"))

    try:
        predicted = model.predict(topo, paths, horizon_s=0.0, demand=None)
    except Exception as exc:  # noqa: BLE001
        out.append(
            Check(
                "predict",
                BROKEN,
                f"raised {type(exc).__name__}: {exc} -- demand=None must be tolerated",
            )
        )
        return False
    if not isinstance(predicted, Mapping):
        out.append(Check("predict", BROKEN, f"returned {type(predicted).__name__}, not a mapping"))
        return False
    missing = [p.path_id for p in paths if p.path_id not in predicted]
    wrong = [k for k, v in predicted.items() if not isinstance(v, Prediction)]
    if wrong:
        out.append(Check("predict", BROKEN, f"values are not Prediction: {wrong[:3]}"))
        return False
    if missing:
        out.append(
            Check(
                "predict",
                CONTRADICTED,
                f"no prediction for {len(missing)} of {len(paths)} offered paths "
                f"({missing[:3]}); a sweep scores only what it gets",
            )
        )
    else:
        out.append(Check("predict", OK, f"a Prediction for each of {len(paths)} paths"))

    try:
        advisory = model.advise(topo, paths, SLA(), n_hosts=100)
    except Exception as exc:  # noqa: BLE001
        out.append(Check("advise", BROKEN, f"raised {type(exc).__name__}: {exc}"))
        return False
    if not isinstance(advisory, Advisory):
        out.append(Check("advise", BROKEN, f"returned {type(advisory).__name__}, not an Advisory"))
        return False
    offered = {p.path_id for p in paths}
    stray = [k for k in advisory.weights if k not in offered]
    if stray:
        out.append(
            Check(
                "advise",
                CONTRADICTED,
                f"weights name {len(stray)} path(s) that were not offered ({stray[:3]}); "
                "the harness drops those, so the advice published is not the advice given",
            )
        )
    elif not advisory.weights:
        out.append(Check("advise", CONTRADICTED, "returned no weights at all"))
    else:
        out.append(Check("advise", OK, f"weights over {len(advisory.weights)} offered paths"))
    return True


def _declaration_checks(model: PathModel, topo: TopologySnapshot, out: list[Check]) -> None:
    paths = list(topo.paths)

    # -- distributional -----------------------------------------------------
    predicted = model.predict(topo, paths, horizon_s=0.0, demand=None)
    sample = next(iter(predicted.values()), None)
    if sample is None:
        out.append(Check("distributional", UNCHECKED, "nothing was predicted to look at"))
    else:
        actual = sample.latency_ms.is_distributional
        declared = _declared(model, "distributional")
        if declared and not actual:
            out.append(
                Check(
                    "distributional",
                    CONTRADICTED,
                    "declared, but latency came back as a point estimate; the accuracy "
                    "family will score coverage against nothing",
                )
            )
        elif actual and not declared:
            out.append(
                Check("distributional", OK, "emits quantiles, and honestly does not claim to")
            )
        else:
            out.append(Check("distributional", OK, "declaration matches what came back"))

    # -- reports_confidence -------------------------------------------------
    if sample is not None:
        has = sample.confidence is not None
        if _declared(model, "reports_confidence") and not has:
            out.append(Check("reports_confidence", CONTRADICTED, "declared, but confidence=None"))
        else:
            out.append(Check("reports_confidence", OK, f"confidence {'set' if has else 'absent'}"))

    # -- demand_conditioned and monotone_in_demand --------------------------
    light = model.predict(topo, paths, horizon_s=0.0, demand=_demand(paths))
    heavy = model.predict(topo, paths, horizon_s=0.0, demand=_demand(paths, heavy=paths[0].path_id))
    target = paths[0].path_id
    moved = target in light and target in heavy and light[target].cost() != heavy[target].cost()
    if _declared(model, "demand_conditioned") and not moved:
        out.append(
            Check(
                "demand_conditioned",
                CONTRADICTED,
                "declared, but the prediction for a path did not move when 90% of the "
                "demand was put on it",
            )
        )
    elif moved:
        out.append(Check("demand_conditioned", OK, "the prediction moved with the demand"))
    else:
        out.append(Check("demand_conditioned", OK, "does not condition, and does not claim to"))

    if _declared(model, "monotone_in_demand"):
        if not moved:
            out.append(Check("monotone_in_demand", UNCHECKED, "the prediction did not move at all"))
        elif heavy[target].cost() < light[target].cost():
            out.append(
                Check(
                    "monotone_in_demand",
                    CONTRADICTED,
                    "predicted cost *fell* when demand on the path rose",
                )
            )
        else:
            out.append(Check("monotone_in_demand", OK, "cost did not fall as demand rose"))

    # -- emits_assignment ---------------------------------------------------
    advisory = model.advise(topo, paths, SLA(), n_hosts=100)
    # Not "entropy > 0". A softmax whose temperature is small next to the cost
    # spread returns weights like 1e-40, which is strictly non-zero and is a
    # ranking in every way that matters: the hosts round it to one path. This
    # was a false pass on the first run, against the worked template itself.
    concentrated = advisory.max_weight >= ONE_HOT
    detail = f"top weight {advisory.max_weight:.3f}, normalised entropy {advisory.normalised_entropy:.2f}"
    if _declared(model, "emits_assignment") and concentrated:
        out.append(
            Check(
                "emits_assignment",
                CONTRADICTED,
                f"declared, but {detail} -- that is a ranking, and the hosts will "
                "treat it as one however it was computed",
            )
        )
    else:
        out.append(Check("emits_assignment", OK, detail))

    # -- unseen interfaces and paths ----------------------------------------
    wider = synthetic_topology(extra=True)
    novel = [p for p in wider.paths if p.path_id == "p4"]
    try:
        model.predict(wider, novel, horizon_s=0.0, demand=None)
    except Exception as exc:  # noqa: BLE001
        state = CONTRADICTED if _declared(model, "handles_unseen_interfaces") else OK
        out.append(
            Check(
                "handles_unseen_interfaces",
                state,
                f"raised {type(exc).__name__} on an interface absent at reset",
            )
        )
    else:
        out.append(Check("handles_unseen_interfaces", OK, "scored a path over a new interface"))

    recombined = [PathRef("pX", SRC, DST, ("if2", "if5"), expiry_s=600.0)]
    try:
        model.predict(topo, recombined, horizon_s=0.0, demand=None)
    except Exception as exc:  # noqa: BLE001
        state = CONTRADICTED if _declared(model, "composes_unseen_paths") else OK
        out.append(
            Check(
                "composes_unseen_paths",
                state,
                f"raised {type(exc).__name__} on a new combination of seen interfaces",
            )
        )
    else:
        out.append(Check("composes_unseen_paths", OK, "scored a new combination of seen hops"))

    # -- reset clears -------------------------------------------------------
    #
    # Two questions, and only the second can be answered here with confidence.
    # "Observing did not move the output" does not prove nothing was kept: a
    # model that needs more evidence before it stops returning a prior is
    # indistinguishable, at this scale, from one that keeps nothing. So that
    # case is UNCHECKED. What *is* provable is the sharper and more damaging
    # bug: observing moved the output and reset() did not put it back, which
    # leaks one episode into the next and looks like a model that learns.
    if _declared(model, "stateful"):
        before = model.predict(topo, paths, horizon_s=0.0, demand=None)
        model.observe(_observations(topo, 2.0, 9.0), topo)
        dirty = model.predict(topo, paths, horizon_s=0.0, demand=None)
        moved = any(abs(dirty[k].cost() - before[k].cost()) > 1e-9 for k in dirty if k in before)
        if not moved:
            out.append(
                Check(
                    "stateful",
                    UNCHECKED,
                    "nine times the observed latency did not move the prediction here, so a "
                    "model that keeps nothing and one that has not learned enough to show it "
                    "cannot be told apart without a run",
                )
            )
        else:
            model.reset(topo, seed=0)
            clean = model.predict(topo, paths, horizon_s=0.0, demand=None)
            leaked = all(abs(dirty[k].cost() - clean[k].cost()) < 1e-9 for k in dirty if k in clean)
            if leaked:
                out.append(
                    Check(
                        "stateful",
                        CONTRADICTED,
                        "observe() moved the prediction and reset() did not put it back; a "
                        "sweep reuses one object over many worlds, so state that survives a "
                        "reset leaks one episode into the next and reads as a model that learns",
                    )
                )
            else:
                out.append(Check("stateful", OK, "observe() moved it and reset() cleared it"))

    # -- uses_tools ---------------------------------------------------------
    if _declared(model, "uses_tools"):
        if callable(getattr(model, "act", None)):
            out.append(Check("uses_tools", UNCHECKED, "act() exists; what it does needs a session"))
        else:
            out.append(
                Check(
                    "uses_tools",
                    CONTRADICTED,
                    "declared, but there is no act(); a sweep under drive='auto' believes "
                    "this and drive='agentic' refuses the cell outright (ADR 0019)",
                )
            )

    for flag, why in NEEDS_SUBSTRATE.items():
        if flag in {c.name for c in out}:
            continue
        if _declared(model, flag):
            out.append(Check(flag, UNCHECKED, why))


def check_model(model: PathModel, spec: str = "") -> Precheck:
    """Put an already-constructed model through the contract.

    Split from :func:`precheck` because loading and checking are different
    failures with different fixes, and because a caller that already has the
    object -- a test, or an interface that loaded it once to show its
    declarations -- should not have to hand back a string to have it rebuilt.
    """
    caps = getattr(model, "capabilities", None)
    report = Precheck(
        spec=spec or type(model).__qualname__,
        name=getattr(caps, "name", "") or type(model).__name__,
        architecture=getattr(caps, "architecture", ""),
    )
    topo = synthetic_topology()
    if _shape_checks(model, topo, report.checks):
        try:
            _declaration_checks(model, topo, report.checks)
        except Exception as exc:  # noqa: BLE001 -- a model that breaks mid-check is a finding
            report.checks.append(
                Check("declarations", BROKEN, f"raised {type(exc).__name__}: {exc}")
            )
    return report


def precheck(spec: str, args: Mapping[str, Any] | None = None) -> Precheck:
    """Load ``spec`` and put it through the contract. Never raises for a model."""
    from .loading import ModelLoadError, load_model

    try:
        model = load_model(spec, args=dict(args or {}))
    except ModelLoadError as exc:
        return Precheck(spec=spec, load_error=str(exc))
    return check_model(model, spec)
