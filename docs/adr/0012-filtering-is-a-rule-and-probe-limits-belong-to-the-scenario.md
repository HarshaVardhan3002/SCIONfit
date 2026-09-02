# 12. Filtering is a rule, and probe limits belong to the scenario

Date: 2026-09-02 · Status: accepted · Supersedes nothing · Amends [0007](0007-scenario-schema.md), [0008](0008-tool-costs-rate-limits-and-the-exposure-boundary.md)

## Context

The mentor's re-review of the harness against Master Spec v1.2 found four substrate
defects. Two of them were straightforward bugs with obvious fixes. The other two forced a
choice about *where a piece of state lives*, and those choices are recorded here because
they change behaviour a scenario file is supposed to pin down.

### Filtering

`as_policy_filter` expresses the corrected domain model's central claim: a path becomes
unavailable without anything physical happening. The handler enumerated
`segments.iter_segments()` when the event fired and hid what it found.

Path composition is lazy by design (ADR 0005) — core segments between a pair of core ASes
are discovered on first ask, because at the realistic tier there are 320,000 ordered core
pairs and a run touches a handful. So every core segment materialised after the event
carried a segment id that had never been tested against the filter. Measured over 60
sampled pairs at the `dev` tier: 335 paths still traversing an AS the scenario had
filtered, while `SegmentStore.filtered` correctly reported the filter as applied. The one
scenario that tests availability-without-connectivity was not testing it.

### Probe limits

Three constants in `exposure/tools.py` set what probing costs a model in *permission*:

```python
SCMP_LIMIT = RateLimit(calls=5, per_s=1.0)
BWTEST_LIMIT = RateLimit(calls=1, per_s=30.0)
PATH_SERVER_LIMIT = RateLimit(calls=2, per_s=1.0)
```

They were written as though a border router enforced them. Open question Q6 asked what the
real limit is. The answer, from the source tree: the SCMP specification says only that SCMP
*may* be subject to rate limiting, and the audited router implements no limiter at all.
There is no token bucket anywhere in the tree. What a deployment applies is an operational
choice, and it may be zero.

## Decision

**Filtering is a standing rule, not a snapshot.** `FilterPolicy` records the AS and the
fraction; `SegmentStore` keeps the active policies and tests each newly registered segment
against them as it arrives, so the hot read path stays a set membership test and a segment
composed an hour later is filtered on discovery.

**A partial filter decides per segment, keyed on the structural id.** `fraction` used to
draw an exact-count random subset of whatever existed at the time. It is now a stable
per-segment draw, so the decision is a property of the segment rather than of what happened
to be composed first, and it survives re-signing.

**Probe limits move into `Scenario.probe_limits`**, beside `BeaconPolicy` and `LinkParams`,
with the old values as defaults. `ProbeLimits` holds plain numbers and the exposure layer
converts them to `RateLimit` at the boundary, because `RateLimit` lives in `exposure` and
`core` may not import upwards (invariant 7). `ToolSpec.limit` takes the limits alongside
the arguments; the session resolves them from the world it is running against.

## Consequences

The filter now catches what it claims to. Two stores that discover the same segments in a
different order hide the same ones, which the old form could not promise and which matters
because M6 will run the same scenario across a matrix of models whose query patterns differ.

`fraction` changes meaning. A scenario asking for `fraction: 0.5` on a small segment set no
longer gets a guaranteed minimum of one hidden segment; it gets about half, and on a set of
three that could be one or two. This is the honest reading of "the AS stops propagating half
its segments" and it is the only reading that extends to segments that do not exist yet, but
it is a behaviour change and a scenario relying on the old guarantee will notice.

A run now states which probe regime it assumed, and `Scenario.to_dict()` carries it. Every
budget-related result the harness has produced is conditional on those three numbers; until
now nothing in the output said so, and two runs made under different assumptions about the
price of information were indistinguishable. M6 gains an axis it would otherwise have baked
in. The schema grows a field without a version bump: reading an older scenario works because
the field defaults, and an older build reading a newer file fails loudly on `_check_unknown`
rather than silently ignoring it.

## Alternatives rejected

**Re-applying the filter on every composition** instead of at registration. Correct, and it
puts a predicate evaluation in the hot read path — `up_segments` is called per composition
at the realistic tier. Testing once when a segment is registered gets the same answer for
the cost of one evaluation per segment rather than one per read.

**Keying the partial draw on the segment id.** It changes on re-signing, so a segment would
flip in and out of the filtered set every refresh cycle — at the corrected 5 s beacon
interval, 720 times an hour. The structural id is stable across re-signing, which is exactly
the property ADR 0002 exists to provide.

**Deleting the rate limits, since the router has none.** An unlimited probe budget is not a
safer assumption than a limited one; it is a different unverified assumption, and a harness
where information is free stops measuring the thing invariant 2 exists for. Master Spec
v1.2 §29 reaches the same conclusion from the other direction and carries the probe budget
as a measured M0 parameter precisely because it is per-deployment.

**Putting `RateLimit` itself in `Scenario`.** Reads better and inverts the dependency
direction: `core` would import `exposure`, which invariant 7 forbids and which import-linter
fails on. Plain numbers in `core`, converted at the boundary, costs one small conversion
function per limit and keeps the seam.
