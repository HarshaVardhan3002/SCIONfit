# 18. The report is a rendering of recorded cells, and it prints how thin each number is

Date: 2026-09-02

## Status

Accepted. Phase 4. This is the artefact the product statement is about — *a person
loads their model, presses run, and a PDF comes out saying what was tested and how
the model did.*

## Context

Every number produced so far has been read by the person who produced it. They knew
the tier, they chose the seed, they remember which axis was moving. The PDF is the
first output read by somebody who was not there, and it is the one attached to a
paper. It has to carry its own context or it is a page of decimals.

Three things follow, and they are the whole of this decision: what the report may
read, what it may not claim, and how many digits a number has earned.

## Decision

### The report is a pure function of a results directory

`bench/report.py` reads `*.json` cells and nothing else. It does not build a
substrate, load a model, or re-compute a metric. A report can therefore be
regenerated months later, from a directory copied off a cluster, on a machine where
the model's own dependencies are not installed.

The cost is that **anything the report says must have been recorded at the time**,
and two things it needs were not: the tier, and the model's declaration of what it
can do. Both join `CellResult`. Re-deriving them at report time would have been
easy and wrong in the same way: the suite object available when the PDF is built is
the *current* suite, and printing its tier against a cell recorded under an earlier
one produces a report that is internally consistent and false.

### Conformance arrives as a file, not as an import

`bench` and `conformance` are siblings and neither may import the other (invariant
7). So the verdicts reach the PDF the way they reach any other reader:

    scionfit check --model pkg:Model --format json --out card.json
    scionarena bench report results/ --conformance card.json --out report.pdf

The report renders the recorded status string, which is what keeps the
`DECLARED_ABSENT` / `FALSE_CLAIM` distinction intact through the transfer. Collapsing
them to a boolean at any point in the chain would erase the one distinction
conformance exists to draw — an honest limitation is not a false claim — and it is
easiest to erase exactly here, where a reader wants a green tick.

A report built without a card says so, in the section where the verdicts would have
been. Silence there would read as "nothing was wrong".

### matplotlib and reportlab, behind a `[report]` extra, imported inside the call

`core/` keeps its numpy-only rule and neither of these is a substrate dependency.
They are also not imported at module scope: `scionarena bench axes` and
`bench show` must keep working on an install that has neither, and a top-level
import would take the whole CLI down with them. A missing dependency raises one
sentence naming the extra.

Both are pure wheels on Windows. The rejected alternative is below and it is the
main reason for this ADR's existence.

### A number is printed to as many digits as it has earned

The requirement is easy to state and easy to lose: *the report states its own
uncertainty.* Concretely, a `Reading` carries the value, the number of observations
behind it, and the observed range, and it renders itself:

- **no observations** — `not measured`. Never `0.000`. A metric the registry could
  not compute returns `None` (ADR 0017) and that `None` survives all the way to the
  page.
- **fewer than the suite's own repeat count** — two significant figures, with the
  count printed beside it and the row marked thin. Three is not an arbitrary
  threshold: it is `SweepSpec.repeats`, chosen because the headline metric was
  measured bimodal across seeds, so below it the spread is not estimable and the
  digits are decoration.
- **enough** — four significant figures and the observed min..max.

Repeats are only half of it. An accuracy metric scored against four forecasts is
thin *inside* one run, and running the cell three more times does not fix that. So
the registry grows one **support** metric per family — a count, flagged
`support=True`, registered through the same decorator so it is recorded on every
cell without the runner learning its name — and the report divides by it. One per
family rather than one overall, because the four families have four different
denominators and a single "n" would be wrong for three of them.

### The report says what it does not claim

A closing section, not a footnote. Regret is an upper bound and says so (ADR 0017);
a metric computed on a tier below `realistic` says which tier; a sweep run in `oat`
mode says that no interaction between axes was measured. These are the claims a
reader would otherwise make on the report's behalf.

## Consequences

`CellResult` gains `tier` and `capabilities`. Neither bumps `SCHEMA`: a reader of
an older cell gets the field's default, and the report prints `unrecorded` rather
than guessing. That is the difference the schema version is for.

Adding the support metrics means a cell recorded before this has no support counts,
so every one of its numbers renders as thin. That is the correct rendering of a
result whose support is genuinely unknown, and it is a visible reason to re-run
rather than a silent one.

The interactive HTML report (`instrument/report.py`) stays exactly as it is. It is
the view you look at while a run is going; this is the artefact that leaves the
building, and they are allowed to be different things because neither is generated
from the other.

## Alternatives rejected

- **HTML to PDF, through a headless browser or weasyprint.** The failure mode is
  output that differs silently from the interactive view — the one failure a
  benchmark report cannot have — plus a browser or GTK stack on the reader's
  machine.
- **Hand-written SVG, as `instrument/report.py` does.** That was the right call for
  two line charts. This needs a ranked comparison, an axis response per axis, and a
  per-horizon panel, with error bars, for every family; hand-rolling that is
  writing a plotting library badly.
- **Build the report from live `LoopResult` objects.** Faster, and it makes the PDF
  a by-product of one process rather than a rendering of a durable record: nobody
  could regenerate it, and a directory merged from two machines could not be
  reported on at all.
- **Read capabilities by loading the model at report time.** Needs the model's
  environment on the reporting machine, and would report what the model declares
  *now* rather than what it declared when the numbers were taken.
- **One "n" for the whole report.** Repeats and per-metric support are different
  quantities. A coverage figure from three repeats of a run that scored four
  forecasts each is not a figure from twelve observations of anything.
- **Omit a metric that could not be computed.** An absent row reads as an absent
  problem. It gets a row saying it was not measured.
