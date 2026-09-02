"""M3: the browser front-end.

The UI is a thin thing over ``demo``, so these tests cover what is actually its
own: that a run happens off the request thread, that progress is visible while
it is happening, that a run can be stopped between rounds, and that a bad
request is a 400 rather than a dead server. The numbers themselves are
``test_loop.py``'s business.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator

import pytest

from scionarena.ui import Jobs, _page, _params, serve

TINY = {"tier": "smoke", "scopes": 2, "cycles": 12, "hosts": 40, "seed": 7, "slow": 0}


@pytest.fixture(scope="module")
def base() -> Iterator[str]:
    server = serve("127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def _open(url: str, data: bytes | None = None) -> str:
    """One request, retried once on a dropped socket.

    Not papering over a server bug: an HTTP/1.0 handler closes the connection
    after every response, and on Windows a machine busy enough to be running
    the rest of this suite will occasionally abort one of them on the client
    side. An HTTPError is a real answer and is re-raised immediately.
    """
    for attempt in (0, 1):
        try:
            with urllib.request.urlopen(url, data=data, timeout=30) as response:
                return str(response.read().decode())
        except urllib.error.HTTPError:
            raise
        except OSError:
            if attempt:
                raise
            time.sleep(0.2)
    raise AssertionError("unreachable")


def post(base: str, path: str, payload: dict | None = None) -> dict:
    return dict(json.loads(_open(f"{base}{path}", json.dumps(payload or {}).encode())))


def get(base: str, path: str) -> str:
    return _open(f"{base}{path}")


def wait(base: str, job_id: str, *, timeout: float = 120.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = json.loads(get(base, f"/api/status?id={job_id}"))
        if status["state"] != "running":
            return status
        time.sleep(0.1)
    raise AssertionError("the run never finished")


# --------------------------------------------------------------------------
# the page


def test_the_page_renders_with_no_placeholders_left(base: str) -> None:
    page = get(base, "/")
    assert "<form" in page and "scionarena" in page
    assert "{css}" not in page and "{tiers}" not in page


def test_the_page_is_the_same_string_the_server_serves(base: str) -> None:
    assert get(base, "/") == _page()


# --------------------------------------------------------------------------
# running


def test_a_run_returns_before_it_has_finished(base: str) -> None:
    """Invariant 3's cousin for the UI: the request thread is not the world."""
    started = post(base, "/api/run", TINY)
    assert started["state"] == "running"
    assert wait(base, started["id"])["state"] == "done"


def test_a_finished_run_renders_charts_and_a_verdict(base: str) -> None:
    job = post(base, "/api/run", TINY)
    assert wait(base, job["id"])["state"] == "done"
    body = get(base, f"/api/result?id={job['id']}")
    assert "<svg" in body and "<polyline" in body
    assert "Acceptance criteria" in body


def test_the_download_is_a_standalone_page(base: str) -> None:
    job = post(base, "/api/run", TINY)
    wait(base, job["id"])
    page = get(base, f"/api/download?id={job['id']}")
    assert page.startswith("<!doctype html>") and "</html>" in page
    assert "<style>" in page, "a page that needs the server to look right is not standalone"


def test_progress_is_visible_while_the_run_is_still_going(base: str) -> None:
    """A run of the realistic tier is an hour; a bar that only moves at the end
    is the same as no bar."""
    job = post(base, "/api/run", {**TINY, "cycles": 400, "scopes": 4})
    seen = []
    for _ in range(200):
        status = json.loads(get(base, f"/api/status?id={job['id']}"))
        seen.append(status["done"])
        if status["state"] != "running" or (seen and max(seen) > 0):
            break
        time.sleep(0.05)
    post(base, f"/api/stop?id={job['id']}")
    assert max(seen) > 0, "no round was ever reported"
    assert wait(base, job["id"])["total"] == 400


def test_a_run_can_be_stopped(base: str) -> None:
    job = post(base, "/api/run", {**TINY, "cycles": 4000, "scopes": 4})
    time.sleep(0.3)
    post(base, f"/api/stop?id={job['id']}")
    assert wait(base, job["id"], timeout=30.0)["state"] == "cancelled"


# --------------------------------------------------------------------------
# bad input


def test_an_unknown_job_is_a_404_not_a_traceback(base: str) -> None:
    with pytest.raises(urllib.error.HTTPError) as raised:
        get(base, "/api/status?id=nope")
    assert raised.value.code == 404


def test_an_unknown_route_is_a_404(base: str) -> None:
    with pytest.raises(urllib.error.HTTPError) as raised:
        get(base, "/api/whatever")
    assert raised.value.code == 404


def test_a_bad_tier_is_refused_with_a_reason(base: str) -> None:
    with pytest.raises(urllib.error.HTTPError) as raised:
        post(base, "/api/run", {**TINY, "tier": "enormous"})
    assert raised.value.code == 400
    assert "enormous" in raised.value.read().decode()


@pytest.mark.parametrize(
    "raw,field,expected",
    [
        ({"scopes": 0}, "scopes", 1),
        ({"scopes": 10**9}, "scopes", 2000),
        ({"cycles": 1}, "cycles", 8),
        ({"hosts": -5}, "hosts", 1),
    ],
)
def test_impossible_sizes_are_clamped_not_obeyed(raw: dict, field: str, expected: int) -> None:
    """The form is not the only thing that can post to this."""
    assert _params(raw)[field] == expected


def test_models_can_be_given_as_a_string() -> None:
    assert _params({"models": "minrtt, reference"})["models"] == ["minrtt", "reference"]


# --------------------------------------------------------------------------
# the registry


def test_a_failing_run_is_recorded_rather_than_killing_the_worker() -> None:
    """Also pins the order the worker publishes in.

    ``state`` is what this loop -- and the browser's poller, and /api/status --
    waits on, so it has to be assigned *after* the traceback. Setting it first
    published a job that said it had failed and could not yet say why, and a
    reader landing between the two lines got an empty ``error``. That is a real
    HTTP-visible window, not just a flaky test; it showed up here as one failure
    in a full-suite run that passed five times in isolation.
    """
    jobs = Jobs()
    job = jobs.start({**_params(TINY), "models": ["no-such-model"]})
    for _ in range(600):
        if job.state != "running":
            break
        time.sleep(0.05)
    assert job.state == "error"
    assert "no-such-model" in job.error
