# Stage 1: Builder
# Uses a slim Python image to install dependencies efficiently.
FROM python:3.13-slim-bookworm AS builder

# Set working directory inside the container
WORKDIR /app

# Copy the requirements file into the builder stage
COPY requirements.txt .

# Install Python dependencies. --no-cache-dir reduces image size.
# Using python3 -m pip ensures the correct pip for the installed python version.
RUN python3 -m pip install --no-cache-dir -r requirements.txt

# Stage 2: Final Image
# Uses the same slim base image for a small final image.
FROM python:3.13-slim-bookworm

# Install curl, cron and tzdata for downloading, scheduling and timezone management
# These operations will run as root during build and container startup.
RUN apt-get update && apt-get install -y \
    curl \
    cron \
    tzdata \
    # Clean up apt cache to keep image size small
    && rm -rf /var/lib/apt/lists/*

# Set working directory inside the container
WORKDIR /app

# Copy installed dependencies from the builder stage
COPY --from=builder /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages

# --- Download and Validate portable sqlite3 binary ---
# Make the SQLite3 release tag configurable at build time
ARG SQLITE3_RELEASE_TAG="sqlite3-3500100-ec62881dc1ee8ce932eb7694ea80d8df9e54f48908864ea13b2c67c5aa84987f"
ENV SQLITE3_BINARY_URL="https://github.com/karan/enstow/releases/download/${SQLITE3_RELEASE_TAG}/sqlite3"
ENV SQLITE3_SHA256_CHECKSUM="ec62881dc1ee8ce932eb7694ea80d8df9e54f48908864ea13b2c67c5aa84987f"
ENV SQLITE3_DEST_PATH="/usr/local/bin/sqlite3_portable_backup"

RUN set -eux; \
    echo "Downloading sqlite3 binary from ${SQLITE3_BINARY_URL}"; \
    curl -LO "${SQLITE3_BINARY_URL}"; \
    echo "${SQLITE3_SHA256_CHECKSUM} sqlite3" | sha256sum -c -; \
    mv sqlite3 "${SQLITE3_DEST_PATH}"; \
    chmod +x "${SQLITE3_DEST_PATH}"; \
    echo "Successfully downloaded, validated, and installed sqlite3 binary."

# Copy application code and configuration
COPY backup_script.py .
COPY start.sh .

# Make the start.sh script executable
RUN chmod +x start.sh

# The container will run as root, which has permissions for cron and timezone setup.
USER root

# Environment variable to ensure Python's stdout/stderr are unbuffered,
# which is important for seeing logs in real-time in Docker.
ENV PYTHONUNBUFFERED=1

# Define the entrypoint script. This script will be executed when the container starts.
ENTRYPOINT ["/app/start.sh"]
