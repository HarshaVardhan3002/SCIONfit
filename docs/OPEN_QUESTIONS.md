# Open questions

Everything the project is standing on that has not been verified. `grep -rn "ASSUMPTION("
src/` must agree with this file.

Status: **OPEN** · **ASKED** · **PARTIAL** · **RESOLVED**

---

## Q1 — Is the SCION path fingerprint stable across re-signing? · RESOLVED · unblocks M1, M5

If a path segment is re-beaconed with identical interfaces and new cryptographic material,
does the identifier a host sees change?

**Answer: no. The fingerprint is structural.** `snet.Fingerprint` is a SHA-256 over the
ordered sequence of `(ISD-AS, interface-ID)` pairs and contains no cryptographic material
whatsoever. The path combinator additionally collapses multiple constructions of the same
interface sequence by default, exposing distinct segment IDs and MACs only to a caller
that explicitly asks (`findAllIdentical=true`). It survives re-beaconing by design.

Answered from the `scionproto/scion` source tree during the v1.2 harness re-review, not
by a mentor. Recorded here rather than left open because the branch it selects was already
written down: "if it does not change, the risk is hypothetical and R4 becomes a robustness
check rather than a defect detector." That is now the situation.

**What follows.**

- `identity_policy` has a documented default: **`structural`**, because that is what the
  deployed stack does. Both policies stay implemented; they are cheap, and the interesting
  comparison has moved rather than disappeared.
- **R4 is re-aimed.** A model that collapses under `crypto_bound` is not wrong about
  deployment, because `crypto_bound` has no correspondent in the real stack, so failing it
  would be a false positive against the thing conformance claims to measure. The deployment
  hazard is a model keying memory on something that is *not* the fingerprint — the raw path
  bytes, the dataplane path object, or the segment IDs `findAllIdentical=true` exposes. The
  probe becomes "does this model key on the identifier the protocol designates", scored as
  a hygiene grade rather than a conformance failure.
- With the beacon interval corrected to 5 s (see the B1 note below), the sharper question
  is no longer `structural` versus `crypto_bound` but **how fast identity can churn before
  a model that keys correctly still loses**. The substrate can already run that.

**Related correction, found the same way.** `BeaconPolicy.interval_s` defaulted to 300 s;
upstream originates, propagates and registers every **5 s**. `lifetime_s` was already right
— `DefaultMaxExpTime = 63` gives 64 × 337.5 s = exactly 6 h. Hop-field expiry is therefore
quantised to `EXPIRY_QUANTUM_S = 337.5`, one of 256 discrete steps, not a float. Every
result recorded at the old cadence understates identity churn by more than an order of
magnitude.

## Q2 — Is per-link offered load observable or estimable? · PARTIAL · blocks M5

R6 and the entire demand-conditioning argument assume a model can know or infer how much
traffic is on a link. If it fundamentally cannot, the response model conditions on a latent
proxy instead and probes R6 and R7 need redesigning.

**Partial answer, from the protocol.** Not *directly* observable: beacon metadata carries
only static capacity, and the data plane exposes no per-hop state to endpoints. But it is
*estimable*, and Master Spec v1.2 §7.1 states the condition exactly — a unit's state is
identifiable iff its column in the observed path-incidence matrix is distinguishable from
its neighbours'. So R6 and R7 do not need redesigning, but their claim is narrower than
their names suggest: they hand the model a demand vector and check that it conditions on
it. That is legitimate — a deployed oracle knows its own published shares — but it is not
a test of whether the model can *infer* load. See also the `Demand.intended` /
`Demand.realised` split, which is what would make it one.

**Still to ask.** Can an end host, or a central node with cooperating hosts, observe or reasonably
estimate per-link load? Does ID-INT in the testbed expose anything like this?

## Q3 — What is realistic scale for a deployed SCION ISD? · OPEN · blocks M1

`realistic` is currently 2,000 ASes / 10,000 links / 100–300 paths per pair. This is a
guess and it sets performance budgets throughout `core/`.

**Ask.** For a production ISD: how many ASes, how many inter-AS links, and how many paths
does a host typically see between a given pair?

## Q4 — Do we have tier-3 hardware? · OPEN · blocks M9

`ietf-scion-testbed` needs a Proxmox host. Without one the ladder tops out at tier 2 and
the sim-to-real claim stays theoretical.

**Ask.** Is there a Proxmox host available, or access to an existing testbed deployment?

## Q5 — How do CAIDA AS relationships map onto SCION core/non-core? · OPEN · blocks M1

The realistic topology generator uses CAIDA-style AS relationships, which are BGP concepts.
The mapping onto SCION's ISD core, parent-child and peering structure is an assumption.

**Provisional handling.** Documented mapping in an ADR, marked `ASSUMPTION(Q5)`.

## Q6 — What is the real SCMP probe rate limit? · RESOLVED · unblocks M2

Sets the tool cost model. If probing is far cheaper or dearer than assumed, every
budget-related result shifts.

**Answer: there isn't one, and that is the finding.** The SCMP specification says only that
SCMP *may* be subject to rate limiting. The audited router implements no limiter — there is
no token bucket anywhere in the tree. What a deployment applies is a per-deployment
operational choice, and it may be zero.

Our three constants were invented:

```python
SCMP_LIMIT = RateLimit(calls=5, per_s=1.0)
BWTEST_LIMIT = RateLimit(calls=1, per_s=30.0)
PATH_SERVER_LIMIT = RateLimit(calls=2, per_s=1.0)
```

**What follows.** Not deletion — an unlimited probe budget is not a safer assumption than a
limited one, and a harness where information is free measures nothing. The limits move out
of `exposure/tools.py` and into `Scenario.probe_limits`, beside `BeaconPolicy` and
`LinkParams`, with the old values as defaults. A run then states which regime it assumed,
and M6 sweeps it as an axis rather than baking it in. Before this, two runs made under
different assumptions about the price of information were indistinguishable in the report,
and every budget result the harness has produced is conditional on exactly that number.

Recorded as **per-deployment, unspecified upstream, must be swept.**

## Q7 — How many hosts share a source-destination pair? · OPEN · affects M3, M6

The independent-sampling argument scales as `1/√N` and is much weaker at N≈10 than at
N≈1000. Sets the host population defaults and how strongly the stochastic-advisory result
can be claimed.

## Q8 — Do we coordinate with netsys-lab before publishing comparatives? · OPEN · affects M6

We intend to show ScionPathML's Task 4 score next to our R8 and oscillation index. It is a
fair comparison of different objectives, but it can read as a criticism of their benchmark,
and they are both the authors of the incumbent Path Oracle and the source of data we want.

**Recommendation.** Talk to them before publishing anything comparative. The framing should
be that the two benchmarks measure different things, because they do.

## Q9 — Which robust filter is "Tier-0" for the purposes of a baseline? · OPEN · affects M6

Master Spec §19.1 specifies Tier-0 as "online robust filters, always-on" — t-digest
quantile trackers at 1 min / 10 min / 6 h plus a Student-t state-space filter per directed
edge, servable with inflated intervals, with no spatial transfer and no forecast head. §28
then makes **Tier-0-only** one of the five mandatory accuracy baselines.

What it does not say is which of those a *baseline implementation* should be, and the
choice moves the number every model is scored against. A t-digest at three horizons and a
Student-t filter are not the same floor, and "the Tier-1 lift" is the difference between
the model and whichever one was used.

**Assumed provisionally.** `reference/baselines.py::Tier0Only` is a rolling median over a
bounded window with the interquartile range as its interval. It keeps the two properties
the spec is leaning on — robustness to heavy-tailed observation noise, and an interval that
widens when the evidence is thin — and it is deliberately the cheapest thing that has them.

**What would settle it.** The netsys-lab Tier-0 implementation, or a statement of which
filter the published Tier-1 deltas were measured against. Until then a reported Tier-1 lift
is conditional on this choice and the report has to say so.
