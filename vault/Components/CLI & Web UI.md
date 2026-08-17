# CLI & Web UI (`scionarena.cli`, `ui.py`, `demo.py`)

> [cli.py](file:///C:/Users/Von/Desktop/PAS_Research_desktop/PAS_Research_desktop/scionfit/src/scionarena/cli.py), [ui.py](file:///C:/Users/Von/Desktop/PAS_Research_desktop/PAS_Research_desktop/scionfit/src/scionarena/ui.py), [demo.py](file:///C:/Users/Von/Desktop/PAS_Research_desktop/PAS_Research_desktop/scionfit/src/scionarena/demo.py)

---

## 📌 Entry Points

### 1. `scionarena` / `scionfit` Command Line Interface
```bash
scionarena conformance list
scionarena conformance check reference
scionarena conformance check ema
scionarena conformance compare
scionarena demo --tier smoke --scopes 8 --cycles 240 --slow 5
scionarena ui --port 8765
```

### 2. Zero-Dependency Web UI ([`ui.py`](file:///C:/Users/Von/Desktop/PAS_Research_desktop/PAS_Research_desktop/scionfit/src/scionarena/ui.py))
* Uses Python's built-in `http.server.ThreadingHTTPServer`.
* Endpoints:
  * `GET /`: Serves complete HTML/CSS/JS interface.
  * `POST /api/run`: Launches background simulation worker thread.
  * `GET /api/status?id=<id>`: Streams progress (rounds completed, current phase, elapsed time).
  * `POST /api/stop?id=<id>`: Cancels running jobs cleanly between rounds.
  * `GET /api/result?id=<id>`: Delivers rendered SVG dashboard and verdict tables.
