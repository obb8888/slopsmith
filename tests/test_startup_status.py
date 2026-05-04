"""Tests for GET /api/startup-status — shape, field types, and the
_set_startup_status / _get_startup_status state helpers introduced in
the async plugin-loading PR (slopsmith#115).
"""

import importlib
import json
import sys
import time
import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

# ── Fake load_plugins event sequences ────────────────────────────────────────

# Events emitted by a single-plugin successful load (mirrors the real loader).
_FAKE_SUCCESS_EVENTS = [
    {"phase": "plugins-discovered", "message": "Discovered 1 plugin(s)", "plugin_id": "", "loaded": 0, "total": 1},
    {"phase": "plugin-start", "message": "Loading plugin 'demo'", "plugin_id": "demo", "loaded": 0, "total": 1},
    {"phase": "plugin-requirements", "message": "Installing requirements for 'demo' (if needed)", "plugin_id": "demo", "loaded": 0, "total": 1},
    {"phase": "plugin-routes", "message": "Loading routes for 'demo'", "plugin_id": "demo", "loaded": 0, "total": 1},
    {"phase": "plugin-registered", "message": "Registered plugin 'demo'", "plugin_id": "demo", "loaded": 1, "total": 1},
    {"phase": "plugins-complete", "message": "Loaded 1 plugin(s)", "plugin_id": "", "loaded": 1, "total": 1},
]

# Error text and events for a single-plugin requirements failure.
# Mirrors the REAL loader sequence from plugins/__init__.py:522-660:
# - plugin-requirements is always emitted before the req_ok check
# - plugin-error is emitted when req_ok is False
# - execution continues: plugin-routes (if routes declared), plugin-registered, plugins-complete
# The "bad" plugin below has no routes file, so no plugin-routes event.
# Including the post-error events ensures the test catches any regression that
# accidentally clears status.error in those follow-up non-error events.
_FAKE_PLUGIN_ERROR_TEXT = "Requirements installation failed"
_FAKE_PLUGIN_ERROR_EVENTS = [
    {"phase": "plugins-discovered", "message": "Discovered 1 plugin(s)", "plugin_id": "", "loaded": 0, "total": 1},
    {"phase": "plugin-start", "message": "Loading plugin 'bad'", "plugin_id": "bad", "loaded": 0, "total": 1},
    {"phase": "plugin-requirements", "message": "Installing requirements for 'bad' (if needed)", "plugin_id": "bad", "loaded": 0, "total": 1},
    {"phase": "plugin-error", "message": "Failed to install requirements for 'bad'",
     "plugin_id": "bad", "loaded": 0, "total": 1, "error": _FAKE_PLUGIN_ERROR_TEXT},
    # Real loader continues after requirements failure: registers the plugin and completes.
    {"phase": "plugin-registered", "message": "Registered plugin 'bad'", "plugin_id": "bad", "loaded": 1, "total": 1},
    {"phase": "plugins-complete", "message": "Loaded 1 plugin(s)", "plugin_id": "", "loaded": 1, "total": 1},
]


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """TestClient with CONFIG_DIR isolated in a per-test tmp_path.

    SLOPSMITH_SYNC_STARTUP=1 makes the plugin-loader run synchronously
    inside startup_events() so startup is complete before TestClient.__enter__
    returns — no threading races, no polling.  load_plugins is still stubbed
    to a no-op so the "load" takes microseconds and startup_scan is also
    suppressed to avoid unrelated background I/O during tests.
    """
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("SLOPSMITH_SYNC_STARTUP", "1")
    sys.modules.pop("server", None)
    server = importlib.import_module("server")
    # Stub out the two background callables that call _set_startup_status.
    # Patching at the function level (not threading.Thread) leaves TestClient
    # and AnyIO free to create real threads for their own internal use.
    monkeypatch.setattr(server, "load_plugins", lambda *a, **kw: None)
    monkeypatch.setattr(server, "startup_scan", lambda: None)
    with TestClient(server.app) as test_client:
        # With SLOPSMITH_SYNC_STARTUP the loader ran inline during startup, so
        # the status must already be complete.  Poll briefly as a safety net in
        # case something unexpected deferred the update.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if not server._get_startup_status().get("running", True):
                break
            time.sleep(0.01)
        last_status = server._get_startup_status()
        assert not last_status.get("running", True), (
            f"Background startup thread did not complete within 5 s; "
            f"last status: {last_status}"
        )
        try:
            yield test_client, server
        finally:
            conn = getattr(getattr(server, "meta_db", None), "conn", None)
            if conn is not None:
                conn.close()


@pytest.fixture()
def startup_harness(tmp_path, monkeypatch, isolate_logging):
    """Shared setup/teardown harness for startup_events() transition tests.

    Yields (server_module, phases_list):
    - server_module: freshly imported server with startup_scan stubbed and
      _set_startup_status wired to record every phase transition.
    - phases_list: accumulates the `phase` field of every _set_startup_status
      call so tests can assert the exact sequence.

    Teardown stops the demo-janitor thread (if accidentally started) and
    closes the meta_db connection.
    """
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("SLOPSMITH_SYNC_STARTUP", "1")
    monkeypatch.delenv("SLOPSMITH_DEMO_MODE", raising=False)
    sys.modules.pop("server", None)
    server = importlib.import_module("server")
    monkeypatch.setattr(server, "startup_scan", lambda: None)

    phases = []
    original_set = server._set_startup_status

    def recording_set(**updates):
        original_set(**updates)
        phases.append(server._get_startup_status()["phase"])

    monkeypatch.setattr(server, "_set_startup_status", recording_set)

    yield server, phases

    server._DEMO_JANITOR_STOP.set()
    thread = server._DEMO_JANITOR_THREAD
    if thread is not None:
        thread.join(timeout=2)
    server._DEMO_JANITOR_STARTED = False
    server._DEMO_JANITOR_THREAD = None
    conn = getattr(getattr(server, "meta_db", None), "conn", None)
    if conn is not None:
        conn.close()


# ── /api/startup-status endpoint ─────────────────────────────────────────────

def test_startup_status_returns_200(client):
    tc, _ = client
    r = tc.get("/api/startup-status")
    assert r.status_code == 200


def test_startup_status_response_has_expected_keys(client):
    tc, _ = client
    data = tc.get("/api/startup-status").json()
    for key in ("running", "phase", "message", "current_plugin", "loaded", "total", "error"):
        assert key in data, f"Missing key '{key}' in /api/startup-status response"


def test_startup_status_field_types(client):
    tc, _ = client
    data = tc.get("/api/startup-status").json()
    assert isinstance(data["running"], bool)
    assert isinstance(data["phase"], str)
    assert isinstance(data["message"], str)
    assert isinstance(data["current_plugin"], str)
    assert isinstance(data["loaded"], int)
    assert isinstance(data["total"], int)
    # error is either None (JSON null) or a string
    assert data["error"] is None or isinstance(data["error"], str)


# ── _set_startup_status / _get_startup_status helpers ────────────────────────

def test_set_get_startup_status_round_trip(client):
    """_set_startup_status partial-updates the state; _get_startup_status
    returns a snapshot dict."""
    _, server = client
    server._set_startup_status(running=False, phase="complete", message="done",
                               current_plugin="", loaded=3, total=3, error=None)
    status = server._get_startup_status()
    assert status["running"] is False
    assert status["phase"] == "complete"
    assert status["loaded"] == 3
    assert status["total"] == 3
    assert status["error"] is None


def test_set_startup_status_partial_update_does_not_clobber_other_keys(client):
    """A partial _set_startup_status call must not lose previously-set keys."""
    _, server = client
    server._set_startup_status(running=True, phase="plugins-loading", message="loading",
                               current_plugin="myplugin", loaded=1, total=5, error=None)
    # Only update message.
    server._set_startup_status(message="installing requirements")
    status = server._get_startup_status()
    assert status["message"] == "installing requirements"
    assert status["phase"] == "plugins-loading"
    assert status["current_plugin"] == "myplugin"
    assert status["loaded"] == 1
    assert status["total"] == 5


def test_startup_status_endpoint_reflects_set_status(client):
    """The HTTP endpoint must reflect what was last written via _set_startup_status."""
    tc, server = client
    server._set_startup_status(running=False, phase="complete", message="All done",
                               current_plugin="", loaded=7, total=7, error=None)
    data = tc.get("/api/startup-status").json()
    assert data["running"] is False
    assert data["phase"] == "complete"
    assert data["loaded"] == 7
    assert data["total"] == 7


def test_startup_status_exact_success_transition_sequence(monkeypatch, startup_harness):
    """Lock the startup phase sequence for successful plugin startup."""
    server, phases = startup_harness

    def fake_load_plugins(_app, _context, progress_cb=None, route_setup_fn=None):
        for event in _FAKE_SUCCESS_EVENTS:
            progress_cb(event)

    monkeypatch.setattr(server, "load_plugins", fake_load_plugins)
    asyncio.run(server.startup_events())
    final = server._get_startup_status()

    assert phases == [
        "starting",
        "plugins-loading",
        "plugins-discovered",
        "plugin-start",
        "plugin-requirements",
        "plugin-routes",
        "plugin-registered",
        "plugins-complete",
        "complete",
    ]
    assert final["phase"] == "complete"
    assert final["running"] is False
    assert final["loaded"] == 1
    assert final["total"] == 1
    assert final["total"] >= final["loaded"]
    assert final["error"] is None

    # Verify the HTTP endpoint exposes the same terminal state — a regression
    # that breaks the endpoint handler or disconnects it from _get_startup_status()
    # would silently pass if we only read the internal helper.  ASGITransport
    # sends requests directly to the ASGI app without re-running lifespan events.
    async def _check_endpoint():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=server.app), base_url="http://test"
        ) as ac:
            r = await ac.get("/api/startup-status")
            return r.json()

    endpoint_data = asyncio.run(_check_endpoint())
    assert endpoint_data["phase"] == "complete"
    assert endpoint_data["running"] is False
    assert endpoint_data["error"] is None


def test_startup_status_exact_error_transition_sequence(monkeypatch, startup_harness):
    """Lock the startup phase sequence when plugin startup raises."""
    server, phases = startup_harness

    def failing_load_plugins(*a, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(server, "load_plugins", failing_load_plugins)
    asyncio.run(server.startup_events())
    final = server._get_startup_status()

    assert phases == [
        "starting",
        "plugins-loading",
        "error",
    ]
    assert final["phase"] == "error"
    assert final["running"] is False
    assert final["loaded"] == 0
    assert final["total"] == 0
    assert isinstance(final["error"], str)
    assert "boom" in final["error"]

    # Verify the HTTP endpoint exposes the terminal error state — a regression
    # that stops the endpoint surfacing the error would silently pass above.
    async def _fetch_endpoint_data():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=server.app), base_url="http://test"
        ) as ac:
            r = await ac.get("/api/startup-status")
            return r.json()

    endpoint_data = asyncio.run(_fetch_endpoint_data())
    assert endpoint_data["phase"] == "error"
    assert endpoint_data["running"] is False
    assert isinstance(endpoint_data["error"], str)
    assert "boom" in endpoint_data["error"]


def test_startup_status_plugin_error_event_preserved_in_complete(monkeypatch, startup_harness):
    """When an individual plugin fails via a plugin-error progress event, startup
    ends in 'complete' (not 'error'), but the error field is propagated to the
    terminal status — a regression in that path would silently pass the
    load_plugins-raises test above.
    """
    server, phases = startup_harness

    def failing_plugin_load_plugins(_app, _context, progress_cb=None, route_setup_fn=None):
        """Simulate load_plugins emitting plugin-error for one plugin then completing."""
        if progress_cb:
            for event in _FAKE_PLUGIN_ERROR_EVENTS:
                progress_cb(event)
        # load_plugins returns normally — startup will set phase to 'complete'

    monkeypatch.setattr(server, "load_plugins", failing_plugin_load_plugins)
    asyncio.run(server.startup_events())
    final = server._get_startup_status()

    assert phases == [
        "starting",
        "plugins-loading",
        "plugins-discovered",
        "plugin-start",
        "plugin-requirements",
        "plugin-error",
        "plugin-registered",
        "plugins-complete",
        "complete",
    ]
    assert final["phase"] == "complete"
    assert final["running"] is False
    # The exact error text from the plugin-error event must survive in the
    # terminal status — a regression that clears it in any of the follow-up
    # non-error events (plugin-registered, plugins-complete) would now fail.
    assert final["error"] == _FAKE_PLUGIN_ERROR_TEXT

    # Verify the HTTP endpoint exposes the preserved error — a bug where the
    # endpoint stops surfacing the plugin error once startup reaches 'complete'
    # would silently pass the _get_startup_status assertion above.
    async def _fetch_endpoint_data():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=server.app), base_url="http://test"
        ) as ac:
            r = await ac.get("/api/startup-status")
            return r.json()

    endpoint_data = asyncio.run(_fetch_endpoint_data())
    assert endpoint_data["phase"] == "complete"
    assert endpoint_data["running"] is False
    assert endpoint_data["error"] == _FAKE_PLUGIN_ERROR_TEXT


def test_startup_status_e2e_real_plugin_loader(tmp_path, monkeypatch, isolate_logging):
    """Integration: run startup_events() with the REAL load_plugins against a
    minimal test plugin, using the production background-thread code path.

    Unlike the fake-load_plugins test, a regression in the plugin loader's
    emitted phase order or a missing phase will cause this test to fail.
    Unlike the sync-mode transition tests, this omits SLOPSMITH_SYNC_STARTUP so
    the background thread runs and route registration is marshalled back onto the
    event loop via call_soon_threadsafe — the path the production server uses.
    """
    import plugins as plugins_mod

    # Create a minimal test plugin whose routes.py registers a sentinel GET
    # endpoint.  This lets us verify that _route_setup_on_main() actually
    # executed the setup() call via call_soon_threadsafe — a no-op setup()
    # would pass even if the callback was queued but never executed.
    plugins_root = tmp_path / "test_plugins"
    plugins_root.mkdir()
    plugin_dir = plugins_root / "e2eplugin"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.json").write_text(
        json.dumps({"id": "e2eplugin", "name": "E2E Plugin", "routes": "routes.py"})
    )
    (plugin_dir / "routes.py").write_text(
        "def setup(app, ctx):\n"
        "    @app.get('/api/plugin-e2eplugin-ok')\n"
        "    def _sentinel():\n"
        "        return {'ok': True}\n"
    )

    # Override the plugin loader's built-in directory to our isolated test
    # plugins root so real installed plugins don't affect the phase sequence.
    monkeypatch.setattr(plugins_mod, "PLUGINS_DIR", plugins_root)
    monkeypatch.delenv("SLOPSMITH_PLUGINS_DIR", raising=False)

    # _PIP_TARGET is computed from CONFIG_DIR at plugins import time, so it
    # may point at a previous test's tmp dir or the system /config path.
    # Redirect it to the current tmp_path so requirement installs (a no-op
    # for our test plugin) stay fully isolated.
    monkeypatch.setattr(plugins_mod, "_PIP_TARGET", tmp_path / "pip_packages")

    # Set up server WITHOUT SLOPSMITH_SYNC_STARTUP so startup_events() spawns
    # the real background thread and route registration is marshalled back onto
    # the event loop via call_soon_threadsafe (the production path).
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("SLOPSMITH_SYNC_STARTUP", raising=False)
    monkeypatch.delenv("SLOPSMITH_DEMO_MODE", raising=False)
    sys.modules.pop("server", None)
    server = importlib.import_module("server")
    monkeypatch.setattr(server, "startup_scan", lambda: None)

    # Wire phase recording on _set_startup_status.  The original uses a lock,
    # so calling it before appending to the list is thread-safe (the background
    # thread and the GIL together make list.append atomic).
    phases = []
    original_set = server._set_startup_status

    def recording_set(**updates):
        original_set(**updates)
        phases.append(server._get_startup_status()["phase"])

    monkeypatch.setattr(server, "_set_startup_status", recording_set)

    # Save state the plugin loader will mutate for cleanup.
    saved_loaded = list(plugins_mod.LOADED_PLUGINS)
    saved_path = list(sys.path)
    saved_e2e_modules = {k for k in sys.modules if k.startswith("plugin_e2eplugin")}
    data: dict | None = None
    try:
        with TestClient(server.app) as tc:
            # startup_events() returned immediately after spawning the background
            # thread; poll /api/startup-status via HTTP until running=False.
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                data = tc.get("/api/startup-status").json()
                if not data.get("running", True):
                    break
                time.sleep(0.05)
            assert data is not None, "No response received from /api/startup-status"
            assert not data.get("running", True), (
                f"Background startup thread did not complete within 10 s; "
                f"last status: {data}"
            )
            # Assert terminal state via the HTTP endpoint (not just the internal
            # helper) so a disconnect between the handler and _get_startup_status
            # would fail.
            assert data["phase"] == "complete"
            assert data["running"] is False
            assert data["loaded"] == 1
            assert data["total"] == 1
            assert data["error"] is None
            # Verify that _route_setup_on_main() actually ran the plugin's setup()
            # call via call_soon_threadsafe — if the callback was queued but never
            # executed the sentinel route would be missing and this would return 404.
            # We check this inside the same TestClient context to avoid opening a
            # second client (which would re-run app lifespan and startup_events()).
            sentinel = tc.get("/api/plugin-e2eplugin-ok")
            assert sentinel.status_code == 200
            assert sentinel.json() == {"ok": True}
    finally:
        server._DEMO_JANITOR_STOP.set()
        thread = server._DEMO_JANITOR_THREAD
        if thread is not None:
            thread.join(timeout=2)
        server._DEMO_JANITOR_STARTED = False
        server._DEMO_JANITOR_THREAD = None
        conn = getattr(getattr(server, "meta_db", None), "conn", None)
        if conn is not None:
            conn.close()
        with plugins_mod.PLUGINS_LOCK:
            plugins_mod.LOADED_PLUGINS.clear()
            plugins_mod.LOADED_PLUGINS.extend(saved_loaded)
        sys.path[:] = saved_path
        for key in list(sys.modules):
            if key.startswith("plugin_e2eplugin") and key not in saved_e2e_modules:
                del sys.modules[key]

    assert phases == [
        "starting",
        "plugins-loading",
        "plugins-discovered",
        "plugin-start",
        "plugin-requirements",
        "plugin-routes",
        "plugin-registered",
        "plugins-complete",
        "complete",
    ]


def test_startup_status_endpoint_background_thread_path(tmp_path, monkeypatch, isolate_logging):
    """Verify /api/startup-status reflects the correct terminal state when the
    background-thread code path is used (SLOPSMITH_SYNC_STARTUP not set).

    All other transition tests force SLOPSMITH_SYNC_STARTUP=1, which exercises
    only the inline branch of startup_events().  In production the loader runs
    in a background thread; thread handoff bugs (missed progress events, route
    marshalling failures, races while the UI polls /api/startup-status) would
    go undetected by the sync-only tests.

    This test omits SLOPSMITH_SYNC_STARTUP so startup_events() spawns the real
    background thread, then polls the HTTP endpoint until running=False and
    asserts the terminal contract.
    """
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("SLOPSMITH_SYNC_STARTUP", raising=False)
    monkeypatch.delenv("SLOPSMITH_DEMO_MODE", raising=False)
    sys.modules.pop("server", None)
    server = importlib.import_module("server")

    _route_setup_called = []

    def _load_plugins_with_events(_app, _context, progress_cb=None, route_setup_fn=None):
        """Emit the full success-path event sequence so the background thread's
        status-propagation logic (_on_progress + _set_startup_status) is exercised.
        Also calls route_setup_fn with a sentinel to exercise _route_setup_on_main()
        and the call_soon_threadsafe path — without this, a no-op stub would never
        invoke route_setup_fn and the route-registration branch would go untested.
        """
        if route_setup_fn:
            route_setup_fn(lambda: _route_setup_called.append(True))
        if progress_cb:
            for event in _FAKE_SUCCESS_EVENTS:
                progress_cb(event)

    monkeypatch.setattr(server, "load_plugins", _load_plugins_with_events)
    monkeypatch.setattr(server, "startup_scan", lambda: None)
    try:
        with TestClient(server.app) as tc:
            # startup_events() returned immediately after spawning the background
            # thread; poll /api/startup-status until the thread sets running=False.
            deadline = time.monotonic() + 5.0
            data: dict = {}
            while time.monotonic() < deadline:
                data = tc.get("/api/startup-status").json()
                if not data.get("running", True):
                    break
                time.sleep(0.02)
            assert not data.get("running", True), (
                f"Background startup thread did not complete within 5 s; "
                f"last status: {data}"
            )
            assert data["phase"] == "complete"
            assert data["loaded"] == 1
            assert data["total"] == 1
            assert data["error"] is None
            # Verify route_setup_fn was invoked by the thread so call_soon_threadsafe
            # actually executed the sentinel — proves the main-loop handoff path ran.
            assert _route_setup_called, "route_setup_fn was never called; call_soon_threadsafe path was not exercised"
    finally:
        server._DEMO_JANITOR_STOP.set()
        thread = server._DEMO_JANITOR_THREAD
        if thread is not None:
            thread.join(timeout=2)
        server._DEMO_JANITOR_STARTED = False
        server._DEMO_JANITOR_THREAD = None
        conn = getattr(getattr(server, "meta_db", None), "conn", None)
        if conn is not None:
            conn.close()


def test_startup_status_endpoint_background_thread_failure(tmp_path, monkeypatch, isolate_logging):
    """Verify /api/startup-status reflects phase='error'/running=False when
    load_plugins raises inside the background thread.

    The success-path background-thread test covers status propagation for a
    normal run; this test covers the exception branch so a regression where
    the async thread never publishes running=False or phase='error' is caught.
    """
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("SLOPSMITH_SYNC_STARTUP", raising=False)
    monkeypatch.delenv("SLOPSMITH_DEMO_MODE", raising=False)
    sys.modules.pop("server", None)
    server = importlib.import_module("server")

    _BG_ERROR = "simulated background load_plugins failure"

    def _load_plugins_raises(_app, _context, progress_cb=None, route_setup_fn=None):
        raise RuntimeError(_BG_ERROR)

    monkeypatch.setattr(server, "load_plugins", _load_plugins_raises)
    monkeypatch.setattr(server, "startup_scan", lambda: None)
    try:
        with TestClient(server.app) as tc:
            deadline = time.monotonic() + 5.0
            data: dict = {}
            while time.monotonic() < deadline:
                data = tc.get("/api/startup-status").json()
                if not data.get("running", True):
                    break
                time.sleep(0.02)
            assert not data.get("running", True), (
                f"Background startup thread did not complete within 5 s; "
                f"last status: {data}"
            )
            assert data["phase"] == "error"
            assert _BG_ERROR in data["error"]
    finally:
        server._DEMO_JANITOR_STOP.set()
        thread = server._DEMO_JANITOR_THREAD
        if thread is not None:
            thread.join(timeout=2)
        server._DEMO_JANITOR_STARTED = False
        server._DEMO_JANITOR_THREAD = None
        conn = getattr(getattr(server, "meta_db", None), "conn", None)
        if conn is not None:
            conn.close()

