# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install deps (runtime + dev)
uv sync --extra dev --no-install-project

# Run app (dev)
python app.py          # http://127.0.0.1:8000

# Lint
.venv/bin/ruff check .
.venv/bin/ruff format --check .

# Tests
.venv/bin/pytest                        # all
.venv/bin/pytest tests/test_app.py::test_health_live_ok  # single test
```

## Architecture

Single-file Flask app (`app.py`) with one gunicorn worker in production.

**Core classes:**
- `CastManager` — thread-safe singleton managing all Chromecast state: mDNS discovery (via `CastBrowser`/`Zeroconf`), playback lifecycle, volume, and health checks. All public methods acquire `_lock`; never hold the lock while calling `thread.join()` (deadlock risk).
- `CastThread` — daemon thread per active device; handles play/loop/stop lifecycle against `pychromecast.MediaController`. Stopped via `request_stop()` + `join()`, never killed.
- `_CastListener` — thin adapter bridging `pychromecast.CastListener` callbacks into `CastManager`.

**Key flows:**
- Device discovery starts at import time unless `AUTO_START_DISCOVERY=0` (used in tests). Devices are keyed by `friendly_name`.
- `POST /play` resolves the stream URL: uses `STREAM_URL` env var if set, otherwise constructs `http://<LAN-IP>:8000/stream` pointing at the bundled `white-noise-20m.mp3`.
- Only one device plays at a time — starting playback on device B stops any thread running on device A.
- `/health/live` checks discovery thread + audio asset; `/health/ready` adds active playback thread liveness.

**Deployment:** Kubernetes `deployment.yaml` uses `hostNetwork: true` — mandatory for mDNS device discovery to work. `STREAM_URL` must be set to a routable address (not localhost) when running in a container.

**Releases:** Conventional commits on `main` trigger semantic-release (GitHub Actions). `beta` branch produces pre-releases.
