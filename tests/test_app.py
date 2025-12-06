import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("AUTO_START_DISCOVERY", "0")

import app as app_module
from app import app as flask_app


@pytest.fixture
def app():
    yield flask_app

@pytest.fixture
def client(app):
    return app.test_client()

def test_index(client):
    """Test if the main page loads."""
    res = client.get('/')
    assert res.status_code == 200
    assert b"Google Home Streamer" in res.data


def _mock_discovery(monkeypatch, alive: bool) -> None:
    fake_thread = SimpleNamespace(is_alive=lambda: alive)
    fake_browser = SimpleNamespace(_zc_browser=fake_thread)
    monkeypatch.setattr(app_module, "browser", fake_browser)
    monkeypatch.setattr(app_module, "zconf", object())


def test_health_live_ok(monkeypatch, client):
    _mock_discovery(monkeypatch, alive=True)
    monkeypatch.setattr(app_module, "cast_threads", {})
    res = client.get("/health/live")
    assert res.status_code == 200
    data = res.get_json()
    assert data["checks"]["chromecast_discovery"]["ok"] is True
    assert data["checks"]["stream_asset"]["ok"] is True


def test_health_live_failure(monkeypatch, client):
    _mock_discovery(monkeypatch, alive=False)
    monkeypatch.setattr(app_module, "cast_threads", {})
    res = client.get("/health/live")
    assert res.status_code == 500
    data = res.get_json()
    assert data["checks"]["chromecast_discovery"]["ok"] is False


def test_health_ready_checks(monkeypatch, client):
    _mock_discovery(monkeypatch, alive=True)
    monkeypatch.setattr(app_module, "cast_threads", {})
    res = client.get("/health/ready")
    assert res.status_code == 200
    data = res.get_json()
    assert data["checks"]["stream_asset"]["ok"] is True
    assert data["checks"]["playback_threads"]["ok"] is True


def test_health_ready_missing_asset(monkeypatch, client):
    _mock_discovery(monkeypatch, alive=True)
    monkeypatch.setattr(app_module, "cast_threads", {})

    def fake_exists(path: str) -> bool:
        return False

    monkeypatch.setattr(os.path, "isfile", fake_exists)

    res = client.get("/health/ready")
    assert res.status_code == 503
    data = res.get_json()
    assert data["checks"]["stream_asset"]["ok"] is False
