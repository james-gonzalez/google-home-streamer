import atexit
import logging
import os
import socket
import threading
import time
from typing import Any, Dict, Optional, Tuple
from uuid import UUID

import pychromecast
from flask import (
    Flask,
    Response,
    jsonify,
    make_response,
    render_template,
    request,
    send_from_directory,
)
from pychromecast.discovery import AbstractCastListener, CastBrowser, CastInfo
from pychromecast.error import PyChromecastError
from zeroconf import Zeroconf

# Suppress excessive zeroconf logging
logging.basicConfig(level=logging.INFO)
logging.getLogger("zeroconf").setLevel(logging.ERROR)

app = Flask(__name__)
logger = logging.getLogger(__name__)

FILE_NAME: str = "white-noise-20m.mp3"
PORT: int = 8000
STREAM_URL: Optional[str] = os.getenv("STREAM_URL")
PUBLIC_BASE_URL: Optional[str] = os.getenv("PUBLIC_BASE_URL")
AUTO_START_DISCOVERY: bool = os.getenv("AUTO_START_DISCOVERY", "1") == "1"

# --- Global state for discovery ---
discovered_casts: Dict[str, Any] = {}
browser: Optional[CastBrowser] = None
discovery_lock = threading.Lock()
zconf: Optional[Zeroconf] = None

HealthReport = Dict[str, Dict[str, Any]]


def _resolve_cast_info(uuid: UUID) -> Optional[CastInfo]:
    current_browser = browser
    if current_browser:
        return current_browser.devices.get(uuid)
    return None


class MyCastListener(AbstractCastListener):
    """Listener for discovering and removing Chromecasts."""

    def add_cast(self, uuid: UUID, service: str) -> None:
        cast = _resolve_cast_info(uuid)
        if not cast or not cast.friendly_name:
            return
        with discovery_lock:
            discovered_casts[cast.friendly_name] = cast
        print(f"Discovered: {cast.friendly_name}")

    def update_cast(self, uuid: UUID, service: str) -> None:
        self.add_cast(uuid, service)

    def remove_cast(self, uuid: UUID, service: str, cast_info: CastInfo) -> None:
        friendly_name = cast_info.friendly_name
        if not friendly_name:
            return
        with discovery_lock:
            if friendly_name in discovered_casts:
                del discovered_casts[friendly_name]
        print(f"Removed: {friendly_name}")


def start_discovery() -> None:
    """Starts the background discovery browser."""
    global browser, zconf
    print("Starting background device discovery...")
    zconf = Zeroconf()
    listener = MyCastListener()
    browser = pychromecast.CastBrowser(listener, zconf)  # type: ignore[arg-type]
    browser.start_discovery()


def stop_discovery() -> None:
    """Stops the background discovery browser."""
    if browser:
        print("Stopping background device discovery...")
        browser.stop_discovery()
    if zconf:
        zconf.close()


def get_cast(name: str) -> Optional["pychromecast.Chromecast"]:
    """Get a Chromecast object from a friendly name."""
    with discovery_lock:
        cast_info = discovered_casts.get(name)
    if cast_info and zconf:
        return pychromecast.get_chromecast_from_cast_info(cast_info, zconf)
    return None


# --- Utility helpers ---


def get_local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        IP = s.getsockname()[0]
    except Exception:
        IP = "127.0.0.1"
    finally:
        s.close()
    return IP


def build_stream_url() -> str:
    """Construct a stable stream URL usable from any replica."""
    if STREAM_URL:
        return STREAM_URL

    if PUBLIC_BASE_URL:
        return f"{PUBLIC_BASE_URL.rstrip('/')}/stream"

    # Fall back to the request host if we have one, otherwise local IP.
    if request and request.host_url:
        host_url = request.host_url.rstrip("/")
        return f"{host_url}/stream"

    ip_address = get_local_ip()
    return f"http://{ip_address}:{PORT}/stream"


# --- Flask Routes ---


def _check_discovery() -> Tuple[bool, str]:
    with discovery_lock:
        current_browser = browser
        current_zconf = zconf

    if not current_browser:
        return False, "Chromecast discovery not initialized"
    if current_zconf is None:
        return False, "Zeroconf instance missing"

    zc_browser = getattr(current_browser, "_zc_browser", None)
    if zc_browser is None:
        return False, "Underlying zeroconf browser not available"
    if not zc_browser.is_alive():
        return False, "Discovery thread not running"

    return True, "Discovery thread healthy"


def _check_stream_asset() -> Tuple[bool, str]:
    file_path = os.path.join(os.getcwd(), FILE_NAME)
    if not os.path.isfile(file_path):
        return False, f"Stream asset {FILE_NAME} not found"
    if not os.access(file_path, os.R_OK):
        return False, f"Stream asset {FILE_NAME} is not readable"
    if os.path.getsize(file_path) == 0:
        return False, f"Stream asset {FILE_NAME} is empty"

    return True, "Stream asset available"


def _check_stream_url() -> Tuple[bool, str]:
    stream_url = build_stream_url()
    if stream_url.startswith("http://") or stream_url.startswith("https://"):
        return True, f"Streaming from {stream_url}"
    return False, "Invalid stream URL"


def _evaluate_health(include_readiness_checks: bool) -> Tuple[bool, HealthReport]:
    checks: HealthReport = {}
    ok = True

    discovery_ok, discovery_detail = _check_discovery()
    checks["chromecast_discovery"] = {"ok": discovery_ok, "detail": discovery_detail}
    ok = ok and discovery_ok

    asset_ok, asset_detail = _check_stream_asset()
    checks["stream_asset"] = {"ok": asset_ok, "detail": asset_detail}
    ok = ok and asset_ok

    if include_readiness_checks:
        stream_ok, stream_detail = _check_stream_url()
        checks["stream_url"] = {"ok": stream_ok, "detail": stream_detail}
        ok = ok and stream_ok

    return ok, checks


@app.route("/")
def index() -> str:
    return render_template("index.html")


@app.route("/status")
def get_status() -> Response:
    with discovery_lock:
        device_names = sorted(discovered_casts.keys())

    return jsonify({"devices": device_names})


@app.route("/play", methods=["POST"])
def play() -> Response:
    data = request.json
    if not data:
        return make_response(
            jsonify({"status": "error", "message": "Invalid request"}), 400
        )

    device_name = data.get("device_name")
    volume = data.get("volume", 0.1)
    loop = data.get("loop", True)

    if not device_name:
        return make_response(
            jsonify({"status": "error", "message": "Device name not provided"}), 400
        )

    cast = get_cast(device_name)
    if not cast:
        return make_response(
            jsonify({"status": "error", "message": "Device not found"}), 404
        )

    cast.wait()
    print(f"[{device_name}] Quitting current app to ensure clean state.")
    cast.quit_app()
    time.sleep(1)
    cast.set_volume(volume)

    stream_url = build_stream_url()
    cast.media_controller.play_media(stream_url, "audio/mpeg")
    if loop:
        cast.media_controller.block_until_active()

    return jsonify({"status": "playing", "stream_url": stream_url})


@app.route("/stop", methods=["POST"])
def stop() -> Response:
    data = request.json
    if not data:
        return make_response(
            jsonify({"status": "error", "message": "Invalid request"}), 400
        )
    device_name = data.get("device_name")

    cast = get_cast(device_name)
    if cast:
        cast.wait()
        cast.quit_app()

    return jsonify({"status": "stopped"})


@app.route("/volume", methods=["POST"])
def set_volume() -> Response:
    data = request.json
    if not data:
        return make_response(
            jsonify({"status": "error", "message": "Invalid request"}), 400
        )
    device_name = data.get("device_name")
    volume = data.get("volume")

    if not device_name or volume is None:
        return make_response(
            jsonify(
                {"status": "error", "message": "Device name or volume not provided"}
            ),
            400,
        )

    cast = get_cast(device_name)
    if not cast:
        return make_response(
            jsonify({"status": "error", "message": "Device not found"}), 404
        )

    print(f"[{device_name}] Setting volume to {volume}")
    cast.wait()
    cast.set_volume(volume)
    return jsonify({"status": "volume updated"})


@app.route("/stream")
def stream_file() -> Response:
    return send_from_directory(os.getcwd(), FILE_NAME)


@app.route("/health/live")
def health_live() -> Response:
    healthy, checks = _evaluate_health(include_readiness_checks=False)
    status = "ok" if healthy else "error"
    code = 200 if healthy else 500
    return jsonify({"status": status, "checks": checks}), code


@app.route("/health/ready")
def health_ready() -> Response:
    healthy, checks = _evaluate_health(include_readiness_checks=True)
    status = "ready" if healthy else "unready"
    code = 200 if healthy else 503
    return jsonify({"status": status, "checks": checks}), code

# --- Application Startup ---
if AUTO_START_DISCOVERY:
    start_discovery()
    atexit.register(stop_discovery)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=True, use_reloader=False)
