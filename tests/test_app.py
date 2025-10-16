import pytest
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
