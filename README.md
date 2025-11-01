# Google Home Streamer

A simple web application to stream audio to your Google Home and Chromecast devices. By default, it plays a continuous loop of white noise, perfect for helping babies (and adults!) sleep.

## Features

- **Web-based UI:** Control your speakers from any device with a web browser.
- **Device Discovery:** Automatically finds all Google Home and Chromecast devices on your network.
- **Playback Control:** Play, stop, and adjust the volume.
- **Looping:** Continuously loop audio for uninterrupted playback.
- **Containerized:** Packaged as a multi-platform container image for easy deployment.
- **Automated CI/CD:** Fully automated testing, linting, versioning, and container publishing pipeline using GitHub Actions.

## Getting Started

### Running Locally

> Requires Python 3.11 or newer.

1.  **Clone the repository:**
    ```bash
    git clone https://github.com/james-gonzalez/google-home-streamer.git
    cd google-home-streamer
    ```

2.  **Install dependencies with uv (runtime + dev extras):**
    ```bash
    uv sync --extra dev --no-install-project
    ```
    This will create a `.venv/` folder populated with runtime, lint, and test tools.

3.  **Activate the virtual environment:**
    ```bash
    source .venv/bin/activate
    ```

4.  **Run the application:**
    ```bash
    python app.py
    ```

5.  Open your browser and navigate to `http://127.0.0.1:8000`.

### Running Tests

```bash
uv sync --extra dev --no-install-project
.venv/bin/pytest
```

### Health Checks

The service exposes health probes suitable for container orchestration:

- `GET /health/live` validates the Zeroconf discovery thread and confirms the audio asset is readable. Failures here signal the container should be restarted.
- `GET /health/ready` extends the checks above and ensures any active playback threads are still alive before the instance receives traffic.

### Running with a Container

The application is published as a multi-platform container image to the GitHub Container Registry.

1.  **Pull the latest image:**
    ```bash
    docker pull ghcr.io/james-gonzalez/google-home-streamer:latest
    ```
    *(You can replace `docker` with `podman` or `container`)*

2.  **Run the container:**

    **Important:** You must run the container with host networking enabled for device discovery to work.

    ```bash
    docker run --rm --net=host ghcr.io/james-gonzalez/google-home-streamer:latest
    ```

3.  Open your browser and navigate to `http://<your-host-ip>:8000`.

## CI/CD Pipeline

This project uses a fully automated CI/CD pipeline powered by GitHub Actions. When a commit is pushed to the `main` branch:

1.  **Linting:** The code is checked for style and errors using `ruff`.
2.  **Testing:** The application is tested using `pytest`.
3.  **Semantic Release:** The commit messages are analyzed to automatically determine the next version number. A new GitHub Release is created with a changelog.
4.  **Publish Container:** A multi-platform (amd64, arm64) container image is built and pushed to the GitHub Container Registry, tagged with `latest` and the new version number.

Pull requests also trigger a Kind-based integration workflow that builds the container locally, deploys it to a temporary Kubernetes cluster, and waits for the health probes to succeed before allowing merges.
