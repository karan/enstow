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

# Install cron and tzdata for scheduling and timezone management
# These operations will run as root during build and container startup.
RUN apt-get update && apt-get install -y \
    cron \
    tzdata \
    # Clean up apt cache to keep image size small
    && rm -rf /var/lib/apt/lists/*

# Set working directory inside the container
WORKDIR /app

# Copy installed dependencies from the builder stage
COPY --from=builder /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages

# Copy application code and configuration
COPY backup_script.py .
COPY config.yaml .
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
