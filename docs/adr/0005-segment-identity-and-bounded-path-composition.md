# 5. Segment identity and bounded path composition

Date: 2026-07-29

## Status

Accepted. Implements the model set out in ADR 0002. Supersedes nothing.

## Context

M1 needs beaconed segments over the static graph of `core/topology.py`. Three
decisions had to be made and none of them is forced by the domain.

**How identity is derived.** ADR 0002 says a re-signed segment may describe the
same interfaces under new cryptographic material. Whether SCION's own path
fingerprint is stable across re-signing is open question Q1 and is unresolved.
The substrate cannot wait for the answer and must not pre-empt it.

**How many paths a scope resolves to.** `CLAUDE.md` puts the realistic tier at
100–300 paths per (src, dst). That number is not a property of any single
component: it comes out of generator fan-out, how many up-segments an AS
registers, how many core segments join two cores, and how composition combines
them. Left to defaults, the first implementation produced a median of four.

**How much search is allowed.** Composition is on the step path. Invariant 3
says the network does not wait for the model; it equally must not wait for its
own path server.

## Decision

**Both identifiers are always carried; neither is default.** Every `Segment` and
every `Path` exposes `structural_id` (blake2b over the ordered interface
sequence) and `segment_id` (blake2b over the structural id, the generation
counter and the signature id). `Path.path_id(policy)` selects between them and
raises on any other value, so a typo cannot silently pick one. When Q1 is
answered, the scenario default changes and nothing else does.

Neither hash is Python's `hash()`. That is salted per process, and an identifier
built on it would move between two runs of one seed — invariant 4, broken
invisibly. There is a cross-process test.

**A re-signed segment keeps its interface sequence and its structural id.**
Re-signing bumps `generation`, draws a new `signature_id` from the store's
seeded generator, and resets `created_s` / `expiry_s`. This is M1's acceptance
criterion and the mechanism probe R4 will rest on.

**Path counts are a tier property, set by `SegmentStore.for_tier`.** The caps
that determine fan-out — up-segments per AS, core segments per core pair,
peering joins per combination, paths per scope — are derived from the tier's
`paths_per_pair` band rather than being global defaults. A caller can still
override any of them.

**Search is bounded everywhere, with a correctness fallback where bounding can
lie.** Up-segment discovery is breadth-first with a width cap. Core-segment
discovery is a beam search: a plain walk over partial paths had a frontier
growing as core-degree to the power of the hop limit, which at the realistic
tier was a million interface lookups per core pair and dominated every query.
The beam trades path diversity between distant cores for two orders of
magnitude. Because a beam can return nothing where a route exists, an empty beam
falls back to a visited-set shortest-path search: **zero paths must mean a
policy filter or a real partition, never a search that gave up.** The two are
indistinguishable downstream and only one of them is a scenario.

**Segments are Python objects; paths are composed lazily.** `core/` is otherwise
array-backed, and this is the exception. Segment count is bounded by the graph
(≈25k at the realistic tier, a few MB); path count is not (two million ordered
pairs), so paths are materialised per scope and cached with LRU eviction. The
rule "no Python object per path in the hot loop" is kept: the hot loop is
composition, and it holds objects only for the scope it was asked about.

## Consequences

- Q1 can be answered later without touching the substrate.
- A model can be run under either identity policy against one world, which is
  what makes identity amnesia measurable rather than arguable.
- Beam width and hop limits are now numbers that affect measured path
  diversity. They are constructor arguments and they belong in the scenario
  schema (`core/scenario.py`), or two runs described as identical will not be.
- Path counts below the tier band for single-homed stubs are expected and are
  not corrected. A stub with one provider genuinely has few paths.
- The tier bands are met on the synthetic generator only. `from_caida` will
  produce a different degree distribution and the bands will have to be
  re-checked against it — see `ASSUMPTION(Q5)` in `core/topology.py`.

## Alternatives rejected

**Pick one identity policy now and make the other configurable later.**
Cheaper, and it would have quietly become the assumption the codebase stands on
before Q1 was ever asked. This is the failure ADR 0002 exists to prevent.

**Derive `segment_id` from the material alone, not from the structural id.**
Slightly cleaner, but then two segments with different interfaces could not be
distinguished by their `segment_id` alone, and every consumer would need both.

**Enumerate all paths per (src, dst) at construction.** Two million scopes at
the realistic tier. Rejected on arithmetic.

**Unbounded k-shortest-paths for core segments.** Better diversity, wrong
budget. Rejected against invariant 3; revisit if measured diversity between
distant ISDs turns out to matter to a front-end.
