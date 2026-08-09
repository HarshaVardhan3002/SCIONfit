# 7. The scenario schema

Date: 2026-08-10

## Status

Accepted. Required by M1 deliverable 5, which says to design the schema before
the adapters harden.

## Context

A scenario says what world to build and what happens to it. Four backends will
run the same one: replay, analytical, `scion-dqn-sim`, and real hardware driven
through `linkd`. Portability across those four is the project's contribution
and it dies quietly if each tier grows its own format, so the schema is fixed
now, while there is exactly one backend and changing it is cheap.

Two things forced the shape.

**Reproducibility is not the same as determinism.** The substrate is already
deterministic from a seed. That buys nothing if two people run "the same
scenario" with different beam widths: both runs are individually reproducible
and their numbers are not comparable. Building deliverables 2 and 3 turned up
two families of parameters with exactly this property -- the path search caps
and the congestion constants -- and neither is something a user would think to
write down.

**A scenario has to be readable without being run.** Diffing two scenarios,
hashing one into a result file, validating a submission to the benchmark in M6:
none of those should require building 2,000 ASes.

## Decision

**A scenario is a frozen dataclass tree, serialised as JSON, versioned by a
`schema` integer.** JSON because `core/` may not take a dependency beyond
numpy and `json` is in the standard library. YAML is a nicer surface for humans
and can be a thin loader at the edge -- `docs/HANDOFF.md` shows YAML and that
stays true -- but the canonical form, the form that gets hashed, is JSON.

**Every parameter that moves a number is in the file.** Search caps, the six
BPR constants, the background traffic parameters and seed, the beacon interval
and lifetime, the identity policy. The test for whether a parameter belongs is
not "would a user set this" but "could two runs differ in it and be described
as the same scenario". `Scenario.digest()` covers all of it, and a result file
carries that digest.

**Unknown keys are an error.** A misspelled key that is silently ignored
produces a run that claims to be one thing and is another; there is no way to
notice from the output. `from_dict` raises on any key it does not know.

**Timeline events are data with a `kind` string, dispatched by name.** Not
callables, not subclasses. A callable cannot be hashed into a trace, serialised
to a file, or mapped onto a `linkd` REST call, and M9 needs all three. Each
kind is a small named thing a backend can implement its own way:
`link_degrade` is a `health` multiplier here and a `linkd` shaping call on
hardware, and the scenario does not know the difference.

**An unknown event kind is an error unless it is namespaced `x:`.** Same
argument as unknown keys, with a hole left deliberately: a front-end that wants
to carry its own events through the timeline prefixes them, and the substrate
passes them to the clock, where they reach whatever registered for them and
nobody else. `x:` is how a scenario stays valid across front-ends without the
substrate having to know what a gym wrapper wants.

**A scheduled topology change rebuilds the world.** `Topology.without_links`
renumbers links and interfaces, so segments and link state cannot survive it;
the rebuild is explicit, costs about a hundred milliseconds at the realistic
tier, and bumps `Substrate.generation` so a downstream component can notice.
This is the right cost model: ADR 0002 says physical topology changes are rare
and planned, and making them cheap would invite using them as the background
churn that they are not. A link *outage*, which is common, is a health
multiplier and costs nothing.

**Scenario and Substrate are separate objects.** `Scenario` is a description
and is cheap; `Scenario.build()` returns a `Substrate`, which is the world.

## Consequences

- M6 result files carry a scenario digest, and a leaderboard entry whose
  digest does not match the scenario it claims is rejectable mechanically.
- Adding a parameter to `LinkParams` and forgetting the schema is caught:
  `test_scenario.py::test_every_tunable_constant_is_serialised` walks the
  dataclass fields and fails on anything the round trip drops.
- M9's `linkd` mapping has a fixed target to hit. The event kinds were chosen
  against its REST surface -- bandwidth, latency, loss, up/down -- rather than
  against what is convenient to implement in numpy.
- Scenario files are verbose, because defaults are written out on save. That is
  the price of "the file says what ran".

## Alternatives rejected

**YAML in `core/`.** Needs a dependency in the one package that is not allowed
one, and YAML's implicit typing (`no` parsing as `False`, unquoted versions
becoming floats) is a poor property for a file whose whole job is to mean
exactly one thing.

**Python scenario files.** Maximally expressive, trivially non-portable, and
unhashable. A tier-3 backend cannot execute a Python closure on a Proxmox host.

**Defaults omitted on save.** Shorter files, and the exact failure mode the
schema exists to prevent: the file no longer says what ran, it says what
differed from whatever the defaults were on the day.
