import atexit
import logging
import os
import socket
import threading
import time
from typing import Any, Dict, Optional, Tuple

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
from pychromecast.discovery import CastBrowser, CastInfo
from pychromecast.error import NotConnected, PyChromecastError, RequestFailed
from zeroconf import Zeroconf

# Suppress excessive zeroconf logging
logging.basicConfig(level=logging.INFO)
logging.getLogger("zeroconf").setLevel(logging.ERROR)

app = Flask(__name__)
logger = logging.getLogger(__name__)

FILE_NAME: str = "white-noise-20m.mp3"
PORT: int = 8000
STREAM_URL: Optional[str] = os.getenv("STREAM_URL")
AUTO_START_DISCOVERY: bool = os.getenv("AUTO_START_DISCOVERY", "1") == "1"

# --- Global state for discovery and playback ---
cast_threads: Dict[str, "CastThread"] = {}
discovered_casts: Dict[str, Any] = {}
browser: Optional[CastBrowser] = None
discovery_lock = threading.Lock()
zconf: Optional[Zeroconf] = None
active_cast_name: Optional[str] = None
state_lock = threading.Lock()
volume_by_device: Dict[str, float] = {}

HealthReport = Dict[str, Dict[str, Any]]


class MyCastListener(pychromecast.CastListener):
    """Listener for discovering and removing Chromecasts."""

    def add_cast(self, uuid: str, service: Any) -> None:
        with discovery_lock:
            # The browser object is needed to get the full cast info
            if browser:
                cast = browser.devices[uuid]  # type: ignore[index]
                if cast.friendly_name:
                    discovered_casts[cast.friendly_name] = cast
                    print(f"Discovered: {cast.friendly_name}")

    def update_cast(self, uuid: str, service: Any) -> None:
        self.add_cast(uuid, service)

    def remove_cast(self, uuid: str, service: Any, cast_info: CastInfo) -> None:  # type: ignore[override]
        with discovery_lock:
            if cast_info.friendly_name in discovered_casts:
                del discovered_casts[cast_info.friendly_name]
                print(f"Removed: {cast_info.friendly_name}")


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


# --- Utility and Playback Thread ---


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


class CastThread(threading.Thread):
    def __init__(
        self, cast: "pychromecast.Chromecast", stream_url: str, loop: bool
    ) -> None:
        super().__init__()
        self.cast = cast
        self.stream_url = stream_url
        self.loop = loop
        self.mc = cast.media_controller
        self.stop_event = threading.Event()

    def run(self) -> None:
        self.mc.play_media(self.stream_url, "audio/mpeg")
        self.mc.block_until_active()
        time.sleep(2)

        while not self.stop_event.is_set():
            if (
                self.loop
                and self.mc.status
                and self.mc.status.player_state == "IDLE"
                and self.mc.status.idle_reason == "FINISHED"
            ):
                print(f"[{self.cast.name}] Looping...")
                self.mc.play_media(self.stream_url, "audio/mpeg")
                self.mc.block_until_active()
            time.sleep(1)

        try:
            self.mc.update_status()
        except PyChromecastError as err:
            logger.debug(
                "[%s] Unable to refresh status before stopping: %s", self.cast.name, err
            )
        status = self.mc.status

        if status and status.player_state != "IDLE":
            try:
                self.mc.stop()
            except RequestFailed:
                logger.warning(
                    "[%s] Stop command rejected; media session already inactive",
                    self.cast.name,
                )
            except (NotConnected, PyChromecastError) as err:
                logger.warning(
                    "[%s] Stop command failed due to connection issue: %s",
                    self.cast.name,
                    err,
                )
        print(f"[{self.cast.name}] Playback stopped.")

    def stop(self) -> None:
        self.stop_event.set()


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


def _check_playback_threads() -> Tuple[bool, str]:
    with state_lock:
        snapshot = list(cast_threads.items())

    inactive = [name for name, thread in snapshot if not thread.is_alive()]
    if inactive:
        return False, f"Playback threads not running: {', '.join(inactive)}"

    return True, "All playback threads healthy"


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
        playback_ok, playback_detail = _check_playback_threads()
        checks["playback_threads"] = {"ok": playback_ok, "detail": playback_detail}
        ok = ok and playback_ok

    return ok, checks


@app.route("/")
def index() -> str:
    return render_template("index.html")


@app.route("/status")
def get_status() -> Response:
    with discovery_lock:
        device_names = sorted(discovered_casts.keys())
    with state_lock:
        active_device = active_cast_name
        volume_snapshot = dict(volume_by_device)
    currently_playing = (
        {"device": active_device, "volume": volume_snapshot.get(active_device)}
        if active_device
        else None
    )
    return jsonify(
        {
            "devices": device_names,
            "playing_device": active_device,
            "volumes": volume_snapshot,
            "currently_playing": currently_playing,
        }
    )


@app.route("/play", methods=["POST"])
def play() -> Response:
    global active_cast_name
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

    # Stop all other playing threads to ensure only one stream is active
    with state_lock:
        for name, thread in list(cast_threads.items()):
            if name != device_name:
                print(f"Stopping playback on {name} to switch to {device_name}")
                thread.stop()
                thread.join()
                del cast_threads[name]

        # Stop the current thread if it exists, to restart it
        if device_name in cast_threads:
            cast_threads[device_name].stop()
            cast_threads[device_name].join()

    cast.wait()
    print(f"[{device_name}] Quitting current app to ensure clean state.")
    cast.quit_app()
    time.sleep(1)
    cast.set_volume(volume)
    with state_lock:
        volume_by_device[device_name] = volume

    if STREAM_URL:
        stream_url = STREAM_URL
    else:
        ip_address = get_local_ip()
        stream_url = f"http://{ip_address}:{PORT}/stream"

    thread = CastThread(cast, stream_url, loop)
    cast_threads[device_name] = thread
    thread.start()

    with state_lock:
        active_cast_name = device_name

    return jsonify({"status": "playing"})


@app.route("/stop", methods=["POST"])
def stop() -> Response:
    global active_cast_name
    data = request.json
    if not data:
        return make_response(
            jsonify({"status": "error", "message": "Invalid request"}), 400
        )
    device_name = data.get("device_name")

    if device_name in cast_threads:
        cast_threads[device_name].stop()
        cast_threads[device_name].join()
        del cast_threads[device_name]

    cast = get_cast(device_name)
    if cast:
        cast.wait()
        cast.quit_app()

    with state_lock:
        if active_cast_name == device_name:
            active_cast_name = None

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
    with state_lock:
        volume_by_device[device_name] = volume
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
