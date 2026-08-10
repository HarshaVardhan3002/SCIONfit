"""Invariant 4: determinism from a seed.

The trace hash exists to fail loudly the first time the substrate acquires a
source of nondeterminism. It has to be here before M1 adds any state, because
after that the question "was this always different?" is unanswerable.
"""

import math
import os
import subprocess
import sys
import textwrap

import pytest

from scionarena import check
from scionarena.core.trace import TraceHash, canonical
from scionarena.reference import REFERENCE_MODELS

# Pinned. If a change moves this, it changed the substrate's behaviour, and the
# PR must say which behaviour and why. Regenerate with:
#   python -m pytest tests/test_trace.py -k pinned -q
CONFORMANCE_TRACE_HASH = "29d940a70777"


def portable(score: float | None) -> float | None:
    """A probe score, rounded to where it is portable between interpreters.

    Probe scores are Python-level floats -- means over per-path costs, added up
    with the builtin ``sum``, which uses Neumaier compensation from CPython 3.12
    and naive addition before it. The two disagree in the last digits: R1 scores
    0.813911544591865 on 3.11 and 0.8139115445918653 on 3.12. Hashing the
    unrounded value therefore pins the interpreter alongside the behaviour, which
    is what turned the whole CI matrix red while every model behaved identically.

    Nine digits is orders of magnitude looser than a summation strategy and
    orders of magnitude tighter than any change in what a probe measures.

    The substrate's own digest needs none of this and does not get it: it is
    numpy arithmetic throughout and comes out identical on both interpreters,
    checked directly rather than assumed
    (``test_performance.py::test_the_same_scenario_hashes_the_same_in_another_process``
    covers the same-interpreter case).
    """
    return None if score is None else round(score, 9)


def conformance_trace_hash(seed: int = 0, repeats: int = 3) -> str:
    """Fold every reference model's report card into one digest."""
    h = TraceHash(label="conformance-v0.1")
    for name, factory in sorted(REFERENCE_MODELS.items()):
        card = check(factory(), seed=seed, repeats=repeats)
        h.update({"model": name, "verdict": card.verdict})
        h.extend(
            {"probe": r.probe_id, "status": r.status.name, "score": portable(r.score)}
            for r in card.results
        )
    return h.short()


# --------------------------------------------------------------------------
# canonical form
# --------------------------------------------------------------------------


def test_mapping_key_order_does_not_change_the_hash():
    """Dict iteration order is an implementation detail, not a trace event."""
    assert canonical({"a": 1, "b": 2}) == canonical({"b": 2, "a": 1})


def test_event_order_does_change_the_hash():
    """A trace is a sequence. Two events swapped is a different run."""
    first = TraceHash().update("a").update("b").hexdigest()
    second = TraceHash().update("b").update("a").hexdigest()
    assert first != second


def test_adjacent_fields_cannot_merge():
    """Without a separator, ("ab", "c") and ("a", "bc") would collide."""
    assert (
        TraceHash().extend(["ab", "c"]).hexdigest() != TraceHash().extend(["a", "bc"]).hexdigest()
    )


def test_signed_zero_folds():
    """-0.0 and 0.0 are the same measurement and must hash the same."""
    assert canonical(-0.0) == canonical(0.0)


def test_every_nan_is_the_same_nan():
    """NaN != NaN, so hashing its repr would make 'not measured' unhashable."""
    assert canonical(float("nan")) == canonical(math.nan)


def test_dataclass_hashes_by_field_not_by_identity():
    from scionarena.exposure.contracts import PathRef

    a = PathRef(path_id="p", src="1-a", dst="1-b", interfaces=("i1", "i2"))
    b = PathRef(path_id="p", src="1-a", dst="1-b", interfaces=("i1", "i2"))
    assert canonical(a) == canonical(b)


def test_unrenderable_object_raises_rather_than_hashing_its_address():
    """The failure mode this guards: a repr with 0x7f... in it hashes fine and
    is different on every run."""
    with pytest.raises(TypeError, match="no canonical form"):
        canonical(object())


def test_label_separates_traces():
    assert TraceHash(label="a").update(1).hexdigest() != TraceHash(label="b").update(1).hexdigest()


def test_count_tracks_events():
    h = TraceHash().extend(range(5))
    assert h.count == 5


# --------------------------------------------------------------------------
# the tripwire itself
# --------------------------------------------------------------------------


def test_same_seed_gives_the_same_hash_twice():
    assert conformance_trace_hash() == conformance_trace_hash()


def test_pinned_conformance_trace_hash():
    assert conformance_trace_hash() == CONFORMANCE_TRACE_HASH


def test_different_seed_gives_a_different_hash():
    """A hash that ignores the seed would pass every other test in this file."""
    assert conformance_trace_hash(seed=1) != conformance_trace_hash(seed=0)


def test_hash_is_stable_across_processes():
    """Names the hazard: ``hash(str)`` is salted per process (PYTHONHASHSEED).
    Anything in the substrate that seeds an RNG from it produces a different
    trace on every run while looking deterministic within one."""
    script = textwrap.dedent(
        """
        import sys
        sys.path.insert(0, %r)
        from test_trace import conformance_trace_hash
        print(conformance_trace_hash())
        """
    ) % os.path.dirname(os.path.abspath(__file__))

    digests = []
    for hash_seed in ("1", "2"):
        env = {**os.environ, "PYTHONHASHSEED": hash_seed}
        out = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            env=env,
            check=True,
        )
        digests.append(out.stdout.strip())

    assert digests[0] == digests[1] == CONFORMANCE_TRACE_HASH


def test_the_pinned_hash_ignores_last_bit_float_noise_and_nothing_larger():
    """Names the bug: the pinned card hash was interpreter-dependent.

    CPython 3.12 sums floats by Neumaier compensation and 3.11 does not, so the
    same probe scored 0.813911544591865 on one and 0.8139115445918653 on the
    other and the 3.12 and 3.13 legs of the matrix failed a determinism test
    while nothing about any model had changed. ``portable`` rounds that away.

    Both halves matter. Rounding that swallowed a real change would turn the
    tripwire into decoration, so the second assertion is the one worth keeping:
    a difference a millionth of the way up the scale still moves the digest.
    """
    score = 0.813911544591865
    assert portable(score) == portable(score + 1e-16)
    assert portable(score) != portable(score + 1e-6)
    assert portable(None) is None
