from flask import Flask, render_template, jsonify, request, send_from_directory
import pychromecast
import threading
import time
import os
import socket

app = Flask(__name__)

FILE_NAME = "whitenoise.aac"
PORT = 8000

# Global state
casts = {}
cast_threads = {}
browser = None

def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('10.255.255.255', 1))
        IP = s.getsockname()[0]
    except Exception:
        IP = '127.0.0.1'
    finally:
        s.close()
    return IP

def discover_devices():
    global casts, browser
    print("Starting device discovery...")
    if browser:
        pychromecast.discovery.stop_discovery(browser)
    chromecasts, browser = pychromecast.get_chromecasts()
    casts = {cc.name: cc for cc in chromecasts}
    print(f"Found devices: {[cc.name for cc in chromecasts]}")

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
        time.sleep(2)  # Allow time for playback to start

        while not self.stop_event.is_set():
            if self.loop and self.mc.status.player_state == 'IDLE' and self.mc.status.idle_reason == 'FINISHED':
                print(f"[{self.cast.name}] Looping...")
                self.mc.play_media(self.stream_url, "audio/aac")
                self.mc.block_until_active()
            time.sleep(1)
        self.mc.stop()
        print(f"[{self.cast.name}] Playback stopped.")

    def stop(self):
        self.stop_event.set()

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/devices')
def get_devices():
    return jsonify([{'name': name} for name in casts.keys()])

@app.route('/play', methods=['POST'])
def play():
    data = request.json
    device_name = data.get('device_name')
    volume = data.get('volume', 0.1)
    loop = data.get('loop', True)

    cast = casts.get(device_name)
    if not cast:
        return jsonify({'status': 'error', 'message': 'Device not found'}), 404

    if device_name in cast_threads:
        cast_threads[device_name].stop()
        cast_threads[device_name].join()  # Wait for the old thread to finish

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

    return jsonify({'status': 'playing'})

@app.route('/stop', methods=['POST'])
def stop():
    data = request.json
    device_name = data.get('device_name')

    if device_name in cast_threads:
        cast_threads[device_name].stop()
        cast_threads[device_name].join()  # Wait for the old thread to finish
        del cast_threads[device_name]

    return jsonify({'status': 'stopped'})

@app.route('/volume', methods=['POST'])
def set_volume():
    data = request.json
    device_name = data.get('device_name')
    volume = data.get('volume')

    cast = casts.get(device_name)
    if not cast:
        return jsonify({'status': 'error', 'message': 'Device not found'}), 404

    print(f"[{device_name}] Setting volume to {volume}")
    cast.set_volume(volume)
    return jsonify({'status': 'volume updated'})

@app.route('/stream')
def stream_file():
    return send_from_directory(os.getcwd(), FILE_NAME)

if __name__ == '__main__':
    discover_devices()
    app.run(host='0.0.0.0', port=PORT, debug=True)