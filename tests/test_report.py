"""Rendering. Mostly one question: does the page say what the numbers say."""

from __future__ import annotations

from scionarena.instrument.report import render_body, verdict_state


def test_a_verdict_has_three_states_not_two() -> None:
    assert verdict_state(True) == ("pass", "met")
    assert verdict_state(False) == ("fail", "NOT met")
    assert verdict_state(None) == ("noted", "noted")


def test_a_reported_only_row_does_not_render_as_a_failure() -> None:
    """Names the bug: ``ok=None`` fell through a truthiness test and read as fail.

    The row it broke is the overrun count, which since M4 is reported rather than
    gated -- so a run of a slow model would have looked like a broken harness.
    """
    body = render_body(
        [],
        [],
        verdicts=[
            {"criterion": "rounds finished inside the slot", "measured": "12/24", "ok": None}
        ],
    )
    assert "noted" in body
    assert "NOT met" not in body
    assert "'fail'" not in body


def test_a_failing_verdict_still_says_so() -> None:
    body = render_body([], [], verdicts=[{"criterion": "c", "measured": "m", "ok": False}])
    assert "NOT met" in body
