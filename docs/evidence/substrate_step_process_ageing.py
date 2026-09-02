"""``substrate_step_s`` measures the process it is measured in, not only the code.

The benchmark gate went red on ``realistic.substrate_step_s`` -- 4.5 ms against a
2.09 ms baseline, a reported 145% regression -- on a branch that changes no file
under ``core/``. Stashing the branch reproduces it, so the regression is not in
the code the gate is protecting.

What it actually is: after a few million segment re-signings, everything in the
process gets about twice as slow, and stays that way. A *freshly built world* run
for 600 simulated seconds costs 2.0 ms per step in a young process and 4.5 ms per
step in an aged one. Same code, same world, same seed.

Ruled out, each by measurement rather than by argument:

* not the accumulator -- draining ``SegmentStore.drain_resigned()`` every step,
  so nothing accumulates, changes nothing (4.77 ms against 4.96 ms).
* not garbage collection -- ``gc.disable()`` for the whole run changes nothing.
* not ``Segment.__dict__`` -- ``@dataclass(frozen=True, slots=True)`` changes
  nothing (4.42 ms against 4.37 ms).
* not the CPU downclocking -- 45 seconds of idle inside the aged process does not
  bring it back, while a new process is immediately fast again.
* not the simulation state -- work per step is flat: 600 ``_resign_all`` calls
  and 1.64 M re-signings in every 600 s window, and ``n_segments`` never moves.

Why it matters to the gate rather than to a run. ``benchmarks/run.py`` measures
``substrate_step_s`` **last**, after four allocation-heavy benchmarks have run in
the same process, and B1 raised the churn those measurements produce by sixty
fold. So the gated number is a function of how much allocation happened earlier
in the process, which is not a property of the code under test.

Run it from the repo root::

    python docs/evidence/substrate_step_process_ageing.py

**Fixed** in ADR 0014: each metric is now measured in its own subprocess, which
does its own setup, so measurement order is no longer an input to a gated number.
The tolerance was not widened. This script is kept because the ageing itself is
unexplained -- five candidate causes were eliminated and none of them was it --
and the next person to see a benchmark drift for no reason should be able to
re-run the elimination rather than repeat it.
"""

from __future__ import annotations

import gc
import time

from scionarena.core.scenario import Scenario, TopologySpec

TIER = "realistic"
SHORT_S = 600.0
LONG_S = 3_600.0


def per_step_ms(duration_s: float, *, drain: bool = False) -> float:
    """Cost of one substrate step, in milliseconds, over a fresh world."""
    world = Scenario(
        name="ageing", topology=TopologySpec(tier=TIER), duration_s=duration_s, step_s=1.0
    ).build()
    started = time.perf_counter()
    steps = 0
    while steps < duration_s:
        world.step(1.0)
        steps += 1
        if drain:
            world.segments.drain_resigned()
    return (time.perf_counter() - started) / steps * 1e3


def main() -> None:
    print(f"{TIER} tier, step_s=1.0, one fresh world per row\n")

    cold = per_step_ms(SHORT_S)
    print(f"  {SHORT_S:6.0f}s, young process          {cold:5.2f} ms/step")

    aged = per_step_ms(LONG_S)
    print(f"  {LONG_S:6.0f}s, same process           {aged:5.2f} ms/step")

    after = per_step_ms(SHORT_S)
    print(f"  {SHORT_S:6.0f}s again, aged process     {after:5.2f} ms/step")

    time.sleep(45)
    rested = per_step_ms(SHORT_S)
    print(f"  {SHORT_S:6.0f}s after 45s idle          {rested:5.2f} ms/step")

    gc.collect()
    drained = per_step_ms(SHORT_S, drain=True)
    print(f"  {SHORT_S:6.0f}s, draining every step    {drained:5.2f} ms/step")

    print()
    print(f"the same 600 s world costs {after / cold:.1f}x more in the aged process,")
    print("and neither an idle gap nor draining the re-signing feed brings it back.")
    print("A new process is immediately fast again, which is what makes this a")
    print("property of benchmarks/run.py's measurement order rather than of core/.")


if __name__ == "__main__":
    main()
