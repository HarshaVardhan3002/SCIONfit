"""``scionarena cockpit`` -- watch a sweep while it happens.

    scionarena cockpit                 # http://127.0.0.1:8770, opens a tab

Standard library only, one page and some JSON endpoints, for the same reason
``ui.py`` is: ``core`` is numpy-only and a demonstration machine is usually one
with nothing installed on it.

Four things, and they are the four Phase 7 asks for.

**The configurator** answers "what am I about to start" *before* it starts:
models, axes, families, the cell count and a time estimate, and -- printed beside
them -- everything the selection leaves out. A narrowed suite that renders like a
full one is the dishonesty Phase 4 exists to prevent, moved into the interface
where it is easier to commit and harder to see.

**Registry-driven panels.** Nothing here lists a metric. ``panels.catalogue``
reads ``REGISTRY``, ``AXES``, ``ALL_PROBES`` and ``MANDATORY_BASELINES``, and the
page renders whatever it finds, one renderer per declared shape. Add a metric in
code and it appears; delete it and it goes.

**The lens.** Three deltas on one timeline: what the model was shown, what it
did, and what the world did back one round later. That is the view that says
*why* a model oscillates rather than that it does.

**The raw log**, one click away and never the default. A researcher chasing
something nobody anticipated needs it, and hiding it would be dishonest about
what the harness holds.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import traceback
import uuid
import webbrowser
from dataclasses import dataclass, field
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from ..bench.axes import AXES, baseline_cell
from ..bench.results import load_results
from ..bench.sweep import SweepSpec, plan, run_sweep
from ..instrument.channel import FrameLog
from .panels import catalogue, estimate_s, selection

__all__ = ["main", "serve", "Cockpit", "Run", "plan_for"]

TITLE = "scionarena — cockpit"
DEFAULT_PORT = 8770


@dataclass
class Run:
    """One sweep, on a worker thread, with a live channel out of its pool."""

    id: str
    spec: SweepSpec
    directory: Path
    total: int
    state: str = "running"  # running | done | error | stopped
    done: int = 0
    started: float = field(default_factory=time.perf_counter)
    finished: float | None = None
    error: str = ""
    last_cell: str = ""
    failures: list[str] = field(default_factory=list)
    stop: bool = False

    def status(self) -> dict[str, Any]:
        end = self.finished if self.finished is not None else time.perf_counter()
        return {
            "id": self.id,
            "state": self.state,
            "done": self.done,
            "total": self.total,
            "elapsed_s": round(end - self.started, 1),
            "last_cell": self.last_cell,
            "failures": self.failures[-8:],
            "error": self.error,
            "suite": self.spec.name,
            "tier": self.spec.tier,
            "digest": self.spec.digest(),
        }


class Cockpit:
    """Server state: one run at a time, and the frames it is producing."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.run: Run | None = None
        self.frames = FrameLog()
        self._lock = threading.Lock()

    # -------------------------------------------------------------- starting

    def start(self, spec: SweepSpec) -> Run:
        with self._lock:
            if self.run is not None and self.run.state == "running":
                raise RuntimeError("a run is already going; stop it before starting another")
            out = self.directory / spec.name
            run = Run(id=uuid.uuid4().hex[:8], spec=spec, directory=out, total=len(plan(spec)))
            self.run = run
            self.frames = FrameLog()
        threading.Thread(target=self._go, args=(run,), daemon=True).start()
        return run

    def _go(self, run: Run) -> None:
        def on_cell(done: int, total: int, cell_id: str, error: str | None) -> None:
            run.done, run.total, run.last_cell = done, total, cell_id
            if error:
                run.failures.append(f"{cell_id}: {error[:160]}")

        try:
            run_sweep(
                run.spec,
                run.directory,
                on_cell=on_cell,
                watch=self.frames.queue,
                workers=1 if run.spec.tier == "smoke" else None,
            )
            run.state = "stopped" if run.stop else "done"
        except Exception as exc:  # noqa: BLE001 -- a failed run is a reported state
            run.state = "error"
            run.error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=4)}"
        finally:
            run.finished = time.perf_counter()

    # -------------------------------------------------------------- reporting

    def status(self) -> dict[str, Any]:
        taken = self.frames.drain()
        run = self.run.status() if self.run is not None else {"state": "idle"}
        return {
            "run": run,
            # Said out loud, always. A cockpit that showed every frame would be
            # one that had slowed the world down to show them (ADR 0025).
            "frames_held": len(self.frames),
            "frames_missing": self.frames.missing,
            "frames_new": taken,
        }

    def since(self, index: int) -> dict[str, Any]:
        self.frames.drain()
        next_index, rows = self.frames.since(index)
        return {"next": next_index, "frames": rows, "missing": self.frames.missing}

    def results(self) -> dict[str, Any]:
        """Whatever is on disk for the current suite, scored.

        Read from the result files rather than held in memory, so a cockpit
        restarted mid-sweep still shows everything that already ran.
        """
        if self.run is None or not self.run.directory.exists():
            return {"cells": [], "metrics": []}
        cells = [
            c for c in load_results(self.run.directory) if c.suite_digest == self.run.spec.digest()
        ]
        names: set[str] = set()
        rows = []
        for cell in cells:
            if not cell.ok:
                continue
            numeric = {k: v for k, v in cell.metrics.items() if isinstance(v, (int, float))}
            names.update(numeric)
            rows.append(
                {
                    "cell_id": cell.cell_id,
                    "label": cell.label,
                    "architecture": cell.capabilities.get("architecture", ""),
                    "drive": cell.drive,
                    "axes": dict(cell.axes),
                    "metrics": numeric,
                }
            )
        return {"cells": rows, "metrics": sorted(names)}

    def raw(self, limit: int = 400) -> dict[str, Any]:
        """The frames, unstyled and unaggregated. One click away, never default."""
        self.frames.drain()
        rows = list(self.frames)[-limit:]
        return {"frames": rows, "held": len(self.frames), "missing": self.frames.missing}


# --------------------------------------------------------------------------
# the spec a form produces


def spec_from(form: dict[str, Any]) -> tuple[SweepSpec, dict[str, Any]]:
    """A suite, and what the selection left out. Both, always, in one call."""
    models = tuple(m for m in str(form.get("models", "ema")).split(",") if m.strip())
    axes = tuple(a for a in str(form.get("axes", "")).split(",") if a.strip())
    families = tuple(f for f in str(form.get("families", "")).split(",") if f.strip())
    chose = selection(families=families, axes=axes)
    spec = SweepSpec(
        name=str(form.get("name", "cockpit")),
        models=tuple(m.strip() for m in models),
        tier=str(form.get("tier", "smoke")),
        cycles=int(form.get("cycles", 60)),
        decision_s=float(form.get("decision_s", 30.0)),
        scopes=int(form.get("scopes", 4)),
        repeats=int(form.get("repeats", 1)),
        only=tuple(chose["axes"]),
        drive=str(form.get("drive", "auto")),
        think=str(form.get("think", "free")),
        think_s=float(form.get("think_s", 0.0)),
        include_baselines=bool(form.get("baselines", True)),
    )
    return spec, chose


# --------------------------------------------------------------------------
# the page

PAGE = """<!doctype html><meta charset="utf-8"><title>__TITLE__</title>
<style>
:root{--ink:#101418;--dim:#5d6b7a;--line:#d8dee6;--bg:#f6f7f9;--card:#fff;
      --accent:#1f5f8b;--warn:#9a5b00;--bad:#8b2222;--ok:#1d6b45}
@media (prefers-color-scheme:dark){:root{--ink:#e8ecf1;--dim:#93a1b0;--line:#28313b;
      --bg:#0f1317;--card:#151b21;--accent:#6db3e2;--warn:#d8a33f;--bad:#e08585;--ok:#6dc79b}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
     font:14px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}
header{padding:14px 20px;border-bottom:1px solid var(--line);display:flex;
       gap:16px;align-items:baseline;flex-wrap:wrap}
h1{font-size:15px;margin:0;letter-spacing:.02em;font-weight:650}
.sub{color:var(--dim);font-size:12px}
main{padding:16px 20px;display:grid;gap:16px;grid-template-columns:320px 1fr}
@media (max-width:900px){main{grid-template-columns:1fr}}
.card{background:var(--card);border:1px solid var(--line);border-radius:6px;padding:14px}
.card h2{font-size:12px;text-transform:uppercase;letter-spacing:.08em;color:var(--dim);
         margin:0 0 10px;font-weight:600}
label{display:block;font-size:12px;color:var(--dim);margin:8px 0 2px}
input,select{width:100%;padding:6px 8px;border:1px solid var(--line);border-radius:4px;
             background:var(--bg);color:var(--ink);font:inherit;font-size:13px}
button{padding:7px 14px;border:1px solid var(--accent);background:var(--accent);color:#fff;
       border-radius:4px;font:inherit;font-size:13px;cursor:pointer}
button.ghost{background:transparent;color:var(--accent)}
button:disabled{opacity:.45;cursor:default}
table{border-collapse:collapse;width:100%;font-size:12.5px}
th,td{text-align:left;padding:4px 8px;border-bottom:1px solid var(--line);
      font-variant-numeric:tabular-nums}
th{color:var(--dim);font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.05em}
.scroll{overflow:auto;max-height:320px}
.omit{color:var(--warn);font-size:12px;margin-top:8px}
.gap{color:var(--bad)}
.pill{display:inline-block;padding:1px 7px;border-radius:9px;border:1px solid var(--line);
      font-size:11px;color:var(--dim)}
code,pre{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}
pre{margin:0;white-space:pre-wrap;word-break:break-word}
.lens{width:100%;height:210px}
.hint{color:var(--dim);font-size:11.5px;margin-top:6px}
.rowset{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}
</style>
<header>
  <h1>scionarena — cockpit</h1>
  <span class="sub" id="head">idle</span>
  <span class="sub" id="drops"></span>
</header>
<main>
  <section>
    <div class="card">
      <h2>Configure</h2>
      <label>models (comma separated)</label><input id="models" value="ema,gbdt,llm">
      <label>tier</label>
      <select id="tier"><option>smoke</option><option>dev</option><option>realistic</option></select>
      <label>rounds per cell</label><input id="cycles" value="60">
      <label>repeats</label><input id="repeats" value="1">
      <label>axes (blank = all)</label><input id="axes" value="scenario">
      <label>families (blank = all)</label><input id="families" value="">
      <label>thinking</label>
      <select id="think"><option>free</option><option>fixed</option><option>measured</option></select>
      <div class="rowset">
        <button id="go">Run</button>
        <button class="ghost" id="rawbtn">Raw log</button>
      </div>
      <div class="hint" id="plan">—</div>
      <div class="omit" id="omit"></div>
    </div>
    <div class="card" style="margin-top:16px">
      <h2>Registry</h2>
      <div class="scroll"><table id="cat"></table></div>
      <div class="hint">Read from the metric registry at load. Nothing here is a
        list in the interface.</div>
    </div>
  </section>
  <section>
    <div class="card">
      <h2>The lens — shown, did, world did back</h2>
      <canvas class="lens" id="lens"></canvas>
      <div class="hint" id="lenskey">No frames yet. A gap in the sequence is drawn
        as a break, never bridged.</div>
    </div>
    <div class="card" style="margin-top:16px">
      <h2>Results</h2>
      <div class="scroll"><table id="res"></table></div>
    </div>
    <div class="card" style="margin-top:16px;display:none" id="rawcard">
      <h2>Raw frames</h2>
      <div class="scroll"><pre id="raw"></pre></div>
    </div>
  </section>
</main>
<script>
const $ = (id) => document.getElementById(id);

// Every string below that came from a loaded model goes through this. Metric
// names, cell labels and architecture tags are all supplied by whoever wrote
// the model under test, and the first version of this page dropped them into
// innerHTML unescaped -- so a model whose Capabilities.name carried markup ran
// it in the operator's browser. The page is served to localhost and the
// operator chose the model, so the blast radius is small; the fix is one
// function, so the size of the radius is not the argument.
const esc = (v) => String(v === undefined || v === null ? "" : v)
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
  .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
let cursor = 0, frames = [], cat = null;

async function get(url){ const r = await fetch(url); return r.json(); }

function form(){
  return {models:$("models").value, tier:$("tier").value, cycles:$("cycles").value,
          repeats:$("repeats").value, axes:$("axes").value, families:$("families").value,
          think:$("think").value};
}

async function refreshPlan(){
  const q = new URLSearchParams(form()).toString();
  const d = await get("/api/plan?"+q);
  if(d.error){ $("plan").textContent = d.error; $("omit").textContent=""; return; }
  $("plan").textContent = d.cells + " cells, about " + d.estimate + " on this machine";
  const o = d.omitted;
  const bits = [];
  if(o.families.length) bits.push(o.families.length+" families not scored ("+o.families.join(", ")+")");
  if(o.axes.length) bits.push(o.axes.length+" axes held at baseline ("+o.axes.join(", ")+")");
  $("omit").textContent = bits.length ? "This run will not cover: " + bits.join("; ") : "";
}

function renderCatalogue(c){
  const rows = ["<tr><th>metric</th><th>family</th><th>dir</th><th>shape</th></tr>"];
  for(const m of c.metrics){
    rows.push(`<tr><td title="${esc(m.doc)}">${esc(m.name)}${m.support?' <span class="pill">count</span>':''}</td>`+
              `<td>${esc(m.family)}</td><td>${esc(m.direction)}</td><td>${esc(m.shape)}</td></tr>`);
  }
  $("cat").innerHTML = rows.join("");
}

function renderResults(d){
  if(!d.cells.length){ $("res").innerHTML = "<tr><td>nothing scored yet</td></tr>"; return; }
  const keys = ["regret_ratio","swing","coverage.h60","decision_p95_s","recovered","recovered_to"]
      .filter(k => d.metrics.includes(k));
  const head = ["<tr><th>model</th><th>arch</th><th>where</th>"+keys.map(k=>`<th>${esc(k)}</th>`).join("")+"</tr>"];
  for(const c of d.cells.slice(-60)){
    const moved = Object.entries(c.axes).filter(([k,v]) => v !== baseline[k]).map(([k,v])=>k+"="+v);
    head.push("<tr><td>"+esc(c.label)+"</td><td>"+esc(c.architecture||"—")+"</td><td>"+
      esc(moved.join(", ")||"baseline")+"</td>"+
      keys.map(k => `<td>${c.metrics[k]===undefined?"—":(+c.metrics[k]).toFixed(3)}</td>`).join("")+"</tr>");
  }
  $("res").innerHTML = head.join("");
}

function drawLens(){
  const cv = $("lens"), dpr = window.devicePixelRatio || 1;
  cv.width = cv.clientWidth*dpr; cv.height = cv.clientHeight*dpr;
  const g = cv.getContext("2d"); g.scale(dpr,dpr);
  const W = cv.clientWidth, H = cv.clientHeight;
  const css = getComputedStyle(document.documentElement);
  const ink = css.getPropertyValue("--ink").trim(), dim = css.getPropertyValue("--dim").trim();
  const acc = css.getPropertyValue("--accent").trim(), bad = css.getPropertyValue("--bad").trim();
  const ok = css.getPropertyValue("--ok").trim();
  g.clearRect(0,0,W,H);
  const show = frames.slice(-160);
  if(!show.length){ return; }
  const lanes = [
    {key:"records", colour:dim,  label:"shown (records/round)"},
    {key:"max_weight", colour:acc, label:"did (advisory concentration)"},
    {key:"mean_cost_ms", colour:ok, label:"world did back (mean cost ms)"}
  ];
  const laneH = H/lanes.length;
  lanes.forEach((lane, li) => {
    const vals = show.map(f => +f[lane.key] || 0);
    const hi = Math.max(...vals, 1e-9), lo = Math.min(...vals, 0);
    const y0 = li*laneH + laneH - 12, span = laneH - 26;
    g.strokeStyle = lane.colour; g.lineWidth = 1.4; g.beginPath();
    let pen = false;
    show.forEach((f, i) => {
      const x = (i/(show.length-1||1))*(W-8)+4;
      const y = y0 - ((vals[i]-lo)/((hi-lo)||1))*span;
      // A frame the sequence numbers prove was produced and never arrived breaks
      // the line. Bridging it would draw a smooth curve through a hole.
      if(f.gap_before){ pen = false; g.stroke(); g.beginPath();
        g.save(); g.strokeStyle = bad; g.setLineDash([2,3]);
        g.beginPath(); g.moveTo(x, li*laneH+6); g.lineTo(x, li*laneH+laneH-6); g.stroke();
        g.restore(); g.beginPath(); }
      if(!pen){ g.moveTo(x,y); pen = true; } else { g.lineTo(x,y); }
    });
    g.stroke();
    g.fillStyle = dim; g.font = "11px ui-sans-serif,system-ui,sans-serif";
    g.fillText(lane.label + "  max " + hi.toFixed(2), 6, li*laneH + 12);
  });
  g.strokeStyle = dim; g.globalAlpha = .25;
  for(let i=1;i<lanes.length;i++){ g.beginPath(); g.moveTo(0,i*laneH); g.lineTo(W,i*laneH); g.stroke(); }
  g.globalAlpha = 1;
}

let baseline = {};
async function tick(){
  const s = await get("/api/status");
  const r = s.run;
  $("head").textContent = r.state === "idle" ? "idle" :
      `${r.suite} · ${r.tier} · ${r.done}/${r.total} cells · ${r.elapsed_s}s · ${r.state}`;
  $("drops").textContent = s.frames_missing
      ? `${s.frames_missing} frames dropped (the run did not wait)` : "";
  if(s.frames_missing) $("drops").className = "sub gap";
  const d = await get("/api/frames?after="+cursor);
  cursor = d.next; if(d.frames.length){ frames = frames.concat(d.frames); drawLens(); }
  if(r.state !== "idle") renderResults(await get("/api/results"));
  if($("rawcard").style.display !== "none"){
    const raw = await get("/api/raw");
    $("raw").textContent = raw.frames.slice(-60).map(f => JSON.stringify(f)).join("\\n");
  }
}

$("go").onclick = async () => {
  $("go").disabled = true;
  const r = await fetch("/api/run", {method:"POST", body: JSON.stringify(form())});
  const d = await r.json();
  if(d.error) alert(d.error);
  frames = []; cursor = 0;
  setTimeout(()=>{ $("go").disabled = false; }, 1500);
};
$("rawbtn").onclick = () => {
  const c = $("rawcard");
  c.style.display = c.style.display === "none" ? "block" : "none";
};
["models","tier","cycles","repeats","axes","families","think"].forEach(
  id => $(id).addEventListener("input", refreshPlan));

(async () => {
  cat = await get("/api/catalogue"); renderCatalogue(cat);
  baseline = (await get("/api/baseline")).baseline;
  await refreshPlan(); await tick(); setInterval(tick, 1200);
  window.addEventListener("resize", drawLens);
})();
</script>
"""


def _page() -> str:
    return PAGE.replace("__TITLE__", TITLE)


# --------------------------------------------------------------------------
# the server


def plan_for(form: dict[str, Any]) -> dict[str, Any]:
    """What this form would cost, answered before anybody commits to it.

    Outside the request handler because the estimate is the interesting part and
    a test should not have to stand up a socket to check it -- ``cycles`` was
    missing from the sum for a whole phase, and the handler is where that hid.
    """
    try:
        spec, chose = spec_from(form)
    except Exception as exc:  # noqa: BLE001 -- a bad form is a message, not a 500
        return {"error": f"{type(exc).__name__}: {exc}"}
    cells = len(plan(spec))
    seconds = estimate_s(cells, spec.tier, cycles=spec.cycles)
    return {
        "cells": cells,
        "estimate_s": round(seconds, 1),
        "estimate": _human(seconds),
        "omitted": chose["omitted"],
        "digest": spec.digest(),
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "scionarena-cockpit"

    def __init__(self, cockpit: Cockpit, *args: Any, **kwargs: Any) -> None:
        self.cockpit = cockpit
        super().__init__(*args, **kwargs)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def do_GET(self) -> None:  # noqa: N802 -- the base class names it
        route = urlparse(self.path)
        query = parse_qs(route.query)
        if route.path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", _page().encode())
        elif route.path == "/api/catalogue":
            self._json(200, catalogue())
        elif route.path == "/api/baseline":
            self._json(200, {"baseline": baseline_cell(), "axes": sorted(AXES)})
        elif route.path == "/api/status":
            self._json(200, self.cockpit.status())
        elif route.path == "/api/frames":
            after = int((query.get("after") or ["0"])[0])
            self._json(200, self.cockpit.since(after))
        elif route.path == "/api/results":
            self._json(200, self.cockpit.results())
        elif route.path == "/api/raw":
            self._json(200, self.cockpit.raw())
        elif route.path == "/api/plan":
            self._json(200, plan_for({k: v[0] for k, v in query.items()}))
        else:
            self._send(404, "text/plain", b"no")

    def do_POST(self) -> None:  # noqa: N802
        route = urlparse(self.path)
        if route.path != "/api/run":
            self._send(404, "text/plain", b"no")
            return
        length = int(self.headers.get("content-length", 0))
        try:
            form = json.loads(self.rfile.read(length) or b"{}")
            spec, chose = spec_from(form)
            run = self.cockpit.start(spec)
        except Exception as exc:  # noqa: BLE001 -- a bad form is a message, not a 500
            self._json(400, {"error": f"{type(exc).__name__}: {exc}"})
            return
        self._json(200, {"run": run.status(), "omitted": chose["omitted"]})

    def _json(self, code: int, payload: dict[str, Any]) -> None:
        self._send(code, "application/json", json.dumps(payload).encode())

    def _send(self, code: int, kind: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("content-type", kind)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _human(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5_400:
        return f"{seconds / 60:.0f} min"
    return f"{seconds / 3600:.1f} h"


def serve(
    host: str = "127.0.0.1", port: int = DEFAULT_PORT, directory: str = "runs"
) -> ThreadingHTTPServer:
    cockpit = Cockpit(Path(directory))
    return ThreadingHTTPServer((host, port), partial(Handler, cockpit))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scionarena cockpit", description="watch a sweep while it happens"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--out", default="runs", help="where result files are written")
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args(argv)

    server = serve(args.host, args.port, args.out)
    url = f"http://{args.host}:{args.port}/"
    print(f"cockpit on {url}", file=sys.stderr)
    if not args.no_open:
        threading.Thread(target=lambda: webbrowser.open(url), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
    return 0
