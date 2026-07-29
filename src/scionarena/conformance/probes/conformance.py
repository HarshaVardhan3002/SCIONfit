"""The ten conformance probes, R1 through R10.

Each probe corresponds to one requirement from the architecture proposal.
Each one changes exactly one thing and watches what the model does.
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
]
