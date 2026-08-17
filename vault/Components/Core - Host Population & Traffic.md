# Core - Host Population & Traffic (`scionarena.core.hosts`)

> [hosts.py](file:///C:/Users/Von/Desktop/PAS_Research_desktop/PAS_Research_desktop/scionfit/src/scionarena/core/hosts.py)

---

## 📌 Role & Responsibilities
Converts published probability advisories into actual Mbps load across physical interfaces via independent host decisions.

## 👥 The Host Population Model
* **Hosts as Counts**: Rather than instantiating millions of Python host objects, a scope $(src, dst, SLA)$ stores host counts $N$.
* **Vectorized Sampling**: Traffic allocation across $K$ candidate paths is sampled via a single NumPy multinomial call:
  $$\vec{n} \sim \text{Multinomial}(N, \vec{w}_{\text{advisory}})$$
  $$\vec{L} = \vec{n} \cdot \text{mbps\_per\_host}$$
* **$O(1/\sqrt{N})$ Concentration**: The empirical traffic split concentrates around the intended advisory distribution with standard deviation $\sigma_k = \sqrt{\frac{w_k (1 - w_k)}{N}}$.

## 😈 Defector Dynamics (Adversary Hook)
* Configurable via `defector_fraction` in [`HostParams`](file:///C:/Users/Von/Desktop/PAS_Research_desktop/PAS_Research_desktop/scionfit/src/scionarena/core/hosts.py#L53).
* Defectors ignore the advisory distribution and greedily select the single path with the lowest observed latency.
* Demonstrates network degradation when a fraction of hosts cheat the central recommendation oracle.
