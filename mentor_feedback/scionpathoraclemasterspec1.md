# SCION Path Oracle — Master Design Specification

**Status:** Master v1.2 (consolidates research synthesis → spec v1.0 → adversarial review → spec v2.0 → implementation audit → design review) · **Nature:** the single authoritative, self-contained specification. No external back-references are required to implement it. · **Audience:** the team building the oracle; reviewers; ISD operators evaluating deployment.

**How to read.** Part I fixes the problem, requirements, and deployment profiles. Part II is the intellectual core: a rigorous statement of what SCION endpoints can observe, and the measurement, identifiability, and composition model derived from it — every later component cites into it. Part III records all seventeen design decisions in final form with the alternatives they beat. Part IV specifies every component with wire formats and algorithms. Parts V–VII cover security, operations, evaluation, and the build plan. A glossary closes the document.

**Lineage note.** This design was stress-tested by a full adversarial review; its five critical findings (latency identifiability, undefined composition, performative feedback, the gateway deployment reality, concentration poisoning) are closed *by construction* in Parts II–III rather than by mitigation. Where a claim was downgraded in that process (calibration guarantees, pseudonymity, stability), the honest form appears here and the stronger v1.0 form is dead.

**Implementation-audit note (v1.1).** Every SCION-specific claim in Part II was subsequently checked against the `scionproto/scion` source tree and its in-tree documentation. Part II survived: G1, G3 and G4's mechanism are what the implementation does, and G3 in particular is one function (`snet.DefaultReplyPather.ReplyPath`). Where the audit found the document reasoning against a SCION that no longer exists, the correction is in the text and the reason is stated inline rather than in a change log:

- **Path revocations were removed.** Liveness now arrives as *unauthenticated data-plane* SCMP driven by BFD, and SCMP authentication is formally on hold for lack of consensus. §9, §15, §16, §20.3 and §23 are rewritten around this, and it introduced one genuinely new attack (§23, *spoofed liveness amplification*).
- **γ_u was built on a misread value.** `StaticInfoExtension` advertises a deliberately conservative *symmetric* estimate, not a propagation floor. The floor is now a penalised soft constraint (§9, §19.3), not a structural one.
- **ADR-08's cadence argument was wrong** by three orders of magnitude and has been removed; the decision stands on its remaining reasons.
- **Hidden paths** were absent from the document and break §6's closure dichotomy; §6 and §24 gain a fourth disposition.
- **The SIG already selects paths**, at 500 ms probe cadence and single-path by default, which changes the Profile B dynamics §21 must claim over.
- **SCMP rate limiting is permitted but unspecified and unimplemented upstream**; it becomes a measured M0 parameter (§29) rather than a protocol constant.

Claims that a mechanism does *not* exist are stated as absences in the audited tree, not as protocol impossibilities. Where Part II now cites implementation, it cites it because pointing at code is stronger than arguing from first principles — not because the code is normative.

**Design-review note (v1.2).** A subsequent review asked whether the design is internally coherent and whether its component choices match current practice. It found the architecture sound, the epistemic contract (§2) the document's real contribution, and one genuine contradiction:

- **§7/§8 versus ADR-05 and ADR-13.** ADR-13 forbids regularising an unidentified parameter; ADR-05 trained latent unit states through a decoder with priors as regularisers, over a partition defined heuristically. Where that partition was not the identifiable one, ADR-05 did to units exactly what ADR-13 forbids doing to directions. **§7.1 now defines the unit as the identifiable partition of the observed incidence matrix**, which closes the contradiction by construction and — usefully — makes §8's ambiguity labels checkable and gives §20.5 a real objective. This is the most consequential change in v1.2.
- **Calibration claimed less than it can.** ADR-15 says the oracle voids exchangeability; v1.0 then chose split conformal, which requires it. §19.5 moves to an online adaptive construction with long-run coverage on arbitrary sequences, and N4 is strengthened accordingly.
- **The latency class ranked on stale load** — the documented oscillation mechanism, in the most visible feature. §20.6 now states per class which layers rank and which only widen.
- **Tier-1 was committed too early.** ADR-04 becomes a slot with a declared incumbent and a bake-off, and its own reopening condition is fired now.
- **Two ordering fixes.** The closed-loop campaign moves ahead of coordination (M4½), because building both together guarantees coordination ships. And M0(d) gains a declared failure branch, because a gate with no stated response is one that gets argued away.
- **One self-correction.** The v1.1 liveness gate punished sparse units — the RFD failure with its sign reversed. §20.3 gains an escape for SP-class units reported by the terminating AS.

Where the review found the design already right — the epistemic contract, ρ-units, fail-open, the dissemination model — it is recorded as such rather than changed.

**Marker convention:** paragraphs added or rewritten by the audits are prefixed `[v1.1]` (implementation) or `[v1.2]` (design), so a reader of any earlier version can find the delta. Where the two disagree, the later one governs.

---

# Part I — Problem, Scope, Requirements

## 1. Problem and mission

SCION gives endpoints path *choice* — an authenticated set of forwarding paths assembled from beaconed segments — but no path *quality* information beyond static metadata and expensive self-probing. Selection is therefore blind exactly where it matters: under congestion, asymmetry, and churn. The **Path Oracle** closes this loop as an **advisory telemetry and prediction service**: it ingests end-to-end observations donated by participating endpoints (hosts or gateways) plus targeted active probes and operator telemetry; estimates and forecasts the state of the network elements SCION paths are built from; and returns calibrated, composable predictions so that selectors can match paths to requirements — latency, throughput, reliability, cost/carbon, compliance. When uncoordinated selection would oscillate, it can act as an opt-in soft coordinator. It is never a dependency: SCION works identically with the oracle absent, ignored, or wrong.

**Prior art this design stands on** (and how): the OVGU Path Oracle (CNSM 2023) validated donation-based link-level scoring and empirically demonstrated the oscillation failure mode this design engineers against; GLIDS demonstrated the correct use of beaconing — slow-moving per-link propagation-latency floors — consumed here as the static prior layer; RouteNet-lineage GNNs and spatio-temporal traffic forecasting (DCRNN/Graph-WaveNet) supply the learned-model family; conformalized quantile regression supplies distribution-free calibration; selfish-routing theory (value-of-information results, FLOSS/CROSS) supplies the stability analysis; and the 2025 SCIONLab measurement studies supply the churn and asymmetry regime the design must survive.

**`[v1.2]` Why now, and why this has been tried before.** This design has a twenty-year-old predecessor that should be named rather than left for a reviewer to find. **iPlane** (OSDI 2006) built a structural model of Internet path performance and served predictions to applications — the same shape as this document, down to the choice of a structural model over black-box latency embeddings. Network coordinate systems pursued the same goal by other means. Neither is deployed at scale today.

The standard post-mortem blames distribution: iPlane's own stated limitation was that its data could not easily be distributed given its size, so serving from a few nodes capped the query rate. This design fixes that directly and deliberately — per-AS caches and a signed bulk mirror (ADR-02, ADR-08) exist because that failure is known.

But the deeper reason is not scale, and it is the reason this attempt is differently placed: **IP applications had no way to act on a path prediction.** Knowing that a better path existed changed nothing, because the path could not be taken; the information had no actuator. Every prediction service for the Internet has had to argue that some overlay, some relay, some multi-homing arrangement might eventually consume its output.

SCION removes exactly that barrier. Path choice is native, host-constructed, and exactly known (G1) — the actuator is in the architecture rather than bolted beside it. **That is the strongest argument this project has**, and it is also a discipline: it means the parts of this design that are worth building are the ones that improve a *selection decision*, and the parts that merely improve a number are worth building only insofar as they do. §28's insistence that decision quality be measured separately from accuracy is the same principle, stated as an evaluation requirement.

## 2. The epistemic contract

The oracle's honesty is structural, not aspirational. Every served state carries: its **evidence tier** (corroborated / thin / prior-only / operator-attested / remote-attested), the **diversity** of the evidence behind it, the **identifiability class** of the unit, its staleness, and the trailing empirical coverage of its intervals. "I don't know" — a prior-anchored wide interval with an honest tier label — is a first-class answer, and much of the trust design consists of forcing that answer where a naive system would manufacture confidence. A consumer can always reconstruct *why* the oracle believes what it serves.

## 3. Deployment profiles

Two profiles share one architecture and wire protocol but differ in who reports, what statistics may assume, and how trust is established. The profile is a deployment property; mixed deployments run both.

**Profile A — Host-Crowd** (SCIONLab, research ISDs, native SCION applications). Many path-aware end hosts run the client agent. Statistics assume vantage diversity; trust rests on AS-bucketed robust aggregation with per-identity reputation. The Waze analogy applies here and only here.

**Profile B — Gateway-Federated** (production ISDs of the SSFN class). Dominant traffic is legacy IP through SCION-IP gateways; **path selection happens at the gateway**, and the realistic reporter is a small number of gateway devices per member AS. Reports are high-volume aggregates over many legacy flows; vantage diversity is structurally low; trust rests on identified, contractually governed participation, anchors, cross-gateway consistency on shared core units, and optional AS interior telemetry. Robustness math is leave-one-out sensitivity capping, not crowd trimming. Units observable from a single member are served, honestly, as *uncorroborated-operator* state.

## 4. Requirements

**Functional.**

- **F1 — State estimation and forecasting.** For every prediction unit (§8) and metric in {round-trip latency contribution, loss exceedance, achievable goodput, liveness}, produce **mesoscale state** (the current ≥15 s window) as a predictive distribution, plus forecast horizons {+60 s, +300 s} that ship *only* for strata where they demonstrably beat persistence (§28).
- **F2 — Path-level scoring.** Compose unit-level predictions into path-level predictive distributions with calibrated intervals, for any legal segment combination a client presents.
- **F3 — Authenticated reporting.** Accept measurement reports authenticated with SCION-native mechanisms (DRKey/SPAO; Profile B may substitute contractual mTLS identities), with replay protection, rate control, and abuse-resistant aggregation.
- **F4 — Dissemination.** Serve predictions via request/response query, subscription push, and a signed bulk unit-state mirror for caches and destination-hiding clients.
- **F5 — Federation.** Exchange signed link-state summaries with peer-ISD oracles so inter-ISD paths score end-to-end.
- **F6 — Coordination (opt-in, gated).** Detect prediction-induced oscillation; for affected destination aggregates, publish damped probabilistic path shares to opted-in clients.
- **F7 — Measurement steering.** Embed sampling hints in responses so the crowd's probe budget concentrates where the oracle is uncertain *and* probing can help; mandate a small exploration floor on compliant elastic traffic so the training data never collapses onto the serving policy.

**Non-functional.**

- **N1 — Advisory-only, fail-open.** SCION's planes never depend on the oracle; clients fall through a defined degradation ladder (§26); connectivity is unaffected by oracle absence, error, or compromise.
- **N2 — Freshness.** Dynamic-metric staleness ≤ 15 s median, ≤ 60 s p99; this is also the product's temporal floor — the oracle is a mesoscale instrument by design.
- **N3 — Query latency.** ≤ 10 ms p99 at the oracle in-ISD; ≤ 1 ms at a per-AS cache.
- **N4 — Calibration (guaranteed in the long run, and monitored).** `[v1.2]` Advertised intervals carry a **long-run coverage guarantee on arbitrary sequences** — no assumption on the data-generating process, and in particular no assumption that the oracle's own influence on it is bounded (§19.5). Trailing empirical coverage per bucket is computed continuously, served as metadata, alarmed outside ±3 %, and drives per-bucket fallback. v1.0 claimed only "monitored under bounded shift", which was the honest statement of what split conformal delivers; the online construction in §19.5 is a strictly stronger guarantee at negligible cost, and monitoring is now the backstop rather than the whole claim. The performativity diagnostic (§19.6) remains part of calibration health.
- **N5 — Manipulation resistance (per profile).** Profile A: with ≤ f = 10 % adversarial identities and evidence-diversity gating active, no unit's served median shifts beyond ε without the unit demoting to a lower tier. Profile B: no single reporter moves any *shared* unit's served median beyond a leave-one-out cap; single-source units are never served as corroborated.
- **N6 — Privacy (stated without euphemism).** Participation is **identified to the oracle** (reputation, replay protection, and contracts require stable identity; reported paths reveal source ASes regardless). Protected: content (never collected), destination hosts (never collected — AS granularity only), volumes (bucketed), timing (coarsened to 1 s), retention (raw path observations ≤ 30 d; aggregates thereafter), jurisdiction (per-ISD residency), query intent (bulk mode). Not protected: the operator's view of linkable communication patterns at AS granularity within the retention window — the explicit trust grant of participation, which is why operatorship is an ISD-governance role. A privacy ladder (field coarsening → local-DP noise → secure aggregation → federated fine-tuning) is available per deployment; unlinkable pseudonymity is *not claimed*.
- **N7 — Scale envelope.** Reference ISD: 200 ASes, up to 10 k prediction units, 100 k reporters (A) or ~10² gateways (B), 5 k reports/s, 100 k queries/s cache-assisted (§25).
- **N8 — Evolvability.** Versioned schemas; model registry with shadow, canary, rollback.
- **N9 — SCION-nativeness.** No data-plane changes; no required control-plane changes; beacon metadata consumed as static priors, never extended with dynamic state. `[v1.1]` This is a scheduling constraint, not a stylistic preference. SCION's control and data planes are specified in individual Internet-Drafts (`draft-dekater-scion-controlplane`, `draft-dekater-scion-dataplane`) that are neither working-group documents nor standards-track, and that carry the standard disclaimer of having had no formal IETF review. A design requiring protocol change would be negotiating against a moving target with no ratification path and no date. Everything the oracle needs already exists in the deployed protocol; N9 keeps it that way deliberately.

**Non-goals.** No bandwidth reservation, no path authorization (hop-field MACs), no intra-AS TE, no admission control, no mandatory dependency, no coverage beyond links appearing in registered SCION segments. `[v1.1]` Earlier drafts attributed reservation to COLIBRI. COLIBRI has no implementation in the current tree — only its design document survives — so the non-goal is stated on its own terms: reservation is a different mechanism with different guarantees, and an advisory predictor must not be mistaken for one whether or not a reservation system exists to point at.

---

# Part II — The Observability Foundation

Everything in this design is derived from a precise statement of what SCION endpoints can and cannot know. This part is normative for every component.

## 5. Ground truth (G1–G4)

**G1 — Forward paths are host-constructed and exactly known.** A forwarding path is assembled from at most three authenticated segments (up, core, down), including crossover joins at core or common-ancestor ASes and peering shortcuts — joins whose AS traversal (ingress-IF from one segment, egress-IF from another) exists in no single segment. Chained hop-field MACs enforce that the data plane follows *exactly* the encoded interface path. Consequence: forward-path attribution carries **zero route uncertainty** — unlike Internet tomography, "which path did this sample traverse" is a *trust* question about the reporter, never an inference question about the network.

> `[v1.1]` **Two bounds from the implementation, both of which tighten later arithmetic.** First, "at most three segments" is normative, not a heuristic: the beacon-metadata specification states that end-to-end paths are obtained by combining up to three (up, core, down) segments, and the path combinator builds exactly that, representing peering links as vertices keyed on (IA, ingress, peer-IA, remote-IF). Second — and not previously accounted for — the combinator discards any path in which **an AS appears more than twice** in the interface sequence (`filterLongPaths`). That is a hard cap on the combination closure of §6: it prunes precisely the pathological repeated-crossover constructions that would otherwise make a high-degree core AS's pair count explode. §25's scale arithmetic reasons from it.

**G2 — Endpoint observations are end-to-end aggregates.** Hosts and gateways observe transport-level RTT, loss, and goodput over a whole path; the data plane exposes no per-hop state to endpoints. Every passive observation is a sum (latency), a min-censoring (goodput), or a survival product (loss) over the traversed elements.

**G3 — RTT couples the return path, which the reporter does not choose — and which is, by default, the same links reversed.** Because forwarding state is packet-carried, SCMP echo responders and standard server stacks reply over the exact *reversed* interface path. The typical RTT sample therefore constrains Σ(x_e→ + x_e←) over one known link set. Per-direction latency terms are **not identified** by any number of such samples from any number of vantage points — all yield equations of the same bidirectional form. Where a path-aware peer chooses a different return path, the reporter cannot observe it, converting variance into bias. Directional latency information enters only through one-way-delay-capable sources.

> `[v1.1]` **This is not a tendency; it is one function.** `snet.DefaultReplyPather.ReplyPath` decodes the received path and returns `p.Reverse()`, and the border router does the same for SCMP replies. G3 therefore holds by construction for any peer using the default stack, and the failure case is precisely characterised: an application that installs its own reply pather. That is exactly what the `reply_path_known` flag in §13 exists to declare, and why a sample lacking it is excluded from decomposition rather than downweighted — the edge set it constrains is genuinely unknown, not merely noisy.

**G4 — The one per-hop instrument is SCMP traceroute, with a characteristic noise.** Router-alert flags elicit SCMP replies from each border router over the reversed *prefix*: RTT to hop *k* is the bidirectional prefix sum Σ_{i≤k}(x_i→ + x_i←) plus a slow-path processing term δ_k that is deprioritized and unrepresentative of data-plane latency. Consecutive differences estimate per-element *bidirectional* contributions with heavy-tailed noise. Traceroute is a genuine decomposition instrument — but coarse, and never directional.

> `[v1.1]` **Deprioritized is confirmed; rate-limited is not.** The router handles router-alert traceroute on its explicit *slow path* (`slowPathRequest` / `slowPathPacketProcessor`), so δ_k is exactly the scheduling artefact described. But SCMP rate limiting is only *permitted* by the protocol specification — "may be subject to rate limiting" — and no rate limiter exists in the audited router. **The censoring model is therefore conditional on the deployment.** Where an operator rate-limits, traceroute replies are missing-not-at-random and the estimator must model the censoring; where nothing rate-limits, the noise is heavy-tailed but missing-*at*-random, and treating it as censored introduces a bias that is not there. These call for different estimators, so the probe-budget parameter is measured in M0 (§29) rather than assumed, and Tier-0's traceroute ingestion (§19.1) selects its noise model from that measurement.

## 6. Element substrate: the combination closure

Atomic elements are **directed edges** of the interface graph: both directions of every inter-AS SCION link, plus intra-AS traversal edges. The traversal universe is the **closure of the path-combination rules over the current segment inventory**: for each AS, all (ingress, egress) interface pairs realizable by (i) consecutive hops within one segment, (ii) crossovers between combinable segment pairs at that AS, (iii) peering-link joins, and (iv) segment-endpoint entry/exit. A background job recomputes the closure on segment-inventory change. Core ASes with many interfaces dominate closure size; the scale budget (§25) accounts for this, bounded by G1's two-appearances-per-AS filter.

`[v1.1]` **The closure is advertised, not merely derivable.** SCION's beacon metadata performs this enumeration already, for the same reason, and specifies the rule: each `ASEntry` carries the inter-AS hop at the egress interface, the intra-AS hop between egress and any other CHILD-link interface *with ID smaller than the egress* (for shortcuts), the intra-AS hop between egress and any other CORE-link interface (for up-core and core-down crossovers), and the intra-AS hop between egress and any PEER-link interface together with the inter-AS hop at that peer interface. The closure job should therefore be *seeded* from `StaticInfoExtension` rather than derived from scratch: `LatencyInfo.Intra`, `BandwidthInfo.Intra` and `InternalHops` are keyed by exactly these interfaces. Two consequences. The intra-AS traversal edges this section makes atomic are first-class in the protocol, which is a firmer footing than derivation. And the advertised set is deliberately asymmetric — only child interfaces *below* the egress ID — so the computed closure is strictly larger than the set for which any metadata exists. That difference is a **predictable prior-only region**, and §17.5's tiering should expect it at startup rather than discover it as anomalous sparsity.

**Dispositions for an element outside the closure.** `[v1.1]` v1.0 offered two, and the audit found a third and fourth that matter:

1. **Stale closure** — reconcile against the control service, then re-derive. Never assume.
2. **Fabricated claim** — the element is derivable from no inventory; score the source (§17.6).
3. **Hidden path** `[v1.1]` — SCION supports hidden path communication: down-segments registered not publicly but at hidden segment services enforcing access control, with the corresponding up-segment optionally registered as hidden locally. An AS authorized for such a segment reports a path the oracle's *public* inventory cannot contain. Under the v1.0 rule this honest reporter is scored as an attacker. It must not be. See the hidden-path handling below.
4. **Out-of-inventory but re-derivable on refresh** — a segment registered between the closure rebuild and the report; the reconcile in (1) resolves it.

**Hidden-path handling (normative).** `[v1.1]` A report whose elements are absent from the public closure but which is otherwise well-formed and authenticated is treated as *possibly hidden* rather than fabricated. Such elements enter a **non-served partition**: state is estimated and retained, the reporter is never scored for the report, and the unit is **never emitted in `SyncSnapshot`** and never appears in the bulk mirror. `GetUnitStates` answers for it only to a requester that has already demonstrated knowledge of the segment — the natural test is possession of the hidden segment itself, presented in the request. `ScorePaths` composes it normally when the client supplies the full path, because a client presenting a hidden path already knows it.

The reason is disclosure, not trust: serving a unit that appears only in hidden segments tells any querier that a link exists and carries traffic, to exactly the parties whose reason for using hidden paths is that this not be known. This is a real cost — hidden-path users get less from the oracle than public-path users, and their units never reach corroborated tier from public evidence — and it is the correct trade. It is stated here rather than left implicit so that a deployment cannot quietly choose the other way. §24 carries the privacy consequence.

## 7. Edge substrate and unit view

**Storage, filtering, and identity live on directed edges** (and their round-trip pairings, §8), stable for the lifetime of the adjacency. **Units** — what the oracle predicts and serves — are a *versioned view*: a partition of edges into tomographic equivalence classes, merging edges that no available evidence can separate. View changes carry continuity rules: on split, children inherit the parent state with inflated variance and a recently-split tag that attracts exploration; on merge, the child is the evidence-weighted combination of parents; Tier-0 filter state survives untouched on the edge substrate. Model features, calibration buckets, and snapshots reference (view-version, unit-id); clients treat a view change like any snapshot update.

### 7.1 `[v1.2]` The partition is computed from an identifiability condition, not a heuristic

v1.0 said "merging edges that no available evidence can separate" and left the criterion unstated. That sentence is exactly right and it has an exact meaning, which this section now supplies. Getting it right is not cosmetic: without a criterion, ADR-05's decoder is asked to produce latents that may be unidentified, and regularising an unidentified parameter is the thing ADR-13 forbids. **Defining the unit as the identifiable object is what makes ADR-05 and ADR-13 consistent** (see ADR-05).

**Construction (additive metrics).** Over the training/serving window, stack the observed path constraints as an incidence matrix **A** — one row per measured path, one column per element, entries the multiplicity with which the path traverses that element. For an additive metric, the linear functionals of the element vector that any set of such measurements can identify are exactly those in **row-space(A)**. A partition {G_i} therefore has each Σ_{e∈G_i} x_e identifiable **iff** every indicator vector 1_{G_i} lies in row-space(A).

The finest partition with that property has a one-line characterisation: **merge elements *e* and *f* exactly when columns A[:,e] and A[:,f] are identical.** Two elements that occur in the same measured paths with the same multiplicity, and in no others, cannot be separated by any linear combination of those measurements — no estimator, no prior, no amount of data of that kind. Computing it is column hashing: O(nnz) in the observation set, recomputed with the view.

This is the classical identifiable-link-set construction of network tomography, and the field's identifiability and measurement-design results apply directly once the view is defined this way (He, Ma, Swami & Towsley is the standard treatment). The practical gain is that **SP-ness becomes checkable rather than assigned** (§8) and **steering acquires a computable objective** (§20.5).

**Exact for latency and loss; weaker for goodput.** ρ-latency is additive by construction and loss is additive in log-survival, so the construction is exact for both. **Goodput is not additive** — it composes by minimum — so a capacity-limited observation yields an *equation* at the bottleneck and *inequalities* everywhere else. The identifiability condition is correspondingly different and stricter: an element that is never the bottleneck on any observed path is unidentifiable for goodput no matter what its column pattern looks like. Goodput therefore carries **its own, coarser identifiability accounting**, and the served view may legitimately be finer for latency than for goodput on the same elements. Reporting one number for "the unit view" across all metrics would be a false economy; the snapshot carries the per-metric identifiability state.

**Observed versus obtainable, and why both are needed.** The served partition is computed from what has been *observed* — that is what is identified now. The ambiguity class (§8) is computed from what is *obtainable* over the combination closure — that is what steering could still fix. The gap between the two is precisely the region a probe budget can buy, and naming it is what turns §20.5 from a heuristic weighting into an optimisation.

**Window and drift.** A is assembled over the window whose observations jointly constrain the estimate; each observed row constrains regardless of when in the window it arrived, so the window's *union* of rows is the correct basis. Because A drifts as paths appear and vanish, the partition drifts with it — which is what the view versioning above exists for. A model trained against view *v* serves view *v*; the transition to *v+1* is a split-or-merge with the stated continuity rules, and a split whose driver was a *new measurement* rather than new traffic is the healthy case, because it means the steering budget worked.

## 8. Identifiability, ρ-units, and ambiguity classes

**Latency lives on round-trip pairs.** For each element *e*, define ρ_e = x_e→ + x_e← (for an intra-AS traversal i→j, the pair is (i→j, j→i), which reversed replies do traverse). By G3–G4, essentially all latency evidence — reply-reversed flows, SCMP echo, traceroute differences — constrains sums of ρ over known element sets. The oracle therefore predicts and serves **ρ-units**. A **directional refinement layer** sits above: where directional evidence exists (OWD-capable anchors; G-SINC-class synchronized sources; cooperating endpoint pairs co-reporting both directions), it estimates a split fraction ρ_e→/ρ_e with its own uncertainty; absent such evidence the split is served as the floor-proportional prior with maximal split uncertainty — and selectors needing only round-trip latency (the overwhelming majority) never consume the split at all. Flow samples with `reply_path_known = false` from non-attesting peers are excluded from decomposition: they constrain an unknown edge set.

**Goodput and loss are genuinely directional** — the data direction is known from the reporter's declared role — and remain on directed edges.

**Ambiguity classes.** Each equivalence class carries a resolution label: **TR** (traceroute-resolvable), **OWD** (directionally resolvable given a one-way-capable vantage), or **SP** (structurally permanent — only AS interior telemetry resolves them). The steering budget spends only on TR classes; SP classes are served at class granularity indefinitely, honestly labeled. This label is part of the served state.

**`[v1.2]` The labels are derived, not assigned.** Given §7.1's construction, a class exists because a set of columns of **A** are identical over the *observed* rows. The label answers one question — *what row would separate them?* — and each answer is checkable against the combination closure rather than judged:

| label | condition | consequence |
|---|---|---|
| *(singleton)* | the element's column is unique among observed rows | identified; no ambiguity metadata |
| **TR** | the columns differ somewhere in the closure, and a **feasible traceroute schedule** realises a row on which they differ | steerable: §20.5 can buy the split, at slow-path-noise cost |
| **OWD** | the columns are identical over every *bidirectional* row but the class is a ρ-pair whose directions differ | splittable only by directional evidence; the refinement layer's target |
| **SP** | the columns are identical over **every row realizable in the closure** | structurally permanent: no end-to-end instrument from any placement can ever separate them. Serial chains and single-vantage stubs fall out of this condition rather than being enumerated into it |

The crisp version: **observed-identical columns are merged now; closure-identical columns are merged forever.** The difference between the two sets is exactly the region a probe budget can act on, and §20.5's objective is to shrink it.

Two consequences worth stating. SP-ness is now a property that can be *tested* — and therefore can be wrong, and therefore can be a bug rather than a judgement call, which is the point. And a class can be **reclassified without any new traffic**: adding a peering link to the closure may make two previously-SP elements separable in principle, moving them to TR and into the steering budget's reach. The closure job (§6) and the ambiguity labelling are therefore the same recomputation, and the label is versioned with the view.

## 9. Metric definitions

- **Round-trip latency contribution** ρ_u, anchored on γ_u, the metadata-derived latency prior where advertised metadata (StaticInfo/GLIDS-class) exists — used for *soft anchoring only* unless anchor-corroborated (§17). Served as a parametric residual distribution around γ_u. `[v1.1]` **γ_u is a prior, not a floor.** See the correction below; v1.0's `ρ_u ≥ γ_u` is withdrawn.
- **Achievable goodput** (deliberately not "available bandwidth"): the goodput a reference loss-based flow would attain if bottlenecked at this unit, represented as a **survival function on a fixed log-spaced rate grid** (16 points, 1 Mbit/s–100 Gbit/s). Passive goodput is *censored* evidence: a capacity-limited flow yields a value at the bottleneck and a lower bound everywhere else it traversed; app-limited intervals are lower-bound-only everywhere. `cc_algo`, RTT, and concurrent-flow context are covariates of the reference-conditions correction.
- **Loss as exceedance:** survival probabilities P(loss ≤ τ) at τ ∈ {10⁻⁴, 10⁻³, 10⁻², 10⁻¹} per directed unit, plus a burst-severity summary. No quantile modeling of zero-inflated rates.
- **Liveness:** survival = **deterministic × residual**. The deterministic component — hop-field/segment expiry — is read exactly from path metadata, never estimated. The residual is a non-parametric hazard (Nelson–Aalen-class) over *unscheduled* transitions per unit, plus a per-(src-AS, dst-AS) segment-churn residual hazard; neither is assumed memoryless. `[v1.1]` v1.0 sourced the residual from "control-plane events". That source no longer exists — see **Liveness, corrected** below.
- **Static layer:** cost/carbon/geo/link-type/compliance attributes consumed from beacon metadata; slow-moving; disseminated with long TTLs.

### 9.1 `[v1.1]` γ_u is a symmetric conservative estimate, not a propagation floor

v1.0 defined γ_u as "the summed bidirectional propagation floor" and §19.3 made `pred ≥ γ_u` **structural** via a softplus construction. Both are withdrawn, because `StaticInfoExtension` does not advertise a floor. It advertises a symmetric estimate, and where the two directions differ it is *defined* to advertise the worse one:

> SCION paths and path segments are reversible. The system described here does not allow representing values differing depending on the usage direction. Instead, we require that all metadata items represent quantities that are symmetric. If a path hop has e.g. different latency in the two directions we can just pick the more conservative estimate.
> — SCION beacon-metadata specification, *Symmetry*

**Why this breaks a structural floor.** Take an asymmetric hop with true one-way delays 4 ms and 12 ms, so ρ = 16 ms. StaticInfo advertises 12 ms, the conservative direction. Doubling for a round-trip gives γ_u = 24 ms, and a structural floor now makes it *impossible* for the model to predict the truth. The failure is silent: predictions are confidently wrong, uncertainty reflects the constraint rather than the network, and the error is largest exactly on the asymmetric links a path-aware architecture exists to exploit. The direction of the bias is set by whichever AS filled in the field.

**Normative correction.**

1. γ_u is a **penalised soft anchor**, never a hard constraint. §19.3's latency head loses the softplus floor construction and gains an asymmetric penalty on excursions below γ_u — cheap to violate a little, expensive to violate a lot, and always possible.
2. Every γ derived from StaticInfo carries an explicit **asymmetry-uncertainty term**. Absent directional evidence, the split between the two constituent one-way delays is unknown, and γ_u = 2 × (advertised) is an upper-biased estimator of ρ_u whose bias equals the undeclared asymmetry. That uncertainty is part of the served prior, not hidden inside it.
3. **Anchor corroboration is what promotes a soft anchor to a hard bound.** Where an anchor's own measurement confirms the advertised value, the floor may be enforced; where it does not, γ_u stays a prior. This is §17.1's existing principle — "an uncorroborated floor is a prior, not a law" — finally applied to the model architecture rather than only to reporter scoring.
4. The same correction applies to **bandwidth**. `BandwidthInfo` is symmetric under the same rule, so the StaticInfo-derived ceiling clamp in §19.3 inherits the identical problem and the identical fix.

This is the second time this document has had to withdraw a confident construction over an unidentified parameter; ADR-13 was the first. The pattern is worth naming: **where SCION declines to represent something, the oracle must not manufacture it, and must not encode the manufactured version as an architectural invariant.**

### 9.2 `[v1.1]` Liveness, corrected: BFD below, unauthenticated SCMP above

v1.0 fed the residual hazard from "control-plane events (registrations/expiries)". The mechanism that would have carried a link going down — **path revocation — was removed from SCION.** Nothing in the audited control service or router originates a `RevInfo`; the surviving handler code is receive-side vestige, and SCION's own design documentation refers to revocations in the past tense as something "we historically used to have". What actually happens now:

- **Below:** border routers run **BFD** between themselves, with a default desired transmit interval of 200 ms. This is the real failure detector, and it is sub-second.
- **Above:** on BFD declaring a session down, the router emits SCMP `ExternalInterfaceDown` or `InternalConnectivityDown` on its slow path. These are *data-plane* messages, and endpoints react to them directly.
- **And they are not authenticated.** SCMP authentication is specified but formally **on hold**: "Status: Postponed. Reason for postponement: no clear consensus that this is the right approach", with experimental router support only. The stated alternatives under consideration range from full DRKey/SPAO authentication to expanded endpoint heuristics with no authentication at all.

**What this changes in the model.** The residual hazard is not estimating "when will this link fail". BFD already knows that, in 200 ms. The residual hazard is estimating **the gap between the network knowing and the endpoint population knowing** — SCMP delivery to whoever happened to be sending, plus the oracle's own dissemination latency. That is a different quantity with a different shape: it is dominated by traffic sparsity on the affected unit, not by link reliability, which means it is *worst exactly where evidence is thinnest*, and the tiering of §17.5 must be read as applying to liveness as much as to latency.

**What this changes in §16.** BFD session state is now the cheapest possible interior-telemetry export: small, already maintained by every router, high-value, and requiring no new instrumentation from the operating AS. §16 previously asked for per-element or per-SP-class state summaries, which is a much larger ask. BFD state should be its first and lowest-friction rung.

**What this changes in security.** An unauthenticated liveness signal that the oracle amplifies is a new attack. It is specified in §23 and gated in §20.3.

### 9.3 `[v1.1]` The deterministic component has a quantum, and it is coarser than N2

"Read exactly from path metadata" is right, and v1.0 never stated what that metadata's resolution is. It is not continuous:

| quantity | value | source |
|---|---|---|
| Maximum hop-field TTL | 24 h | `path.MaxTTL` |
| Expiry quantum | 24 h / 256 = **337.5 s** (~5 m 38.5 s) | `expTimeUnit` |
| Encoded expiry | `Timestamp + (1 + ExpTime) × 337.5 s`, `ExpTime` an 8-bit field | hop-field layout |
| Default registered segment lifetime | `DefaultMaxExpTime = 63` ⇒ 64 × 337.5 s = **6 h** | beacon policy |
| Default beacon origination / propagation / registration interval | **5 s** each | control-service config |

Three consequences.

**The two halves of the liveness metric have incompatible granularity.** N2 targets 15 s median staleness, and the deterministic component resolves to 337.5 s. Near expiry the product `deterministic × residual` is therefore dominated by a step function whose step is 22× the freshness target. Composition (§10) must not present the deterministic component as if it were as sharp as the residual, and the served representation carries the quantum so a client can see the granularity rather than infer it.

**§20.6's reliability selector needs the quantum to size its lead time.** "Schedules re-resolution ahead of expiry" is only well-defined against a known resolution: the selector must re-resolve at least one quantum before the encoded expiry, because it cannot distinguish positions within the quantum.

**The re-beaconing ratio is about 4,300:1.** A six-hour segment against a five-second beaconing interval means the *identity* of a path is stable across enormous numbers of refresh rounds (G1, ADR-01) while its *cryptographic material* is replaced constantly. The oracle's unit state is keyed on the former, and §10's "deterministic expiry = exact minimum over units" is exact because it is a minimum over hop-field `ExpTime` values, each of which is one of 256 possibilities.

## 10. Canonical distributional composition

Defined once; the canonical `oracle-compose` library implements exactly this, server- and client-side.

**Served representations.** Latency: (γ_u exact; residual log-normal μ_u, σ_u). Goodput: survival values on the fixed rate grid. Loss: survival values at fixed thresholds. Liveness: (expiry timestamp exact; residual hazard on a fixed age grid).

**Dependence model.** Units partition into **correlation groups** — units sharing an AS, or flagged by the model as sharing a probable queue/bottleneck. Composition treats units *within* a group as comonotone (worst-case co-movement) and groups as independent. The structure is deliberately simple; its residual error is systematic, and systematic error is what Tier-2 calibrates.

**Operators.** *Latency:* floors add exactly; residuals combine by moment-matched log-normal summation — within a group, scale parameters add (comonotone; conservative); across groups, variances add (independent; optimistic). *Goodput:* path survival at each grid rate = product of unit survivals across groups × minimum within a group; quantiles read off the grid. *Loss:* log-survival adds across groups; minimum survival within a group. *Liveness:* deterministic expiry = exact minimum over units; residual hazards add. Bias directions are declared per metric so calibration corrections are interpretable.

**Reproducibility.** Fixed grids, float32, round-to-nearest-even, canonical unit ordering: a path score is reproducible from (snapshot version, path, library version) within a declared tolerance (one grid cell / 1 % relative). Bit-exactness across heterogeneous hardware is explicitly not claimed.

**Calibration contract.** Tier-2 calibrates *this* operator per (metric, horizon, hop-count bin, intra/inter-ISD, correlation-group density) — the approximation corrected is the approximation specified.

---

# Part III — Decision Records (final form)

Seventeen decisions shaped this design. Each is stated with its choice, the alternatives it beat, and its reopening condition. Together they are the design's argument.

**ADR-01 · Prediction granularity: tomographic units over an edge substrate; never per-path.** Per-path modeling fails on combinatorics, cold start, and shared-edge waste; per-raw-edge modeling overclaims what end-to-end evidence identifies; per-segment modeling ties state to objects with hours-scale lifetimes while the underlying links persist. Units — equivalence classes exactly as fine as evidence supports, computed as a versioned view over stable edges (§7) — inherit edge-level generalization (unseen paths score from their units on day one) without false precision, and carry ambiguity labels (§8) so coarseness is visible, not hidden. `[v1.1]` **The identity argument is stronger than the lifetime argument, and the implementation settles it.** SCION's canonical path identifier, `snet.Fingerprint`, is a SHA-256 over the ordered sequence of (ISD-AS, interface-ID) pairs and *contains no cryptographic material at all* — no segment IDs, no hop-field MACs, no timestamps. The path combinator reinforces it: by default, multiple constructions of the same interface sequence collapse to one entry keeping the latest expiry, and the distinct segment IDs and MACs are visible only to a caller who explicitly asks, for the stated purpose of checking whether an AS misbehaves based on them. So a consumer keying state on the fingerprint retains it across re-beaconing *by design*, not by luck — which matters at a re-beaconing-to-lifetime ratio of roughly 4,300:1 (§9.3). Per-segment modelling is wrong not merely because segments are short-lived but because the protocol has already told you which object is the identity. *Reopen if* production unit graphs collapse too coarse for path discrimination — the remedy is AS interior telemetry for SP classes and traceroute budget for TR classes, not a different abstraction.

**ADR-02 · Placement: per-ISD oracle instances, per-AS caches, thin federation.** The ISD is simultaneously SCION's trust, routing-locality, governance, and jurisdiction boundary; a global oracle concentrates unwarranted trust and legal exposure; per-AS oracles starve statistically and maximize federation fan-out; full decentralization makes robust aggregation and calibration accounting research problems for no deployment-driven reason. Per-ISD placement matches data locality, keeps donations in-jurisdiction, bounds blast radius, and needs federation only for core links and exported summaries — mirroring how SCION's own control plane crosses ISDs. Per-AS *caches* recover latency without fragmenting statistics. Profile B strengthens this: gateway-federated telemetry is naturally ISD-governed. *Reopen if* an ISD exceeds ~10³ ASes — shard internally, still one logical instance.

**ADR-03 · Predictor structure: a three-tier cascade.** Tier-0 (online robust filters, always-on) is the availability floor and feature source; Tier-1 (learned spatio-temporal model) adds spatial transfer, seasonality, and forecasting; Tier-2 (conformal calibration) converts both into honest intervals. A single learned model couples availability to MLOps health and has no cold-start fallback; pure statistics cannot transfer across topology or forecast. The cascade also defines failure semantics: per-unit/per-bucket circuit breakers fall back to Tier-0-with-inflated-intervals — the reason every pathology found in review was repairable rather than fatal. *Reopen if* Tier-1's decision-regret reduction stops justifying its cost in a deployment — ship Tier-0+Tier-2 there; the architecture is unchanged.

**ADR-04 · Tier-1 family: edge-centric spatio-temporal GNN with a TCN temporal core.** Message passing on the line graph of the unit graph (where congestion correlation lives) plus gated dilated temporal convolutions (training stability, parallelism, controlled receptive field), multi-horizon heads. Steady-state simulator-surrogate GNNs answer the wrong (forward) question; independent per-unit sequence models forgo the spatial transfer that graph models demonstrably add in isomorphic forecasting problems; graph transformers/foundation models are parameter-hungry for a ≤10 k-unit graph and pay off only with multiple deployments to transfer between. Inductive representation is mandatory: units are feature-representable (priors, topology role, tier, ambiguity class); optional ID embeddings train with dropout so the model never depends on them.

`[v1.2]` **Restated: Tier-1 is a slot with a declared incumbent, not an architecture commitment.** The reasoning above is sound about *why* a graph — congestion correlation lives on adjacency, and spatial borrowing is how an under-observed unit gets scored at all — but it argues against the wrong comparison and it rejects one option for a reason that has expired.

- **The relevant comparison is not GNN-vs-sequence-model.** A plain MLP with spatial and temporal *identity embeddings* (the STID result) matches or beats a range of spatio-temporal GNNs on standard benchmarks. Its gain is attributed to the identity embeddings — which this decision explicitly forbids depending on. That may well be the right call here, since units split, merge and appear, and a new unit must score on day one. But it means the published STGNN comparisons are not evidence for *this* architecture, and the decision should not lean on them.
- **The dismissal of foundation models is backwards for zero-shot.** "Pay off only with multiple deployments to transfer between" is true of a shared *fine-tuned* backbone and false of a pretrained one: the pretraining **is** the transfer, so there is nothing to accumulate. Evaluated on real ISP telemetry, a zero-shot time-series foundation model outperformed trained GRU/LSTM baselines and held error nearly flat across horizons where the trained models degraded — at inference cost small enough to run on a CPU. That directly addresses §19.4's cold start, which currently depends on emulation pretraining.
- **And the same evidence carries the warning.** The identical study saw the foundation model collapse on the sparsest series. Nothing rescues a thin unit. This is confirmation that the **tiering does the real work** (§17.5), not an argument for any architecture.

**Therefore, two changes.** First, **split the question Tier-1 is asked.** §28 already distinguishes them and the model choice should follow: *(a) does spatial structure improve the nowcast* — this is where a graph earns its place, because it is how an under-observed unit borrows strength; *(b) does anything beat persistence at +60 s / +300 s* — a purely temporal question where a pretrained model is a strong and cheap candidate. Expect different answers, and ship per stratum accordingly. Second, **run the bake-off before committing the MLOps stack**: persistence, an inductive MLP over the same features, a zero-shot foundation model, and the edge-centric STGNN below as incumbent. A hybrid is admissible and possibly best — a frozen pretrained temporal encoder feeding learned spatial message passing keeps the spatial mechanism while removing the temporal training burden.

**Incumbent architecture** (unchanged, and what the bake-off must beat): message passing on the line graph plus gated dilated temporal convolutions, multi-horizon heads, parameter budget ≤ 10 M at the 10 k-unit envelope. *Reopening condition, fired:* v1.0 said "reopen when ≥3 ISD-scale deployments exist — evaluate a shared pretrained backbone". Zero-shot models removed that precondition, so it is reopened now rather than in three deployments.

**ADR-05 · Supervision: composition-decoder training on path-level labels.** The model predicts latent unit states; the fixed metric-specific operator (§10) maps them to path predictions; loss is computed at path level against what reporters actually measured, plus unit-level loss where direct evidence exists, plus physics regularizers. Supervising only direct observations wastes the dominant (path-level) data; a separate tomography-solver stage would turn its own errors into training targets and destroy uncertainty at the interface. Folding composition into the decoder makes the tomographic inverse part of the objective: overlapping path constraints jointly pin unit latents, and priors enter as differentiable regularizers.

`[v1.2]` **This decision was in tension with ADR-13 until §7.1 fixed it, and the tension is worth recording because it nearly shipped.** ADR-13 forbids regularising an unidentified parameter, on the grounds that the result is confident fiction whose uncertainty reflects the prior rather than the network. But "overlapping path constraints jointly pin unit latents" is a *claim*, not a guarantee: whether they pin them is exactly the question of whether the path–unit incidence matrix has a null space, and v1.0's §8 SP class was a description of that null space. Under the v1.0 heuristic view, the decoder would have been asked to produce latents inside it, with the differentiable priors choosing the answer — doing to units precisely what ADR-13 forbids doing to directions.

**§7.1 closes it by construction, not by mitigation.** With the unit defined as the identifiable partition of the observed incidence matrix, every latent the decoder is asked for is identified by definition; where it would not be, the elements are merged into one unit and the decoder predicts the sum, which is identified. The priors then regularise *estimation variance* on identified quantities, which is ordinary statistics, rather than *selecting a point in a null space*, which is fiction. Anyone weakening §7.1's criterion re-opens this contradiction, so the two decisions must move together.

**Prior art worth reading before building the decoder.** Path-centric neural tomography is an active area: DeepNT infers path performance by aggregating learned embeddings of the constituent elements, and PlatoNT pursues a unified representation across tomography tasks. Both are candidate implementations of this decision. Neither removes the identifiability obligation — a learned inverse is still an inverse — so both inherit §7.1.

*Reopen if* rarely-traversed units show gradient starvation — the fix is exploration budget, not architecture.

**ADR-06 · Uncertainty: quantile-analogue heads + split conformal, path-level, monitored.** Uncertainty is simultaneously the tail-aware selection feature and the first anti-herding mechanism, so it needs calibration that is cheap at serving time and honest under drift. Ensembles multiply inference cost; Bayesian methods need post-hoc calibration anyway. Split conformal on *composed* predictions absorbs exactly what unit-level modeling cannot see (inter-unit correlation, composition bias). The guarantee is stated as monitored-under-shift (N4), with trailing coverage served as metadata and one-sided calibration for one-sidedly consumed quantities. *Reopen if* conditional-coverage audits show per-destination miscalibration despite bucketing — move to covariate-dependent conformal adjustments.

**ADR-07 · Data contribution: authenticated raw donation with a privacy ladder; not FL-by-default.** The sensitive object is report content; if reports are donated, federated learning adds no privacy, and if they are not, FL's gradients still leak participation while making Byzantine-robustness a gradient-space open problem instead of a tractable measurement-space one (physics clamps, buckets, anchors). Default: authenticated, minimized, in-ISD-resident donation — the Waze contract made explicit — with rungs (coarsening → LDP → secure aggregation → FL fine-tuning) purchasable per deployment at quantified accuracy/robustness cost. *Reopen* per-deployment on regulatory demand; the architecture supports every rung.

**ADR-08 · Dissemination: pull-primary + push + signed bulk mirror; beacons carry only static priors.** Beacon piggybacking of dynamic state imposes a broadcast cost on non-participants — a PCB reaches every AS on every propagated path whether or not it wants the payload — bloats PCBs, and builds on experimental extension formats; its correct use, slow-moving floors, is exactly what the static layer consumes. `[v1.1]` **A withdrawn argument.** v1.0's first reason was that beaconing "is bound to minutes-to-hours cadences". That is false: origination, propagation and registration all default to **5 s** upstream. The decision is unchanged — the broadcast-cost argument holds at any interval, and is in fact *strengthened* by a fast cadence — but the cadence clause is deleted rather than quietly repaired, because a decision record whose stated first reason is checkably wrong obliges a reader to re-verify the other sixteen. Pull serves one-shot queries; push serves long-lived interests; the mirror serves caches, offline tolerance, and destination-hiding clients, with per-subscriber jitter (ADR/M6 fix, §20.4) so it cannot become a synchronized herding substrate. *Reopen if* operators want oracle pointers in path-server responses — a pointer (endpoint + snapshot version), never dynamic state.

**ADR-09 · Composition happens on both sides via one canonical library.** Path-mode queries give convenience, freshest calibration, and joint candidate-set analysis (shared-bottleneck warnings); unit-mode gives scale, outage tolerance, and query privacy. Both are cheap iff the semantics are written once: the versioned `oracle-compose` library (identical code in oracle, cache, client) with the calibration table shipped inside the signed snapshot, reproducible within declared tolerance. Divergent reimplementation is forbidden by spec. *Never reopened* — this is a semantics invariant.

**ADR-10 · Stability: calibrated intervals + client discipline by default; coordination as a gated, damped, opt-in overlay.** Point rankings are empirically discredited in this exact setting (observed oscillation under synchronized re-consultation) and theoretically expected (best-response to shared state *is* the oscillation mechanism; finer information can worsen equilibria). Always-on central assignment overreaches: hosts hold private constraints, and coercion breaks N1. The ladder: intervals make near-ties visible; discipline (ε-argmax over the near-optimal set, minimum dwell, hysteresis, TTL-gated re-consultation, jittered timers) breaks synchronization; when the oscillation detector still fires, damped share vectors go to opted-in clients. Claims are *conditional on measured compliant share*; the defector fraction is a primary evaluation axis; mirror jitter bounds the defector regime. *Reopen after* the closed-loop campaign: ship coordination dormant if intervals+discipline suffice; strengthen opt-in incentives if not.

**ADR-11 · Report trust: SCION-native identity, profile-appropriate aggregation, anchor-referenced reputation — subordinated to diversity gating.** DRKey/SPAO gives cheap symmetric per-report authentication with AS-level accountability (Profile B may substitute contractual mTLS); identities are rate-limitable and sybils collapse into their AS bucket. Aggregation: Profile A — within-bucket reputation-weighted median, across-bucket trimmed/geometric-median with per-bucket influence caps; Profile B — leave-one-out sensitivity capping and cross-gateway consistency. Reputation: per-source Beta posterior on agreement with robust consensus ∪ anchors, decaying over 30 d. All of it feeds — and is overridden by — the diversity gate (ADR-17). *Reopen if* red-teaming breaks the bound under AS-level collusion — raise anchor density and cross-ISD anchor exchange.

**ADR-12 · Time: RTT-primary; OWD as tagged refinement evidence; coarse authenticated timestamps.** Requiring synchronized clocks gates participation on absent infrastructure and adds an attack surface. RTT is self-referential; OWD from attesting synced sources (anchors; future G-SINC deployments) is accepted as a separately tagged stream feeding the directional refinement layer (§8). Timestamps coarsen to 1 s inside the authenticated envelope — replay protection and privacy in one stroke. *Reopen when* synchronized coverage exceeds ~30 % of units — evaluate promoting directional latency (see ADR-13).

**ADR-13 · Latency lives on round-trip pairs; direction is a refinement, not an assumption.** Per-direction latency is unidentifiable from reply-reversed RTT evidence, which is nearly all latency evidence (G3): regularizing an unidentified parameter produces confident fiction whose "uncertainty" reflects the prior, not the network. Serving ρ-units serves exactly what evidence pins down and exactly what most selectors need; the refinement layer adds direction precisely where OWD evidence exists, with split uncertainty served separately. Dropping directionality everywhere would discard goodput/loss direction, which *is* identified via the role field. `[v1.1]` **SCION reached the same conclusion about its own metadata and wrote it down.** The beacon-metadata specification declines to represent direction at all — "the system described here does not allow representing values differing depending on the usage direction... we require that all metadata items represent quantities that are symmetric" — and resolves genuine asymmetry by advertising the more conservative direction. This is a stronger argument than the identifiability one alone: where the protocol's own static layer refuses to encode direction, an oracle that manufactures it from bidirectional evidence is claiming to know something the substrate does not carry. It also has a consequence v1.0 missed, which is §9.1: the same symmetry rule makes γ_u unsuitable as a hard floor. *Reopen when* OWD-capable coverage crosses ~30 % of units.

**ADR-14 · Composition is specified, simple, and calibrated — not inferred, clever, or exact.** Quantiles do not compose, so something must be chosen: full histograms explode payloads and still need a dependence model (the actually hard part); Monte-Carlo sample payloads break the mirror budget and determinism. Fixed parametric/grid summaries + the two-level dependence model + closed-form operators are the engineering optimum because their errors are *systematic and therefore calibratable* — and the declared bias directions make Tier-2's corrections interpretable. *Reopen if* calibration residuals show persistent cross-group dependence structure — enrich the group graph, not the operators.

**ADR-15 · The oracle is a contextual bandit and must pay for its data.** Predictions steer traffic; steered traffic is the training data; naive supervision eats its own tail — abandoned-unit staleness (phantom permanent congestion), self-negating forecasts, voided exchangeability. Full closed-loop identification is the right research program and the wrong v1 component. The pragmatic contract: mandated ε-exploration (1–2 % of *elastic* bytes, uniform over the feasible set, logged; latency-class traffic never taxed) keeps every unit observable at bounded QoE cost; `SelectionEvent` logs (candidate set, choice, scores shown, exploratory flag) enable inverse-propensity and doubly-robust off-policy evaluation — the only honest decision-quality measurement outside the emulator; staleness decay sends unobserved units toward the prior with widening intervals instead of freezing; the performativity diagnostic (§19.6) alarms residual self-negation and falls the affected forecasts back to persistence. *Graduation path:* persistently self-negating aggregates get explicit response models (performative-stable prediction).

**ADR-16 · Two profiles, one architecture: the crowd is a special case, not the general one.** Production SCION selects paths at gateways; native host crowds live in research settings. A host-crowd-only design builds the right system for the wrong decade; a gateway-only design abandons the environments where the richest science and the eventual end-state live. One abstraction — the reporter model, with explicit aggregation counts — lets both be first-class: Profile B gets few-vantage statistics, contractual identity, and AS-telemetry ingestion instead of degenerate crowd math; Profile A keeps crowd statistics where crowds exist; mixed deployments compose because a gateway is a legitimate mega-reporter in Profile A terms. *Blend continuously* as native adoption grows — by construction, no reopening needed.

**ADR-17 · Trust is gated on evidence diversity, not averaged over it.** Global-f trimming defends well-observed units while the attacker concentrates on sparse ones; trimming harder removes honest tails, not adversarial mass; and path claims are unverifiable until proof-of-traversal exists. Robustness is therefore reframed as *tiering*: served state is corroborated only above a minimum count of independent, reputation-weighted, influence-capped evidence buckets; below it, prior-anchored wide intervals with the tier exposed — capturing a sparse unit's report stream buys the attacker a wide honest answer, not a false confident one. Report-mass anomaly quarantine kills rush attacks. The conflict-of-interest repair follows the same logic: an operator's uncorroborated self-reports are served *as such* (tagged, wider) rather than trusted or — the v1.0 self-contradiction — discarded, which would have deleted the only evidence for stub links. Proof-of-traversal (EPIC-authenticator receipts) is a scheduled milestone whose landing relaxes gating for receipt-backed reports — the adoption incentive is designed in. *Reopen* threshold parameters (D_min, caps) against M0's measured diversity distributions.

---

# Part IV — Architecture & Component Specifications

## 11. Architecture overview

One Oracle Instance per ISD; stateless service tiers around two stores; per-AS edge caches in front; a federation mesh between instances. All interfaces gRPC/protobuf; all served state signed.

```
                        ┌─────────────────────────── ISD Oracle Instance ───────────────────────────┐
  Reporters             │                                                                            │
 ┌─────────────┐reports │  ┌───────────┐   ┌───────────────┐   ┌───────────────┐   ┌────────────┐   │
 │ Host agents │────────┼─▶│ Ingestion │──▶│ Trust Pipeline│──▶│ Telemetry     │──▶│ Feature    │   │
 │ (Profile A) │        │  │ Gateway   │   │ clamp·filters │   │ Store (TSDB,  │   │ Service    │   │
 ├─────────────┤        │  │ (+replay  │   │ ·diversity    │   │ edge substrate│   └─────┬──────┘   │
 │ SIG/Gateway │        │  │  KV tier) │   │ ·profile agg  │   │ + unit view)  │         │          │
 │ agents (B)  │        │  └─────┬─────┘   │ ·reputation   │   └───────────────┘   ┌─────▼──────┐   │
 └──────┬──────┘        │        ▼         │ ·quarantine   │          ▲            │ Prediction │   │
        │queries/push   │  ┌───────────┐   └───────────────┘    ┌─────┴─────┐      │ Engine     │   │
        ▼               │  │ Stream Bus│                        │ Anchor    │      │ T0·T1·T2 + │   │
 ┌─────────────┐        │  └───────────┘   ┌───────────────┐    │ Prober    │      │ performa-  │   │
 │ Per-AS Edge │◀───────┼───────────────┐  │ AS interior   │───▶│ Fleet     │      │ tivity diag│   │
 │ Caches      │ jitter │  ┌────────────┴┐ │ telemetry (§16)│   └───────────┘      └─────┬──────┘   │
 └─────────────┘ deltas │  │Dissemination│ └───────────────┘                             │          │
                        │  │ query·push· │  ┌───────────────┐   ┌──────────────┐         │          │
  Peer-ISD oracles ◀────┼─▶│ bulk mirror │◀─│ State Store   │◀──│ Coordination │◀────────┘          │
  (federation +         │  │ ·hints      │  │ (signed unit  │   │ detector +   │                    │
   cross-anchors)       │  └─────────────┘  │  snapshots)   │   │ share solver │                    │
                        │                   └───────────────┘   └──────────────┘                    │
                        │      Control plane: Model Registry · Config/Policy · Monitoring           │
                        └────────────────────────────────────────────────────────────────────────────┘
```

**Steady state.** Reporter agents stream authenticated `MeasurementReport`s to the Ingestion Gateway; the Trust Pipeline clamps, filters, accounts diversity, aggregates per profile, and updates reputation; aggregates land on the edge substrate in the Telemetry Store. Tier-0 filters update in-stream; Tier-1 re-infers unit-state distributions each 10 s inference epoch; Tier-2 calibrates composed predictions against fresh held-out observations; the signed unit-state snapshot (with view version, tier/diversity/ambiguity/staleness metadata, and calibration table) is what Dissemination serves — by query, push, and jittered bulk deltas to caches. Coordination watches for oscillation and, where triggered, overlays damped shares from corroborated-tier inputs only. Federation exchanges signed summaries and cross-anchors with peers. Responses carry sampling hints; compliant selectors run the exploration floor. The loop closes.

## 12. Reporter agents

### 12.1 Host agent (Profile A)

A `pan`-library extension plus a small companion daemon beside `sciond`; applications declare a requirement class and constraints via the path-policy API and never talk to the oracle directly. Fully functional with the oracle absent (N1). Sub-modules:

1. **Passive sampler** — QUIC-first: tracer callbacks yield RTT samples, loss counters, delivery rate, and the app-limited flag; records role (sender/receiver), `cc_algo`, concurrent-flow count, and whether the peer attests reply-path reversal. Default cadence: one `FlowSample` per flow per 30 s, plus at path switch and flow end; server-adjustable. `[v1.1]` **QUIC-only is a v1 scope decision, not a fact about SCION traffic.** v1.0 justified it as "SCION application traffic is QUIC/UDP". Both TCP/SCION (protocol number 6) and UDP/SCION (17) are assigned, and this document's own Profile B says the dominant production traffic is legacy IP encapsulated by a gateway, which is not QUIC. The decision stands — QUIC's tracer interface is the only one that hands over RTT, delivery rate and the app-limited flag without kernel work — but the coverage consequence must be stated: **the passive sampler observes a subset of traffic selected by transport, and that selection composes with the selection bias of ADR-15.** Only one of the two has a remedy in this document. The transport mix is measured in M0 (§29) alongside the profile mix, and where a deployment's non-QUIC share is material the honest response is a lower evidence tier for units observed only through it, not a silent extrapolation.
2. **Active prober** — budgeted SCMP echo and traceroute (hard cap default 1 probe/s), serving local selector needs first, then oracle sampling hints.
3. **Reporter** — batches samples into `MeasurementReport`s, authenticates (DRKey host key, SPAO MAC, sequence number), streams over persistent gRPC; local drop-oldest queue — reporting is always sacrificeable.
4. **Prediction cache + composer** — holds unit states with TTLs; embeds the canonical `oracle-compose` library and the signed calibration table; verifies snapshot signatures; answers selector requests locally when fresh.
5. **Selector** — §20.6, including the stability discipline and the ε-exploration floor on elastic traffic; emits `SelectionEvent`s when decision logging is opted in.
6. **Fallback ladder driver** — walks §26 automatically; always runs the private shadow estimator (per-path robust EWMA over own samples) so rung 4 stays warm.

Application-facing sketch:

```go
type Requirement struct {
    Class       Class         // Latency | Throughput | Reliability | Scavenger | Custom
    Constraints Constraints   // ISD/AS allow/deny, geo fences, cost/carbon ceilings, min tier
    FlowHint    FlowHint      // expected duration/volume → probing & subscription behavior
}
func SelectPaths(dst addr.IA, req Requirement, k int) ([]ScoredPath, error)
```

### 12.2 Gateway agent (Profile B)

Embedded in or beside a SCION-IP gateway — simultaneously the reporter, the path selector for its members, and (per ADR-17) a tagged interested party for units it terminates. Differences forced by the setting: samples are **flow-aggregates** per (path, epoch) — aggregation_count, goodput/RTT/loss summaries — never per-legacy-flow (privacy and volume); sampling stratified by traffic class under a per-path budget; identity and terms provisioned at ISD-governance level (contractual, not consumer opt-in); `SelectionEvent` logging strongly recommended — gateways are production's densest decision-log source and the backbone of Profile B off-policy evaluation.

`[v1.1]` **The gateway is not a blank slate, and this changes two conclusions.** The SCION-IP gateway already contains a complete path-selection subsystem, and v1.0 described a parallel one. What exists:

| what the gateway already does | consequence |
|---|---|
| Probes every candidate path at a default interval of **500 ms** (`pathhealth`) | §12.2's "stratified sampling under a per-path budget" describes something *poorer* than what already runs. A deployed gateway fleet is an active-probe mesh at 2 Hz per path — free, dense, liveness-relevant evidence that the reporter should forward rather than duplicate. |
| Selects with a filtering selector keyed on `snet.PathFingerprint`, holding a `PathPolicy` and a revocation store | §20.6's hard-constraint filter **already exists** as `PathPolicy`, and the current-path stickiness is already fingerprint-keyed. The oracle must *drive* this selector, not sit beside it: the integration point is the scoring input to an existing selection loop, not a new loop. |
| Defaults to selecting **one** path (`PathCount = 1`) | The dynamics are not the crowd's. See below. |

**The single-path default is the substantive one.** §20.6's throughput class assumes multipath with weights proportional to headroom, and §21's share vectors assume a population that can sample a distribution. A fleet of gateways each selecting one path and switching discretely is a different dynamical system: closer to a best-response game over a finite action set than to the mixed-strategy setting the stability analysis is written for. Critically, **there is no within-reporter mixing to damp it** — a gateway either moves or does not, so the smoothing that makes a host crowd's aggregate continuous is absent, and ADR-10's ε-argmax-then-sample discipline degenerates to plain ε-argmax with a population of one. Stability claims must be stated separately for this regime (§21), and the number of paths a Profile B gateway will actually use becomes a measured M0 parameter (§29) and a second axis in §28's stability campaign.

## 13. Wire protocol (complete)

```protobuf
message MeasurementReport {                  // authenticated envelope
  uint32 schema_version = 1;
  bytes  src_id         = 2;                 // stable per-ISD identity (see N6: identified participation)
  uint64 seq_no         = 3;                 // strictly increasing; replay window at gateway
  uint32 ts_coarse      = 4;                 // unix seconds
  repeated FlowSample       flows       = 5;
  repeated ProbeSample      probes      = 6;
  repeated TracerouteSample traceroutes = 7;
  repeated PathEvent        events      = 8;
  repeated SelectionEvent   selections  = 9; // opt-in
  bytes  auth_mac       = 15;                // SPAO/DRKey (A) or session-bound MAC under mTLS (B)
}

message InterfacePath { repeated Hop hops = 1; bytes path_digest = 2; }   // Hop = {isd_as, ingress_if, egress_if}

message FlowSample {
  InterfacePath path        = 1;
  uint32 duration_ms        = 2;
  uint64 bytes_acked_bucket = 3;             // bucketed (privacy)
  RTTStats rtt              = 4;             // min / mean / p95 (ms); min is the tomography workhorse
  float  loss_rate          = 5;
  float  goodput_mbps       = 6;
  bool   app_limited        = 7;             // true ⇒ never capacity evidence
  float  app_limited_frac   = 8;
  Role   role               = 9;             // SENDER | RECEIVER | BIDIR — direction attribution
  string cc_algo            = 10;            // bbr | cubic | reno | other — goodput confounder
  uint32 concurrent_flows   = 11;            // reporter's own parallel flows on this path
  bool   reply_path_known   = 12;            // peer attests reversed return path (G3)
  uint32 aggregation_count  = 13;            // >1 for gateway aggregates (Profile B)
}

message ProbeSample      { InterfacePath path = 1; uint32 rtt_us = 2; bool timeout = 3; uint32 hint_id = 4; }
message TracerouteSample { InterfacePath path = 1; repeated HopRTT hops = 2; uint32 hint_id = 3; }
                           // HopRTT = {hop_index, rtt_us, missing}; prefix sums, slow-path noise (G4)
message PathEvent {
  InterfacePath path = 1;
  enum Kind { SCMP_ERROR = 0; EXPIRED = 1; BLACKHOLE_SUSPECT = 2; RECOVERED = 3; }
  Kind kind = 2; uint32 scmp_code = 3;
}
message SelectionEvent {                     // the off-policy backbone (ADR-15)
  repeated InterfacePath candidates = 1;     // feasible set after hard filters
  uint32 chosen_index  = 2;
  bytes  scores_digest = 3;                  // hash of the ScoredPath set shown (snapshot_seq recoverable)
  bool   exploratory   = 4;                  // chosen by the ε-floor
  RequirementClass klass = 5;
}
```

Normative rules: `rtt.min` is the primary latency observable; `reply_path_known = false` from non-attesting peers excludes the sample from latency decomposition (constrains an unknown edge set); `app_limited` excludes goodput evidence, not RTT/loss; `role = RECEIVER` goodput attributes to reverse-direction edges of the named forward path; paths containing elements outside the combination closure are quarantined pending closure reconciliation vs. source scoring (§6).

## 14. Ingestion Gateway & Stream Bus

Per report, in order: schema validation → MAC verification (cached DRKey AS-level keys / mTLS session) → sequence/replay check against the **replicated replay KV** (the front tier is stateless *except* this named, budgeted store — an honest exception, included in failure analysis) → per-source token bucket (default 1 report/5 s sustained, burst 20) → per-AS aggregate ceiling (defense-in-depth against a compromised popular AS) → decode into per-sample records tagged with identity and reputation snapshot → publish to the stream bus (Kafka/NATS-class; partitioned by edge for Tier-0 locality; 48 h retention as the online replay buffer). One node validates ≥ 20 k reports/s (symmetric crypto only); scaling is horizontal.

## 15. Instrument inventory

What each evidence source identifies — the pipeline treats them accordingly: **passive flow samples** (path-level sums / min-censoring / survival; ρ-sums when reply-reversed; directional goodput/loss via role) — the volume source; **SCMP echo** (clean ρ-sums; cheap) — the latency and liveness workhorse; **SCMP traceroute** (bidirectional prefix sums; heavy-tailed slow-path noise; censored *only where the deployment rate-limits*, G4) — the decomposition instrument, spent on TR classes only; **OWD anchors** (directional sums) — the refinement layer's evidence; **AS interior telemetry** (§16), including exported BFD session state — the only cure for SP classes; **beacon metadata** (priors, capacities, static attributes) — priors and soft anchors.

`[v1.1]` **Liveness sources, restated.** v1.0 listed "control-plane events (registrations/expiries)" as the liveness source. Segment registrations and expiries remain — they carry the *deterministic* component and the segment-churn hazard — but the mechanism for a link going down is gone (§9.2). The corrected inventory for liveness:

| source | what it identifies | trust |
|---|---|---|
| **Hop-field / segment expiry** | deterministic component, exact to a 337.5 s quantum (§9.3) | read from signed path metadata; not estimated, not spoofable |
| **SCMP `ExternalInterfaceDown` / `InternalConnectivityDown`** | unscheduled transitions — the residual hazard's only endpoint-visible event | **unauthenticated** (§9.2); a *candidate* until corroborated (§20.3) |
| **Exported BFD session state** (§16) | the same transitions, at the router, sub-second | operator-attested; the cheapest §16 rung |
| **Anchor probe failure** | independent confirmation of the above | anchor-tier |
| **Gateway path-health probes** (§12.2) | dense liveness evidence at 2 Hz per path, already running in Profile B | reporter-tier, but structurally correlated per gateway |
| **Segment registration / expiry churn** | per-(src-AS, dst-AS) churn hazard | control-plane, signed |

The residual hazard is fitted against the *first* row's schedule and the *second* row's arrivals, and §9.2 explains why that quantity is dominated by traffic sparsity rather than by link reliability.

## 16. AS interior telemetry ingestion

Optional, operator-attested per-element (or per-SP-class) state summaries signed by the operating AS. Ingested as high-weight, *operator-source-tagged* evidence: corroborated where external evidence exists; where sole source — the SP case it exists for — served at **operator-attested** tier, explicitly distinct from crowd-corroborated. Deliberate incentive design: an AS wanting its interior links scored well either exposes telemetry or accepts class-granular wide-interval treatment.

`[v1.1]` **Start with BFD state, not with per-element summaries.** v1.0 opened with the largest possible ask, which is why operators would decline it. Every SCION border router already maintains BFD sessions with its neighbours at a 200 ms default transmit interval (§9.2). That state — session up/down per interface, with transition timestamps — is small, already computed, requires no new instrumentation, reveals nothing about traffic or customers, and is *precisely* the corroboration the liveness gate of §20.3 needs. It should be the first rung of §16 and the one asked for in a pilot.

The ladder, in increasing order of operator cost and decreasing order of likelihood:

1. **BFD session state per inter-AS interface** — liveness corroboration; near-zero cost; no commercial disclosure.
2. **Interface up/down history with cause codes** — churn hazard fitting.
3. **Per-SP-class aggregate latency/loss** — the SP-resolution case §16 was written for.
4. **Per-element interior state** — full resolution; the largest ask, and the last.

Rungs 1 and 2 are liveness-only and should be negotiable separately from 3 and 4, because they carry no competitive information and an operator who refuses 3 may readily grant 1.

## 17. Trust Pipeline

Stream processors between bus and store, in stages:

**17.1 Plausibility clamp.** Bounds derived from advertised metadata **clamp estimates but never punish reporters** unless anchor-corroborated: advertised values are AS-self-declared, sparsely deployed, and manipulable by the AS, not the reporter — an uncorroborated bound is a prior, not a law. Corroborated-bound violations are discarded *and* scored against the source. `[v1.1]` This principle now also governs the *model architecture*, not only reporter scoring: §9.1 removes the structural latency floor for exactly the reason stated here, and adds the third reason the audit found — advertised latency is not a lower bound at all but a symmetric conservative estimate, so even an honest, unmanipulated value can sit above the truth on an asymmetric link.

**17.2 Semantic filters.** Goodput evidence routing per app-limited and role; **edge-bottlenecked-source capping** — a source persistently below peer consensus across *disjoint* unit sets is bottlenecked locally, so its goodput reports become lower-bound-only evidence everywhere; dedup by (source, path_digest, ts) window.

**17.3 Diversity accounting.** Per unit per epoch, an evidence-bucket census (Profile A: source-AS buckets; Profile B: per-gateway identities) feeding the gate (§17.5).

**17.4 Profile aggregation.** *Profile A:* within-bucket reputation-weighted median; across buckets, trimmed mean (scalars) / geometric median (vectors), each bucket's influence capped at c_max (default 25 %). Path-level samples reach units through the tomographic assignment implied by the composition operator — the Tier-0 residual view is kept consistent with the Tier-1 decoder by shared priors. *Profile B:* leave-one-out sensitivity capping (served state must not move beyond a cap on deletion of any single reporter; else demote to thin tier); cross-gateway consistency checks on shared core units, disagreements arbitrated by anchors; operator/terminating-party tags per ADR-17.

**17.5 Evidence-diversity gate (normative).** Per unit: effective diversity D_u = count of independent evidence buckets weighted by reputation mass, influence-capped. Tiers: **corroborated** (D_u ≥ D_min, default 4) — full pipeline output; **thin** (1 ≤ D_u < D_min) — output blended toward prior, intervals inflated by the deficit, tier served; **prior-only** (D_u = 0 or quarantined) — prior state, maximal intervals. A path's tier is its weakest unit's tier. Selectors may filter on tier; coordination consumes corroborated only.

**17.6 Reputation & quarantine.** Per-source Beta posterior on agreement with robust consensus ∪ anchors, decaying to prior over 30 d, weighting within-bucket influence; COI tagging per ADR-17. **Report-mass anomaly quarantine:** units whose report volume or bucket mix shifts beyond control limits serve prior-tier state until re-corroborated — the rush-attack killer.

## 18. Telemetry Store & Feature Service

**Store** (ClickHouse-class columnar TSDB): `edge_agg` (per directed edge / ρ-pair × metric × 5 s epoch: robust stats, censoring masses, evidence quality), `unit_view` (versioned partition + ambiguity labels + continuity lineage), `path_obs` (compacted path-level observations, 30 d — calibration and training), `topology_closures`, `events`, `reputation`, `selection_log` (opt-in, 30 d). Nothing rawer is retained (N6).

**Feature Service** materializes point-in-time-correct tensors for training and ≤ 1 s-lag streams for inference: per-unit windows (64 × 15 s epochs of Tier-0 stats per metric, evidence quality, event flags), static features (floors, capacities, link type, geo distance, topology role, degree), tier/diversity/ambiguity/staleness/recently-split flags, temporal encodings (time-of-day, day-of-week), and the current unit-graph adjacency plus line graph.

## 19. Prediction Engine

### 19.1 Tier-0 — online robust filters

Per directed edge (goodput, loss) and per ρ-pair (latency), in-stream: t-digest quantile trackers at 1 min / 10 min / 6 h; latency residual via a **Student-t state-space filter** (heavy-tailed observation noise; level + slope; process noise scaled by evidence quality); goodput as censoring-aware grid-survival counts (a lower bound decrements survival mass only below its value); loss as exceedance counters. Traceroute differences enter as direct-but-noisy ρ evidence (Student-t; `[v1.1]` missing-not-at-random **only where the deployment rate-limits** — the noise model is selected from M0's measured probe-budget parameter, per G4, because assuming censoring where there is none biases the estimator). Liveness: expiry tracked exactly from metadata; Nelson–Aalen residual hazard per unit and per churn aggregate. Tier-0 is always servable with inflated intervals — the availability floor and the cascade's fallback target.

### 19.2 Tier-1 — spatio-temporal GNN

**Graph:** nodes = units of the current view (line-graph formulation); model-edges connect units sharing an AS, consecutive in ≥ θ registered paths, or sharing a declared intra-AS resource. **Architecture:** per-unit temporal encoder (stacked gated dilated causal convolutions; receptive field ≈ 16 min) → L = 6 rounds of edge-conditioned message passing with GRU state updates → multi-head outputs per metric × horizon {0, +60 s, +300 s}. Inductive representation mandatory: features first; optional ID embeddings trained with 30 % embedding-dropout. Parameter budget ≤ 10 M at the 10 k-unit envelope. **Canary semantics:** challenger and champion both run full-graph; outputs mix per unit (message passing forbids partial-graph canarying).

### 19.3 Heads and objectives

- **Latency:** `[v1.1]` (μ, σ) of the log-normal residual **around** γ_u per horizon; NLL on direct ρ evidence + path-composition NLL through the canonical operator + an **asymmetric soft-anchor penalty** on excursions below γ_u. *The v1.0 softplus floor construction is removed* — §9.1 explains why a structural `pred ≥ γ_u` is unsatisfiable on asymmetric links, whose advertised metadata is a conservative symmetric estimate rather than a lower bound. The penalty is cheap for small excursions and steep for large ones, so the prior still dominates where evidence is thin without being able to override evidence where it is not; its steepness is scaled by the γ asymmetry-uncertainty term, so an anchor-corroborated γ constrains hard and an uncorroborated one barely constrains at all. Where an anchor confirms the advertised value, the penalty may be promoted to a hard constraint for that unit.
- **Goodput:** survival on the rate grid via cumulative-softmax (monotone by construction); **censored likelihood** — capacity-limited path observations contribute a bottleneck-attribution term (soft assignment across traversed units by current survival mass, temperature-annealed) plus lower-bound terms elsewhere; app-limited contributes lower bounds only.
- **Loss:** exceedance survivals with structural monotonicity across thresholds; binomial likelihood; composed in log-survival.
- **Liveness residual:** hazard multipliers on the age grid.
- **Regularizers:** temporal smoothness, congestion sparsity (most units are uncongested most of the time), ceiling clamps `[v1.1]` — where a ceiling derives from `BandwidthInfo` it is a *penalty*, not a clamp, for the reason given in §9.1(4): bandwidth metadata is symmetric under the same rule as latency and inherits the same asymmetry bias. Monotonicity across horizons is structural — no post-hoc crossing fixes anywhere.

### 19.4 Training regime

Nightly full retrain on 30 d (point-in-time-correct joins); 15-min online fine-tune on the 48 h bus replay with an EMA teacher against drift-chasing. Selection-policy correction where `SelectionEvent` coverage permits (clipped inverse-propensity weighting; unweighted-plus-diagnostics elsewhere); exploratory-tagged samples are the privileged evaluation slice — the closest thing to randomized data the system owns. Cold start for new deployments: emulation pretraining (seed-emulator/ns-3 SCION topologies) + priors; the cascade covers the gap.

### 19.5 Tier-2 — conformal calibration

CQR-style nonconformity over a rolling 24 h stream of (composed prediction, realized path observation) pairs, bucketed per §10; one-sided variants for one-sidedly consumed quantities (upper latency, lower goodput). Adjustments ship inside the signed snapshot so client-side composition applies identical calibration. Trailing coverage per bucket is served, alarmed at N4 bounds, and drives the per-bucket circuit breaker to Tier-0-inflated service.

**`[v1.2]` The calibration is online and adaptive, not split conformal.** This is the correction of an inconsistency the document carried from v1.0: ADR-15 states that the oracle's predictions steer traffic and that this **voids exchangeability**, and v1.0 then selected split conformal, whose only guarantee *requires* exchangeability. The stated remedy — monitor coverage, break the circuit per bucket — detects the failure after serving the miscalibrated intervals, and detection is not a guarantee.

The fix is small and well-established. Rather than a fixed calibration quantile, maintain a **per-bucket miscoverage level α_t updated online** from realised coverage:

> α_{t+1} = α_t + η (target − 1{y_t ∈ C_t})

This is adaptive conformal inference. It yields **long-run coverage converging to target over arbitrary sequences**, with no assumption on the data-generating process — which is exactly the property needed when part of the sequence is the system's own doing. Three notes on fitting it here:

- It **composes with everything already specified**: the nonconformity scores, the one-sided variants, the §10 bucketing and the snapshot-shipped adjustment table are unchanged. Only the quantile level becomes a state variable per bucket, which the snapshot already has room for.
- **Choose the step size η against the bucket's observation rate, not globally.** A thin bucket updating on few observations with a large η oscillates; the same α-tracking that guarantees coverage will otherwise manufacture interval width instability, which is a small version of the pathology this whole document is about. Where a bucket's rate is too low to track, fall back to the conservative fixed quantile and say so in the tier metadata.
- Where regime changes are expected rather than gradual — a coordination activation, a topology change, a view version bump — **a change-point-aware variant is the right tool**, and those three events are known to the oracle, so they can be signalled to the calibrator rather than inferred by it. This is the cheap version of the harder problem in §19.6.

Strongly-adaptive variants tighten the guarantee from long-run average to local windows and are worth evaluating at M3, but the plain update above is the one this design should ship: it converts N4 from a monitored aspiration into a stated property, in roughly the amount of code the monitoring already takes.

### 19.6 Performativity diagnostic

Per aggregate and horizon: signed correlation between forecast error and the crowd's post-forecast traffic shift onto/off forecasted units, estimated from selection logs + subsequent observations. Persistent self-negation ⇒ the aggregate's forecast heads fall back to persistence, the flag is served, and the aggregate enters the ADR-15 graduation queue (explicit response modeling).

**`[v1.2]` The graduation queue has a literature, and this system sits in its most tractable corner.** v1.0 named "performative-stable prediction" and left it as a research pointer. Three things are now worth importing rather than re-deriving.

The **stateful** formulation is the right one: distributions at successive epochs depend on both the previous distribution and the deployed predictor, which is this system exactly — the crowd's placement is carried forward by minimum dwell (§20.6) rather than redrawn each epoch, so the loop has memory and the memoryless formulation would mis-model it.

More usefully, this system is **partially performative**: only the compliant share responds to the prediction, while defectors, background cross-traffic and non-participating flows do not. That is a strictly easier problem than full performativity, it has recent treatment, and the spec is unusually well placed to exploit it because **the responsive fraction is already a measured quantity here** — the compliant share is §28's primary evaluation axis and the ε-floor gives a randomised slice for estimating it. A response model that knows what fraction of demand it can move is a much smaller object than one that must infer it.

Practical consequence for the queue: an aggregate entering it does **not** need a full performative-stable retraining loop. It needs an estimate of the responsive fraction and a damped best-response, which is the same machinery §21 already specifies for coordination — with the difference that it applies to the *forecast* rather than to a published share vector. Graduating an aggregate should therefore be cheap, and the fallback-to-persistence above stays the safe default while the estimate is thin.

### 19.7 Anchor Prober Fleet

Oracle- and volunteer-AS-operated probers with two schedules: coverage (every unit in a ≤ 2-unit combination at least once per 10 min *where topologically possible* — SP classes excepted by definition) and steering (§20.5). Anchors feed the Trust Pipeline as references, Tiers 0/1 as high-weight evidence, Tier-2 as calibration data for thin-coverage regions, and the coordination detector as its corroboration source. In Profile B and in federation, anchors carry structurally higher relative weight.

## 20. Dissemination Service

### 20.1 State Store and snapshot discipline

Per inference epoch, a signed **unit-state snapshot**: for every unit × metric × horizon, the served representation (§10) plus tier, diversity, ambiguity class, staleness, Tier indicator (T1-calibrated vs T0-fallback); the Tier-2 calibration table; view version; model version; monotonically increasing `snapshot_seq`; oracle service signature (certificate chained through the hosting AS into the ISD TRC). Every answer names its `snapshot_seq`; monotonicity defeats cache rollback; deltas between snapshots are the distribution unit; the reproducibility tolerance is declared in the snapshot header.

### 20.2 Query API

```protobuf
service PathOracle {
  rpc ScorePaths    (ScorePathsRequest)      returns (ScorePathsResponse);    // path mode
  rpc GetUnitStates (UnitStatesRequest)      returns (UnitStatesResponse);    // unit mode
  rpc Subscribe     (SubscribeRequest)       returns (stream OracleUpdate);   // push
  rpc SyncSnapshot  (SyncRequest)            returns (stream SnapshotDelta);  // bulk mirror
  rpc Report        (stream MeasurementReport) returns (stream ReportAck);    // ingestion; Ack carries hints
}

message ScorePathsRequest { repeated InterfacePath candidates = 1; Metrics wanted = 2; Horizon h = 3; }
message ScoredPath {
  InterfacePath path = 1;
  map<string, MetricScore> scores = 2;       // per metric: calibrated distributional summary (§10 reps)
  EvidenceTier tier = 3;                     // weakest-unit rule
  float shared_bottleneck_flag = 4;          // P(this path shares its likely bottleneck unit with
                                             //   another candidate in this request)  — ADR-09 benefit
  SurvivalCurve liveness = 5;                // deterministic expiry exact × residual hazard
  bool  directional_split_prior_only = 6;    // set iff any consumed directional latency is prior-split
  uint32 ttl_s = 7; uint64 snapshot_seq = 8; float trailing_coverage = 9;     // honesty metadata
  ShareHint coordination = 10;               // present only when §21 is active for this aggregate
  repeated SamplingHint hints = 11;
  bytes scores_digest = 12;                  // echoed in SelectionEvent when logging is opted in
}
```

TTL semantics: default 15 s dynamic, 10 min static; compliant selectors do not re-query earlier absent a triggering event (push, expiry, flow start) — part of the ADR-10 discipline. Tier-change is a first-class push trigger: a subscribed path demoting from corroborated is exactly what a reliability-class consumer must hear.

### 20.3 Subscription push

Interest = (dst-AS aggregate, metric set, threshold spec); soft state, 10 min expiry without refresh; per-interest push floor 5 s. Triggers: threshold crossings, tier changes, liveness-hazard spikes, coordination share changes.

**`[v1.1]` Liveness-hazard pushes are corroboration-gated (normative).** A liveness-hazard spike derived from SCMP `ExternalInterfaceDown` / `InternalConnectivityDown` is **unauthenticated evidence** (§9.2), and §20.6's reliability selector acts on such a push by pre-arming or executing a switch. Ungated, that is an amplifier from one spoofable packet to a fleet-wide coordinated reroute (§23, *spoofed liveness amplification*). The gate:

- An SCMP-derived liveness event raises the unit's hazard to a **candidate** state. Candidate state **widens intervals and lowers the served tier**; it does **not** fire a push.
- The candidate is **promoted** — and only then pushed — on independent confirmation from any of: an anchor probe failure on a path traversing the unit; a second, disjoint evidence bucket reporting the same transition; exported BFD state from the operating AS (§16); or a control-plane segment withdrawal consistent with it.
- Promotion carries the corroborating source in the push payload, so a client can apply its own policy — a reliability-class flow may reasonably act on candidate state, and should be able to tell that it is doing so.
- Absent promotion, the candidate **decays back** on the unit's normal staleness schedule. It is never escalated by repetition: a thousand identical unauthenticated reports of the same transition are one uncorroborated observation, because they cost the attacker the same as one.

**`[v1.2]` Escape for structurally sparse units, and why it is required rather than optional.** The gate as stated above has a distributional consequence: **a unit with few witnesses can never have an event promoted.** Sparse stub links are where a failure matters most — there is often no alternative path — and they are exactly the units the gate would serve last. That is not a corner case; it is the modal case for the edge of the graph.

The precedent is specific and unhappy. BGP route flap damping was deployed widely, then found to "severely penalize sites for being well connected because topological richness amplifies the number of update messages exchanged", after which many operators disabled it outright — losing the protection it did provide. A decade later the recommendation was to raise the suppress threshold by an order of magnitude so that damping fires on a fraction of a percent of prefixes. **The lesson generalises past its sign: a gate whose trigger correlates with topological position produces systematically unfair outcomes, users notice, and the mechanism gets turned off entirely.** RFD punished the well-connected; this gate would punish the poorly-connected. Same structure, and the same ending.

Therefore, normatively: where a unit's ambiguity class is **SP** (§8) — meaning no end-to-end instrument from any placement can corroborate it, so the corroboration requirement is not merely hard but *unsatisfiable* — and the reporter is the **terminating AS**, single-witness promotion is permitted, served at **uncorroborated-operator** tier with the tag carried in the push. This is exactly the treatment ADR-17 already prescribes for an operator's self-reports about its own stub links, applied to liveness rather than to latency, and for the same reason given there: discarding the only evidence that exists produces a worse answer than serving it honestly labelled.

The residual is stated: this is the attack surface the escape opens, it is bounded to units the attacker must also *terminate*, and it is exactly the trade ADR-17 already accepted elsewhere. Tune so the escape is rare — if a large fraction of promotions are arriving through it, the diversity distribution is worse than M0 predicted and §29's contingency applies, not this clause.

This is deliberately the same shape as §21's requirement that the oscillation detector be anchor-corroborated before coordination activates, and for the same reason stated there: *the trigger for a mechanism that moves traffic must have independent evidence, because that is exactly when the attacker's return is highest.* Liveness was the one such trigger v1.0 left ungated.

### 20.4 Bulk mirror and per-AS caches

`SyncSnapshot` streams the latest full snapshot then continuous deltas, with **per-subscriber jittered delta release**: each subscriber receives each delta at a uniform random offset within the freshness SLO window (default jitter ∈ [0, 10 s] on the 10 s epoch). This removes the common clock that would otherwise make the mirror a synchronized herding substrate for non-compliant selectors — at zero median-freshness cost and full compatibility with the mirror's privacy purpose (clients fetching all units reveal no destinations). Per-AS Edge Caches are dumb replicas: verify signature, hold current snapshot, answer path/unit queries locally with the canonical library, forward report streams upstream. Envelope: full snapshot 2–4 MB; steady-state deltas 20–100 KB/epoch (~5–10 KB/s per cache).

### 20.5 Sampling hints

Attached to `ReportAck`s and query responses: (path template, weight, hint_id) triples asking opted-in clients to spend spare probe budget where it buys the most. Weights per epoch ∝ predictive uncertainty × decision demand × identifiability deficit × **staleness** — restricted to **TR-class** targets (probing cannot help SP classes; §8).

**`[v1.2]` "Identifiability deficit" now has a definition, and the term becomes an objective rather than a weight.** Under §7.1 and §8, a TR class is a set of columns of **A** that are identical over the observed rows and differ somewhere in the closure. A probe adds a row. So the deficit is the dimension of the space a probe could still resolve, and the steering problem is a **measurement-design problem**: choose the probe schedule that maximises the rank increase of A restricted to the steerable region, weighted by decision demand.

That is a well-posed objective with known structure — the greedy solution has the usual submodular flavour, so a rank-greedy schedule is both computable per epoch and defensible against the alternative. It replaces a product of four heuristic factors with two: *how much identifiability does this probe buy* (computed) × *how much does anyone care about the units it would split* (demand). Uncertainty and staleness remain, but as inputs to demand rather than as independent multipliers, which removes the double-counting v1.0's product invites — a stale unit is uncertain *because* it is stale.

Two properties fall out that are worth having. The schedule **self-terminates**: once a class's steerable region is exhausted it is SP-in-practice and stops attracting budget, without needing a separate rule. And **efficacy accounting becomes a prediction rather than a post-hoc correlation**: the design says which split a probe should produce, and the echoed `hint_id` says whether it did, so a hint that repeatedly fails to split what it promised is evidence the closure is stale (§6) rather than evidence the probe was wasted. Hint distribution is **randomized across recipients** so the hint stream does not broadcast the oracle's uncertainty map to any single observer; echoed hint_ids close the efficacy accounting loop. Hints direct *probes*; the ε-floor (ADR-15, §20.6) directs *traffic*; together they are the system's paid-for data budget.

### 20.6 Selector algorithms (per requirement class)

Shared discipline, in order: hard constraint filters (fail-closed for compliance) → tier filter if requested → build the ε-optimal set on the class objective → sample uniformly within it → minimum dwell (30 s or flow lifetime) → switch only if the challenger's *pessimistic* score (upper latency / lower goodput) beats the incumbent's *typical* score (median) by the hysteresis margin (10 % relative + absolute floor) → all timers jittered → ε-exploration floor: 1–2 % of *elastic* bytes routed by uniform sampling over the feasible set, tagged exploratory (latency-class and inelastic traffic never taxed).

**`[v1.1]` Where this discipline actually runs, in Profile B.** The gateway already has a selection loop with a policy filter, fingerprint-keyed stickiness and a revocation store (§12.2). This discipline is not a replacement loop; it is a specification for **that** loop's scoring input and switching rule. Concretely: the hard-constraint filter *is* the existing `PathPolicy`; the incumbent for hysteresis purposes *is* the existing current-path fingerprint; the minimum dwell must be reconciled with the gateway's own path-health hysteresis rather than layered on top of it. An oracle-driven selector that runs beside the gateway's, rather than inside it, produces two controllers on one actuator, which is a well-known way to manufacture the oscillation this whole section exists to prevent.

**`[v1.1]` And "sample uniformly within the ε-set" degenerates when the population is one.** The step assumes a reporter that can split its traffic across the ε-optimal set. A gateway defaulting to a single path cannot: it picks one member and the "distribution" is a point mass. In that regime the discipline reduces to ε-argmax with dwell and hysteresis, the within-reporter smoothing disappears, and stability rests entirely on the *inter*-gateway desynchronization provided by jittered timers and jittered mirror delivery (§20.4) — which therefore stop being a defence-in-depth measure and become the primary one. §21 states the consequence for the stability claims.

**`[v1.2]` Which layers each class may consume.** v1.0 left this implicit and the omission had a specific cost, set out under the latency class below. The oracle serves three layers with different temporal validity — **static** (propagation priors, capacities, attributes; valid for hours), **liveness** (deterministic expiry plus residual hazard; event-driven), and **dynamic** (congestion-derived latency, goodput, loss; valid for N2's 15 s median, 60 s p99). A class must declare which it *ranks* on and which merely *widen* its intervals, because those are different uses with different failure modes.

| class | ranks on | widens on | never ranks on |
|---|---|---|---|
| **Latency** | static + liveness | dynamic | dynamic |
| **Throughput** | dynamic | static | — |
| **Reliability** | liveness (deterministic exactly) | dynamic | — |
| **Scavenger / cost / carbon** | static (hard filter) then latency-class logic | dynamic | dynamic |
| **Compliance** | static attributes only (fail-closed) | — | everything else |

- **Latency class.** Objective: composed upper-quantile **round-trip** latency (ρ-composition; no directional fiction consumed). ε-set: within max(5 ms, 10 %) of best; tie-break by liveness. First-packet decisions (no fresh dynamic state) use the static floor layer — the propagation-latency prior is the latency class's cold-start selector by design.

  `[v1.2]` **And not only for the first packet: the static layer ranks throughout, with the dynamic layer widening intervals rather than reordering them.** The reason is a timescale mismatch that v1.0 did not confront. Congestion-induced latency moves on sub-second to seconds timescales; N2 delivers it at 15 s median and 60 s p99, and this document already, correctly, calls itself "a mesoscale instrument by design". A latency ranking driven by a 15-second-old congestion estimate is not a prediction, it is a lagged observation — and routing on lagged load is not a subtle failure. It is *the* documented one: oscillation occurs when path-selection decisions are taken on the basis of outdated load information, and a delay-derived metric closes the loop directly, since low measured RTT attracts traffic which raises RTT.

  So the v1.0 latency class was in one of two states, and neither was good: dominated by the static layer, in which case it needed no oracle and should have said so; or ranking on stale congestion, in which case it re-created the failure ADR-10 exists to prevent, reached through the metric instead of the mechanism, in the document's most visible feature. Ranking on static-plus-liveness and widening on dynamic keeps the useful part — a congested path gets a wider upper quantile and therefore loses the ε-set comparison on its *pessimistic* score — without letting a stale point estimate reorder anything. Deployments whose freshness materially beats N2 may promote the dynamic layer to ranking, and must then state the achieved staleness alongside the claim.
- **Throughput class.** Objective: predicted bottleneck achievable-goodput median, penalized by loss exceedance. Multipath: greedily select top-k paths maximizing joint headroom under shared-bottleneck exclusion via the **correlation groups** of §10 (one dependence mechanism serves composition and disjointness alike); schedule weights ∝ per-path headroom; re-weight on push; pin one low-latency path for control traffic.
- **Reliability class.** Objective: P(survival over the flow horizon) — deterministic expiry consumed *exactly* (a path about to expire is known to die, not predicted to; the selector schedules re-resolution ahead of it, `[v1.1]` by **at least one 337.5 s expiry quantum**, since positions within a quantum are indistinguishable — see §9.3) × residual survival × (1 − upper loss exceedance). Maintain a hot standby maximally unit-disjoint (correlation-group-disjoint) from the primary; `[v1.1]` pre-arm switch on **promoted** liveness-hazard pushes and widen on candidate ones (§20.3) — a selector that executes on uncorroborated liveness is the amplifier §23 describes; optionally require corroborated tier end-to-end and EPIC on the data plane.
- **Scavenger / cost / carbon class.** Hard-filter by static cost/carbon ceiling, then latency-class logic within the filtered set; first to yield under coordination shares — elastic traffic is the system's shock absorber, which is also why the exploration tax lands here.
- **Compliance class (composable with all).** Allow/deny over ISDs/ASes/geo evaluated against the interface path before any scoring; fail-closed: no compliant path ⇒ error, never a fallback path.

## 21. Coordination Service

Dormant per-aggregate overlay; activates only on detection, only for opted-in clients, always damped and TTL'd.

**Detector.** Per destination aggregate, over the units its candidate paths traverse, each epoch: (i) load-estimate variance ratio (short/long window); (ii) spectral concentration in periods 2–20× the response TTL — the signature of consultation-synchronized flapping; (iii) crowd switch-rate among the aggregate's candidates (from path-switch flow samples and selection logs). Trigger at θ_on for 3 consecutive epochs; release at θ_off < θ_on after 10 quiet epochs. **Anchor corroboration is required**: a detector fed only by reported load with no anchor-visible congestion stays dormant and raises an alarm instead — coordination activates when poisoning ROI peaks, so its trigger gets independent evidence.

**Share computation.** For active aggregate with candidates P and demand estimate D: water-filling `max_w min_u headroom_u(w)` s.t. Σw = 1, w ≥ 0, over **corroborated-tier** conservative (lower-quantile) headroom only. Damping: ‖w(t) − w(t−1)‖∞ ≤ 0.1/epoch (trust region against estimated demand elasticity — formal contraction analysis is a tracked open problem, §30). Published as `ShareHint{weights, ttl, aggregate_id}`; opted-in selectors sample within their own hard filters (renormalize w over the private feasible set — constraints compose cleanly). Non-participants see ordinary intervals; incentive alignment (fresher tiers, priority hints for participants) follows the FLOSS/CROSS logic of making the stable strategy individually attractive rather than coercing it.

**`[v1.1]` The claims above are for the mixed-strategy regime. Profile B may not be in it.** Everything in this section — share vectors, water-filling over weights, damping in ‖w‖∞ — assumes a population that can *sample* a distribution. A fleet of SCION-IP gateways selecting a single path each (§12.2) cannot. That regime differs in three ways that matter, and none of them is a detail:

1. **No within-reporter smoothing.** A share vector delivered to a single-path selector is realised as a Bernoulli draw, so the aggregate is smooth only across *many* gateways. With the ~10² gateways of N7 rather than the 10⁵ hosts, the realised aggregate has ~30× the relative sampling noise, and that noise is itself an oscillation driver rather than a damper.
2. **Discrete best response.** The dynamics are a finite-action game, not a continuous one. Damping ‖w(t) − w(t−1)‖∞ ≤ 0.1 constrains the *published* vector and does not constrain the realised assignment, which can move by a whole gateway's traffic in one epoch.
3. **Correlated demand.** One gateway carries an entire member AS's traffic. Gateway moves are large, lumpy and correlated with each other in a way host moves are not.

**Therefore:** the stability results of §28 must be reported separately for the discrete single-path regime, with gateway count and per-gateway path count as explicit axes; the compliant-share threshold is a *different number* there and must not be quoted across regimes; and where a deployment's gateways can be configured for multipath, doing so is a stability intervention in its own right and should be evaluated as one rung of the mechanism ladder. Formal treatment of the discrete regime joins the tracked open problems in §30 alongside the contraction analysis.

## 22. Federation Service

Per-ISD oracles form a mesh along provisioned core relationships, exchanging every 10–30 s, all TRC-anchored-signed: **CoreLinkStates** (units incident to core ASes and inter-ISD links — the shared vocabulary) and **ExportedUnitSummaries** (aggregated states for units in the exporter's globally registered down-segments, at the exporter's chosen granularity — full detail, coarsened super-units, or nothing; an explicit policy knob made honest by tiering). **Cross-ISD anchor exchange is a membership requirement**: a peer's exports are accepted at corroborated tier only where cross-anchors can arbitrate; otherwise served at **remote-attested** tier. A source-side oracle scores a cross-ISD path as: local units (own engine) + core units (CoreLinkStates) + remote units (destination ISD's export, pull-through-cached) + the destination ISD's calibration table for the remote portion — every remote-derived score flagged with provenance ISD and that ISD's trailing coverage. Degradation is additive: absent an export, the remote portion falls to static floors with maximal intervals — inter-ISD paths get *less precise*, never unscoreable.

---

# Part V — Security & Privacy

## 23. Threat → mechanism map

| Threat | Mechanisms |
|---|---|
| Poisoning reporters (concentration on sparse units — the primary scenario) | evidence-diversity gating: capture buys tier demotion to wide honest priors, never false confident state (§17.5); per-bucket influence caps; report-mass anomaly quarantine (§17.6); profile-appropriate aggregation (§17.4); anchor-referenced reputation |
| False path claims (reports attributing measurements to untraversed paths) | economically cheap until proof-of-traversal (M6 milestone: EPIC-authenticator receipts); mitigated meanwhile by diversity gating, quarantine, closure validation (§6), and attributable identity — **residual risk stated plainly** |
| Sybil hosts | identities mintable only within controlled ASes ⇒ sybils collapse into their AS bucket; per-AS aggregate ceilings (§14) |
| Self-promoting / competitor-deflating AS | operator-source tagging with uncorroborated-operator tier (ADR-17); anchors + cross-vantage evidence dominate; contradicting one's own signed static metadata is attributable |
| Floor/metadata manipulation harming honest reporters | bounds clamp, never punish, unless anchor-corroborated (§17.1) |
| Query eavesdropping / intent leakage | TLS everywhere; bulk-mirror mode hides destinations entirely; per-AS caches keep queries in-AS; path-mode batching/padding as best-effort |
| Herding via the mirror by non-compliant clients | per-subscriber jittered delta release (§20.4); stability claims conditioned on measured compliant share; defector fraction a primary evaluation axis |
| Uncertainty-map reconnaissance via hints | recipient-randomized hint distribution (§20.5) |
| Manipulated coordination | corroborated-tier-only inputs; anchor-corroborated triggering; damped, TTL'd, signed shares; opt-in scope (§21) |
| Oracle compromise | advisory-only bounds impact to degraded *choices*; signed snapshots + monotonic seq defeat forgery/rollback at caches; public coverage metadata makes degradation observable; per-ISD blast radius |
| DoS | stateless front tiers (+ replicated replay KV, budgeted); per-source/per-AS budgets; caches absorb reads; the system is safely ignorable under attack (§26) |
| Lying federation peers | remote-attested tier absent cross-anchor arbitration; provenance + remote coverage flags; additive degradation to floors (§22) |
| `[v1.1]` **Spoofed liveness amplification** — the oracle turns one unauthenticated packet into a fleet-wide reroute | corroboration gate on liveness pushes (§20.3): SCMP-derived events raise a *candidate* hazard that widens intervals and lowers tier but does not push; promotion requires anchor, disjoint-bucket, exported-BFD or control-plane confirmation; repetition never substitutes for corroboration; reliability selectors pre-arm on promoted events only (§20.6) |
| `[v1.1]` **Honest hidden-path reporter scored as a fabricator** | fourth closure disposition (§6): out-of-closure-but-well-formed reports are *possibly hidden*, never scored; unit held in the non-served partition |
| `[v1.1]` **Topology disclosure via served hidden-path units** | hidden units never emitted in `SyncSnapshot` or the bulk mirror; `GetUnitStates` answers only on demonstrated segment knowledge (§6, §24) |

**`[v1.1]` On the new liveness row.** It belongs here because it is an amplification the oracle *creates*: SCION endpoints already receive unauthenticated SCMP interface-down messages and already react to them, but each endpoint reacts for itself, on its own traffic, with its own timing. An oracle that ingests the same message, raises a hazard, and pushes to every subscriber holding a traversing interest converts an uncoordinated per-endpoint reaction into a synchronized one — which is precisely the herding failure ADR-10 exists to prevent, reached through the liveness door rather than the load door. The mechanism that makes the oracle valuable is the mechanism that makes this attack work, so the gate is not optional.

The residual risk is stated plainly, as elsewhere in this document: **until SCMP authentication is settled upstream, corroboration is a delay, not a proof.** An attacker who can also suppress or delay the corroborating evidence — by injecting during an anchor-coverage gap, or on a unit whose only witnesses are structurally correlated — buys a window. The gate bounds the blast radius and makes the attack visible in the quarantine and coverage metrics (§27); it does not close it. Closing it requires either authenticated SCMP or exported BFD state (§16), and both are outside this design's control.

## 24. Privacy posture

Exactly as N6: identified participation, minimization at source, AS-granular destinations only, bucketed volumes, 1 s timestamps, ≤ 30 d raw retention, per-ISD residency, destination-hiding bulk mode, ladder rungs per deployment, contractual governance in Profile B, and the residual honestly named — the operator's 30-day linkable view of AS-granular communication patterns is the trust grant of participation. Blind-token reputation transfer (unlinkability compatible with reputation) is tracked research (§30).

**`[v1.1]` Topology privacy is a fourth protected class, and v1.0 omitted it.** N6 enumerates content, destinations, volumes, timing, retention and jurisdiction. It does not cover *the existence of a link*. SCION's hidden path communication exists precisely so that certain segments are known only to authorized ASes, and an oracle that serves state for a unit appearing only in hidden segments discloses that link's existence, and that it carries traffic, to anyone who reads the bulk mirror. The disclosure is worse than it first looks: it is *inferable in aggregate* even without a direct query, because a unit appearing in no public segment but carrying corroborated state is self-identifying.

The handling is normative in §6 and summarised here: hidden units are estimated but held in a **non-served partition**, never emitted in `SyncSnapshot`, and answered by `GetUnitStates` only to a requester demonstrating prior knowledge of the segment. The cost is real and is accepted: hidden-path users receive less from the oracle than public-path users and their units cannot reach corroborated tier on public evidence. Stating the trade here prevents a deployment from making it silently in the other direction, which is the failure mode — nobody decides to leak a topology, they decide not to special-case it.

Two consequences for the rest of the document. The **coverage metadata** of §27, which is public by design so that degradation is observable, must aggregate over public units only; a coverage figure that moves when a hidden unit is added is a side channel. And **federation** (§22) must never export hidden-partition units, at any granularity, since the exporter's policy knob cannot express "coarsen this enough to be safe" for a quantity whose sensitivity is existence rather than value.

---

# Part VI — Operations

## 25. Scale envelope (the arithmetic)

Reference ISD: 200 ASes, heavy-tailed interface counts (core ASes up to ~30 interfaces ⇒ crossover pairs dominate the combination closure) ⇒ closure of order 10⁴ directed elements ⇒ **≤ 10 k units** after equivalence-class merging (most crossover pairs co-occur and collapse). `[v1.1]` **Two bounds tighten this, and both were previously unaccounted.** The path combinator discards any path in which an AS appears more than twice (G1), which removes the repeated-crossover constructions that would otherwise make a 30-interface core AS's contribution superlinear — the closure grows with crossover *pairs*, not with paths through them. And the intra-AS metadata rule of §6 advertises only a defined subset of interface pairs (child interfaces below the egress ID, core-link interfaces, peer interfaces), so the *metadata-covered* closure is smaller than the computed one; the difference is prior-only units, which cost snapshot bytes but no model capacity. The 10 k figure remains the planning number and is now an upper bound with a stated derivation rather than an estimate. Snapshot: 10 k units × 4 metrics × 3 horizons × grid/parametric summaries + metadata ≈ 2–4 MB full; 20–100 KB per 10 s delta ⇒ a cache mirror costs less bandwidth than a voice call. Ingest: 100 k hosts at 1 report/30 s ≈ 3.3 k reports/s < 1 MB/s wire — one gateway node's symmetric-crypto budget is ~6× that; Profile B ingest is smaller still. Tier-1: ≤ 10 M params over ≤ 10 k units, 64-epoch windows ⇒ sub-second inference per 10 s epoch on one mid-range GPU; nightly retrain single-digit GPU-hours. Query: path scoring composes ≤ ~15 units — microseconds; 100 k qps is a cache problem, not compute. Federation: hundreds of exported units per peer ⇒ KB/s. **The binding constraints are statistical (evidence diversity per unit — now measured and served, not assumed) and behavioral (compliant share — now an evaluation axis, not an assumption). Compute is 10–100× under-provisioned everywhere on commodity hardware.**

## 26. Failure modes & degradation ladder

Client-driven, automatic, per-destination where possible:

1. **Full service** — fresh T1-calibrated state + push + hints (± coordination).
2. **Pull-only** — push lost; TTL'd queries continue.
3. **Cache/mirror-only** — oracle unreachable; per-AS cache or last mirror serves; staleness marked; intervals inflated by a staleness-scaled factor from the calibration table.
4. **Static + local** — no fresh oracle artifacts: static floor layer + the client's always-warm private shadow estimator (per-path robust EWMA over its own samples).
5. **Vanilla SCION** — policy-filtered default path + minimal liveness probing.

Rung transitions are logged and reported on reconnection — oracle unavailability is itself telemetry. Server-side: per-unit/per-bucket circuit breakers (T1→T0 on coverage breach), snapshot-seq monotonicity against cache rollback, registry auto-rollback on canary regression, replay-KV replication in the failure budget.

## 27. SLOs & monitoring

Served-state SLOs: N2 freshness, N3 latency, N4 trailing coverage within ±3 % per bucket, tier-labeling correctness (audited by anchor spot-checks). Oracle-health dashboards: coverage per bucket, performativity diagnostic per aggregate, diversity distributions per unit class, quarantine rates, hint efficacy, exploration-floor realized rates, defection estimate (share of observed switching behavior violating discipline timing), rung-transition rates, federation peer coverage. Alarms drive the documented fallbacks; nothing pages a human for a condition the cascade already absorbs.

---

# Part VII — Evaluation, Build Plan, Open Issues

## 28. Evaluation campaign

**Environments.** E1 local `scion.sh` topologies (protocol/unit tests); E2 seed-emulator/ns-3 SCION at 50–200 AS with scripted load — the only environment with exportable ground-truth link state, which is therefore an E2 *requirement*; E3 SCIONLab (wild-testbed realism, overlay artifacts accepted); E4 production ISD in shadow mode.

**Accuracy** — reported **stratified by evidence tier, ambiguity class, and horizon**, against mandatory baselines: persistence, Tier-0-only, static-only, latest-sample, EWMA (≈ the OVGU oracle), network-coordinate/matrix-factorization embeddings, steady-state GNN. Forecast heads ship per stratum only where they beat persistence by the declared margin. Nowcast-vs-Tier-0 deltas are reported as the marginal value of spatial structure — never headlined (the nowcast contains its own inputs).

**Decision quality** — true regret vs hindsight-optimal in E2 only; E3/E4 via doubly-robust off-policy estimation over selection logs, with the exploratory slice as the gold subset; application-level QoE (flow completion time, throughput, MOS-proxy) per requirement class.

**Stability (the headline)** — E2 closed loop, primary axes: population size (10²–10⁴ selectors) × **defector fraction (0–100 %)** × mechanism ladder {point-rankings ablation, +intervals, +discipline, +jittered mirror, +coordination} `[v1.1]` × **paths per selector (1 … k)**. Deliverables: oscillation amplitude, price-of-anarchy estimates, convergence times, and **the compliant-share threshold below which each mechanism fails** — the number that conditions every stability claim this document makes.

`[v1.2]` **A fifth axis: staleness.** The stability results this design leans on — client-side strategy selection converging to pure equilibria, with reported switching-frequency reductions of roughly 60–80 % over naive best-response — carry an explicit precondition of *information freshness*, alongside sufficient path-quality separation and tuned thresholds. This document's freshness target is 15 s median and 60 s p99. **Whether the cited stability results survive at the design's own SLO is not established, and it is the single most load-bearing unexamined link between this architecture and the theory it rests on.** Sweep staleness as a first-class axis and report the freshness at which each mechanism's threshold degrades; a mechanism that is stable only at freshness the deployment cannot deliver is not a mechanism this document may claim.

`[v1.2]` **And separate the two questions Tier-1 is asked**, since §28 already distinguishes them and ADR-04 now ships per stratum: *(a) does spatial structure improve the **nowcast*** — measured as the Tier-1-vs-Tier-0 delta, which is where a graph earns its place because it is how an under-observed unit borrows strength — and *(b) does anything beat **persistence** at +60 s / +300 s*, a purely temporal question. Report them separately rather than as one "Tier-1 lift" figure. Expect (a) to be positive on corroborated units and (b) to be negative on many strata; that pattern is a finding, not a disappointment, and it determines what actually ships.

`[v1.1]` **The fourth axis is not a refinement, it is a separate regime.** Profile B gateways default to a single path (§12.2), which removes within-reporter mixing entirely and turns the mechanism ladder's "sample uniformly within the ε-set" rung into a no-op (§20.6, §21). The campaign must therefore report the compliant-share threshold **per regime**, and the results must not be quoted across them: a threshold measured with 10⁴ mixing hosts says nothing about 10² single-path gateways, and the gateway case is the production one. Two specific requirements follow. Run the ladder at least at `paths=1` and `paths=k` for the same population, so the value of gateway multipath configuration is measured as a stability intervention in its own right. And report gateway-count sensitivity separately from host-count sensitivity, since N7's ~10² gateways give the realised aggregate roughly 30× the relative sampling noise of the 10⁵-host case, and that noise drives oscillation rather than damping it.

**Robustness** — red-team with concentration attacks on sparse units as the primary scenario, plus false-path-claim campaigns, floor manipulation, rush attacks vs quarantine, and Profile B single-reporter capture; success criterion is correct *tier demotion*, not estimate accuracy under attack.

**Performativity** — §19.6 validated in E2 with scripted demand response. **Steering** — accuracy-per-probe with hints on/off; exploration-floor value via ablation. **Systems** — staleness, latency, cost against §25.

## 29. Build plan

**M0 — Observability & population study (gating).** Before any predictive component, instrument reporters in each target environment and measure the parameters the design is conditioned on: (a) the reply-reversed fraction of RTT evidence (sizes ADR-13's refinement layer), (b) the native-host vs gateway traffic split (fixes the profile mix), (c) static-metadata coverage `[v1.1]` **and its asymmetry** (sets the γ anchoring posture — §9.1 needs not merely how often StaticInfo is present but how far its symmetric estimate sits from anchor-measured ρ/2, which is the only way to calibrate the asymmetry-uncertainty term), (d) the empirical evidence-diversity distribution per unit (sets D_min and c_max). M0's output is a parameterization; its by-product dataset is independently publishable.

`[v1.1]` **Three parameters added by the implementation audit**, each because some part of this document is sized against a number that turned out to be per-deployment rather than a protocol constant:

- **(e) The SCMP probe budget.** Rate limiting is *permitted* by the SCMP specification and *absent* from the audited router. G4's censoring model, §12.1's 1 probe/s cap and §20.5's steering economics all assume scarcity. Measure the actual limit per target deployment — it may be zero — and record which noise model Tier-0 should select (§19.1). A deployment with no rate limiting makes traceroute cheap and missing-*at*-random, which is a materially easier estimation problem and a materially larger probe budget.
- **(f) Paths per Profile B gateway.** The gateway's configured `PathCount` and its realised switching behaviour. This selects the stability regime (§21) and is an axis of the §28 campaign, not a detail.
- **(g) Transport mix.** The non-QUIC share of observable traffic, since the passive sampler is QUIC-first by scope decision (§12.1) and the resulting coverage gap composes with ADR-15's selection bias.

Parameters (e) and (f) are **gating for their dependent components** in the same sense (a)–(d) are: shipping steering (§20.5) without (e), or quoting a stability threshold without (f), means shipping a number whose meaning has not been established.

**`[v1.2]` What happens if (d) comes back badly — declared in advance.** §25 identifies evidence diversity as *the* binding constraint, and M0(d) measures it. A gate with no declared failure branch is a gate that gets argued away when the inconvenient number arrives, so the branch is written here, before anyone has seen it.

The concern is concrete. D_min defaults to 4 *independent source-AS buckets* (§17.5) — not four reports, four buckets, so a thousand hosts in one AS is one. Traffic over paths is heavy-tailed: a small set of popular destination pairs carries most flows and traverses an even smaller set of core units, while stub and edge units are traversed by few source ASes almost by construction. That a large fraction of a 10 k-unit graph reaches D_min ≥ 4 is not obviously true.

If the measured distribution says most units sit at thin or prior-only, the response is **(1) coarsen the view**, and only then consider the others:

1. **Coarsen.** §7.1's partition is the *finest identifiable* one; nothing requires serving at that granularity. Merging further trades resolution for corroboration, and it is a knob this design already has. **A 2 k-unit graph in which most units are corroborated is a better product than a 10 k-unit graph in which most are priors** — the served state is what a selector acts on, and a wide prior-anchored interval does not discriminate between paths. Coarsening is therefore the default response, not a retreat.
2. **Lower D_min, with the tier semantics carrying the change.** Permitted by ADR-17 in principle; it shifts risk onto the labelling, and the manipulation bound of N5 must be re-derived at the new threshold rather than assumed to survive it.
3. **Reposition.** If only the core corroborates, the product is a core-network oracle with an honest prior layer over the edge. That still beats the prior art and is still worth deploying; it is a different pitch and should be made deliberately rather than discovered.

**What is not permitted:** serving thin units as corroborated, or quietly moving D_min without re-deriving N5. The tiering is the product's honesty mechanism; trading it for coverage would trade away the thing that distinguishes this design from the systems §28 benchmarks it against.

**M1** — schemas + host and gateway reporter agents + Ingestion Gateway + Tier-0 on the edge substrate + canonical `oracle-compose` (§10 semantics) + pull API. *Alone, this reproduces and exceeds the prior state of the art and is a working measurement instrument.* **M2** — Trust Pipeline with diversity gating + anchor fleet + bulk mirror with jitter + degradation ladder. **M3** — combination-closure and unit-view jobs + Tier-1 + Tier-2 + model registry, shadow mode; `SelectionEvent` + ε-floor land here (they precede any decision-quality claim by construction). **M4** — push + steering + federation with cross-anchors between two testbed ISDs. **M4½ `[v1.2]`** — the E2 closed-loop campaign (§28), run to the "+jittered mirror" rung of the mechanism ladder, with staleness and paths-per-selector as axes. **M5** — coordination, *if M4½ says it is needed*.

`[v1.2]` **Why the split.** v1.0 put coordination and the campaign in the same milestone — the campaign whose entire purpose is to determine whether coordination is necessary. Building both together guarantees coordination ships, because by the time the answer arrives the code exists. Splitting them costs nothing if the answer is "yes" and saves the riskiest component in this document if the answer is "no".

Three reasons to expect it may be "no". The mechanisms below coordination — calibrated intervals, ε-argmax with dwell and hysteresis, jittered timers, jittered mirror delivery — are reported in the literature this design cites to cut switching frequency by roughly 60–80 % on their own, converging to pure equilibria under stated conditions. ADR-10 already carries the reopening condition "ship coordination dormant if intervals+discipline suffice". And §21 is the one component that *actively moves traffic* while its contraction analysis remains an acknowledged open problem (§30) — the combination this document treats with most suspicion everywhere else.

The route-flap-damping history (§20.3) is the cautionary case: a globally-deployed damping mechanism, well-motivated, that produced systematically unfair outcomes and was widely disabled. Coordination is a damping mechanism with a published share vector. It should be built only against a measured demonstration that the cheaper rungs are insufficient, and the campaign is what produces that demonstration. **M6 (parallel track)** — proof-of-traversal: EPIC-authenticator-derived receipts binding reports to data-plane traversal; on landing, ADR-17 gating relaxes for receipt-backed reports. `[v1.1]` **Two scope constraints from the audit.** First, EPIC-HP is marked complete in its design record and the EPIC path type is handled in the router, but the supporting library still sits under `pkg/experimental`; treat EPIC as available-but-experimental, not as settled infrastructure, and do not let ADR-17's adoption incentive depend on a timeline this project does not control. Second, and structurally: **a reply to an EPIC path is a plain SCION path.** The default reply pather substitutes the embedded SCION path when reversing, so EPIC-derived receipts attest **forward traversal only**. That matters differently per metric — goodput and loss are directional and forward-attributed anyway, so receipts are strong evidence for them; ρ is bidirectional by G3, so a forward-only receipt authenticates half of what a latency report claims. Scope M6 accordingly: full gating relaxation for goodput and loss, partial for latency, and say which in the tier metadata rather than treating "receipt-backed" as one undifferentiated flag.

**Stack.** Go for services, caches, and the canonical library (the `scionproto` ecosystem); Rust optional on the trust hot path; PyTorch → TorchScript/ONNX serving sidecar; ClickHouse; NATS JetStream/Kafka; Redis-class KV for state-store fronting and the replay tier; Prometheus/Grafana. Client: `pan` extension + companion daemon over `sciond`'s gRPC API.

**Rollout.** Shadow (log, decide nothing) → advisory for opt-in applications → push → coordination pilot on one congested aggregate → per-AS caches; each stage gated on the previous stage's SLOs.

## 30. Open issues (tracked, non-blocking)

Formal contraction analysis of the coordination loop under heterogeneous demand elasticity; performative-stable prediction for graduated aggregates (ADR-15); blind-token reputation transfer (unlinkability with reputation); incentive design beyond freshness tiers (receipt-backed priority; reputation-linked reservation priority); cross-ISD model transfer once ≥ 3 deployments exist (ADR-04); ISD-governance templates (operatorship, export policy, Profile B contracts); the economics of gateway participation among commercial competitors — what minimal telemetry export is individually rational, and does the donation equilibrium hold without it; conditional-coverage conformal upgrades (ADR-06).

**`[v1.1]` Added by the implementation audit.** *Stability in the discrete single-path regime* — the analysis in §21 is written for mixed strategies and Profile B may not be in that regime (§12.2); the finite-action case needs its own treatment, and it is the production one. *Liveness corroboration without authenticated SCMP* — §20.3's gate bounds the blast radius of a spoofed interface-down but does not close it, and closing it depends on upstream decisions (SCMP authentication is on hold for lack of consensus) or on operator BFD export (§16), neither of which this design controls; the open question is what corroboration is achievable on units whose only witnesses are structurally correlated. *Calibrating the γ asymmetry term* — §9.1 requires an uncertainty term for the undeclared asymmetry in symmetric StaticInfo values, and the only way to estimate it is anchor measurement of both directions, which exists for a small and non-random subset of units; whether that subset generalises is unknown. *Hidden-path units and the diversity gate* — units held in the non-served partition (§6) can never reach corroborated tier from public evidence, so the gate is structurally unreachable for them; whether authorized-party corroboration should form a parallel tier, and what that does to §17.5's arithmetic, is unresolved.

**`[v1.2]` Closed by the design review, and what replaced them.** Three items left open by v1.0 and v1.1 are now resolved in the text rather than tracked: the unit view's identifiability criterion (§7.1 — was a heuristic, now a rank condition), calibration validity under self-influence (§19.5 — was monitored, now an online guarantee), and the steering objective (§20.5 — was a product of heuristic weights, now a measurement-design problem). What remains open in their place is narrower and more tractable: the **step-size policy for per-bucket α tracking** at low observation rates, where the coverage-tracking update can itself manufacture interval instability; the **goodput identifiability accounting**, which §7.1 gives only in weaker form because min-composition yields inequalities rather than equations, and which deserves its own treatment; and whether the **rank-greedy steering schedule's** submodularity is exact enough here to inherit the usual approximation guarantee, or only suggestive.

**Watch list — upstream dependencies with no owner here.** SCMP authentication (currently postponed; its resolution changes §20.3 and §23 materially, in either direction); the standards-track status of the SCION Internet-Drafts (N9's argument is scheduled against it); EPIC's graduation from experimental (M6). None of these blocks anything in this document — every one of them is handled by a mechanism that degrades honestly if the dependency never lands — but each should be re-checked at each milestone boundary rather than assumed stable, and this document's SCION-specific claims should be re-audited against the implementation on the same cadence. **The v1.1 audit is the template: the failures it found were not errors of reasoning but statements that were true when written.**

---

# Glossary

**ρ-unit** — round-trip latency prediction unit: the paired quantity x_e→ + x_e← for an element, the identified object under reply-reversed RTT evidence (G3). **Unit** — a tomographic equivalence class of directed edges: the finest granularity the evidence supports; the object predicted and served. **Unit view** — the versioned partition of the stable edge substrate into units, with split/merge continuity rules. **Combination closure** — the set of intra-AS traversal edges realizable by SCION path-combination rules over the current segment inventory (not merely per-segment pairs). **Ambiguity class** — a unit's resolution label: TR (traceroute-resolvable), OWD (directionally resolvable with one-way evidence), SP (structurally permanent; only AS interior telemetry resolves). **Evidence tier** — corroborated / thin / prior-only / operator-attested / remote-attested; served with every state; a path's tier is its weakest unit's. **Correlation group** — units treated as comonotone in composition (shared AS or flagged shared bottleneck); also the multipath disjointness criterion. **Profile A / B** — Host-Crowd / Gateway-Federated deployment profiles. **Tier-0/1/2** — online robust filters / spatio-temporal GNN / conformal calibration. **ε-floor** — the mandated exploration share of compliant elastic traffic. **SelectionEvent** — the opt-in decision log enabling off-policy evaluation. **Snapshot** — the signed, versioned unit-state object that all dissemination derives from. **Anchors** — trusted probers referenced by reputation, calibration, and coordination triggering. **Degradation ladder** — the five service rungs from full service to vanilla SCION (N1's mechanism). `[v1.1]` **γ_u** — the metadata-derived latency prior for a unit: a *soft anchor* with an asymmetry-uncertainty term, not a floor (§9.1). **Expiry quantum** — 337.5 s, the resolution of hop-field expiry and therefore of the deterministic half of liveness (§9.3). **Candidate hazard** — a liveness-hazard elevation from unauthenticated SCMP that widens intervals and lowers tier but does not trigger a push; becomes *promoted* on independent corroboration (§20.3). **Non-served partition** — units estimated but withheld from the mirror and from unqualified queries because serving them would disclose a hidden path (§6, §24). **Discrete regime** — the single-path-selector dynamics of Profile B gateways, in which within-reporter mixing is absent and §21's mixed-strategy stability results do not apply.

*End of Master Specification (v1.1).*
