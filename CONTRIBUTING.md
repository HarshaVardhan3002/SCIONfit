# Contributing

## The one rule for new probes

**A new probe must come with at least one reference model that fails it.**

A probe that everything passes is not measuring anything, and a suite of such
probes gives false assurance, which is worse than no assurance. If you cannot
write a plausible model that fails your probe, the probe is not yet testing a
real distinction.

`tests/test_conformance.py::test_probes_discriminate` enforces the spirit of
this at the suite level; please enforce it at the probe level yourself.

## Probe design rules

1. **Change exactly one thing.** A probe that varies two inputs cannot
   attribute the outcome to either.
2. **Compare a model against itself, never against another model.** That is
   what makes results portable across model families and across fidelity tiers.
3. **Report evidence, not just a verdict.** Every `ProbeResult` carries an
   `evidence` dict. Someone should be able to argue with your conclusion from
   the numbers you recorded.
4. **Write a remedy.** If a probe can fail, the author needs to know what to
   change. `remedy` is not optional on blocking probes.
5. **Prefer `NOT_APPLICABLE` over a free pass.** If a model cannot represent
   the thing your probe tests, say so rather than passing it by default. R9 is
   the worked example: self-consistency is untestable on a model that ignores
   demand, so it returns `NOT_APPLICABLE` instead of `PASS`.

## Running things

```bash
pip install -e ".[dev]"
pytest -q
ruff check src tests
python -m scionfit.cli compare      # the discrimination matrix
```

CI runs all three, plus `compare`, so a change that makes the probes stop
discriminating fails the build.

## Adapters

Adapter code that has not been validated against a live upstream must say so in
its module docstring and must be listed as unvalidated in `docs/ADAPTERS.md`.
Do not publish tier 0, 2 or 3 numbers from an unvalidated adapter.
