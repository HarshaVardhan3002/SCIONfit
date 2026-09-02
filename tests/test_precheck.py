"""The pre-flight, and the one thing it must never do (ADR 0020).

It must never report a pass over something it did not check. That is the whole
reason it has three states, and most of what is tested here is that the third
one survives -- in the check, in the summary line, and in the exit code.

Two of these name bugs the module found the first time it ran, on shipped code:
a baseline declaring ``stateful`` by inheriting a dataclass default, and the
worked template declaring ``emits_assignment`` while returning a softmax so
concentrated it was a ranking.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from scionarena.cli import main as arena_main
from scionarena.exposure.contracts import (
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
from scionarena.exposure.precheck import (
    BROKEN,
    CONTRADICTED,
    OK,
    UNCHECKED,
    Check,
    check_model,
    precheck,
    synthetic_topology,
)


class Fine:
    """A minimal, honest model. Everything it declares, it does."""

    def __init__(self) -> None:
        self.capabilities = Capabilities(
            name="Fine", architecture="toy", emits_assignment=True, stateful=True
        )
        self.seen: dict[str, float] = {}

    def reset(self, topo: TopologySnapshot, seed: int = 0) -> None:
        self.seen.clear()

    def observe(self, obs: Sequence[Observation], topo: TopologySnapshot) -> None:
        for o in obs:
            if o.latency_ms is not None:
                self.seen[o.path_id] = o.latency_ms

    def predict(
        self,
        topo: TopologySnapshot,
        paths: Sequence[PathRef],
        horizon_s: float = 0.0,
        demand: Demand | None = None,
    ) -> Mapping[str, Prediction]:
        return {
            p.path_id: Prediction(
                latency_ms=Dist.point_estimate(self.seen.get(p.path_id, 10.0 * p.hop_count)),
                throughput_mbps=Dist.point_estimate(100.0),
                loss=Dist.point_estimate(0.0),
            )
            for p in paths
        }

    def advise(
        self, topo: TopologySnapshot, paths: Sequence[PathRef], sla: SLA, n_hosts: int = 1
    ) -> Advisory:
        share = 1.0 / max(1, len(paths))
        return Advisory(weights={p.path_id: share for p in paths}, reason="uniform")


def _report(cls: type) -> Any:
    return check_model(cls())


# --------------------------------------------------------------------------
# the three states


def test_an_honest_model_passes_and_nothing_is_unchecked_that_need_not_be() -> None:
    report = _report(Fine)
    assert report.blocking == []
    assert report.by_state(CONTRADICTED) == []
    assert {c.name for c in report.by_state(OK)} >= {"reset", "observe", "predict", "advise"}


def test_a_declaration_needing_a_world_is_unchecked_rather_than_passed() -> None:
    """The decision this module exists for. A cheap check with two states has to
    guess, and guessing pass sends a broken adaptor into an hour of compute with
    a tick behind it. Guessing fail is the mistake DECLARED_ABSENT avoids."""

    class Claims(Fine):
        def __init__(self) -> None:
            super().__init__()
            self.capabilities = Capabilities(
                name="Claims", staleness_aware=True, self_consistent=True, emits_assignment=True
            )

    report = _report(Claims)
    unchecked = {c.name for c in report.by_state(UNCHECKED)}
    assert {"staleness_aware", "self_consistent"} <= unchecked
    assert not any(c.name in unchecked for c in report.by_state(OK))
    assert "unchecked" in report.summary()


def test_the_summary_counts_the_states_apart() -> None:
    """A single number would be rebuilt in the reader's head as pass/fail, and
    then unchecked would silently become one or the other."""
    text = _report(Fine).summary()
    for state in (OK, CONTRADICTED, BROKEN, UNCHECKED):
        assert state in text


# --------------------------------------------------------------------------
# what it catches


def test_a_prediction_returned_as_a_list_is_caught_here_not_in_a_sweep() -> None:
    """The most common adaptor bug, and the one that fails silently: the harness
    keys truth on the path id, so a list joins against nothing and every
    accuracy metric reports None while the run looks healthy."""

    class Listy(Fine):
        def predict(self, topo, paths, horizon_s=0.0, demand=None):  # type: ignore[no-untyped-def]
            return [Prediction(Dist.point_estimate(1.0), Dist(), Dist()) for _ in paths]

    report = _report(Listy)
    assert [c.name for c in report.blocking] == ["predict"]
    assert "not a mapping" in report.blocking[0].detail


def test_a_model_that_raises_on_demand_none_is_caught() -> None:
    """``predict`` must tolerate ``demand=None``; the contract says so and the
    fixed cycle calls it that way on every round."""

    class Fussy(Fine):
        def predict(self, topo, paths, horizon_s=0.0, demand=None):  # type: ignore[no-untyped-def]
            if demand is None:
                raise ValueError("I need demand")
            return super().predict(topo, paths, horizon_s, demand)

    report = _report(Fussy)
    assert report.blocking and report.blocking[0].name == "predict"
    assert "demand=None must be tolerated" in report.blocking[0].detail


def test_weights_over_paths_that_were_not_offered_are_named() -> None:
    """The harness drops them, so the advice published is not the advice given,
    and nothing in a sweep would have said so."""

    class Stray(Fine):
        def advise(self, topo, paths, sla, n_hosts=1):  # type: ignore[no-untyped-def]
            return Advisory(weights={"not-a-path": 1.0}, reason="wrong keys")

    report = _report(Stray)
    bad = [c for c in report.by_state(CONTRADICTED) if c.name == "advise"]
    assert bad and "not offered" in bad[0].detail


def test_declaring_uses_tools_without_act_is_a_contradiction() -> None:
    """It stopped being harmless when the drive started believing it: under
    ``drive='auto'`` a sweep now takes a different code path on the strength of
    this declaration, and under ``drive='agentic'`` it refuses the cell."""

    class Pretender(Fine):
        def __init__(self) -> None:
            super().__init__()
            self.capabilities = Capabilities(name="Pretender", uses_tools=True)

    report = _report(Pretender)
    bad = [c for c in report.by_state(CONTRADICTED) if c.name == "uses_tools"]
    assert bad and "no act()" in bad[0].detail


def test_an_almost_one_hot_softmax_is_a_ranking() -> None:
    """Names the bug the worked template shipped with: an absolute temperature
    against a cost spread of hundreds gives weights like 1e-130 -- strictly
    non-zero, so an entropy>0 test passed it, and a ranking in every way that
    matters because the hosts round it to one path."""

    class Peaked(Fine):
        def advise(self, topo, paths, sla, n_hosts=1):  # type: ignore[no-untyped-def]
            ids = [p.path_id for p in paths]
            weights = {k: (1e-40 if i else 1.0) for i, k in enumerate(ids)}
            return Advisory(weights=weights, reason="numerically one-hot")

    report = _report(Peaked)
    bad = [c for c in report.by_state(CONTRADICTED) if c.name == "emits_assignment"]
    assert bad and "ranking" in bad[0].detail


def test_a_stateless_model_that_claims_state_is_caught() -> None:
    """Names the bug found on shipped code the first time this ran:
    ``Capabilities.stateful`` defaults to True, so it is the one declaration a
    model can make by not thinking about it, and ``CapacityProportional`` --
    whose own docstring says it is deliberately blind -- was making it."""

    class Blind(Fine):
        def __init__(self) -> None:
            super().__init__()
            self.capabilities = Capabilities(name="Blind", stateful=True)

        def observe(self, obs, topo) -> None:  # type: ignore[no-untyped-def]
            pass

        def predict(self, topo, paths, horizon_s=0.0, demand=None):  # type: ignore[no-untyped-def]
            return {
                p.path_id: Prediction(Dist.point_estimate(float(p.hop_count)), Dist(), Dist())
                for p in paths
            }

    report = _report(Blind)
    bad = [c for c in report.by_state(CONTRADICTED) if c.name == "stateful"]
    assert bad and "reset()" in bad[0].detail


def test_the_shipped_baselines_declare_nothing_they_do_not_do() -> None:
    """The regression for the above: a false declaration on a mandatory baseline
    discredits the floor every other number in the report is measured against."""
    for alias in ("ema", "minrtt", "proportional", "reference", "prober"):
        report = precheck(alias)
        assert report.blocking == [], (alias, report.blocking)
        assert report.by_state(CONTRADICTED) == [], (alias, report.by_state(CONTRADICTED))


# --------------------------------------------------------------------------
# the worked template


def test_the_worked_template_passes_its_own_preflight() -> None:
    """It is documentation that runs, and documentation that runs rots unless
    something fails when it does."""
    report = precheck("./examples/adaptor_template.py:TemplateAdaptor")
    assert report.load_error == "", report.load_error
    assert report.blocking == []
    assert report.by_state(CONTRADICTED) == []
    assert report.architecture == "linear"


def test_the_template_takes_the_constructor_argument_it_documents() -> None:
    report = precheck("./examples/adaptor_template.py:TemplateAdaptor", {"temperature": 0.05})
    assert report.blocking == []


# --------------------------------------------------------------------------
# no substrate, and no verdict


def test_the_preflight_builds_no_world(monkeypatch: pytest.MonkeyPatch) -> None:
    """Its entire value is being seconds rather than minutes. A substrate import
    creeping in here would make it another way to spend an hour."""
    import scionarena.core.scenario as scenario_module

    def explode(self: Any) -> None:
        raise AssertionError("the pre-flight built a world")

    monkeypatch.setattr(scenario_module.Scenario, "build", explode)
    assert _report(Fine).blocking == []


def test_the_output_says_it_is_not_the_check_that_decides() -> None:
    """A pre-flight printing a verdict becomes the thing people quote, and it is
    not measuring what the probes measure."""
    text = "\n".join(_report(Fine).lines())
    assert "not a verdict" in text
    assert "scionfit check" in text


def test_a_topology_made_of_dataclasses_is_still_a_topology() -> None:
    topo = synthetic_topology(extra=True)
    assert len(topo.paths_for("1-ff00:0:1", "1-ff00:0:9")) == 4
    novel = [p for p in topo.paths if "if99" in p.interfaces]
    assert novel and novel[0].interfaces[0] in topo.interfaces


# --------------------------------------------------------------------------
# the command


def test_the_exit_code_separates_would_not_run_from_scored_badly(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """0 means nothing would break, not that the model is good. --strict is
    where a contradicted declaration also fails, and it is opt-in because an
    honest limitation is not a build failure."""
    assert arena_main(["adapt", "reference"]) == 0
    assert arena_main(["adapt", "nope.module:Missing"]) == 1
    capsys.readouterr()


def test_a_contradiction_fails_only_under_strict(capsys: pytest.CaptureFixture[str]) -> None:
    spec = f"{__name__}:Pretends"
    assert arena_main(["adapt", spec]) == 0
    assert arena_main(["adapt", spec, "--strict"]) == 1
    capsys.readouterr()


class Pretends(Fine):
    """Declares a tool user, implements none. Module-level so the CLI can load it."""

    def __init__(self) -> None:
        super().__init__()
        self.capabilities = Capabilities(name="Pretends", uses_tools=True)


def test_the_json_output_carries_every_state(capsys: pytest.CaptureFixture[str]) -> None:
    import json

    assert arena_main(["adapt", "prober", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["architecture"] == "stochastic"
    assert {c["state"] for c in payload["checks"]} >= {OK, UNCHECKED}


def test_a_check_knows_whether_it_is_bad() -> None:
    assert Check("x", BROKEN).bad
    assert Check("x", CONTRADICTED).bad
    assert not Check("x", UNCHECKED).bad
    assert not Check("x", OK).bad
