# ADR 0004 — The M0 package layout and how the import direction is enforced

**Status.** Accepted. Implements the layout in `docs/HANDOFF.md` §3.

## Context

`scionfit` v0.1 was one flat package: `interface.py`, `env/`, `probes/`, `report.py`,
`runner.py`, `adapters/`, `reference/`. The target is four front-ends sharing one
substrate, with four seams that a future contributor can substitute a component across
without touching anything else. Seams stay clean by being respected while each layer is
still small. Moving the files later, once M1–M4 have filled them, is a much larger and
riskier change than moving them now while the whole tree is 2,500 lines.

## Decision

The v0.1 modules move to their target homes with **no behaviour change**:

| v0.1 | M0 |
|---|---|
| `interface.py` | `exposure/contracts.py` |
| `env/world.py` | `backends/analytical.py` |
| `adapters/{dqnsim,scionpathml,testbed}.py` | `backends/` |
| `probes/`, `report.py`, `runner.py`, `cli.py` | `conformance/` |
| `reference/` | `reference/`, top level |

`core/`, `instrument/`, `bench/`, `gym/`, `agent/` and `deploy/` exist as docstring-only
packages so the shape is visible from the first commit and nothing lands in the wrong place
by default.

The dependency direction is enforced by `import-linter` in CI, as three contracts:

1. **layers** — `{conformance, bench, gym, agent, deploy}` above `exposure` above `core`,
   with the front-ends declared as independent siblings so none may import another.
2. **forbidden** — `reference/` may not import `core`, `backends` or `conformance`.
   `reference/` stands in for every user-written model, so what it may not do, they may
   not do. That is seam D.
3. **forbidden** — `core` may not import anything above it, including `instrument`.

`scionarena` is the umbrella console script and dispatches to a front-end's own parser.
The `scionfit` script is kept pointing at the conformance CLI: the name is public in v0.1
and both names are cosmetic until M6.

## Consequences

An illegal import fails the build rather than a review, which is the only way a dependency
rule survives contact with a deadline.

`mypy --strict` is adopted now, on `core/` and `exposure/` only, while those are two files.
It cost two annotations at M0 and would have cost a week at M4.

**`backends/` is deliberately outside the layers contract.** The v0.1 analytical world is
written against `exposure.contracts` rather than against `core`, which is upside down: a
backend should compute the substrate's truth, not consume the model-facing types. Placing
it in the contract today would either encode the inversion or force a rewrite inside a
restructure PR. M1 rewrites that module against the corrected domain model and it joins the
layers contract there. Until then contract 3 stops `core` from depending on it.

## Alternatives rejected

*Restructure later, once there is more to move.* The cost grows with every milestone and
the seams are what the codebase is for.

*Enforce the direction by convention and review.* Reviews miss imports. `lint-imports`
does not.

*Rename the repo and the `scionfit` entry point now.* Gratuitous breakage for existing
users, and the naming is not settled until M6.

*Fold `adapters/` into `core/` as the substrate's own tier plumbing.* Breaks seam A: a
backend is how faithfully something is computed, and nothing tier-specific may leak into
`core/`.
