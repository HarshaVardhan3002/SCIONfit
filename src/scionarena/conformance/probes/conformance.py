"""The thirteen conformance probes, R1 through R13.

Each probe corresponds to one requirement from the architecture proposal.
Each one changes exactly one thing and watches what the model does.

R11 to R13 arrived with ADR 0023, and R4 was re-aimed at the same time: it was
written to catch a model that collapses under ``crypto_bound`` identifiers, and
Q1 resolved that the deployed fingerprint hashes the interface sequence alone --
so failing a model there is a false positive against the thing conformance claims
to measure. R4 keeps what it actually tests, which is generalisation to
interfaces that did not exist at reset, and the identity question moved to R12,
where it is graded and never blocks.
"""

from __future__ import annotations

import math
from statistics import mean

from ...exposure.contracts import SLA, Demand
from .base import Probe, Status

WARMUP_STEPS = 12


def _warm(model, world, rng, steps: int = WARMUP_STEPS, coverage: float = 1.0):
    """Reset the model and feed it a plausible observation history."""
    topo = world.snapshot()
    model.reset(topo, seed=rng.randint(0, 2**31 - 1))
    d = world.uniform_demand()
    for _ in range(steps):
        obs = world.observe(demand=d, coverage=coverage)
        model.observe(obs, world.snapshot())
        world.step()
    return world.snapshot()


def _rel(a: float, b: float) -> float:
    """Relative change, robust to zeros."""
    denom = max(abs(a), abs(b), 1e-9)
    return abs(a - b) / denom


# --------------------------------------------------------------------------


class R1SharedLinkSensitivity(Probe):
    probe_id, requirement = "R1", "Operates on a graph with typed edges"
    title = "Shared-link coupling"
    remedy = (
        "Predictions must respond to the state of the links a path "
        "traverses. A model that scores paths independently cannot "
        "represent shared bottlenecks."
    )

    def run(self, model, world, rng):
        topo = _warm(model, world, rng)
        base = model.predict(topo, topo.paths)

        # pick an interface used by some paths but not all
        counts = {
            i: len(world.paths_using(i))
            if hasattr(world, "paths_using")
            else len([p for p in topo.paths if i in p.interfaces])
            for i in topo.interfaces
        }
        shared = [i for i, c in counts.items() if 0 < c < len(topo.paths)]
        if not shared:
            return self.result(
                Status.NOT_APPLICABLE, "No interface is used by a strict subset of paths."
            )
        iid = max(shared, key=lambda i: counts[i])
        users = {p.path_id for p in topo.paths if iid in p.interfaces}
        others = {p.path_id for p in topo.paths} - users

        world.perturb_link(iid, latency_factor=4.0)
        for _ in range(6):
            model.observe(world.observe(demand=world.uniform_demand()), world.snapshot())
            world.step()
        topo2 = world.snapshot()
        after = model.predict(topo2, topo2.paths)

        du = (
            mean([_rel(base[p].cost(), after[p].cost()) for p in users if p in after])
            if users
            else 0.0
        )
        do = (
            mean([_rel(base[p].cost(), after[p].cost()) for p in others if p in after])
            if others
            else 0.0
        )

        ev = dict(
            perturbed_interface=iid,
            n_users=len(users),
            n_others=len(others),
            mean_change_on_users=round(du, 4),
            mean_change_on_others=round(do, 4),
        )
        if du < 0.02:
            return self.result(
                Status.FAIL,
                "Degrading a shared link barely moved the predictions of the paths that use it.",
                score=0.0,
                **ev,
            )
        if du <= do * 1.2:
            return self.result(
                Status.WEAK,
                "Paths using the degraded link moved no more than paths "
                "that do not. The model is reacting, but not through "
                "the graph.",
                score=0.35,
                **ev,
            )
        return self.result(
            Status.PASS,
            f"Affected paths moved {du / max(do, 1e-9):.1f}x more than unaffected ones.",
            score=min(1.0, du / max(do, 1e-9) / 5),
            **ev,
        )


class R2UnseenPathComposition(Probe):
    probe_id, requirement = "R2", "Treats paths as first-class objects"
    title = "Composition onto an unseen path"
    capability = "composes_unseen_paths"
    remedy = (
        "A path assembled from links you have observed should be "
        "predictable even though the path itself was never measured."
    )

    def run(self, model, world, rng):
        topo = _warm(model, world, rng)
        pool = [i for p in topo.paths for i in p.interfaces]
        if len(set(pool)) < 3:
            return self.result(Status.NOT_APPLICABLE, "Not enough interfaces.")
        picked = list(dict.fromkeys(pool))[:3]
        newp = world.add_path_using(picked, "p_unseen")
        topo2 = world.snapshot()

        preds = model.predict(topo2, [newp])
        if newp.path_id not in preds:
            return self.result(
                Status.FAIL,
                "Model returned no prediction for a path built from links it has observed.",
                score=0.0,
            )
        pr = preds[newp.path_id]
        v = pr.latency_ms.point
        if math.isnan(v) or v <= 0:
            return self.result(
                Status.FAIL,
                "Prediction for the unseen path is not a usable number.",
                value=v,
                score=0.0,
            )
        truth, _, _ = world.path_metrics(newp)
        err = _rel(v, truth)
        ev = dict(
            predicted_latency_ms=round(v, 2),
            true_latency_ms=round(truth, 2),
            relative_error=round(err, 3),
        )
        if err > 1.0:
            return self.result(
                Status.WEAK, "Produced a prediction, but off by more than 100%.", score=0.3, **ev
            )
        return self.result(
            Status.PASS,
            f"Composed a prediction within {err * 100:.0f}% of truth.",
            score=max(0.0, 1.0 - err),
            **ev,
        )


class R3MissingnessDiscrimination(Probe):
    probe_id, requirement = "R3", "Consumes sparse, irregular, partly missing data"
    title = "Missing is not zero"
    remedy = (
        "Pair every telemetry feature with an observed/not-observed mask. "
        "Imputing missing values as zero silently poisons the model."
    )

    def run(self, model, world, rng):
        topo = world.snapshot()
        target = topo.paths[0]

        # arm A: metric genuinely missing
        model.reset(topo, seed=1)
        for _ in range(WARMUP_STEPS):
            obs = world.observe(demand=world.uniform_demand())
            obs = [
                o
                if o.path_id != target.path_id
                else type(o)(
                    t=o.t,
                    path_id=o.path_id,
                    latency_ms=None,
                    throughput_mbps=o.throughput_mbps,
                    loss=o.loss,
                    source=o.source,
                )
                for o in obs
            ]
            model.observe(obs, world.snapshot())
            world.step()
        a = model.predict(world.snapshot(), [target])[target.path_id].latency_ms.point

        # arm B: metric observed, and it is zero
        model.reset(topo, seed=1)
        for _ in range(WARMUP_STEPS):
            obs = world.observe(demand=world.uniform_demand())
            obs = [
                o
                if o.path_id != target.path_id
                else type(o)(
                    t=o.t,
                    path_id=o.path_id,
                    latency_ms=0.0,
                    throughput_mbps=o.throughput_mbps,
                    loss=o.loss,
                    source=o.source,
                )
                for o in obs
            ]
            model.observe(obs, world.snapshot())
            world.step()
        b = model.predict(world.snapshot(), [target])[target.path_id].latency_ms.point

        ev = dict(
            pred_when_missing=round(a, 3) if a == a else None,
            pred_when_observed_zero=round(b, 3) if b == b else None,
        )
        if math.isnan(a) or math.isnan(b):
            return self.result(Status.ERROR, "Model produced NaN under missingness.", **ev)
        if _rel(a, b) < 1e-6:
            return self.result(
                Status.FAIL,
                "Identical prediction whether the value was missing or "
                "genuinely zero. Missingness is being imputed away.",
                score=0.0,
                **ev,
            )
        return self.result(
            Status.PASS, "Distinguishes an absent measurement from a zero one.", score=1.0, **ev
        )


class R4UnseenInterfaces(Probe):
    """Generalisation to new interfaces. **Not** the identity question -- see R12.

    Re-aimed by ADR 0023. This probe was justified by the hazard that a model
    keying on a path identifier loses its history whenever a segment is
    re-signed. Q1 resolved that the deployed fingerprint hashes the interface
    sequence alone, so that hazard is not reached through the identifier and a
    model failing here is failing something else: whether it can score a path
    built from interfaces that did not exist when it was reset. That is a real
    deployment requirement and it is what the code below has always tested.
    """

    probe_id, requirement = "R4", "Generalises to interfaces not seen in training"
    title = "Survives topology churn"
    capability = "handles_unseen_interfaces"
    remedy = (
        "Replace identity lookups with structural features computable "
        "for a node that appeared one second ago."
    )

    def run(self, model, world, rng):
        topo = _warm(model, world, rng)
        before = model.predict(topo, topo.paths)
        n_before = len(before)

        new_ifaces = world.add_new_interfaces(3)
        newp = world.add_path_using(new_ifaces, "p_churn")
        topo2 = world.snapshot()

        try:
            after = model.predict(topo2, list(topo2.paths))
        except Exception as exc:
            return self.result(
                Status.FAIL,
                f"Raised on a topology containing new interfaces: {type(exc).__name__}: {exc}",
                score=0.0,
                new_interfaces=new_ifaces,
            )

        if newp.path_id not in after:
            return self.result(
                Status.FAIL,
                "Silently dropped the path made of unseen interfaces.",
                score=0.0,
                new_interfaces=new_ifaces,
            )
        v = after[newp.path_id].latency_ms.point
        if math.isnan(v):
            return self.result(
                Status.FAIL,
                "Returned NaN for the unseen path.",
                score=0.0,
                new_interfaces=new_ifaces,
            )

        # uncertainty should not shrink on something never observed
        sp_new = after[newp.path_id].latency_ms.spread
        sp_old = mean([after[p].latency_ms.spread for p in before if p in after]) or 0.0
        ev = dict(
            n_paths_before=n_before,
            n_paths_after=len(after),
            new_interfaces=new_ifaces,
            spread_unseen=round(sp_new, 3),
            spread_known=round(sp_old, 3),
        )
        if sp_old > 0 and sp_new < sp_old * 0.9:
            return self.result(
                Status.WEAK,
                "Handles the new topology but is *more* confident about "
                "the never-observed path than about known ones.",
                score=0.5,
                **ev,
            )
        return self.result(
            Status.PASS, "Handled interfaces that did not exist at reset.", score=1.0, **ev
        )


class R5Distributional(Probe):
    probe_id, requirement = "R5", "Emits a distribution, not a point estimate"
    title = "Distributional output"
    capability = "distributional"
    remedy = "Emit at least three quantiles per metric, ordered, and widen them when unobserved."

    def run(self, model, world, rng):
        topo = _warm(model, world, rng, coverage=0.6)
        preds = model.predict(topo, topo.paths)
        if not preds:
            return self.result(Status.ERROR, "No predictions returned.")

        n_dist = sum(1 for p in preds.values() if p.latency_ms.is_distributional)
        frac = n_dist / len(preds)
        bad_order = [k for k, v in preds.items() if not v.latency_ms.quantiles_monotone()]
        ev = dict(fraction_distributional=round(frac, 3), quantile_order_violations=bad_order[:5])

        if frac == 0:
            return self.result(
                Status.FAIL,
                "Point estimates only. Nothing downstream can tell a "
                "well-measured path from an unobserved one.",
                score=0.0,
                **ev,
            )
        if bad_order:
            return self.result(
                Status.FAIL,
                f"{len(bad_order)} path(s) returned non-monotone quantiles.",
                score=0.2,
                **ev,
            )
        if frac < 0.99:
            return self.result(
                Status.WEAK,
                f"Only {frac * 100:.0f}% of paths got a distribution.",
                score=frac,
                **ev,
            )
        return self.result(
            Status.PASS, "All predictions are distributional and ordered.", score=1.0, **ev
        )


class R6DemandConditioning(Probe):
    probe_id, requirement = "R6", "Conditions the prediction on offered demand"
    title = "Demand sensitivity"
    capability = "demand_conditioned"
    remedy = (
        "Take demand as an explicit input. Without it the model cannot "
        "represent the effect of its own advice and will oscillate in "
        "closed loop."
    )

    def run(self, model, world, rng):
        topo = _warm(model, world, rng)
        light = Demand({p.path_id: 1.0 / len(topo.paths) for p in topo.paths}, n_hosts=100)
        target = topo.paths[0].path_id
        heavy = world.concentrated_demand(target, mass=0.95)

        a = model.predict(topo, topo.paths, demand=light)
        b = model.predict(topo, topo.paths, demand=heavy)
        if target not in a or target not in b:
            return self.result(Status.ERROR, "Target path missing from predictions.")

        d_target = _rel(a[target].cost(), b[target].cost())
        others = [p.path_id for p in topo.paths if p.path_id != target]
        d_other = mean([_rel(a[o].cost(), b[o].cost()) for o in others]) if others else 0.0
        ev = dict(
            loaded_path=target,
            change_on_loaded=round(d_target, 4),
            mean_change_on_others=round(d_other, 4),
        )

        if d_target < 1e-6 and d_other < 1e-6:
            return self.result(
                Status.FAIL,
                "Predictions are identical under uniform and 95%-concentrated "
                "demand. The model cannot see its own effect.",
                score=0.0,
                **ev,
            )
        if d_target < 0.01:
            return self.result(
                Status.WEAK,
                "Responds to demand, but only marginally on the path that "
                "received 95% of the traffic.",
                score=0.3,
                **ev,
            )
        return self.result(
            Status.PASS,
            f"Concentrating demand changed the loaded path's predicted "
            f"cost by {d_target * 100:.0f}%.",
            score=min(1.0, d_target * 4),
            **ev,
        )


class R7Monotonicity(Probe):
    probe_id, requirement = "R7", "Cost is non-decreasing in demand"
    title = "Monotone in load"
    capability = "monotone_in_demand"
    remedy = (
        "Constrain the demand pathway architecturally. Monotonicity is "
        "what makes the equilibrium unique; without it a solver can "
        "cycle between several self-consistent answers."
    )

    def run(self, model, world, rng):
        topo = _warm(model, world, rng)
        target = topo.paths[0].path_id
        levels = [0.05, 0.2, 0.35, 0.5, 0.65, 0.8, 0.95]
        costs = []
        for m in levels:
            d = world.concentrated_demand(target, mass=m)
            pr = model.predict(topo, topo.paths, demand=d)
            costs.append(pr[target].cost() if target in pr else float("nan"))

        if any(math.isnan(c) for c in costs):
            return self.result(Status.ERROR, "NaN in the demand sweep.", costs=costs)

        drops = [
            (levels[i + 1], round(costs[i + 1] - costs[i], 6))
            for i in range(len(costs) - 1)
            if costs[i + 1] < costs[i] - 1e-9
        ]
        span = max(costs) - min(costs)
        ev = dict(
            levels=levels,
            costs=[round(c, 3) for c in costs],
            violations=drops,
            total_span=round(span, 3),
        )

        if span < 1e-6:
            return self.result(
                Status.FAIL,
                "Cost is flat across the whole demand sweep; the model is "
                "not conditioning on demand at all.",
                score=0.0,
                **ev,
            )
        if drops:
            frac = len(drops) / (len(costs) - 1)
            return self.result(
                Status.FAIL,
                f"Predicted cost *fell* as load rose at {len(drops)} of {len(costs) - 1} steps.",
                score=max(0.0, 1 - frac),
                **ev,
            )
        return self.result(
            Status.PASS, "Cost rose monotonically across the sweep.", score=1.0, **ev
        )


class R8EmitsAssignment(Probe):
    probe_id, requirement = "R8", "Outputs a distribution over paths, not a ranking"
    title = "Assignment, not ranking"
    capability = "emits_assignment"
    remedy = (
        "Publish probabilities and let hosts sample. Every host that reads "
        "a ranking reads the same ranking and picks the same top entry."
    )

    def run(self, model, world, rng):
        topo = _warm(model, world, rng)
        adv = model.advise(topo, topo.paths, SLA.presets()["bulk"], n_hosts=1000)
        w = adv.normalised()
        tot = sum(w.values())
        ev = dict(
            n_paths=len(w),
            max_weight=round(adv.max_weight, 4),
            normalised_entropy=round(adv.normalised_entropy, 4),
            weights_sum=round(tot, 6),
            temperature=adv.temperature,
        )

        if abs(tot - 1.0) > 1e-6:
            return self.result(Status.FAIL, "Weights do not sum to 1.", score=0.0, **ev)
        if adv.max_weight > 0.999:
            return self.result(
                Status.FAIL,
                "One-hot advisory. Every host receives the same single "
                "path, which is the herding failure mode.",
                score=0.0,
                **ev,
            )
        if adv.max_weight > 0.9:
            return self.result(
                Status.WEAK,
                f"{adv.max_weight * 100:.0f}% of traffic on one path. "
                f"Legal, but close to a ranking.",
                score=0.4,
                **ev,
            )
        return self.result(
            Status.PASS,
            f"Spread across paths (max weight {adv.max_weight:.2f}, "
            f"normalised entropy {adv.normalised_entropy:.2f}).",
            score=adv.normalised_entropy,
            **ev,
        )


class R9SelfConsistency(Probe):
    probe_id, requirement = "R9", "The published distribution is self-consistent"
    title = "Advice survives its own consequences"
    capability = "self_consistent"
    remedy = (
        "Solve for a fixed point before publishing: predict, compute the "
        "demand the advice induces, re-predict, and iterate with damping "
        "until the advisory stops moving."
    )

    def run(self, model, world, rng):
        topo = _warm(model, world, rng)
        paths = list(topo.paths)
        bulk = SLA.presets()["bulk"]

        # Self-consistency is only a meaningful question if the model can
        # represent consequences at all.  Check that first rather than handing
        # out a free PASS to a model that ignores demand: a deterministic
        # greedy scorer trivially returns the same advisory twice, which is
        # repeatability, not self-consistency.
        uni = Demand({p.path_id: 1.0 / len(paths) for p in paths}, n_hosts=1000)
        conc = world.concentrated_demand(paths[0].path_id, mass=0.95)
        pa = model.predict(topo, paths, demand=uni)
        pb = model.predict(topo, paths, demand=conc)
        sensitive = any(_rel(pa[k].cost(), pb[k].cost()) > 1e-6 for k in pa if k in pb)
        if not sensitive:
            return self.result(
                Status.NOT_APPLICABLE,
                "Predictions do not respond to demand, so whether the advice "
                "survives its own consequences cannot be tested. Returning the "
                "same answer twice is repeatability, not self-consistency.",
                demand_sensitive=False,
            )

        a1 = model.advise(topo, paths, bulk, n_hosts=1000)
        w1 = a1.normalised()

        induced = Demand(w1, n_hosts=1000)  # the load a1 itself creates
        p2 = model.predict(topo, paths, demand=induced)
        p0 = model.predict(topo, paths)

        a2 = model.advise(topo, paths, bulk, n_hosts=1000)
        w2 = a2.normalised()
        tv = 0.5 * sum(abs(w1.get(k, 0.0) - w2.get(k, 0.0)) for k in set(w1) | set(w2))

        top = max(w1, key=w1.get)
        drift = _rel(p0[top].cost(), p2[top].cost()) if top in p0 and top in p2 else 0.0

        # the real test: re-solve against the induced load and see how far the
        # advisory moves.  A fixed point should barely move.
        u2 = {k: -p2[k].cost() for k in p2}
        rank_shift = 0.0
        if u2:
            best_after = max(u2, key=u2.get)
            rank_shift = (
                0.0 if best_after == top else abs(w1.get(top, 0.0) - w1.get(best_after, 0.0))
            )

        ev = dict(
            total_variation_between_rounds=round(tv, 4),
            top_path=top,
            top_path_cost_drift_under_own_advice=round(drift, 4),
            best_path_changes_once_loaded=(rank_shift > 0),
            solver_converged=a1.solver_converged,
            solver_iterations=a1.solver_iterations,
        )

        if a1.solver_converged is False:
            return self.result(
                Status.WEAK,
                "Model reports its solver did not converge. Honest; the "
                "fallback path should be exercised.",
                score=0.3,
                **ev,
            )
        if tv > 0.15:
            return self.result(
                Status.FAIL,
                f"Advisory moved by {tv:.2f} total variation between two "
                f"consecutive asks on identical state.",
                score=0.0,
                **ev,
            )
        if rank_shift > 0 and a1.max_weight > 0.6:
            return self.result(
                Status.FAIL,
                "The path the advisory concentrates on stops being the "
                "best one as soon as the advised load lands on it. "
                "The advice does not survive its own publication.",
                score=0.1,
                **ev,
            )
        if drift > 0.25 and a1.max_weight > 0.5:
            return self.result(
                Status.WEAK,
                "Advisory is stable, but the path it recommends degrades "
                f"{drift * 100:.0f}% once the recommended load arrives.",
                score=0.4,
                **ev,
            )
        return self.result(
            Status.PASS, "Advisory is stable and survives the demand it induces.", score=1.0, **ev
        )


class R10StalenessResponse(Probe):
    probe_id, requirement = "R10", "Degrades safely as information ages"
    title = "Confidence falls with information age"
    capability = "staleness_aware"
    remedy = (
        "Make the concentration of the advisory a decreasing function of "
        "observation age, so stale advice becomes vague rather than "
        "confidently wrong."
    )

    def run(self, model, world, rng):
        # fresh
        topo = _warm(model, world, rng)
        fresh = model.advise(topo, topo.paths, SLA.presets()["bulk"], n_hosts=1000)
        fresh_pred = model.predict(topo, topo.paths)
        fresh_conf = (
            mean([p.confidence for p in fresh_pred.values() if p.confidence is not None])
            if any(p.confidence is not None for p in fresh_pred.values())
            else None
        )

        # stale: no new observations for a long simulated interval
        for _ in range(600):
            world.step()
        stale_topo = world.snapshot()
        stale = model.advise(stale_topo, stale_topo.paths, SLA.presets()["bulk"], n_hosts=1000)
        stale_pred = model.predict(stale_topo, stale_topo.paths)
        stale_conf = (
            mean([p.confidence for p in stale_pred.values() if p.confidence is not None])
            if any(p.confidence is not None for p in stale_pred.values())
            else None
        )

        ev = dict(
            fresh_entropy=round(fresh.normalised_entropy, 4),
            stale_entropy=round(stale.normalised_entropy, 4),
            fresh_max_weight=round(fresh.max_weight, 4),
            stale_max_weight=round(stale.max_weight, 4),
            fresh_confidence=None if fresh_conf is None else round(fresh_conf, 4),
            stale_confidence=None if stale_conf is None else round(stale_conf, 4),
            elapsed_s=600,
        )

        widened = stale.normalised_entropy > fresh.normalised_entropy + 1e-3
        less_sure = (
            fresh_conf is not None and stale_conf is not None and stale_conf < fresh_conf - 1e-3
        )

        if not widened and not less_sure:
            return self.result(
                Status.FAIL,
                "After 600 s with no new measurements the advisory is just "
                "as concentrated and just as confident.",
                score=0.0,
                **ev,
            )
        if widened and less_sure:
            return self.result(
                Status.PASS,
                "Both the advisory and the reported confidence relax as information ages.",
                score=1.0,
                **ev,
            )
        return self.result(
            Status.PASS if widened else Status.WEAK,
            "Advisory relaxes with age."
            if widened
            else "Confidence falls but the advisory stays as concentrated.",
            score=0.7 if widened else 0.5,
            **ev,
        )


# --------------------------------------------------------------------------
# R11-R13: the three the review made possible (ADR 0023)


def _order(pred) -> list[str]:
    """Path ids cheapest first, by the model's own scalar cost."""
    return [pid for pid, _ in sorted(pred.items(), key=lambda kv: kv[1].cost())]


def _published_order(advisory) -> list[str]:
    """The ranking a model actually publishes: heaviest weight first.

    R11 measures this rather than the order of the point estimates, and the
    distinction is the whole design of the probe. A latency-class model is
    entitled to nowcast congestion -- refusing to would fail R1 and R3 for
    reasons that have nothing to do with layers -- and is *not* entitled to let
    that nowcast decide who goes first. Only the advisory says who goes first.
    """
    weights = advisory.normalised()
    return [pid for pid, _ in sorted(weights.items(), key=lambda kv: (-kv[1], kv[0]))]


def _mean_spread(pred) -> float:
    widths = [p.latency_ms.spread for p in pred.values()]
    return mean(widths) if widths else 0.0


def _kendall_swaps(a: list[str], b: list[str]) -> int:
    """Discordant pairs between two orderings of the same set.

    A count rather than a boolean because "it swapped one adjacent pair" and
    "it inverted the ranking" are different findings and the evidence should
    say which.
    """
    rank = {pid: i for i, pid in enumerate(a)}
    seq = [rank[pid] for pid in b if pid in rank]
    return sum(1 for i in range(len(seq)) for j in range(i + 1, len(seq)) if seq[i] > seq[j])


class R11LayerDiscipline(Probe):
    probe_id, requirement = "R11", "Ranks on the layer its requirement class allows"
    title = "Layer discipline"
    remedy = (
        "Rank on the static layer plus liveness; let the dynamic layer widen the "
        "interval and move mass, never reorder the ranking."
    )

    #: Backgrounds tried, in order, until the world's own cheapest path stops
    #: being the cheapest. The probe refuses to grade a change that did not
    #: happen, so it checks its own experiment before it checks the model.
    levels = (0.45, 0.6, 0.75, 0.88, 0.95)

    def run(self, model, world, rng):
        declared = getattr(model.capabilities, "requirement_class", "")
        if declared != "latency":
            return self.result(
                Status.NOT_APPLICABLE,
                "Only a model declaring requirement_class='latency' has claimed "
                f"anything this probe can contradict; this one declares "
                f"{declared or 'nothing'}.",
            )

        topo = _warm(model, world, rng)
        sla = SLA.presets()["bulk"]
        before_advice = model.advise(topo, list(topo.paths), sla, n_hosts=1000)
        before_pred = model.predict(topo, list(topo.paths))
        rank_before = _published_order(before_advice)
        width_before = _mean_spread(before_pred)
        if len(rank_before) < 2:
            return self.result(Status.NOT_APPLICABLE, "Fewer than two paths to order.")

        # The **world's** cheapest path, not the model's front-runner. What the
        # probe has to stage is reordering *pressure*: some other path must
        # genuinely become the cheapest, so that a model ranking on the dynamic
        # layer has a reason to swap and a model ranking on the static layer has
        # something to resist. Which path the model happened to prefer is what
        # is being measured, so it must not choose the experiment.
        was_cheapest = self._true_cheapest(world)
        target = next((p for p in topo.paths if p.path_id == was_cheapest), None)
        if target is None:
            return self.result(Status.ERROR, "The cheapest path is not in the snapshot.")
        users: dict[str, int] = {}
        for iid in dict.fromkeys(target.interfaces):
            users[iid] = sum(1 for p in topo.paths if p is not target and iid in p.interfaces)
        fewest = min(users.values(), default=0)
        # Least-shared interfaces first, so the congestion lands on the target and
        # as little else as possible; every interface of the path if that is not
        # enough to move it off the top. Aiming matters less than it looks --
        # whatever is congested, nothing is graded until the world's own cheapest
        # path has actually changed.
        narrow = [iid for iid, n in users.items() if n == fewest]
        wide = list(dict.fromkeys(target.interfaces))
        if not narrow:
            return self.result(
                Status.NOT_APPLICABLE, "The cheapest path has no interfaces to congest."
            )

        saved = dict(world.links)
        staged = None
        exclusive = narrow
        for aim in (narrow, wide):
            for level in self.levels:
                world.links = dict(saved)
                world.congest(aim, background=level)
                if self._true_cheapest(world) != target.path_id:
                    staged, exclusive = level, aim
                    break
            if staged is not None:
                break
        if staged is None:
            world.links = dict(saved)
            return self.result(
                Status.NOT_APPLICABLE,
                "No congestion level short of saturation made another path genuinely "
                "cheaper, so there is no reordering pressure to resist.",
                was_cheapest=was_cheapest,
                tried=wide,
            )

        for _ in range(6):
            model.observe(world.observe(demand=world.uniform_demand()), world.snapshot())
            world.step()
        topo2 = world.snapshot()

        # The probe's own controlled change, checked. Nothing static may have
        # moved, or a reordering is not attributable to congestion.
        moved = [
            iid
            for iid in exclusive
            if topo.interfaces[iid].declared_latency_ms != topo2.interfaces[iid].declared_latency_ms
            or topo.interfaces[iid].declared_bw_mbps != topo2.interfaces[iid].declared_bw_mbps
        ]
        if moved or {p.path_id for p in topo.paths} != {p.path_id for p in topo2.paths}:
            return self.result(
                Status.ERROR,
                "The probe moved something other than the dynamic layer; its own "
                "controlled change is not controlled.",
                static_moved=moved,
            )

        after_advice = model.advise(topo2, list(topo2.paths), sla, n_hosts=1000)
        after_pred = model.predict(topo2, list(topo2.paths))
        rank_after = _published_order(after_advice)
        width_after = _mean_spread(after_pred)
        swaps = _kendall_swaps(rank_before, rank_after)
        ev = dict(
            congested=exclusive,
            background=staged,
            was_cheapest=was_cheapest,
            now_cheapest=self._true_cheapest(world),
            rank_before=rank_before,
            rank_after=rank_after,
            swaps=swaps,
            spread_before=round(width_before, 4),
            spread_after=round(width_after, 4),
            weight_shifted=round(
                abs(
                    after_advice.normalised().get(rank_before[0], 0.0)
                    - before_advice.normalised().get(rank_before[0], 0.0)
                ),
                4,
            ),
        )

        if swaps:
            return self.result(
                Status.FAIL,
                f"Reordered {swaps} pair(s) of its published ranking on a change in "
                "congestion alone. A latency-class model ranking on a lagged "
                "congestion estimate is routing on lagged load, which is the "
                "oscillation mechanism the requirement classes exist to prevent.",
                score=0.0,
                **ev,
            )
        if width_after <= width_before * 1.01 and ev["weight_shifted"] < 1e-3:
            return self.result(
                Status.WEAK,
                "Held its ranking, which is the requirement, but the congestion "
                "widened nothing and moved no mass either -- so the dynamic layer "
                "is being ignored rather than used in the right place.",
                score=0.6,
                **ev,
            )
        return self.result(
            Status.PASS,
            "Held its published ranking through congestion that genuinely made "
            "another path cheaper, and answered with width and mass instead.",
            score=1.0,
            **ev,
        )

    @staticmethod
    def _true_cheapest(world) -> str:
        """The world's own cheapest path by realised latency, no demand."""
        return min(world.paths, key=lambda p: world.path_metrics(p)[0]).path_id


class R12IdentityChurnHygiene(Probe):
    """A grade, never a blocking failure. See ADR 0023."""

    probe_id, requirement = "R12", "Memory survives a re-signed segment"
    title = "Identity churn hygiene"
    remedy = (
        "Key per-path memory on the interface sequence or on structural features, "
        "never on the identifier the path arrived under."
    )

    def run(self, model, world, rng):
        topo = _warm(model, world, rng)
        before = model.predict(topo, list(topo.paths))

        # Re-beaconing: new identifiers, identical interface sequences, identical
        # network. Nothing about the world has changed.
        renamed = world.resign(fraction=1.0)
        topo2 = world.snapshot()
        if not renamed:
            return self.result(Status.NOT_APPLICABLE, "Nothing was re-signed.")

        try:
            after = model.predict(topo2, list(topo2.paths))
        except Exception as exc:
            return self.result(
                Status.WEAK,
                f"Raised on a re-signed path set: {type(exc).__name__}: {exc}. "
                "Graded rather than failed, because Q1 resolved that the deployed "
                "fingerprint is stable across re-signing, so this is hygiene.",
                score=0.0,
                n_renamed=len(renamed),
            )

        moved, total = 0, 0
        for old, new in renamed.items():
            if old not in before or new not in after:
                continue
            total += 1
            a, b = before[old].cost(), after[new].cost()
            if _rel(a, b) > 0.02:
                moved += 1
        if total == 0:
            return self.result(Status.NOT_APPLICABLE, "No path was scored both ways.")

        retained = 1.0 - moved / total
        ev = dict(
            n_renamed=len(renamed), n_compared=total, n_moved=moved, retained=round(retained, 3)
        )
        if retained >= 0.95:
            return self.result(
                Status.PASS,
                "Re-signing changed the identifiers and changed nothing the model said.",
                score=retained,
                **ev,
            )
        return self.result(
            Status.WEAK,
            f"{moved} of {total} predictions moved when only the identifiers did. "
            "The deployed fingerprint hashes the interface sequence alone (Q1), so "
            "this is a hygiene grade rather than a conformance failure -- but a "
            "model keying on the identifier discards its history every refresh "
            "cycle while every one of its outputs still looks plausible.",
            score=retained,
            **ev,
        )


class R13CalibrationUnderShift(Probe):
    probe_id, requirement = "R13", "Intervals recover nominal coverage after a shift"
    title = "Calibration under shift"
    # Deliberately *not* ``capability = "distributional"``. A model that emits
    # intervals and fails to recover their coverage after a shift has not lied
    # about being distributional -- it is distributional and badly calibrated,
    # which is a different and more interesting finding. Tying the probe to the
    # flag would have printed FALSE_CLAIM for it.
    capability = None
    remedy = (
        "Adapt the interval level from realised coverage rather than fixing it at calibration time."
    )

    #: The interval a distributional model is asked for, and what coverage is
    #: scored against. Same figure as the accuracy family's NOMINAL.
    nominal = 0.8
    #: Steps after the shift, and how many samples make one coverage window.
    horizon = 24
    window = 6

    def run(self, model, world, rng):
        topo = _warm(model, world, rng)
        sample = model.predict(topo, list(topo.paths))
        if not any(p.latency_ms.is_distributional for p in sample.values()):
            return self.result(
                Status.DECLARED_ABSENT,
                "Point estimator: there is no interval whose coverage could recover.",
            )

        before = self._coverage(model, world, steps=self.window)

        # A real degrade rather than congestion: this is a shift in the world,
        # which is what an adaptive level is for.
        hurt = world.busiest_interfaces(1)
        world.perturb_link(hurt[0], latency_factor=3.0)

        windows: list[float] = []
        for _ in range(self.horizon // self.window):
            windows.append(self._coverage(model, world, steps=self.window))

        floor = 0.75 * self.nominal
        recovered_at = next((i for i, c in enumerate(windows) if c >= floor), None)
        ev = dict(
            nominal=self.nominal,
            coverage_before=round(before, 3),
            coverage_after=[round(c, 3) for c in windows],
            degraded=hurt,
            recovered_window=recovered_at,
        )
        if before < floor:
            return self.result(
                Status.NOT_APPLICABLE,
                f"Coverage was already {before:.2f} against a nominal {self.nominal} "
                "before the shift, so there is no calibration here to lose.",
                **ev,
            )
        if recovered_at is None:
            return self.result(
                Status.FAIL,
                f"Coverage fell to {windows[0]:.2f} and never came back within "
                f"{self.horizon} steps. A fixed interval width does exactly this, and "
                "every coverage figure the model reports afterwards is describing a "
                "world that has moved.",
                score=0.0,
                **ev,
            )
        if recovered_at == 0:
            return self.result(
                Status.PASS,
                "Coverage held through the shift.",
                score=1.0,
                **ev,
            )
        return self.result(
            Status.WEAK,
            f"Coverage recovered, after {recovered_at * self.window} steps of "
            "reporting intervals it was not achieving.",
            score=0.6,
            **ev,
        )

    def _coverage(self, model, world, steps: int) -> float:
        """Fraction of realised latencies that fell inside the model's own interval."""
        hits = total = 0
        demand = world.uniform_demand()
        for _ in range(steps):
            topo = world.snapshot()
            predicted = model.predict(topo, list(topo.paths))
            world.step()
            for o in world.observe(demand=demand, noise=0.0):
                p = predicted.get(o.path_id)
                if p is None or o.latency_ms is None or not p.latency_ms.is_distributional:
                    continue
                qs = p.latency_ms.quantiles or {}
                ks = sorted(qs)
                lo, hi = qs[ks[0]], qs[ks[-1]]
                total += 1
                hits += int(lo <= o.latency_ms <= hi)
            model.observe(world.observe(demand=demand), world.snapshot())
        return hits / total if total else 0.0


ALL_PROBES = [
    R1SharedLinkSensitivity(),
    R2UnseenPathComposition(),
    R3MissingnessDiscrimination(),
    R4UnseenInterfaces(),
    R5Distributional(),
    R6DemandConditioning(),
    R7Monotonicity(),
    R8EmitsAssignment(),
    R9SelfConsistency(),
    R10StalenessResponse(),
    # Appended rather than inserted, so an existing report card's column order
    # is unchanged and two cards from different versions still line up.
    R11LayerDiscipline(),
    R12IdentityChurnHygiene(),
    R13CalibrationUnderShift(),
]
