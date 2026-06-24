# Use an official Python runtime as a parent image
FROM python:3.14-slim-bookworm

# Bring in uv directly from its official image -- no pip bootstrap needed.
# The pinned tag is kept up to date by Renovate (Docker manager).
COPY --from=ghcr.io/astral-sh/uv:0.11.24 /uv /uvx /bin/

# Set the working directory in the container
WORKDIR /app

# uv build tuning: compile bytecode for faster cold starts and copy (don't
# hardlink) packages out of the cache mount to avoid cross-filesystem warnings.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# Install runtime dependencies in an isolated layer for better caching.
# --no-install-project: the app runs as a module (app:app), not an installed
# package. A BuildKit cache mount keeps the uv cache out of the image layer.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project

# Copy the rest of the application's code to the working directory
COPY . .

# Run inside the uv-managed virtual environment.
ENV PATH="/app/.venv/bin:${PATH}"

# Make port 8000 available to the world outside this container
EXPOSE 8000

# Define environment variable
ENV FLASK_APP=app.py

# Run app.py with gunicorn when the container launches (long timeout for streaming).
# MUST stay at a single worker: CastManager is an in-process singleton (mDNS discovery,
# device registry, playback threads all live in process memory). A second worker means a
# second process with its own divergent state and competing cast connections, which breaks
# discovery/playback. Use threads (not workers) for request concurrency.
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "2", "--timeout", "1800", "--graceful-timeout", "1800", "app:app"]
