"""The PDF, and the honesty rules it exists to enforce.

Separate from ``test_report.py``, which covers the interactive HTML view. The
two artefacts are deliberately not generated from each other (ADR 0018), so
they are tested apart.

Every test here names the bug it prevents. Four of them are regressions for
mistakes this module actually made on its first run against a real sweep, and
they are the reason the file is worth its length: each one produced a page that
rendered cleanly and said something false.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scionarena.bench.report import (
    THIN_REPEATS,
    Reading,
    _conformance,
    _plain,
    _sig,
    _strata,
    _styles,
    _what_was_tested,
    build_pdf,
    gather,
    summary_lines,
)
from scionarena.bench.results import CellResult, write_result
from scionarena.instrument.figures import EXTRA_HINT, MissingReportDeps, available
from scionarena.instrument.metrics import FAMILIES, REGISTRY

pytest.importorskip("matplotlib", reason="the report needs the [report] extra")
pytest.importorskip("reportlab", reason="the report needs the [report] extra")


# --------------------------------------------------------------------------
# readings: how many digits a number has earned
# --------------------------------------------------------------------------


def test_a_value_from_zero_observations_is_not_a_score() -> None:
    """A run whose warmup ate the whole episode reported swing 0.0 over zero
    samples, and a report that printed ``0`` for it would rank the model as the
    most stable in the suite. Found on the first real sweep, at cycles=40."""
    reading = Reading(metric="swing", value=0.0, n=3, lo=0.0, hi=0.0, support=0.0)
    assert not reading.measured
    assert reading.render() == "no observations"


def test_a_metric_that_was_never_computed_is_not_zero() -> None:
    assert Reading(metric="coverage.h60").render() == "not measured"


def test_a_thin_reading_is_not_printed_to_four_figures() -> None:
    """Two repeats of a metric measured bimodal across seeds is not a number to
    four decimal places, and printing it as one is the specific dishonesty this
    whole mechanism exists to prevent."""
    thin = Reading(metric="swing", value=0.258134, n=2, lo=0.18, hi=0.39, support=100.0)
    assert thin.thin
    rendered = thin.render()
    assert rendered.startswith("0.26")
    assert "n=2" in rendered and "†" in rendered

    fat = Reading(metric="swing", value=0.258134, n=THIN_REPEATS, lo=0.18, hi=0.39, support=100.0)
    assert not fat.thin
    assert fat.render().startswith("0.2581")


def test_unknown_support_is_thin_rather_than_fine() -> None:
    """A cell recorded before the support metrics existed knows nothing about
    how much is behind its numbers. Unknown support is not good support."""
    assert Reading(metric="swing", value=1.0, n=5, lo=1.0, hi=1.0, support=None).thin


def test_infinity_stays_a_word() -> None:
    """``convergence_s`` returns inf for "never settled", and folding that into
    a large number makes it indistinguishable from "settled very late"."""
    assert _sig(float("inf"), 4) == "inf"
    assert _sig(float("-inf"), 4) == "-inf"
    assert _sig(float("nan"), 4) == "nan"


# --------------------------------------------------------------------------
# columns: what a stratified metric looks like on a page
# --------------------------------------------------------------------------


def test_horizons_sort_numerically_not_lexicographically() -> None:
    """The first render put the columns in the order h0, h300, h60, which
    invites exactly the misreading that stratifying by horizon exists to
    prevent."""
    assert _strata(["coverage.h300", "coverage.h0", "coverage.h60"]) == [
        "coverage.h0",
        "coverage.h60",
        "coverage.h300",
    ]


def test_the_bare_name_is_dropped_when_strata_exist() -> None:
    """A metric returning a mapping for one model and ``None`` for another lands
    under both its bare name and its stratified ones, and the first render gave
    every accuracy table a leading column of "not measured" headed ``value``."""
    assert _strata(["coverage", "coverage.h0", "coverage.h60"]) == [
        "coverage.h0",
        "coverage.h60",
    ]
    assert _strata(["swing"]) == ["swing"]


def test_registry_markup_does_not_reach_the_page() -> None:
    """``regret_ms``'s docstring says ``**An upper bound.**`` because it is read
    in an editor. On a page that is leaked markup, not emphasis."""
    assert _plain("Mean cost. **An upper bound.** Read beside ``coverage``.") == (
        "Mean cost. An upper bound. Read beside coverage."
    )


# --------------------------------------------------------------------------
# support: the denominator has to match what the numerator used
# --------------------------------------------------------------------------


def test_every_family_has_a_support_metric() -> None:
    """A family without one has no denominator, so every number in it renders as
    thin -- which is correct, and is a reason to notice rather than to ship."""
    from scionarena.bench.report import SUPPORT_OF

    for family in FAMILIES:
        name = SUPPORT_OF[family]
        assert REGISTRY[name].support, f"{name} is the {family} denominator but is not flagged"
        assert REGISTRY[name].family == family


def test_a_support_count_cannot_exceed_what_its_family_measured() -> None:
    """``n_cost_samples`` reported forty samples behind a regret of ``None``:
    it counted the finite pairs while every metric in its family discards the
    warmup first. A denominator that disagrees with its numerator is worse than
    no denominator."""
    import numpy as np

    from scionarena.instrument.metrics import MetricInput, n_cost_samples, regret_ms
    from scionarena.instrument.sampler import Series

    series = Series(interval_s=1.0)
    for index in range(40):
        series.times.append(float(index))
        series.mean_cost_ms.append(100.0 + index)
        series.best_cost_ms.append(90.0 + index)
    data = MetricInput(series=series, cadence_s=30.0, sample_s=1.0)

    warm = int(data.band["warmup"])
    support = n_cost_samples(data)
    assert support is not None
    assert support == float(max(0, 40 - warm))
    if support == 0.0:
        assert regret_ms(data) is None
    else:
        assert regret_ms(data) is not None
        assert np.isfinite(float(regret_ms(data) or 0.0))


# --------------------------------------------------------------------------
# gathering: what the report is allowed to say
# --------------------------------------------------------------------------


def _cell(**kw: Any) -> CellResult:
    base = dict(
        cell_id="",
        suite="t",
        suite_digest="dddd",
        model="pkg:M",
        label="M",
        mandatory=False,
        axes={
            "population": "1k",
            "defectors": "none",
            "discipline": "none",
            "paths": "all",
            "staleness": "fresh",
            "probes": "audited",
        },
        repeat=0,
        seed=1,
        scenario="t/x",
        tier="smoke",
    )
    base.update(kw)
    axes = dict(base["axes"])  # type: ignore[arg-type]
    base["cell_id"] = f"{base['label']}-{sorted(axes.items())}-{base['repeat']}"
    return CellResult(**base)  # type: ignore[arg-type]


def _sweep(directory: Path) -> None:
    for repeat in range(3):
        write_result(
            directory,
            _cell(
                repeat=repeat,
                metrics={"swing": 0.2 + 0.01 * repeat, "n_samples": 400.0},
            ),
        )
        write_result(
            directory,
            _cell(
                label="Base",
                mandatory=True,
                model="pkg:B",
                repeat=repeat,
                capabilities={"name": "Base", "distributional": False, "stateful": True},
                metrics={"swing": 2.0 + repeat, "n_samples": 400.0},
            ),
        )
    for repeat in range(3):
        write_result(
            directory,
            _cell(
                axes={
                    "population": "1k",
                    "defectors": "30pct",
                    "discipline": "none",
                    "paths": "all",
                    "staleness": "fresh",
                    "probes": "audited",
                },
                repeat=repeat,
                metrics={"swing": 0.9, "n_samples": 400.0},
            ),
        )
    write_result(directory, _cell(repeat=9, error="RuntimeError: no multi-path scope"))


def test_an_unswept_axis_is_not_counted_as_swept(tmp_path: Path) -> None:
    """Every cell carries all six axis labels, so counting the keys said "six
    axes swept" for a suite that moved one."""
    _sweep(tmp_path)
    data = gather(list(_load(tmp_path)))
    swept = [a for a, labels in data.axes_seen.items() if len(labels) > 1]
    assert swept == ["defectors"]
    assert "1 axes swept and 5 held" in " ".join(summary_lines(data))


def test_a_failed_cell_is_a_row_and_not_a_hole(tmp_path: Path) -> None:
    _sweep(tmp_path)
    data = gather(list(_load(tmp_path)))
    assert len(data.failures) == 1
    assert "no multi-path scope" in data.failures[0][2]


def test_the_tier_comes_from_the_cell_not_from_the_suite(tmp_path: Path) -> None:
    """Reading it off the suite object at report time prints the *current*
    tier against an older cell: internally consistent and false."""
    _sweep(tmp_path)
    write_result(tmp_path, _cell(repeat=7, tier="realistic", metrics={"swing": 0.1}))
    data = gather(list(_load(tmp_path)))
    assert data.tier == "realistic/smoke"


def test_a_cell_recorded_before_tier_existed_says_unrecorded(tmp_path: Path) -> None:
    write_result(tmp_path, _cell(tier="", metrics={"swing": 0.1}))
    assert gather(list(_load(tmp_path))).tier == "unrecorded"


def test_cells_from_another_suite_are_excluded_and_named(tmp_path: Path) -> None:
    """Dropping them silently reports a subset; a directory holding two suites
    is usually a merge that should not have happened."""
    _sweep(tmp_path)
    write_result(tmp_path, _cell(suite_digest="eeee", repeat=5, metrics={"swing": 9.0}))
    data = gather(list(_load(tmp_path)))
    assert data.stale == ["eeee"]
    assert any("eeee" in line for line in summary_lines(data))


def _load(directory: Path) -> Any:
    from scionarena.bench.results import load_results

    return load_results(directory)


# --------------------------------------------------------------------------
# the document
# --------------------------------------------------------------------------


def _text(story: list[Any]) -> str:
    out = []
    for item in story:
        getter = getattr(item, "getPlainText", None)
        if getter is not None:
            out.append(str(getter()))
        cells = getattr(item, "_cellvalues", None)
        if cells is not None:
            out.extend(str(cell) for row in cells for cell in row)
    return "\n".join(out)


def test_an_absent_conformance_card_is_stated_not_silent() -> None:
    """Silence in the conformance section reads as "nothing was wrong"."""
    story: list[Any] = []
    _conformance(story, _styles(), None)
    text = _text(story)
    assert "No conformance card was supplied" in text
    assert "scionfit check" in text


def test_declared_absent_survives_the_trip_to_the_page() -> None:
    """The one distinction conformance exists to draw is easiest to erase here,
    where a reader wants a green tick: an honest limitation is not a false
    claim."""
    card = {
        "model": {"name": "M", "version": "1"},
        "summary": {"verdict": "PARTIAL", "mean_score": 0.5, "closed_loop_ready": False},
        "results": [
            {
                "probe_id": "R5",
                "requirement": "distributional",
                "status": "DECLARED_ABSENT",
                "score": None,
                "finding": "declared absent",
            },
            {
                "probe_id": "R6",
                "requirement": "demand",
                "status": "FALSE_CLAIM",
                "score": 0.0,
                "finding": "claimed and contradicted",
            },
        ],
    }
    story: list[Any] = []
    _conformance(story, _styles(), card)
    text = _text(story)
    assert "DECLARED_ABSENT" in text
    assert "FALSE_CLAIM" in text
    assert "DECLARED_ABSENT is not FALSE_CLAIM" in text


def test_the_probe_limit_regime_is_named(tmp_path: Path) -> None:
    """Two runs made under different assumptions about the price of information
    are not the same experiment, and the report has to say which one it is."""
    _sweep(tmp_path)
    story: list[Any] = []
    _what_was_tested(story, _styles(), gather(list(_load(tmp_path))))
    text = _text(story)
    assert "probe-limit regime" in text
    assert "audited" in text


def test_a_pdf_is_a_pure_function_of_the_directory(tmp_path: Path) -> None:
    """No substrate, no model, no metric recomputed: a directory copied off a
    cluster renders on a machine where none of that is installed."""
    results = tmp_path / "results"
    _sweep(results)
    out = build_pdf(results, tmp_path / "report.pdf")
    body = out.read_bytes()
    assert body.startswith(b"%PDF")
    assert len(body) > 20_000, "a report with no figures in it is not this report"


def test_a_report_with_a_conformance_card_embeds_it(tmp_path: Path) -> None:
    results = tmp_path / "results"
    _sweep(results)
    card = tmp_path / "card.json"
    card.write_text(
        json.dumps(
            {
                "model": {"name": "M", "version": "1"},
                "summary": {"verdict": "CONFORMANT", "mean_score": 1.0, "closed_loop_ready": True},
                "results": [
                    {
                        "probe_id": "R1",
                        "requirement": "x",
                        "status": "PASS",
                        "score": 1.0,
                        "finding": "fine",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    out = build_pdf(results, tmp_path / "r.pdf", conformance=card)
    assert out.exists()


def test_an_empty_directory_is_an_error_not_an_empty_report(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no result files"):
        build_pdf(tmp_path, tmp_path / "r.pdf")


def test_the_missing_dependency_message_names_the_extra() -> None:
    """The failure a user without the extra hits, and the only place they find
    out what to install."""
    assert "scionarena[report]" in EXTRA_HINT
    assert issubclass(MissingReportDeps, RuntimeError)
    assert available() is True  # this test file is skipped otherwise
