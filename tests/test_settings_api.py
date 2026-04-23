"""Tests for server.py /api/settings — partial-update safety and the
master_difficulty key added in slopsmith#48 PR 2.

The endpoint must merge only keys present in the request body so that
single-key POSTs (like the difficulty slider's oninput fire-and-forget)
don't clobber unrelated settings on disk.
"""

import importlib
import json
import sys

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # Point CONFIG_DIR at a per-test temp path BEFORE server's
    # import-time side effects run. server.py reads CONFIG_DIR from the
    # environment at module load (line 35) and immediately constructs
    # `meta_db = MetadataDB()` at module level, which calls
    # CONFIG_DIR.mkdir(...) and opens a sqlite file — a plain
    # post-import monkeypatch on server.CONFIG_DIR wouldn't catch those
    # side effects, and the real user config dir would get written to.
    # Forcing a fresh import inside the patched env means each test
    # gets an isolated meta_db + config dir.
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    sys.modules.pop("server", None)
    server = importlib.import_module("server")
    test_client = TestClient(server.app)
    try:
        yield test_client
    finally:
        # Close both the HTTP client and the sqlite connection meta_db
        # opened at import. Without this teardown each test would leak
        # a file handle; pytest's per-test tmp_path cleanup can also
        # fail on Windows when the sqlite handle is still open.
        test_client.close()
        meta_db = getattr(server, "meta_db", None)
        conn = getattr(meta_db, "conn", None)
        if conn is not None:
            conn.close()


def _read_cfg(tmp_path):
    return json.loads((tmp_path / "config.json").read_text())


# ── master_difficulty round-trip ─────────────────────────────────────────────

def test_post_master_difficulty_persists(client, tmp_path):
    r = client.post("/api/settings", json={"master_difficulty": 75})
    assert r.status_code == 200
    assert _read_cfg(tmp_path)["master_difficulty"] == 75


def test_get_returns_persisted_master_difficulty(client, tmp_path):
    client.post("/api/settings", json={"master_difficulty": 60})
    r = client.get("/api/settings")
    assert r.status_code == 200
    assert r.json()["master_difficulty"] == 60


def test_master_difficulty_clamped_to_range(client, tmp_path):
    client.post("/api/settings", json={"master_difficulty": 150})
    assert _read_cfg(tmp_path)["master_difficulty"] == 100
    client.post("/api/settings", json={"master_difficulty": -5})
    assert _read_cfg(tmp_path)["master_difficulty"] == 0


def test_master_difficulty_accepts_numeric_string(client, tmp_path):
    # Some clients stringify numbers before POSTing. int(float(...))
    # covers both "75" and "75.0" without introducing a hard type
    # constraint on the wire.
    client.post("/api/settings", json={"master_difficulty": "75"})
    assert _read_cfg(tmp_path)["master_difficulty"] == 75
    client.post("/api/settings", json={"master_difficulty": "42.9"})
    assert _read_cfg(tmp_path)["master_difficulty"] == 42


@pytest.mark.parametrize("bad_value", [
    None, "", "abc", [], {},
    "inf", "-inf", "1e309",  # float("inf") / overflow past int range
    True, False,             # bool is a subclass of int in Python
])
def test_master_difficulty_rejects_non_numeric(client, tmp_path, bad_value):
    # Public endpoint — a bad value shouldn't 500. Returns an error
    # object like the dlc_dir validation branch, and doesn't write
    # anything to disk. Overflow cases (int(float("inf"))) raise
    # OverflowError distinctly from ValueError, so the handler catches
    # both.
    (tmp_path / "config.json").write_text(json.dumps({"master_difficulty": 50}))
    r = client.post("/api/settings", json={"master_difficulty": bad_value})
    assert r.status_code == 200  # handler returns dict, not HTTPException
    assert "error" in r.json()
    # Previous value is preserved
    assert _read_cfg(tmp_path)["master_difficulty"] == 50


# ── Partial-update safety: a single-key POST must not clobber siblings ──────

def test_slider_post_does_not_clobber_other_keys(client, tmp_path):
    # Seed all three "soft" keys.
    (tmp_path / "config.json").write_text(json.dumps({
        "default_arrangement": "Lead",
        "demucs_server_url": "http://demucs.example:9000",
        "master_difficulty": 100,
    }))

    # Simulate the slider's fire-and-forget POST — just the one key.
    client.post("/api/settings", json={"master_difficulty": 50})

    cfg = _read_cfg(tmp_path)
    assert cfg["master_difficulty"] == 50
    assert cfg["default_arrangement"] == "Lead"
    assert cfg["demucs_server_url"] == "http://demucs.example:9000"


def test_default_arrangement_post_does_not_clobber_master_difficulty(client, tmp_path):
    # Symmetric: persisting default_arrangement from the arrangement picker
    # must not wipe a previously-set master_difficulty.
    (tmp_path / "config.json").write_text(json.dumps({
        "master_difficulty": 80,
    }))

    client.post("/api/settings", json={"default_arrangement": "Bass"})

    cfg = _read_cfg(tmp_path)
    assert cfg["master_difficulty"] == 80
    assert cfg["default_arrangement"] == "Bass"


def test_dlc_dir_null_is_noop_not_clear(client, tmp_path):
    # Pre-refactor, absent dlc_dir was implicitly ignored. Some clients
    # send `null` rather than omitting the key; those should also be a
    # no-op so an unrelated POST can't silently wipe the DLC setting.
    (tmp_path / "config.json").write_text(json.dumps({
        "dlc_dir": "/existing/path",
    }))
    client.post("/api/settings", json={"dlc_dir": None, "master_difficulty": 50})
    assert _read_cfg(tmp_path)["dlc_dir"] == "/existing/path"


@pytest.mark.parametrize("bad_content", ["[]", '"hello"', "42", "null", "not valid json {"])
def test_post_recovers_from_malformed_config_file(client, tmp_path, bad_content):
    # If config.json is valid JSON but a non-dict (e.g. a migrated
    # version or user tampering), assignments like cfg["dlc_dir"] = ...
    # would crash with TypeError. Treat non-dict parsed values the same
    # as missing — fall back to defaults, merge the request, write back
    # a clean dict-shaped file.
    (tmp_path / "config.json").write_text(bad_content)
    r = client.post("/api/settings", json={"master_difficulty": 60})
    assert r.status_code == 200
    cfg = _read_cfg(tmp_path)
    assert isinstance(cfg, dict)
    assert cfg["master_difficulty"] == 60


def test_first_run_slider_post_preserves_default_dlc_dir(client, tmp_path):
    # Regression: on first run there's no config.json yet. If the
    # slider's single-key POST is the first write, the server must
    # seed cfg with _default_settings() first — otherwise the written
    # config.json would lack dlc_dir, and subsequent GETs would return
    # blank instead of the fallback DLC_DIR path.
    assert not (tmp_path / "config.json").exists()
    client.post("/api/settings", json={"master_difficulty": 50})
    cfg = _read_cfg(tmp_path)
    assert cfg["master_difficulty"] == 50
    # dlc_dir key must be present (value can be empty string if the
    # default DLC_DIR doesn't exist on this host — the point is the
    # key survives rather than getting dropped).
    assert "dlc_dir" in cfg


def test_dlc_dir_empty_string_clears(client, tmp_path):
    # Explicit empty string IS "clear" — keeps a route for a user who
    # wants to unset the DLC dir via the settings panel.
    (tmp_path / "config.json").write_text(json.dumps({
        "dlc_dir": "/existing/path",
    }))
    client.post("/api/settings", json={"dlc_dir": ""})
    assert _read_cfg(tmp_path)["dlc_dir"] == ""


@pytest.mark.parametrize("key", ["default_arrangement", "demucs_server_url"])
def test_string_key_null_is_noop(client, tmp_path, key):
    # Match the dlc_dir contract: null preserves the on-disk value.
    (tmp_path / "config.json").write_text(json.dumps({key: "existing"}))
    client.post("/api/settings", json={key: None, "master_difficulty": 50})
    assert _read_cfg(tmp_path)[key] == "existing"


@pytest.mark.parametrize("key", ["default_arrangement", "demucs_server_url"])
@pytest.mark.parametrize("bad_value", [42, [], {}, True])
def test_string_key_non_string_rejected(client, tmp_path, key, bad_value):
    # Downstream consumers call string methods on these values
    # (e.g. demucs_server_url.rstrip('/') in lib/sloppak_convert.py).
    # Reject non-strings at the boundary so garbage can't persist.
    (tmp_path / "config.json").write_text(json.dumps({key: "existing"}))
    r = client.post("/api/settings", json={key: bad_value})
    assert "error" in r.json()
    assert _read_cfg(tmp_path)[key] == "existing"


@pytest.mark.parametrize("key", ["default_arrangement", "demucs_server_url"])
def test_string_key_empty_string_clears(client, tmp_path, key):
    (tmp_path / "config.json").write_text(json.dumps({key: "existing"}))
    client.post("/api/settings", json={key: ""})
    assert _read_cfg(tmp_path)[key] == ""


def test_dlc_dir_non_string_rejected(client, tmp_path):
    # Non-string JSON (number, list, object) shouldn't reach Path(...)
    # and crash. Returns the structured error + preserves on-disk value.
    (tmp_path / "config.json").write_text(json.dumps({
        "dlc_dir": "/existing/path",
    }))
    r = client.post("/api/settings", json={"dlc_dir": 42})
    assert "error" in r.json()
    assert _read_cfg(tmp_path)["dlc_dir"] == "/existing/path"


def test_empty_post_preserves_all_existing_keys(client, tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({
        "default_arrangement": "Lead",
        "demucs_server_url": "http://demucs.example:9000",
        "master_difficulty": 42,
    }))

    client.post("/api/settings", json={})

    assert _read_cfg(tmp_path) == {
        "default_arrangement": "Lead",
        "demucs_server_url": "http://demucs.example:9000",
        "master_difficulty": 42,
    }


# ── Absent master_difficulty → GET falls through (frontend default) ─────────

def test_get_without_master_difficulty_omits_key(client, tmp_path):
    # When no master_difficulty has been saved, the GET response should
    # not include it — frontend defaults to 100 on its own side. This
    # matches the other keys' behaviour (GET reflects what's on disk).
    r = client.get("/api/settings")
    assert r.status_code == 200
    assert "master_difficulty" not in r.json()
