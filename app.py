import logging
import os
import socket
import threading
import time

import pychromecast
from flask import Flask, jsonify, render_template, request, send_from_directory
from zeroconf import Zeroconf

# Suppress excessive zeroconf logging
logging.basicConfig(level=logging.INFO)
logging.getLogger("zeroconf").setLevel(logging.ERROR)

app = Flask(__name__)

FILE_NAME = "whitenoise.aac"
PORT = 8000

# --- Global state for discovery and playback ---
cast_threads = {}
discovered_casts = {}
browser = None
discovery_lock = threading.Lock()
zconf = None
active_cast_name = None
state_lock = threading.Lock()

class MyCastListener(pychromecast.CastListener):
    """Listener for discovering and removing Chromecasts."""
    def add_cast(self, uuid, service):
        with discovery_lock:
            # The browser object is needed to get the full cast info
            cast = browser.devices[uuid]
            discovered_casts[cast.friendly_name] = cast
            print(f"Discovered: {cast.friendly_name}")

    def update_cast(self, uuid, service):
        self.add_cast(uuid, service)

    def remove_cast(self, uuid, service, cast):
        with discovery_lock:
            if cast.friendly_name in discovered_casts:
                del discovered_casts[cast.friendly_name]
                print(f"Removed: {cast.friendly_name}")

def start_discovery():
    """Starts the background discovery browser."""
    global browser, zconf
    print("Starting background device discovery...")
    zconf = Zeroconf()
    listener = MyCastListener()
    browser = pychromecast.CastBrowser(listener, zconf)
    listener.browser = browser  # Give listener a reference to the browser
    browser.start_discovery()

def stop_discovery():
    """Stops the background discovery browser."""
    if browser:
        print("Stopping background device discovery...")
        browser.stop_discovery()
    if zconf:
        zconf.close()

def get_cast(name):
    """Get a Chromecast object from a friendly name."""
    with discovery_lock:
        cast_info = discovered_casts.get(name)
    if cast_info:
        return pychromecast.get_chromecast_from_cast_info(cast_info, zconf)
    return None

# --- Utility and Playback Thread ---

def get_local_ip():
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
    def __init__(self, cast, stream_url, loop):
        super().__init__()
        self.cast = cast
        self.stream_url = stream_url
        self.loop = loop
        self.mc = cast.media_controller
        self.stop_event = threading.Event()

    def run(self):
        self.mc.play_media(self.stream_url, "audio/aac")
        self.mc.block_until_active()
        time.sleep(2)

        while not self.stop_event.is_set():
            if (
                self.loop
                and self.mc.status.player_state == "IDLE"
                and self.mc.status.idle_reason == "FINISHED"
            ):
                print(f"[{self.cast.name}] Looping...")
                self.mc.play_media(self.stream_url, "audio/aac")
                self.mc.block_until_active()
            time.sleep(1)

        if self.mc.status.player_state != 'IDLE':
            self.mc.stop()
        print(f"[{self.cast.name}] Playback stopped.")

    def stop(self):
        self.stop_event.set()

# --- Flask Routes ---

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/status")
def get_status():
    with discovery_lock:
        device_names = sorted(discovered_casts.keys())
    with state_lock:
        active_device = active_cast_name
    return jsonify({"devices": device_names, "playing_device": active_device})

@app.route("/play", methods=["POST"])
def play():
    global active_cast_name
    data = request.json
    device_name = data.get("device_name")
    volume = data.get("volume", 0.1)
    loop = data.get("loop", True)

    cast = get_cast(device_name)
    if not cast:
        return jsonify({"status": "error", "message": "Device not found"}), 404

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

    ip_address = get_local_ip()
    stream_url = f"http://{ip_address}:{PORT}/stream"

    thread = CastThread(cast, stream_url, loop)
    cast_threads[device_name] = thread
    thread.start()

    with state_lock:
        active_cast_name = device_name

    return jsonify({"status": "playing"})

@app.route("/stop", methods=["POST"])
def stop():
    global active_cast_name
    data = request.json
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
def set_volume():
    data = request.json
    device_name = data.get("device_name")
    volume = data.get("volume")

    cast = get_cast(device_name)
    if not cast:
        return jsonify({"status": "error", "message": "Device not found"}), 404

    print(f"[{device_name}] Setting volume to {volume}")
    cast.wait()
    cast.set_volume(volume)
    return jsonify({"status": "volume updated"})

@app.route("/stream")
def stream_file():
    return send_from_directory(os.getcwd(), FILE_NAME)

if __name__ == "__main__":
    start_discovery()
    import atexit
    atexit.register(stop_discovery)
    app.run(host="0.0.0.0", port=PORT, debug=True, use_reloader=False)
