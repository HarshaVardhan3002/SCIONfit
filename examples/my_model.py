"""A minimal model, written to show how little the interface demands.

Run it:  python examples/my_model.py
"""

from scionarena import Advisory, Capabilities, Dist, Prediction, check


class NaiveAverager:
    """Averages what it has seen per path. Point estimates. Picks the best one.

    Deliberately bad. Run it and read the report: the interesting part is not
    that it fails, it is *which* probes fail and what each one says to do.
    """

    capabilities = Capabilities(
        name="NaiveAverager",
        version="0.1.0",
        notes="Per-path mean latency, greedy selection.",
        # everything else defaults to False, which is the honest declaration
        handles_unseen_interfaces=True,
        composes_unseen_paths=False,
    )

    def reset(self, topo, seed=0):
        self.total, self.count = {}, {}

    def observe(self, obs, topo):
        for o in obs:
            if o.latency_ms is None:  # None means not measured
                continue
            self.total[o.path_id] = self.total.get(o.path_id, 0.0) + o.latency_ms
            self.count[o.path_id] = self.count.get(o.path_id, 0) + 1

    def _mean(self, path, topo):
        n = self.count.get(path.path_id, 0)
        if n:
            return self.total[path.path_id] / n
        return (
            sum(
                (topo.interfaces[i].declared_latency_ms or 10.0)
                for i in path.interfaces
                if i in topo.interfaces
            )
            or 10.0
        )

    def predict(self, topo, paths, horizon_s=0.0, demand=None):
        return {
            p.path_id: Prediction(
                latency_ms=Dist.point_estimate(self._mean(p, topo)),
                throughput_mbps=Dist.point_estimate(100.0),
                loss=Dist.point_estimate(0.001),
            )
            for p in paths
        }

    def advise(self, topo, paths, sla, n_hosts=1):
        pred = self.predict(topo, paths)
        best = min(pred, key=lambda k: pred[k].latency_ms.point)
        return Advisory(weights={k: (1.0 if k == best else 0.0) for k in pred})


if __name__ == "__main__":
    card = check(NaiveAverager(), seed=0, repeats=3)
    print(card.to_terminal(colour=False))
