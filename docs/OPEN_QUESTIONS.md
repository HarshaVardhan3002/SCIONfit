# Open questions

Everything the project is standing on that has not been verified. `grep -rn "ASSUMPTION("
src/` must agree with this file.

Status: **OPEN** · **ASKED** · **RESOLVED**

---

## Q1 — Is the SCION path fingerprint stable across re-signing? · OPEN · blocks M1, M5

If a path segment is re-beaconed with identical interfaces and new cryptographic material,
does the identifier a host sees change?

**Why it matters.** If it changes, a model keying per-path memory on it silently discards
its history every refresh cycle while its outputs still look plausible. That is identity
amnesia and it would be a real deployment risk. If it does not change, the risk is
hypothetical and R4 becomes a robustness check rather than a defect detector.

**Provisional handling.** Both behaviours implemented behind `identity_policy:
"structural" | "crypto_bound"`. Neither is the blessed default. R4 runs both and reports
the delta. **Do not hardcode either.**

**Ask.** In the deployed SCION stack, is the path fingerprint a hash of the interface
sequence alone, or does it incorporate segment timestamps or signatures?

## Q2 — Is per-link offered load observable or estimable? · OPEN · blocks M1, M5

R6 and the entire demand-conditioning argument assume a model can know or infer how much
traffic is on a link. If it fundamentally cannot, the response model conditions on a latent
proxy instead and probes R6 and R7 need redesigning.

**Ask.** Can an end host, or a central node with cooperating hosts, observe or reasonably
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

## Q6 — What is the real SCMP probe rate limit? · OPEN · blocks M2

Sets the tool cost model. If probing is far cheaper or dearer than assumed, every
budget-related result shifts.

**Ask.** What rate limit do border routers apply to SCMP echo, and is it configurable per
AS?

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
