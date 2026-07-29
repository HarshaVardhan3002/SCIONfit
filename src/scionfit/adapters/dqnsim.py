"""Tier 2: topology and beaconing from netsys-lab/scion-dqn-sim.

Upstream generates SCION topologies with BRITE, simulates the control plane
(beaconing, path discovery, segment registration) and writes
``scion_topology.json``. We consume that rather than reimplementing SCION
beaconing, which is exactly the part where guessing would show.

STATUS: stub. Upstream is marked work-in-progress (4 commits at time of
writing) so the JSON schema below is provisional. Clone, run
``01_generate_topology.py``, and check a real file before relying on this.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..interface import InterfaceAttrs, PathRef, TopologySnapshot

LINK_TYPE_MAP = {
    "core": "core", "CORE": "core",
    "parent": "parent_child", "child": "parent_child", "provider": "parent_child",
    "peer": "peering", "peering": "peering",
}


def load_topology(path: str | Path, t: float = 0.0) -> TopologySnapshot:
    """Read a scion-dqn-sim topology JSON into a TopologySnapshot."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))

    ifaces: dict[str, InterfaceAttrs] = {}
    for node in raw.get("interfaces", raw.get("links", [])):
        iid = str(node.get("id") or node.get("iface_id") or node.get("name"))
        ifaces[iid] = InterfaceAttrs(
            iface_id=iid,
            as_id=str(node.get("as") or node.get("as_id") or "unknown"),
            isd=int(node.get("isd", 1)),
            link_type=LINK_TYPE_MAP.get(str(node.get("type", "core")), "core"),
            declared_bw_mbps=node.get("bandwidth_mbps") or node.get("capacity"),
            declared_latency_ms=node.get("latency_ms") or node.get("delay"),
            mtu=node.get("mtu"),
        )

    paths: list[PathRef] = []
    for i, p in enumerate(raw.get("paths", [])):
        seq = p.get("interfaces") or p.get("hops") or []
        paths.append(PathRef(
            path_id=str(p.get("id", f"p{i}")),
            src=str(p.get("src", "")), dst=str(p.get("dst", "")),
            interfaces=tuple(str(h) for h in seq),
            expiry_s=p.get("expiry_s"), mtu=p.get("mtu"),
        ))
    return TopologySnapshot(t=t, interfaces=ifaces, paths=tuple(paths))
