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

    const getDevices = async () => {
        statusDiv.textContent = 'Scanning for devices...';
        try {
            const response = await fetch('/devices');
            const devices = await response.json();
            devicesSelect.innerHTML = '';
            devices.forEach(device => {
                const option = document.createElement('option');
                option.value = device.name;
                option.textContent = device.name;
                devicesSelect.appendChild(option);
            });
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
        } catch (error) {
            statusDiv.textContent = `Error ${action}ing playback.`;
            console.error(error);
        }
    };

    const setVolume = async () => {
        const device = devicesSelect.value;
        if (!device) {
            return; // Don't do anything if no device is selected
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

    refreshButton.addEventListener('click', getDevices);
    volumeSlider.addEventListener('input', updateVolumeLabel);
    volumeSlider.addEventListener('change', setVolume); // Send volume on release
    playButton.addEventListener('click', () => controlPlayback('play'));
    stopButton.addEventListener('click', () => controlPlayback('stop'));

    // Initial load
    updateVolumeLabel();
    getDevices();
});