document.addEventListener('DOMContentLoaded', () => {
    const devicesSelect = document.getElementById('devices');
    const refreshButton = document.getElementById('refresh');
    const volumeUp = document.getElementById('volume-up');
    const volumeDown = document.getElementById('volume-down');
    const nowPlaying = document.getElementById('now-playing');
    const nowVolume = document.getElementById('now-volume');
    const deviceCount = document.getElementById('device-count');
    const loopCheckbox = document.getElementById('loop');
    const playButton = document.getElementById('play');
    const stopButton = document.getElementById('stop');
    const statusDiv = document.getElementById('status');
    let volumeValue = 10;
    let holdTimer = null;
    let holdInterval = null;
    let pendingVolumeRequest = null;
    let playingDevice = null;

    const updateVolumeLabel = () => {
        if (playingDevice && playingDevice === devicesSelect.value) {
            nowVolume.textContent = `${volumeValue}%`;
        }
    };

    const sendVolume = async () => {
        const device = devicesSelect.value;
        if (!device) {
            return;
        }
        try {
            await fetch('/volume', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({
                    device_name: device,
                    volume: volumeValue / 100,
                }),
            });
        } catch (error) {
            statusDiv.textContent = 'Error updating volume.';
            console.error(error);
        }
    };

    const scheduleVolumeUpdate = () => {
        if (pendingVolumeRequest) {
            clearTimeout(pendingVolumeRequest);
        }
        pendingVolumeRequest = setTimeout(() => {
            pendingVolumeRequest = null;
            void sendVolume();
        }, 150);
    };

    const changeVolume = (delta) => {
        const next = Math.max(0, Math.min(100, volumeValue + delta));
        if (next === volumeValue) return;
        volumeValue = next;
        updateVolumeLabel();
        if (playingDevice && playingDevice === devicesSelect.value) {
            nowVolume.textContent = `${volumeValue}%`;
        }
        scheduleVolumeUpdate();
    };

    const startHold = (delta) => {
        holdTimer = setTimeout(() => {
            holdInterval = setInterval(() => changeVolume(delta * 5), 120);
        }, 1000);
    };

    const stopHold = () => {
        if (holdTimer) {
            clearTimeout(holdTimer);
            holdTimer = null;
        }
        if (holdInterval) {
            clearInterval(holdInterval);
            holdInterval = null;
        }
    };

    const updateStatus = async () => {
        statusDiv.textContent = 'Scanning for devices...';
        try {
            const response = await fetch('/status');
            const data = await response.json();
            console.log("Received data:", data); // Log the data for debugging

            const currentSelection = devicesSelect.value;
            const volumes = data.volumes || {};
            
            devicesSelect.innerHTML = '';
            data.devices.forEach(device => {
                const option = document.createElement('option');
                option.value = device;
                option.textContent = device;
                devicesSelect.appendChild(option);
            });

            // Set the default selection
            if (data.devices.includes("Alejandro")) {
                devicesSelect.value = "Alejandro";
            } else if (currentSelection && data.devices.includes(currentSelection)) {
                devicesSelect.value = currentSelection;
            } else if (data.devices.length) {
                devicesSelect.value = data.devices[0];
            }

            statusDiv.textContent = 'Scan complete.';
            deviceCount.textContent = `${data.devices.length} found`;

            const playing = data.currently_playing;
            playingDevice = playing && playing.device ? playing.device : null;
            nowPlaying.textContent = playingDevice || 'Nothing playing';
            const playingVolume = playing && typeof playing.volume === 'number' ? Math.round(playing.volume * 100) : null;
            nowVolume.textContent = playingVolume !== null ? `${playingVolume}%` : '--%';

            const selectedVolume = volumes[devicesSelect.value];
            if (typeof selectedVolume === 'number') {
                volumeValue = Math.round(selectedVolume * 100);
                updateVolumeLabel();
            }
        } catch (error) {
            statusDiv.textContent = 'Error finding devices.';
            console.error(error);
        }
    };

    const controlPlayback = async (action) => {
        const device = devicesSelect.value;
        if (!device) {
            statusDiv.textContent = 'Please select a device.';
            return;
        }
        statusDiv.textContent = `${action.charAt(0).toUpperCase() + action.slice(1)}ing...`;
        try {
            await fetch(`/${action}`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({
                    device_name: device,
                    volume: volumeValue / 100,
                    loop: loopCheckbox.checked,
                }),
            });
            statusDiv.textContent = `Playback ${action}ed.`;
            // Refresh the status to update the UI
            await updateStatus();
        } catch (error) {
            statusDiv.textContent = `Error ${action}ing playback.`;
            console.error(error);
        }
    };

    refreshButton.addEventListener('click', updateStatus);
    const attachPressControl = (el, delta) => {
        const onDown = (event) => {
            event.preventDefault();
            changeVolume(delta);
            startHold(delta);
        };
        const onUp = (event) => {
            event.preventDefault();
            stopHold();
        };
        el.addEventListener('pointerdown', onDown);
        ['pointerup', 'pointerleave', 'pointercancel'].forEach(evt => {
            el.addEventListener(evt, onUp);
        });
    };
    attachPressControl(volumeUp, 1);
    attachPressControl(volumeDown, -1);
    playButton.addEventListener('click', () => controlPlayback('play'));
    stopButton.addEventListener('click', () => controlPlayback('stop'));

    // Initial load
    updateStatus();
});
