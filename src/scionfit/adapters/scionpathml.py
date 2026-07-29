"""Tier 0: replay real SCIONLab measurements from ScionPathML.

Upstream: Rossi, Herschbach & Keshvadi, "SCION Path Performance Toolkit and
Benchmark", arXiv:2509.07154. Their toolkit wraps `scion showpaths / ping /
traceroute / bwtestclient` and emits per-measurement JSON, convertible to CSV.

STATUS: stub. The column mapping below is taken from the paper's description
of the CSV schema and has NOT yet been validated against a real export.
Validate before trusting: see docs/ADAPTERS.md.
"""

from __future__ import annotations

import csv
from collections.abc import Iterator
from pathlib import Path

from ..interface import Observation

#: our field <- their column. Verify against a real export before use.
COLUMN_MAP = {
    "t":               ("timestamp", "time", "measured_at"),
    "path_id":         ("path_fingerprint", "path_id", "fingerprint"),
    "latency_ms":      ("rtt_ms", "rtt", "latency_ms"),
    "throughput_mbps": ("bandwidth_mbps", "bw_mbps", "throughput"),
    "loss":            ("loss", "loss_rate", "packet_loss"),
    "src":             ("src_ia", "source_as", "src"),
    "dst":             ("dst_ia", "dest_as", "dst"),
}


def _pick(row: dict, names: tuple[str, ...]):
    for n in names:
        if n in row and row[n] not in ("", None):
            return row[n]
    return None


def _f(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def read_observations(csv_path: str | Path) -> Iterator[Observation]:
    """Yield Observations from a ScionPathML CSV export.

    Missing columns become ``None``, which is the correct representation:
    probe R3 checks that models distinguish that from a measured zero.
    """
    with open(csv_path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            t = _f(_pick(row, COLUMN_MAP["t"]))
            pid = _pick(row, COLUMN_MAP["path_id"])
            if t is None or pid is None:
                continue
            yield Observation(
                t=t, path_id=str(pid),
                latency_ms=_f(_pick(row, COLUMN_MAP["latency_ms"])),
                throughput_mbps=_f(_pick(row, COLUMN_MAP["throughput_mbps"])),
                loss=_f(_pick(row, COLUMN_MAP["loss"])),
                source="scionpathml",
            )


def qoe_profiles():
    """Their Task 4 QoE profiles, already mirrored in ``SLA.presets()``."""
    from ..interface import SLA
    return {k: v for k, v in SLA.presets().items() if k != "bulk"}
