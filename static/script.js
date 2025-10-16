document.addEventListener('DOMContentLoaded', () => {
    const devicesSelect = document.getElementById('devices');
    const refreshButton = document.getElementById('refresh');
    const volumeSlider = document.getElementById('volume');
    const volumeLabel = document.getElementById('volume-label');
    const loopCheckbox = document.getElementById('loop');
    const playButton = document.getElementById('play');
    const stopButton = document.getElementById('stop');
    const statusDiv = document.getElementById('status');

    const updateVolumeLabel = () => {
        volumeLabel.textContent = `${volumeSlider.value}%`;
    };

    const updateStatus = async () => {
        statusDiv.textContent = 'Scanning for devices...';
        try {
            const response = await fetch('/status');
            const data = await response.json();
            console.log("Received data:", data); // Log the data for debugging

            const currentSelection = devicesSelect.value;
            
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
            } else if (data.playing_device) {
                devicesSelect.value = data.playing_device;
            } else if (currentSelection && data.devices.includes(currentSelection)) {
                devicesSelect.value = currentSelection;
            }

            statusDiv.textContent = 'Scan complete.';
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
                    volume: parseInt(volumeSlider.value, 10) / 100,
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

    const setVolume = async () => {
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
                    volume: parseInt(volumeSlider.value, 10) / 100,
                }),
            });
        } catch (error) {
            statusDiv.textContent = 'Error updating volume.';
            console.error(error);
        }
    };

    refreshButton.addEventListener('click', updateStatus);
    volumeSlider.addEventListener('input', updateVolumeLabel);
    volumeSlider.addEventListener('change', setVolume);
    playButton.addEventListener('click', () => controlPlayback('play'));
    stopButton.addEventListener('click', () => controlPlayback('stop'));

    // Initial load
    updateVolumeLabel();
    updateStatus();
});