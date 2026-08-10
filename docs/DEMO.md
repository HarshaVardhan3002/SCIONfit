# Demo — ten minutes, one command

What to run, in what order, and what the numbers mean. Everything here is reproducible
from a clean checkout; nothing is a recording.

```bash
pip install -e ".[dev]"
scionarena ui                      # opens a tab on http://127.0.0.1:8765
```

Click **Run**. Ten seconds later the page is the whole argument.

The form is the scenario: tier, how many scopes contend, how many decision rounds, how
many hosts each scope has, the seed, and how many extra seconds of decision latency to
give a deliberately slow third model. Three presets sit under it; the middle one is the
one to use if the room has half an hour. Long runs report progress per decision round and
can be stopped, and every finished run has a **download report.html** link that is
standalone — no server, no network, openable from a USB stick.

Same thing without a browser:

```bash
scionarena demo                    # writes demo/report.html and prints the card
```

---

## The claim

A centrally deployed AI that hands out *ranked* path advice makes every host it advises
move the same way at the same time. On a network where paths share bottlenecks, that
turns a recommendation into a stampede: the advised link congests, the next round of
advice moves everyone off it, and the model's own objective gets worse than if it had
never spoken. Advice sampled from a *distribution* does not do this, because hosts
disagree with each other by construction.

The demo runs both, changing nothing else.

## The four things to point at

1. **Two runs, one world.** Same topology, same seed, same scopes, same host counts, same
   tool budget. The report header prints all of it, and the per-run digest at the bottom
   is the hash of the whole session log. If a judge doubts the runs are comparable, the
   digests are what to check: reruns reproduce them exactly.

2. **The first figure — offered load on one interface.** Red is the greedy model. It is a
   square wave: the population is thrown onto the link and off it, round after round.
   Blue is the stochastic model on the same interface. Background traffic is excluded
   from this figure so that what is left is what the model caused; the second figure adds
   it back and is what an operator's own graph would show.

3. **The third figure — where the hosts actually went.** Not where they were told to go.
   Advice is delayed by the model's own decision latency and hosts resample at their own
   rate, so this line is the realised split, and for the greedy model it is a square wave
   between 0 and 1.

4. **The last figure and the report card — it costs something.** Mean path cost,
   load-weighted across every scope. The greedy model is roughly 3x worse *at the thing
   it is optimising*. Oscillation here is not an aesthetic complaint.

## The measure that was replaced, and why that is the interesting part

The report card carries two oscillation numbers. The headline is **amplitude**: how far
the load moves fast, in the units of the series. The other is **peak dominance**, the
measure the proposal published — how *periodic* the movement is — and the milestone was
originally written against it: greedy above 0.5, stochastic below 0.15.

It does not travel. Sweeping the number of contending scopes from 4 to 24 on the same tier
and seed, the amplitude ratio between the two models never drops below 6x — and dominance
goes wherever the experiment's shape sends it. Measured with the decision cadence widened
per row so that every round fits it, the greedy model's dominance *climbs* from 0.33 to
0.85, straight through the 0.5 line. Measured on a held 30 s cadence, the same sweep has it
*falling* from 0.257 to 0.079, and at 24 scopes the two models are 0.079 against 0.067,
which is no separation at all. Run length moves it too: at 4 scopes, 100 rounds gives 0.375
against 0.249 and 240 rounds gives 0.330 against 0.076.

The reason is worth saying out loud, because it is the strongest version of the point.
Widening the cadence widens the sample interval with it, and coarse sampling folds the same
power into fewer bins, which raises dominance for *any* series. Decimating one run's grid
from 30 s to 120 s takes the greedy model from 0.297 to 0.554 and the stochastic one from
0.206 to 0.459 — the calm model crosses most of the way to the 0.5 threshold without
changing its behaviour at all. A threshold on dominance is a threshold on your sampling
choices as much as on the model.

So the criterion was restated as a ratio between two models on one scenario, and the old
number was kept on the card rather than deleted. `docs/adr/0010-...` has the reasoning, the
measurements and the rejected alternatives — including a revision that withdraws a claim
its own first draft made, and a note at the end recording that the revision's own headline
number did not survive being measured on a fixed grid either. `docs/adr/0011-...` is the
grid.

That is the thing worth pointing at: the criterion written down before the data was not
quietly rewritten after it, and neither was the ADR. Two of its numbers have been withdrawn
in public and both withdrawals are still readable.

## If there is more time

```bash
scionarena demo --tier dev --scopes 40 --slow 8      # a model that thinks too slowly
scionarena demo --tier realistic --scopes 100 --cycles 80
```

(the second and third presets in the UI, if you are driving it from there)

The `--slow` run is the third line on every figure: same model, same seed, eight extra
seconds of decision latency. The network does not wait, so the advice lands against a
world that has moved, and the cost is measurably worse. That is invariant 3 — *the
network does not wait for the model* — shown rather than asserted.

The realistic tier is 2,000 ASes and 10,000 links with 100 concurrent scopes. Committed
runs of all three are in `docs/evidence/`.

**Expect the realistic run to fail three of its five criteria, and do point at that.** The
headline holds -- the greedy model still swings 12x wider than the stochastic one -- but
the split-amplitude and cost thresholds were set at the smoke tier and do not transfer,
and peak dominance comes out at 0.349 after reaching 0.848 on smaller scenarios. The
absolute swing collapses too, from 3.25 to 0.144: a hundred scopes spread over ten
thousand links do not concentrate on any one of them. A harness whose demo passes at every
scale is a harness that is not measuring anything, and `docs/milestones/M3.md` says which
of these are thresholds to restate and which is a real limit.

## Questions to expect

**"Is the network real?"** It is a simulation with a real control plane: beaconing,
segment expiry and re-signing, path composition from segments, per-interface capacities
spanning three orders of magnitude, and a diurnal background load. Tier-1 through tier-3
backends (a real SCION testbed among them) are the M8 milestone; the substrate is behind
one interface precisely so the front-ends do not change when they land.

**"Is the model real?"** The two here are deliberately simple reference models, not LLMs
— that is the point of a *harness*: the comparison has to be reproducible. The model
never sees a summary the harness computed for it, every tool call is charged, and the
model reaches the substrate through nothing but the tool contract, which
`lint-imports` enforces in CI rather than by review.

**"Did you tune this?"** The criteria are ratios between two models on one scenario, not
thresholds. The detectors are tested against series whose answers are known by hand — a
constant, a ramp, a diurnal cycle, white noise — and must score all of them low, because
a detector is worth what its false positives cost.
