"""``scionarena ui`` -- the demo with knobs, in a browser.

    scionarena ui                 # http://127.0.0.1:8765, opens a tab

The same runs as ``scionarena demo`` and the same rendering, driven from a form
instead of from argv, so a scenario can be changed and re-run in front of an
audience without anyone watching a terminal. A run of the realistic tier takes
the better part of an hour, which is why runs happen on a worker thread, report
progress per decision round, and can be stopped.

Standard library only, no framework and no build step: the page is served as one
string and talks to four JSON endpoints. That is a deliberate constraint rather
than an aesthetic -- ``core`` is numpy-only and a demonstration machine is
usually one with nothing installed on it.
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
from typing import Any
from urllib.parse import parse_qs, urlparse

from scionarena.demo import run_demo, sections, verdicts
from scionarena.exposure.loading import ModelLoadError, capability_report, load_model
from scionarena.instrument.report import CSS, render_body, render_html

__all__ = ["main", "serve", "Jobs"]

TITLE = "scionarena — the closed loop"

#: Roughly how long each tier takes for two models at the default cadence, on
#: one core. Shown in the form because a judge who clicks "realistic" without
#: being told deserves to know before, not after.
TIER_HINT = {
    "smoke": "seconds",
    "dev": "minutes",
    "realistic": "tens of minutes",
    "stress": "hours -- not gated yet",
}


class Cancelled(Exception):
    """Raised inside the progress callback to stop a run between rounds."""


@dataclass
class Job:
    """One demo run, on its own thread."""

    id: str
    params: dict[str, Any]
    state: str = "running"  # running | done | error | cancelled
    label: str = "starting"
    done: int = 0
    total: int = 0
    step: int = 0  # which run of how many
    steps: int = 1
    started: float = field(default_factory=time.perf_counter)
    finished: float | None = None
    body: str = ""
    page: str = ""
    error: str = ""
    stop: bool = False

    def status(self) -> dict[str, Any]:
        end = self.finished if self.finished is not None else time.perf_counter()
        return {
            "id": self.id,
            "state": self.state,
            "label": self.label,
            "done": self.done,
            "total": self.total,
            "step": self.step,
            "steps": self.steps,
            "elapsed_s": round(end - self.started, 1),
            "error": self.error,
        }


class Jobs:
    """Every run this server has been asked for. Memory only; restart clears it."""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def start(self, params: dict[str, Any]) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], params=params)
        job.steps = len(params["models"]) + (1 if params["slow_s"] > 0 else 0)
        with self._lock:
            self._jobs[job.id] = job
        thread = threading.Thread(target=self._run, args=(job,), daemon=True)
        thread.start()
        return job

    def _run(self, job: Job) -> None:
        seen: list[str] = []

        def progress(label: str, done: int, total: int) -> None:
            if job.stop:
                raise Cancelled
            if label not in seen:
                seen.append(label)
            job.label, job.done, job.total, job.step = label, done, total, len(seen)

        try:
            results = run_demo(progress=progress, **job.params)
        except Cancelled:
            job.state, job.label = "cancelled", "stopped"
        except Exception:  # a broken scenario is a result, not a dead server
            # The traceback goes on before the state does. ``state`` is what a
            # poller waits on, so assigning it first publishes a job that says
            # it failed and cannot yet say why -- a reader that looks in the
            # window between the two lines gets an empty ``error`` over HTTP.
            job.error = traceback.format_exc(limit=6)
            job.state, job.label = "error", "failed"
        else:
            rows = [r.report() for r in results]
            checks = verdicts(results)
            meta = dict(job.params) | {
                "wall_clock_s": round(time.perf_counter() - job.started, 1),
                "digests": {r.model: r.session_summary.get("digest", "") for r in results},
            }
            job.body = render_body(sections(results), rows, verdicts=checks, meta=meta)
            job.page = render_html(
                TITLE,
                _subtitle(job.params),
                sections(results),
                rows,
                verdicts=checks,
                meta=meta,
            )
            job.state, job.label = "done", "finished"
        finally:
            job.finished = time.perf_counter()


def _subtitle(params: dict[str, Any]) -> str:
    return (
        f"{params['tier']} tier, {params['scopes']} concurrent scopes, "
        f"{params['cycles']} decision rounds, seed {params['seed']}. "
        "Same world, same scopes, same seed; only the model differs."
    )


# --------------------------------------------------------------------------
# the page


_PAGE = """<!doctype html><html><head><meta charset='utf-8'>
<meta name='color-scheme' content='only light'>
<meta name='darkreader-lock'>
<title>scionarena</title><style>{css}
form.run {{ background:#f7f9fc; border:1px solid #e3e8ee; border-radius:8px;
  padding:14px 16px; display:flex; flex-wrap:wrap; gap:14px; align-items:flex-end; }}
form.run label {{ display:block; font-size:12px; color:#5b6b7c; margin-bottom:3px; }}
form.run input, form.run select {{ font:14px inherit; padding:5px 7px; border:1px solid #cfd8e3;
  border-radius:5px; background:#fff; width:104px; }}
button {{ font:14px inherit; padding:7px 16px; border:0; border-radius:5px; cursor:pointer;
  background:#0f4c81; color:#fff; }}
button.ghost {{ background:#fff; color:#35485c; border:1px solid #cfd8e3; }}
button:disabled {{ opacity:.45; cursor:default; }}
.presets {{ margin:10px 0 0; display:flex; gap:8px; flex-wrap:wrap; }}
.presets button {{ background:#eef3f9; color:#35485c; font-size:13px; padding:5px 11px; }}
#bar {{ height:6px; background:#e3e8ee; border-radius:3px; overflow:hidden; margin:14px 0 4px; }}
#bar div {{ height:100%; width:0; background:#0f4c81; transition:width .2s; }}
#status {{ font-size:13px; color:#5b6b7c; }}
#caps {{ margin:12px 0 0; display:flex; flex-wrap:wrap; gap:10px; }}
#caps .m {{ border:1px solid #e3e8ee; border-radius:6px; padding:9px 12px; font-size:13px;
  background:#fff; min-width:250px; }}
#caps .m b {{ display:block; font-size:14px; }}
#caps .m code {{ font-size:11px; color:#5b6b7c; }}
#caps .m .yes {{ color:#0a7040; }} #caps .m .no {{ color:#8a6d3b; }}
#caps .bad {{ border:1px solid #f0c8c4; background:#fdf3f2; color:#b42318;
  border-radius:6px; padding:9px 12px; font:12px ui-monospace,Consolas,monospace;
  white-space:pre-wrap; }}
#err {{ white-space:pre-wrap; font:12px ui-monospace,Consolas,monospace; color:#b42318; }}
</style></head><body>
<h1>scionarena</h1>
<p class='sub'>Two reference models, one network. Nothing differs but the model, so
anything you see is the model's doing. <a href='#' id='what'>what am I looking at?</a></p>
<div class='note' id='explain' style='display:none'>
Ranked advice makes every advised host move the same way at the same time; on a network
where paths share bottlenecks that is a stampede, and it costs the model the very thing it
is optimising. Advice <em>sampled from a distribution</em> does not, because hosts disagree
by construction. Red is the greedy model, blue the stochastic one.
</div>

<form class='run' id='form'>
  <div style='flex:1 1 340px'><label>models &mdash; a built-in name, my.module:MyModel,
       or ./my_model.py:MyModel</label>
       <input name='models' value='minrtt,reference' style='width:100%'></div>
  <div><label>tier</label><select name='tier'>{tiers}</select></div>
  <div><label>concurrent scopes</label><input name='scopes' type='number' min='1' value='8'></div>
  <div><label>decision rounds</label><input name='cycles' type='number' min='8' value='240'></div>
  <div><label>hosts per scope</label><input name='hosts' type='number' min='1' value='200'></div>
  <div><label>seed</label><input name='seed' type='number' value='7'></div>
  <div><label>slow model (extra s)</label><input name='slow' type='number' min='0' step='1'
       value='0'></div>
  <div><button id='go'>Run</button></div>
  <div><button id='stop' class='ghost' type='button' disabled>Stop</button></div>
  <div><a id='save' href='#' style='display:none'>download report.html</a></div>
</form>
<div class='presets'>
  <button data-p='8,240,0,smoke'>smoke, 8 scopes</button>
  <button data-p='40,240,8,dev'>dev, 40 scopes, +8 s model</button>
  <button data-p='100,120,0,realistic'>realistic, 100 scopes</button>
</div>
<div id='caps'></div>
<div id='bar'><div></div></div>
<div id='status'>idle</div>
<div id='err'></div>
<div id='out'></div>
<script>
const $ = s => document.querySelector(s);
const hints = {hints};
let job = null, timer = null;

$('#what').onclick = e => {{ e.preventDefault();
  const b = $('#explain'); b.style.display = b.style.display === 'none' ? 'block' : 'none'; }};

document.querySelectorAll('.presets button').forEach(b => b.onclick = () => {{
  const [s, c, slow, tier] = b.dataset.p.split(',');
  $('[name=scopes]').value = s; $('[name=cycles]').value = c;
  $('[name=slow]').value = slow; $('[name=tier]').value = tier;
}});

$('#form').onsubmit = async e => {{
  e.preventDefault();
  const f = new FormData($('#form'));
  const body = {{}};
  for (const [k, v] of f.entries()) body[k] = v;
  $('#out').innerHTML = ''; $('#err').textContent = ''; $('#save').style.display = 'none';
  const r = await fetch('/api/run', {{method: 'POST', body: JSON.stringify(body)}});
  const j = await r.json();
  if (j.error) {{ $('#err').textContent = j.error; return; }}
  job = j.id; $('#go').disabled = true; $('#stop').disabled = false;
  timer = setInterval(poll, 500); poll();
}};

$('#stop').onclick = () => job && fetch('/api/stop?id=' + job, {{method: 'POST'}});

async function poll() {{
  if (!job) return;
  const s = await (await fetch('/api/status?id=' + job)).json();
  const pct = s.total ? Math.round(100 * s.done / s.total) : 0;
  $('#bar div').style.width = (s.state === 'running' ? pct : 100) + '%';
  $('#status').textContent = s.state === 'running'
    ? `run ${{s.step}}/${{s.steps}} (${{s.label}}) — round ${{s.done}}/${{s.total}} — ${{s.elapsed_s}} s elapsed`
    : `${{s.state}} in ${{s.elapsed_s}} s`;
  if (s.state === 'running') return;
  clearInterval(timer); $('#go').disabled = false; $('#stop').disabled = true;
  if (s.error) $('#err').textContent = s.error;
  if (s.state === 'done') {{
    $('#out').innerHTML = await (await fetch('/api/result?id=' + job)).text();
    $('#save').href = '/api/download?id=' + job; $('#save').style.display = 'inline';
  }}
}}

$('[name=tier]').onchange = e => {{ $('#status').textContent = 'expect ' + hints[e.target.value]; }};

const esc = t => String(t).replace(/[&<>]/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;'}}[c]));

function capCard(r) {{
  const yes = r.tested.length ? r.tested.join(' ') : 'none';
  const no = r.declared_absent.length ? r.declared_absent.join(' ') : 'none';
  return `<div class='m'><b>${{esc(r.name)}} v${{esc(r.version)}}</b>
    <code>${{esc(r.resolved)}}</code>
    <div class='yes'>tested: ${{esc(yes)}}</div>
    <div class='no'>declared absent: ${{esc(no)}}</div></div>`;
}}

// What the model says about itself, before anything runs. A declaration is the
// part the author controls, so it is worth seeing while it can still be changed.
async function showCaps() {{
  const specs = $('[name=models]').value.split(',').map(s => s.trim()).filter(Boolean);
  const cards = await Promise.all(specs.map(async s => {{
    const r = await (await fetch('/api/model?spec=' + encodeURIComponent(s))).json();
    return r.error ? `<div class='bad'>${{esc(r.error)}}</div>` : capCard(r);
  }}));
  $('#caps').innerHTML = cards.join('');
}}
$('[name=models]').onchange = showCaps;
showCaps();
</script></body></html>
"""


def _page() -> str:
    options = "".join(
        f"<option value='{t}'{' selected' if t == 'smoke' else ''}>{t} — {h}</option>"
        for t, h in TIER_HINT.items()
    )
    return _PAGE.format(css=CSS, tiers=options, hints=json.dumps(TIER_HINT))


# --------------------------------------------------------------------------
# the server


def _params(raw: dict[str, Any]) -> dict[str, Any]:
    """Whatever the form sent, turned into arguments ``run_demo`` will accept.

    Bounds are here rather than in the form because the form is not the only
    thing that can post: this is a local tool, but "local" is not a validator.
    """
    tier = str(raw.get("tier", "smoke"))
    if tier not in TIER_HINT:
        raise ValueError(f"unknown tier {tier!r}")
    models = raw.get("models") or ["minrtt", "reference"]
    if isinstance(models, str):
        models = [m.strip() for m in models.split(",") if m.strip()]
    if not 1 <= len(models) <= 6:
        raise ValueError(f"give between 1 and 6 models, not {len(models)}")
    for spec in models:  # fail here, with a sentence, rather than on the worker
        load_model(spec)
    return {
        "tier": tier,
        "scopes": max(1, min(int(raw.get("scopes", 8)), 2000)),
        "cycles": max(8, min(int(raw.get("cycles", 240)), 5000)),
        "hosts": max(1, min(int(raw.get("hosts", 200)), 100_000)),
        "seed": int(raw.get("seed", 7)),
        "slow_s": max(0.0, float(raw.get("slow", 0) or 0)),
        "models": list(models),
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "scionarena"

    def __init__(self, jobs: Jobs, *args: Any, **kwargs: Any) -> None:
        self.jobs = jobs
        super().__init__(*args, **kwargs)

    # one line per request is noise during a demonstration
    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802 - the base class names it
        route = urlparse(self.path)
        if route.path in ("/", "/index.html"):
            return self._send(200, "text/html; charset=utf-8", _page())
        if route.path == "/api/model":
            spec = (parse_qs(route.query).get("spec") or [""])[0]
            try:
                report = capability_report(load_model(spec), spec)
            except ModelLoadError as exc:
                return self._json(200, {"error": str(exc)})
            return self._json(200, report.to_dict())
        if route.path == "/api/status":
            job = self._job(route.query)
            return None if job is None else self._json(200, job.status())
        if route.path == "/api/result":
            job = self._job(route.query)
            return None if job is None else self._send(200, "text/html; charset=utf-8", job.body)
        if route.path == "/api/download":
            job = self._job(route.query)
            if job is None:
                return None
            return self._send(
                200,
                "text/html; charset=utf-8",
                job.page,
                extra={"Content-Disposition": 'attachment; filename="report.html"'},
            )
        return self._json(404, {"error": "no such thing"})

    def do_POST(self) -> None:  # noqa: N802
        route = urlparse(self.path)
        if route.path == "/api/stop":
            job = self._job(route.query)
            if job is None:
                return None
            job.stop = True
            return self._json(200, job.status())
        if route.path != "/api/run":
            return self._json(404, {"error": "no such thing"})
        length = int(self.headers.get("Content-Length") or 0)
        try:
            raw = json.loads(self.rfile.read(length) or b"{}")
            params = _params(raw)
        except (ValueError, TypeError, ModelLoadError) as exc:
            return self._json(400, {"error": str(exc)})
        return self._json(200, self.jobs.start(params).status())

    def _job(self, query: str) -> Job | None:
        job_id = (parse_qs(query).get("id") or [""])[0]
        job = self.jobs.get(job_id)
        if job is None:
            self._json(404, {"error": f"no job {job_id!r}"})
        return job

    def _json(self, code: int, payload: dict[str, Any]) -> None:
        self._send(code, "application/json", json.dumps(payload))

    def _send(
        self, code: int, content_type: str, body: str, *, extra: dict[str, str] | None = None
    ) -> None:
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)


def serve(host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    """Bind and return the server, not yet serving. ``port=0`` picks a free one."""
    return ThreadingHTTPServer((host, port), partial(Handler, Jobs()))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scionarena ui",
        description="Run the closed-loop demo from a browser.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-open", action="store_true", help="do not open a browser tab")
    args = parser.parse_args(argv)

    server = serve(args.host, args.port)
    url = f"http://{args.host}:{server.server_address[1]}/"
    print(f"scionarena ui on {url}  (ctrl-c to stop)")
    if not args.no_open:
        threading.Timer(0.4, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
