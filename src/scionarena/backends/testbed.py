"""Tier 3: a real SCION stack -- netsys-lab/ietf-scion-testbed.

12 ASes as Proxmox LXC containers, each running a border router, control
service and sciond. Crucially it ships ``linkd``, a per-AS link-shaping daemon
that applies tc netem/tbf to the inter-AS interfaces behind a REST API.

That daemon is what makes tier 3 usable as a benchmark rather than only a
demo: the same scenario that perturbs a link in the tier-1 analytical world
can perturb a real link here, so a scenario definition is portable across the
whole fidelity ladder.

STATUS: stub. Endpoint paths below are inferred from the repository README and
MUST be checked against ``linkd``'s actual REST surface before use. Nothing
here is exercised in CI, by design -- tier 3 needs hardware.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class LinkShape:
    """One link's shaping parameters, mirroring tc netem/tbf."""
    latency_ms: float | None = None
    jitter_ms: float | None = None
    loss: float | None = None
    rate_mbps: float | None = None


class LinkdClient:
    """Thin client for a per-AS linkd instance.

    Deliberately dependency-free and lazy about imports so that installing
    scionfit does not drag in an HTTP library for a tier most users never run.
    """

    def __init__(self, base_url: str, timeout: float = 5.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _call(self, method: str, path: str, payload: dict | None = None) -> dict:
        import json as _json
        import urllib.request
        url = f"{self.base_url}{path}"
        data = None if payload is None else _json.dumps(payload).encode()
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            body = r.read().decode() or "{}"
        return _json.loads(body)

    # --- endpoints below are PROVISIONAL, verify against linkd ---

    def list_links(self) -> dict:
        return self._call("GET", "/links")

    def shape(self, link_id: str, shape: LinkShape) -> dict:
        body = {k: v for k, v in vars(shape).items() if v is not None}
        return self._call("PUT", f"/links/{link_id}/shape", body)

    def clear(self, link_id: str) -> dict:
        return self._call("DELETE", f"/links/{link_id}/shape")

    def bgp_sessions(self) -> dict:
        return self._call("GET", "/bgp/sessions")


def scenario_to_shapes(scenario: dict) -> dict[str, LinkShape]:
    """Translate a scionfit scenario into per-link tc settings.

    This function is the whole point of tier 3: one scenario definition, four
    fidelity levels. Filling it in properly is the first task once the tier-1
    scenario schema settles.
    """
    out: dict[str, LinkShape] = {}
    for link_id, spec in scenario.get("links", {}).items():
        out[link_id] = LinkShape(
            latency_ms=spec.get("latency_ms"),
            jitter_ms=spec.get("jitter_ms"),
            loss=spec.get("loss"),
            rate_mbps=spec.get("rate_mbps"),
        )
    return out
