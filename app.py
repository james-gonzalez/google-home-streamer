import atexit
import logging
import os
import socket
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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
from pychromecast.discovery import CastBrowser, CastListener
from pychromecast.error import NotConnected, PyChromecastError, RequestFailed
from pychromecast.models import CastInfo
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
BASE_DIR: Path = Path(__file__).resolve().parent

HealthReport = Dict[str, Dict[str, Any]]


# --- Utility ---


def get_local_ip() -> str:
    """Return the LAN IP of this host using a UDP probe."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip


# --- Playback Thread ---


class CastThread(threading.Thread):
    """Background thread that streams audio to a single Chromecast device."""

    def __init__(
        self, cast: "pychromecast.Chromecast", stream_url: str, loop: bool
    ) -> None:
        super().__init__(daemon=True)
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
                logger.info("[%s] Looping...", self.cast.name)
                self.mc.play_media(self.stream_url, "audio/mpeg")
                self.mc.block_until_active()
            time.sleep(1)

        try:
            self.mc.update_status()
        except PyChromecastError as err:
            logger.debug(
                "[%s] Unable to refresh status before stopping: %s",
                self.cast.name,
                err,
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
        logger.info("[%s] Playback stopped.", self.cast.name)

    def request_stop(self) -> None:
        """Signal the thread to stop (non-blocking)."""
        self.stop_event.set()


# --- Cast Manager ---


class CastManager:
    """Thread-safe manager for Chromecast discovery, playback, and state."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cast_threads: Dict[str, CastThread] = {}
        self._discovered_casts: Dict[str, Any] = {}
        self._browser: Optional[CastBrowser] = None
        self._zconf: Optional[Zeroconf] = None
        self._active_cast_name: Optional[str] = None
        self._volume_by_device: Dict[str, float] = {}

    # -- Discovery --

    def start_discovery(self) -> None:
        """Start the background mDNS discovery browser."""
        logger.info("Starting background device discovery...")
        zconf = Zeroconf()
        listener = _CastListener(self)
        browser = pychromecast.CastBrowser(listener, zconf)  # type: ignore[arg-type]
        browser.start_discovery()
        with self._lock:
            self._zconf = zconf
            self._browser = browser

    def stop_discovery(self) -> None:
        """Stop the background discovery browser and release resources."""
        with self._lock:
            browser = self._browser
            zconf = self._zconf
        if browser:
            logger.info("Stopping background device discovery...")
            browser.stop_discovery()
        if zconf:
            zconf.close()

    def _on_cast_discovered(self, uuid: str) -> None:
        """Called by the listener when a cast device is found or updated."""
        with self._lock:
            if self._browser:
                cast = self._browser.devices[uuid]  # type: ignore[index]
                if cast.friendly_name:
                    self._discovered_casts[cast.friendly_name] = cast
                    logger.info("Discovered: %s", cast.friendly_name)

    def _on_cast_removed(self, cast_info: CastInfo) -> None:
        """Called by the listener when a cast device disappears."""
        with self._lock:
            if cast_info.friendly_name in self._discovered_casts:
                del self._discovered_casts[cast_info.friendly_name]
                logger.info("Removed: %s", cast_info.friendly_name)

    # -- Device access --

    def get_discovered_devices(self) -> List[str]:
        """Return a sorted list of discovered device names."""
        with self._lock:
            return sorted(self._discovered_casts.keys())

    def get_cast(self, name: str) -> Optional["pychromecast.Chromecast"]:
        """Get a Chromecast connection from a friendly name."""
        with self._lock:
            cast_info = self._discovered_casts.get(name)
            zconf = self._zconf
        if cast_info and zconf:
            return pychromecast.get_chromecast_from_cast_info(cast_info, zconf)
        return None

    # -- Playback --

    def play(
        self,
        device_name: str,
        volume: float,
        loop: bool,
        stream_url: str,
    ) -> Tuple[bool, str]:
        """Start playback on *device_name*, stopping any other active streams.

        Returns ``(success, message)``.
        """
        cast = self.get_cast(device_name)
        if not cast:
            return False, "Device not found"

        # Collect threads to stop while holding the lock, then join outside.
        threads_to_stop: List[Tuple[str, CastThread]] = []
        with self._lock:
            for name, thread in list(self._cast_threads.items()):
                if name != device_name:
                    logger.info(
                        "Stopping playback on %s to switch to %s",
                        name,
                        device_name,
                    )
                    threads_to_stop.append((name, thread))
                    del self._cast_threads[name]
            if device_name in self._cast_threads:
                threads_to_stop.append(
                    (device_name, self._cast_threads.pop(device_name))
                )

        # Join outside the lock to avoid deadlock.
        for _name, thread in threads_to_stop:
            thread.request_stop()
            thread.join(timeout=10)

        cast.wait()
        logger.info("[%s] Quitting current app to ensure clean state.", device_name)
        cast.quit_app()
        time.sleep(1)
        cast.set_volume(volume)

        new_thread = CastThread(cast, stream_url, loop)
        with self._lock:
            self._cast_threads[device_name] = new_thread
            self._volume_by_device[device_name] = volume
            self._active_cast_name = device_name
        new_thread.start()

        return True, "playing"

    def stop(self, device_name: str) -> str:
        """Stop playback on *device_name*."""
        thread: Optional[CastThread] = None
        with self._lock:
            thread = self._cast_threads.pop(device_name, None)

        if thread:
            thread.request_stop()
            thread.join(timeout=10)

        cast = self.get_cast(device_name)
        if cast:
            cast.wait()
            cast.quit_app()

        with self._lock:
            if self._active_cast_name == device_name:
                self._active_cast_name = None

        return "stopped"

    def set_volume(self, device_name: str, volume: float) -> Tuple[bool, str]:
        """Set volume on *device_name*. Returns ``(success, message)``."""
        cast = self.get_cast(device_name)
        if not cast:
            return False, "Device not found"

        logger.info("[%s] Setting volume to %s", device_name, volume)
        cast.wait()
        cast.set_volume(volume)
        with self._lock:
            self._volume_by_device[device_name] = volume
        return True, "volume updated"

    # -- Status --

    @property
    def active_device(self) -> Optional[str]:
        with self._lock:
            return self._active_cast_name

    @property
    def volumes(self) -> Dict[str, float]:
        with self._lock:
            return dict(self._volume_by_device)

    @property
    def cast_threads_snapshot(self) -> Dict[str, CastThread]:
        with self._lock:
            return dict(self._cast_threads)

    def fetch_device_status(self, name: str) -> Optional[Dict[str, Any]]:
        """Return current status dict for *name*, or ``None`` on failure."""
        with self._lock:
            thread = self._cast_threads.get(name)

        if thread:
            try:
                thread.mc.update_status()
                return self._build_status_dict(thread.mc.status, thread.cast.status)
            except PyChromecastError as err:
                logger.debug("Unable to fetch status from thread for %s: %s", name, err)

        cast = self.get_cast(name)
        if not cast:
            return None
        try:
            cast.wait(timeout=3)
            mc = cast.media_controller
            mc.update_status()
            return self._build_status_dict(mc.status, cast.status)
        except PyChromecastError as err:
            logger.debug("Unable to fetch status for %s: %s", name, err)
        return None

    # -- Health --

    def check_discovery(self) -> Tuple[bool, str]:
        """Return ``(ok, detail)`` for the discovery subsystem."""
        with self._lock:
            current_browser = self._browser
            current_zconf = self._zconf

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

    def check_playback_threads(self) -> Tuple[bool, str]:
        """Return ``(ok, detail)`` for active playback threads."""
        with self._lock:
            snapshot = list(self._cast_threads.items())

        inactive = [name for name, thread in snapshot if not thread.is_alive()]
        if inactive:
            return False, f"Playback threads not running: {', '.join(inactive)}"

        return True, "All playback threads healthy"

    # -- Helpers --

    @staticmethod
    def _build_status_dict(media_status: Any, cast_status: Any) -> Dict[str, Any]:
        """Build a normalised status dict from pychromecast status objects."""
        player_state = media_status.player_state if media_status else None
        is_playing = player_state and player_state != "IDLE"
        return {
            "volume": cast_status.volume_level if cast_status else None,
            "player_state": player_state,
            "content_id": media_status.content_id if media_status else None,
            "title": getattr(media_status, "title", None) if media_status else None,
            "is_playing": bool(is_playing),
        }


class _CastListener(CastListener):
    """Listener that delegates to a :class:`CastManager`."""

    def __init__(self, manager: CastManager) -> None:
        self._manager = manager

    def add_cast(self, uuid: str, service: Any) -> None:
        self._manager._on_cast_discovered(uuid)

    def update_cast(self, uuid: str, service: Any) -> None:
        self._manager._on_cast_discovered(uuid)

    def remove_cast(self, uuid: str, service: Any, cast_info: CastInfo) -> None:  # type: ignore[override]
        self._manager._on_cast_removed(cast_info)


# --- Singleton manager instance ---
cast_manager = CastManager()


# --- Health helpers ---


def _check_stream_asset() -> Tuple[bool, str]:
    file_path = BASE_DIR / FILE_NAME
    if not file_path.is_file():
        return False, f"Stream asset {FILE_NAME} not found"
    if not os.access(file_path, os.R_OK):
        return False, f"Stream asset {FILE_NAME} is not readable"
    if file_path.stat().st_size == 0:
        return False, f"Stream asset {FILE_NAME} is empty"

    return True, "Stream asset available"


def _evaluate_health(include_readiness_checks: bool) -> Tuple[bool, HealthReport]:
    checks: HealthReport = {}
    ok = True

    discovery_ok, discovery_detail = cast_manager.check_discovery()
    checks["chromecast_discovery"] = {"ok": discovery_ok, "detail": discovery_detail}
    ok = ok and discovery_ok

    asset_ok, asset_detail = _check_stream_asset()
    checks["stream_asset"] = {"ok": asset_ok, "detail": asset_detail}
    ok = ok and asset_ok

    if include_readiness_checks:
        playback_ok, playback_detail = cast_manager.check_playback_threads()
        checks["playback_threads"] = {"ok": playback_ok, "detail": playback_detail}
        ok = ok and playback_ok

    return ok, checks


# --- Flask Routes ---


@app.route("/")
def index() -> str:
    return render_template("index.html")


@app.route("/status")
def get_status() -> Response:
    device_names = cast_manager.get_discovered_devices()
    active_device = cast_manager.active_device
    volume_snapshot = cast_manager.volumes

    statuses: Dict[str, Any] = {}
    currently_playing = None
    for name in device_names:
        status = cast_manager.fetch_device_status(name)
        if status:
            statuses[name] = status
            if status.get("volume") is not None:
                volume_snapshot[name] = status["volume"]
            if not currently_playing and status.get("is_playing"):
                currently_playing = {
                    "device": name,
                    "volume": status.get("volume"),
                    "title": status.get("title"),
                }
    if not currently_playing and active_device:
        currently_playing = {
            "device": active_device,
            "volume": volume_snapshot.get(active_device),
            "title": None,
        }

    return jsonify(
        {
            "devices": device_names,
            "playing_device": active_device,
            "volumes": volume_snapshot,
            "currently_playing": currently_playing,
            "statuses": statuses,
        }
    )


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

    # Validate volume
    try:
        volume = float(volume)
    except (TypeError, ValueError):
        return make_response(
            jsonify({"status": "error", "message": "Invalid volume value"}), 400
        )
    volume = max(0.0, min(1.0, volume))

    if STREAM_URL:
        stream_url = STREAM_URL
    else:
        ip_address = get_local_ip()
        stream_url = f"http://{ip_address}:{PORT}/stream"

    success, message = cast_manager.play(device_name, volume, loop, stream_url)
    if not success:
        return make_response(jsonify({"status": "error", "message": message}), 404)
    return jsonify({"status": message})


@app.route("/stop", methods=["POST"])
def stop() -> Response:
    data = request.json
    if not data:
        return make_response(
            jsonify({"status": "error", "message": "Invalid request"}), 400
        )
    device_name = data.get("device_name")

    message = cast_manager.stop(device_name)
    return jsonify({"status": message})


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

    # Validate volume
    try:
        volume = float(volume)
    except (TypeError, ValueError):
        return make_response(
            jsonify({"status": "error", "message": "Invalid volume value"}), 400
        )
    volume = max(0.0, min(1.0, volume))

    success, message = cast_manager.set_volume(device_name, volume)
    if not success:
        return make_response(jsonify({"status": "error", "message": message}), 404)
    return jsonify({"status": message})


@app.route("/stream")
def stream_file() -> Response:
    return send_from_directory(str(BASE_DIR), FILE_NAME)


@app.route("/health/live")
def health_live() -> Tuple[Response, int]:
    healthy, checks = _evaluate_health(include_readiness_checks=False)
    status = "ok" if healthy else "error"
    code = 200 if healthy else 500
    return jsonify({"status": status, "checks": checks}), code


@app.route("/health/ready")
def health_ready() -> Tuple[Response, int]:
    healthy, checks = _evaluate_health(include_readiness_checks=True)
    status = "ready" if healthy else "unready"
    code = 200 if healthy else 503
    return jsonify({"status": status, "checks": checks}), code


# --- Application Startup ---
if AUTO_START_DISCOVERY:
    cast_manager.start_discovery()
    atexit.register(cast_manager.stop_discovery)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=True, use_reloader=False)
