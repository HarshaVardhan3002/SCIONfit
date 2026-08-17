# Core - Topology Engine (`scionarena.core.topology`)

> [topology.py](file:///C:/Users/Von/Desktop/PAS_Research_desktop/PAS_Research_desktop/scionfit/src/scionarena/core/topology.py)

---

## 📌 Role & Responsibilities
Maintains the static inter-AS graph structure, link capacities, propagation delays, MTUs, and SCION business relationships across ISDs (Isolation and Segmentation Domains).

## 🧱 Data Structures & Memory Layout
* **NumPy-Backed Arrays**: Stored in contiguous 1D read-only arrays for high-performance vectorized operations:
  * `as_isd` (int32): ISD assignment per AS.
  * `as_is_core` (bool): Whether an AS is a core AS.
  * `link_a`, `link_b` (int32): Endpoint AS IDs for each undirected link.
  * `link_rel` (int8): Relationship (`REL_CORE = 0`, `REL_PARENT_CHILD = 1`, `REL_PEER = 2`).
  * `link_capacity_mbps` (float32), `link_latency_ms` (float32), `link_mtu` (int32).
* **Compressed Sparse Row (CSR) Adjacency**:
  * `adj_indptr` (int64), `adj_neighbour` (int32), `adj_iface` (int32), `adj_link` (int32), `adj_role` (int8).

## ⚙️ Key Algorithms & Generators
1. **`synthetic(seed, tier)`**: Generates hierarchical multi-ISD topologies with core meshes, provider-customer DAGs, and lateral peering links according to scale tiers (`smoke`, `dev`, `realistic`, `stress`).
2. **`Topology.without_links(...)`**: Creates a new immutable topology instance for scheduled network outage events without in-place mutation.
