# Demo — ten minutes, one command

What to run, in what order, and what the numbers mean. Everything here is reproducible
from a clean checkout; nothing is a recording.

```bash
pip install -e ".[dev]"
scionarena demo                    # ~10 s, opens nothing, writes demo/report.html
```

Open `demo/report.html`. That page is the whole argument.

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

## The number that failed, and why that is the interesting part

The report card shows one row marked **NOT met**: the milestone's original criterion was
"oscillation index above 0.5", where the index is spectral peak dominance — a measure of
how *periodic* the movement is. It comes in around 0.3, and in the first multi-scope run
it ranked the two models backwards.

The reason is worth the thirty seconds it takes to say. With many scopes deciding
against each other, the herding is violent but has no fixed beat: the thing each scope is
chasing is being moved by all the others. Peak dominance is looking for a metronome that
is not there. Amplitude in the same frequency band separates the two models by 6x in the
same runs where dominance separates them by nothing.

So the headline measure was replaced — with the reasoning, the measurements and the
rejected alternatives written down in `docs/adr/0010-...`, and the failure of the old
measure pinned as a test so a later change cannot quietly erase it. The old number is
still on the report card. **The criterion that was written down before the data was not
quietly deleted after it.**

## If there is more time

```bash
scionarena demo --tier dev --scopes 40 --slow 8      # a model that thinks too slowly
scionarena demo --tier realistic --scopes 100 --cycles 120
```

The `--slow` run is the third line on every figure: same model, same seed, eight extra
seconds of decision latency. The network does not wait, so the advice lands against a
world that has moved, and the cost is measurably worse. That is invariant 3 — *the
network does not wait for the model* — shown rather than asserted.

The realistic tier is 2,000 ASes and 10,000 links with 100 concurrent scopes. Committed
runs of all three are in `docs/evidence/`.

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
