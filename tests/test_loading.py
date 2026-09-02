"""Loading a model the harness did not write (ADR 0013).

Two things are under test here and they fail in different ways. Loading is a
user-facing surface: every failure has to name the spec, the problem and the
fix, because the person reading it is integrating a model and has no reason to
know how this package is laid out. The capability table is a *duplicate* --
``exposure`` may not import ``conformance``, so the probe each flag gates is
written down twice -- and the test at the bottom is what keeps the copies equal.
"""

from __future__ import annotations

import dataclasses
import textwrap
from pathlib import Path

import pytest

from scionarena.conformance.probes import ALL_PROBES
from scionarena.demo import labels_for
from scionarena.exposure.contracts import Capabilities
from scionarena.exposure.loading import (
    BUILTIN_MODELS,
    CAPABILITY_NOTES,
    REQUIRED_METHODS,
    ModelLoadError,
    capability_report,
    load_model,
    resolve,
)

OUTSIDE_MODEL = '''
"""A model that knows nothing about this repository except the four methods."""

from scionarena.exposure.contracts import Capabilities


class Outsider:
    capabilities = Capabilities(name="Outsider", version="2.1.0", emits_assignment=True)

    def reset(self, topo, seed=0):
        self.seen = 0

    def observe(self, obs, topo):
        self.seen += len(obs)

    def predict(self, topo, paths, horizon_s=0.0, demand=None):
        flat = Prediction(Dist.point_estimate(10.0), Dist.point_estimate(100.0),
                          Dist.point_estimate(0.0))
        return {p.path_id: flat for p in paths}

    def advise(self, topo, paths, sla, n_hosts=1):
        share = 1.0 / max(1, len(paths))
        return Advisory(weights={p.path_id: share for p in paths})
'''


@pytest.fixture
def outside_model(tmp_path: Path) -> Path:
    path = tmp_path / "outsider.py"
    path.write_text(textwrap.dedent(OUTSIDE_MODEL), encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# the door opens


def test_a_model_in_a_file_nobody_packaged_loads_and_reports(outside_model: Path) -> None:
    """The whole point of Phase 1: a model from outside this repo can be run.

    Before ADR 0013 the only models the harness could reach were the four in
    ``REFERENCE_MODELS``, so there was no path by which an outsider's model
    entered at all.
    """
    spec = f"{outside_model}:Outsider"
    model = load_model(spec)

    assert model.capabilities.name == "Outsider"
    report = capability_report(model, spec)
    assert report.tested == ("R8",), "it declares emits_assignment and nothing else"
    assert "R5" in report.absent, "an undeclared capability is absent, not tested"


def test_each_load_builds_a_fresh_instance(outside_model: Path) -> None:
    """These models are stateful, so a comparison is only controlled if each run
    starts from a model that has seen nothing. ``demo``'s slow run relies on it."""
    spec = f"{outside_model}:Outsider"
    first, second = load_model(spec), load_model(spec)

    first.reset(topo=None, seed=0)  # type: ignore[arg-type]
    first.observe([1, 2, 3], topo=None)  # type: ignore[arg-type,list-item]
    second.reset(topo=None, seed=0)  # type: ignore[arg-type]

    assert first is not second
    assert first.seen == 3 and second.seen == 0  # type: ignore[attr-defined]


@pytest.mark.parametrize("name", sorted(BUILTIN_MODELS))
def test_a_builtin_name_is_an_alias_for_an_import_path(name: str) -> None:
    """One resolution rule, ours included -- there is no second lookup table."""
    target = resolve(name)
    assert ":" in target
    assert load_model(name).capabilities.name
    assert load_model(target).capabilities.name, "the alias and its target load the same way"


# --------------------------------------------------------------------------
# and every failure is a sentence


def test_a_mistyped_builtin_name_offers_the_name_it_meant() -> None:
    with pytest.raises(ModelLoadError) as caught:
        load_model("minrt")
    assert "minrtt" in str(caught.value)


def test_a_module_that_is_not_installed_says_how_to_install_it() -> None:
    with pytest.raises(ModelLoadError) as caught:
        load_model("definitely_not_installed.models:Thing")
    message = str(caught.value)
    assert "definitely_not_installed" in message
    assert "pip install" in message, "a missing package is a fixable problem; say the fix"


def test_a_mistyped_attribute_offers_the_names_that_module_has() -> None:
    with pytest.raises(ModelLoadError) as caught:
        load_model("scionarena.reference.models:MinRTTGreedyy")
    assert "MinRTTGreedy" in str(caught.value)


def test_a_constructor_that_needs_arguments_names_them() -> None:
    """``BudgetedProber`` needs the scopes it will advise. The old loader raised
    a bare ``TypeError`` from inside ``__init__`` and named nothing."""
    with pytest.raises(ModelLoadError) as caught:
        load_model("scionarena.reference.agents:BudgetedProber")
    assert "scopes" in str(caught.value)


def test_a_constructor_argument_can_be_supplied() -> None:
    model = load_model("scionarena.reference.agents:BudgetedProber", args={"scopes": []})
    assert model.capabilities.uses_tools


def test_a_missing_method_is_named_before_the_run_rather_than_during_it(tmp_path: Path) -> None:
    """A model missing ``advise`` used to fail several seconds into a scenario,
    from inside the driver, where the traceback blames the harness."""
    path = tmp_path / "half.py"
    path.write_text(
        textwrap.dedent("""
            from scionarena.exposure.contracts import Capabilities

            class HalfModel:
                capabilities = Capabilities(name="HalfModel")

                def reset(self, topo, seed=0): pass
                def observe(self, obs, topo): pass
        """),
        encoding="utf-8",
    )
    with pytest.raises(ModelLoadError) as caught:
        load_model(f"{path}:HalfModel")
    message = str(caught.value)
    assert "predict" in message and "advise" in message
    assert all(name in message for name in REQUIRED_METHODS)


def test_a_capabilities_object_missing_a_flag_is_refused(tmp_path: Path) -> None:
    """An absent flag reads as False everywhere it is consulted, so a model with
    a hand-rolled capabilities object silently loses probes it could have passed."""
    path = tmp_path / "partial.py"
    path.write_text(
        textwrap.dedent("""
            class Caps:
                name = "Partial"
                distributional = True

            class Partial:
                capabilities = Caps()

                def reset(self, topo, seed=0): pass
                def observe(self, obs, topo): pass
                def predict(self, topo, paths, horizon_s=0.0, demand=None): return {}
                def advise(self, topo, paths, sla, n_hosts=1): return None
        """),
        encoding="utf-8",
    )
    with pytest.raises(ModelLoadError) as caught:
        load_model(f"{path}:Partial")
    assert "staleness_aware" in str(caught.value)


def test_a_module_that_raises_on_import_blames_the_module(tmp_path: Path) -> None:
    path = tmp_path / "broken.py"
    path.write_text("raise RuntimeError('no configuration file')\n", encoding="utf-8")
    with pytest.raises(ModelLoadError) as caught:
        load_model(f"{path}:Anything")
    message = str(caught.value)
    assert "no configuration file" in message
    assert "before the harness touches the model" in message


def test_a_windows_absolute_path_is_not_split_at_the_drive_letter() -> None:
    """``C:/models/mine.py:MyModel`` split at the first colon gave module ``C``,
    and the error told the user to ``pip install C``. Every model loaded from an
    absolute path on Windows hit it, which is every model a Windows user drags
    into the field. The separator is the *last* colon."""
    from scionarena.exposure.loading import _split

    assert _split("s", "C:/models/mine.py:MyModel") == ("C:/models/mine.py", "MyModel")
    assert _split("s", "mypkg.mymodule:MyModel") == ("mypkg.mymodule", "MyModel")

    with pytest.raises(ModelLoadError, match="module:attribute"):
        _split("s", "C:/models/mine.py")


def test_a_nested_class_is_reachable_through_dots() -> None:
    from scionarena.exposure.loading import _split

    assert _split("s", "pkg.mod:Outer.Inner") == ("pkg.mod", "Outer.Inner")


def test_a_file_that_is_not_there_says_where_it_looked() -> None:
    with pytest.raises(ModelLoadError) as caught:
        load_model("./nowhere/at/all.py:Model")
    assert "no file at" in str(caught.value)


# --------------------------------------------------------------------------
# what the harness tells the user before it runs anything


def test_the_report_separates_an_honest_absence_from_a_test() -> None:
    report = capability_report(load_model("minrtt"), "minrtt")
    outcomes = {line.flag: line.outcome for line in report.lines}

    assert outcomes["distributional"] == "DECLARED_ABSENT", "MinRTTGreedy is a point estimator"
    assert outcomes["handles_unseen_interfaces"] == "tested"
    assert set(report.tested) & set(report.absent) == set(), "a probe is in exactly one list"
    assert "FALSE_CLAIM" in report.to_terminal(), "the asymmetry is the thing worth saying"


def test_two_models_declaring_the_same_name_stay_two_lines() -> None:
    """``sections`` keys its series on the label, so a collision draws two runs
    as one line and the figure quietly loses a model."""
    twin = Capabilities(name="Twin")
    models = [type("A", (), {"capabilities": twin})(), type("B", (), {"capabilities": twin})()]
    labels = labels_for(["pkg:A", "pkg:B"], models)

    assert len(set(labels)) == 2
    assert all("Twin" in label for label in labels)

    solo = labels_for(["pkg:A"], [models[0]])
    assert solo == ["Twin"], "no collision, no decoration"


# --------------------------------------------------------------------------
# the duplicated table


def test_the_capability_table_still_agrees_with_the_probe_registry() -> None:
    """``exposure`` may not import ``conformance``, so the flag-to-probe mapping
    is written down in both places. This is the only thing keeping them equal:
    a probe that gains, loses or renames its capability fails here.
    """
    from_probes = {
        probe.capability: probe.probe_id for probe in ALL_PROBES if probe.capability is not None
    }
    from_table = {flag: probe for flag, (probe, _) in CAPABILITY_NOTES.items() if probe}

    assert from_table == from_probes, (
        "CAPABILITY_NOTES in exposure/loading.py disagrees with ALL_PROBES; "
        "update the table to match the probes"
    )


def test_every_flag_a_model_can_declare_is_described() -> None:
    """A flag missing from the table is a declaration the user is never shown."""
    flags = {
        field.name
        for field in dataclasses.fields(Capabilities)
        if field.type in ("bool", bool)  # a string, under `from __future__ import annotations`
    }
    assert flags == set(CAPABILITY_NOTES), "add the new flag to CAPABILITY_NOTES"


def test_the_ui_refuses_an_unloadable_model_before_it_starts_a_job() -> None:
    """Otherwise the failure arrives from the worker thread, several seconds in,
    as a traceback in a box rather than as a sentence in the form."""
    from scionarena.ui import _params

    with pytest.raises(ModelLoadError):
        _params({"models": "minrtt,not.a.module:Model"})

    assert _params({"models": "minrtt"})["models"] == ["minrtt"]
