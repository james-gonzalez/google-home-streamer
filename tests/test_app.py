import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("AUTO_START_DISCOVERY", "0")

import app as app_module  # noqa: E402
from app import CastManager, get_local_ip  # noqa: E402
from app import app as flask_app


@pytest.fixture
def app():
    yield flask_app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def manager() -> CastManager:
    """Return the singleton CastManager used by the Flask app."""
    return app_module.cast_manager


# --- Helpers ---


def _mock_discovery(manager: CastManager, monkeypatch, alive: bool) -> None:
    """Patch the manager's internal browser/zconf to simulate discovery state."""
    fake_thread = SimpleNamespace(is_alive=lambda: alive)
    fake_browser = SimpleNamespace(_zc_browser=fake_thread, devices={})
    monkeypatch.setattr(manager, "_browser", fake_browser)
    monkeypatch.setattr(manager, "_zconf", object())


# --- Index ---


def test_index(client):
    """Test if the main page loads."""
    res = client.get("/")
    assert res.status_code == 200
    assert b"Google Home Streamer" in res.data


# --- Health endpoints ---


def test_health_live_ok(monkeypatch, client, manager):
    _mock_discovery(manager, monkeypatch, alive=True)
    with manager._lock:
        manager._cast_threads.clear()
    res = client.get("/health/live")
    assert res.status_code == 200
    data = res.get_json()
    assert data["checks"]["chromecast_discovery"]["ok"] is True
    assert data["checks"]["stream_asset"]["ok"] is True


def test_health_live_failure(monkeypatch, client, manager):
    _mock_discovery(manager, monkeypatch, alive=False)
    with manager._lock:
        manager._cast_threads.clear()
    res = client.get("/health/live")
    assert res.status_code == 500
    data = res.get_json()
    assert data["checks"]["chromecast_discovery"]["ok"] is False


def test_health_ready_checks(monkeypatch, client, manager):
    _mock_discovery(manager, monkeypatch, alive=True)
    with manager._lock:
        manager._cast_threads.clear()
    res = client.get("/health/ready")
    assert res.status_code == 200
    data = res.get_json()
    assert data["checks"]["stream_asset"]["ok"] is True
    assert data["checks"]["playback_threads"]["ok"] is True


def test_health_ready_missing_asset(monkeypatch, client, manager):
    _mock_discovery(manager, monkeypatch, alive=True)
    with manager._lock:
        manager._cast_threads.clear()

    monkeypatch.setattr(Path, "is_file", lambda self: False)

    res = client.get("/health/ready")
    assert res.status_code == 503
    data = res.get_json()
    assert data["checks"]["stream_asset"]["ok"] is False


# --- /status endpoint ---


def test_status_empty(client, manager):
    """Status returns empty device list when nothing is discovered."""
    with manager._lock:
        manager._discovered_casts.clear()
        manager._cast_threads.clear()
        manager._active_cast_name = None
        manager._volume_by_device.clear()

    res = client.get("/status")
    assert res.status_code == 200
    data = res.get_json()
    assert data["devices"] == []
    assert data["playing_device"] is None
    assert data["currently_playing"] is None


def test_status_with_devices(client, manager):
    """Status returns discovered devices and volume info."""
    fake_cast_info = SimpleNamespace(friendly_name="Living Room")
    with manager._lock:
        manager._discovered_casts["Living Room"] = fake_cast_info
        manager._cast_threads.clear()
        manager._active_cast_name = None
        manager._volume_by_device["Living Room"] = 0.5

    res = client.get("/status")
    assert res.status_code == 200
    data = res.get_json()
    assert "Living Room" in data["devices"]
    assert data["volumes"]["Living Room"] == 0.5

    # Cleanup
    with manager._lock:
        manager._discovered_casts.clear()
        manager._volume_by_device.clear()


# --- /play endpoint ---


def test_play_missing_body(client):
    """Play returns 400 when no JSON body is provided."""
    res = client.post("/play", content_type="application/json")
    assert res.status_code == 400


def test_play_missing_device_name(client):
    """Play returns 400 when device_name is missing."""
    res = client.post(
        "/play",
        data=json.dumps({"volume": 0.5}),
        content_type="application/json",
    )
    assert res.status_code == 400
    data = res.get_json()
    assert "Device name not provided" in data["message"]


def test_play_device_not_found(client, manager):
    """Play returns 404 when the device is not discovered."""
    with manager._lock:
        manager._discovered_casts.clear()

    res = client.post(
        "/play",
        data=json.dumps({"device_name": "Nonexistent", "volume": 0.1}),
        content_type="application/json",
    )
    assert res.status_code == 404
    data = res.get_json()
    assert "Device not found" in data["message"]


def test_play_invalid_volume(client, manager):
    """Play clamps invalid volume values instead of erroring."""
    fake_cast = MagicMock()
    fake_cast.media_controller = MagicMock()

    with patch.object(manager, "get_cast", return_value=fake_cast):
        with patch.object(manager, "play", return_value=(True, "playing")):
            res = client.post(
                "/play",
                data=json.dumps({"device_name": "Test", "volume": 5.0}),
                content_type="application/json",
            )
            assert res.status_code == 200


def test_play_volume_validation_negative(client, manager):
    """Play clamps negative volume to 0."""
    with patch.object(manager, "play", return_value=(True, "playing")) as mock_play:
        with patch.object(manager, "get_cast", return_value=MagicMock()):
            res = client.post(
                "/play",
                data=json.dumps({"device_name": "Test", "volume": -0.5}),
                content_type="application/json",
            )
            assert res.status_code == 200
            # Volume should have been clamped to 0.0
            called_volume = mock_play.call_args[0][1]
            assert called_volume == 0.0


# --- /stop endpoint ---


def test_stop_missing_body(client):
    """Stop returns 400 when no JSON body is provided."""
    res = client.post("/stop", content_type="application/json")
    assert res.status_code == 400


def test_stop_device(client, manager):
    """Stop returns success for a device name."""
    with patch.object(manager, "stop", return_value="stopped") as mock_stop:
        res = client.post(
            "/stop",
            data=json.dumps({"device_name": "Test Speaker"}),
            content_type="application/json",
        )
        assert res.status_code == 200
        data = res.get_json()
        assert data["status"] == "stopped"
        mock_stop.assert_called_once_with("Test Speaker")


# --- /volume endpoint ---


def test_volume_missing_body(client):
    """Volume returns 400 when no JSON body is provided."""
    res = client.post("/volume", content_type="application/json")
    assert res.status_code == 400


def test_volume_missing_fields(client):
    """Volume returns 400 when device_name or volume is missing."""
    res = client.post(
        "/volume",
        data=json.dumps({"device_name": "Test"}),
        content_type="application/json",
    )
    assert res.status_code == 400

    res = client.post(
        "/volume",
        data=json.dumps({"volume": 0.5}),
        content_type="application/json",
    )
    assert res.status_code == 400


def test_volume_device_not_found(client, manager):
    """Volume returns 404 when device is not found."""
    with patch.object(manager, "set_volume", return_value=(False, "Device not found")):
        res = client.post(
            "/volume",
            data=json.dumps({"device_name": "Ghost", "volume": 0.5}),
            content_type="application/json",
        )
        assert res.status_code == 404


def test_volume_success(client, manager):
    """Volume returns success when device is found."""
    with patch.object(
        manager, "set_volume", return_value=(True, "volume updated")
    ) as mock_vol:
        res = client.post(
            "/volume",
            data=json.dumps({"device_name": "Speaker", "volume": 0.7}),
            content_type="application/json",
        )
        assert res.status_code == 200
        data = res.get_json()
        assert data["status"] == "volume updated"
        mock_vol.assert_called_once_with("Speaker", 0.7)


def test_volume_clamped(client, manager):
    """Volume values outside 0-1 are clamped."""
    with patch.object(
        manager, "set_volume", return_value=(True, "volume updated")
    ) as mock_vol:
        res = client.post(
            "/volume",
            data=json.dumps({"device_name": "Speaker", "volume": 2.5}),
            content_type="application/json",
        )
        assert res.status_code == 200
        called_volume = mock_vol.call_args[0][1]
        assert called_volume == 1.0


# --- /stream endpoint ---


def test_stream_serves_file(client):
    """Stream endpoint serves the audio file."""
    res = client.get("/stream")
    # The file exists (it's an LFS pointer or real file), so we expect 200
    assert res.status_code == 200


# --- get_local_ip ---


def test_get_local_ip_success():
    """get_local_ip returns a non-loopback IP when network is available."""
    ip = get_local_ip()
    assert isinstance(ip, str)
    assert len(ip) > 0


def test_get_local_ip_fallback(monkeypatch):
    """get_local_ip returns 127.0.0.1 when network is unavailable."""
    import socket

    def fail_connect(self, addr):
        raise OSError("Network unreachable")

    monkeypatch.setattr(socket.socket, "connect", fail_connect)
    ip = get_local_ip()
    assert ip == "127.0.0.1"


# --- CastManager unit tests ---


def test_cast_manager_get_discovered_devices(manager):
    """get_discovered_devices returns sorted names."""
    with manager._lock:
        manager._discovered_casts["Zebra"] = SimpleNamespace(friendly_name="Zebra")
        manager._discovered_casts["Alpha"] = SimpleNamespace(friendly_name="Alpha")

    devices = manager.get_discovered_devices()
    assert devices == ["Alpha", "Zebra"]

    # Cleanup
    with manager._lock:
        manager._discovered_casts.clear()


def test_cast_manager_check_discovery_not_initialized(manager):
    """check_discovery fails when browser is None."""
    with manager._lock:
        old_browser = manager._browser
        manager._browser = None

    ok, detail = manager.check_discovery()
    assert ok is False
    assert "not initialized" in detail

    with manager._lock:
        manager._browser = old_browser


def test_cast_manager_check_playback_threads_empty(manager):
    """check_playback_threads is healthy when no threads exist."""
    with manager._lock:
        manager._cast_threads.clear()

    ok, detail = manager.check_playback_threads()
    assert ok is True


def test_cast_manager_build_status_dict():
    """_build_status_dict normalises media and cast status."""
    media = SimpleNamespace(
        player_state="PLAYING",
        content_id="http://example.com/audio.mp3",
        title="Test Track",
    )
    cast_status = SimpleNamespace(volume_level=0.42)

    result = CastManager._build_status_dict(media, cast_status)
    assert result["volume"] == 0.42
    assert result["player_state"] == "PLAYING"
    assert result["is_playing"] is True
    assert result["content_id"] == "http://example.com/audio.mp3"
    assert result["title"] == "Test Track"


def test_cast_manager_build_status_dict_idle():
    """_build_status_dict reports not playing for IDLE state."""
    media = SimpleNamespace(
        player_state="IDLE",
        content_id=None,
        title=None,
    )
    cast_status = SimpleNamespace(volume_level=0.1)

    result = CastManager._build_status_dict(media, cast_status)
    assert result["is_playing"] is False


def test_cast_manager_build_status_dict_none():
    """_build_status_dict handles None media/cast status."""
    result = CastManager._build_status_dict(None, None)
    assert result["volume"] is None
    assert result["player_state"] is None
    assert result["is_playing"] is False


# --- CastManager play/stop orchestration ---


@pytest.fixture
def clean_manager(manager):
    """Reset all mutable manager state before and after a test."""

    def _reset():
        with manager._lock:
            manager._cast_threads.clear()
            manager._discovered_casts.clear()
            manager._volume_by_device.clear()
            manager._active_cast_name = None

    _reset()
    yield manager
    _reset()


def test_play_device_not_found_returns_false(clean_manager):
    """play() reports failure when the device cannot be resolved."""
    with patch.object(clean_manager, "get_cast", return_value=None):
        success, message = clean_manager.play("Ghost", 0.2, True, "http://x/stream")
    assert success is False
    assert message == "Device not found"
    assert clean_manager.active_device is None


def test_play_starts_thread_and_records_state(clean_manager):
    """play() starts a thread, sets volume, and records active device/volume."""
    fake_cast = MagicMock()
    with (
        patch.object(clean_manager, "get_cast", return_value=fake_cast),
        patch.object(app_module, "CastThread") as fake_thread_cls,
        patch.object(app_module.time, "sleep"),
    ):
        success, message = clean_manager.play(
            "Living Room", 0.3, True, "http://x/stream"
        )

    assert (success, message) == (True, "playing")
    fake_cast.set_volume.assert_called_once_with(0.3)
    fake_cast.quit_app.assert_called_once()
    fake_thread_cls.assert_called_once_with(fake_cast, "http://x/stream", True)
    fake_thread_cls.return_value.start.assert_called_once()
    assert clean_manager.active_device == "Living Room"
    assert clean_manager.volumes["Living Room"] == 0.3


def test_play_switches_device_stops_previous(clean_manager):
    """Starting playback on a new device stops the previously active thread."""
    old_thread = MagicMock()
    with clean_manager._lock:
        clean_manager._cast_threads["Old Device"] = old_thread
        clean_manager._active_cast_name = "Old Device"

    fake_cast = MagicMock()
    with (
        patch.object(clean_manager, "get_cast", return_value=fake_cast),
        patch.object(app_module, "CastThread") as fake_thread_cls,
        patch.object(app_module.time, "sleep"),
    ):
        success, _ = clean_manager.play("New Device", 0.4, False, "http://x/stream")

    assert success is True
    old_thread.request_stop.assert_called_once()
    old_thread.join.assert_called_once()
    snapshot = clean_manager.cast_threads_snapshot
    assert "Old Device" not in snapshot
    assert snapshot["New Device"] is fake_thread_cls.return_value
    assert clean_manager.active_device == "New Device"


def test_play_restarts_same_device(clean_manager):
    """Replaying the same device stops the existing thread before starting anew."""
    existing = MagicMock()
    with clean_manager._lock:
        clean_manager._cast_threads["Speaker"] = existing
        clean_manager._active_cast_name = "Speaker"

    fake_cast = MagicMock()
    with (
        patch.object(clean_manager, "get_cast", return_value=fake_cast),
        patch.object(app_module, "CastThread") as fake_thread_cls,
        patch.object(app_module.time, "sleep"),
    ):
        clean_manager.play("Speaker", 0.5, True, "http://x/stream")

    existing.request_stop.assert_called_once()
    existing.join.assert_called_once()
    snapshot = clean_manager.cast_threads_snapshot
    assert snapshot["Speaker"] is fake_thread_cls.return_value


def test_stop_stops_thread_and_clears_active(clean_manager):
    """stop() halts the thread, quits the app, and clears active device."""
    thread = MagicMock()
    fake_cast = MagicMock()
    with clean_manager._lock:
        clean_manager._cast_threads["Speaker"] = thread
        clean_manager._active_cast_name = "Speaker"

    with patch.object(clean_manager, "get_cast", return_value=fake_cast):
        message = clean_manager.stop("Speaker")

    assert message == "stopped"
    thread.request_stop.assert_called_once()
    thread.join.assert_called_once()
    fake_cast.quit_app.assert_called_once()
    assert "Speaker" not in clean_manager.cast_threads_snapshot
    assert clean_manager.active_device is None


def test_stop_unknown_device_is_noop(clean_manager):
    """Stopping a device with no thread still returns 'stopped' without error."""
    with patch.object(clean_manager, "get_cast", return_value=None):
        message = clean_manager.stop("Nobody")
    assert message == "stopped"
    assert clean_manager.active_device is None


def test_status_does_not_connect_to_idle_devices(client, manager):
    """Regression: /status must not open connections to idle speakers.

    Connecting to every discovered device wakes its media receiver (the
    "chiming" bug). With no active device, get_cast/fetch_device_status must
    not be invoked for any discovered speaker.
    """
    with manager._lock:
        manager._discovered_casts.clear()
        manager._cast_threads.clear()
        manager._volume_by_device.clear()
        manager._active_cast_name = None
        manager._discovered_casts["Kitchen"] = SimpleNamespace(friendly_name="Kitchen")
        manager._discovered_casts["Bedroom"] = SimpleNamespace(friendly_name="Bedroom")

    with (
        patch.object(manager, "get_cast") as mock_get_cast,
        patch.object(manager, "fetch_device_status") as mock_fetch,
    ):
        res = client.get("/status")

    assert res.status_code == 200
    data = res.get_json()
    assert sorted(data["devices"]) == ["Bedroom", "Kitchen"]
    assert data["currently_playing"] is None
    mock_get_cast.assert_not_called()
    mock_fetch.assert_not_called()

    with manager._lock:
        manager._discovered_casts.clear()


def test_status_queries_only_active_device(client, manager):
    """/status fetches status for the active device only, never idle ones."""
    with manager._lock:
        manager._discovered_casts.clear()
        manager._cast_threads.clear()
        manager._volume_by_device.clear()
        manager._active_cast_name = "Kitchen"
        manager._discovered_casts["Kitchen"] = SimpleNamespace(friendly_name="Kitchen")
        manager._discovered_casts["Bedroom"] = SimpleNamespace(friendly_name="Bedroom")

    with patch.object(
        manager,
        "fetch_device_status",
        return_value={
            "volume": 0.2,
            "player_state": "PLAYING",
            "content_id": "http://x/stream",
            "title": None,
            "is_playing": True,
        },
    ) as mock_fetch:
        res = client.get("/status")

    assert res.status_code == 200
    data = res.get_json()
    mock_fetch.assert_called_once_with("Kitchen")
    assert data["currently_playing"]["device"] == "Kitchen"

    with manager._lock:
        manager._discovered_casts.clear()
        manager._active_cast_name = None
