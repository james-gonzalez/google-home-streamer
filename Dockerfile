# Use an official Python runtime as a parent image
FROM python:3.13-slim-bookworm

# Set the working directory in the container
WORKDIR /app

# Install uv so we can sync dependencies from pyproject metadata
RUN python -m pip install --upgrade pip && \
    pip install --no-cache-dir uv

# Copy dependency metadata separately for better layer caching
COPY pyproject.toml uv.lock ./

# Sync only runtime dependencies into a virtual environment
RUN uv sync --frozen --no-install-project && \
    rm -rf ~/.cache/uv

# Copy the rest of the application's code to the working directory
COPY . .

# Ensure the uv-managed virtual environment is used
ENV VIRTUAL_ENV=/app/.venv
ENV PATH="/app/.venv/bin:${PATH}"

# Make port 8000 available to the world outside this container
EXPOSE 8000

# Define environment variable
ENV FLASK_APP app.py

# Run app.py with gunicorn when the container launches (long timeout for streaming).
# MUST stay at a single worker: CastManager is an in-process singleton (mDNS discovery,
# device registry, playback threads all live in process memory). A second worker means a
# second process with its own divergent state and competing cast connections, which breaks
# discovery/playback. Use threads (not workers) for request concurrency.
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "2", "--timeout", "1800", "--graceful-timeout", "1800", "app:app"]
