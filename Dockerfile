# Dockerfile for the DB Backup Agent Container (Multi-stage build)

# --- Stage 1: Build Environment for Dependencies and Tests ---
FROM python:3.13-slim-bookworm AS builder

# Set Python output to be unbuffered during build too
ENV PYTHONUNBUFFERED=1

# Install build dependencies for apt packages
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
        default-libmysqlclient-dev \
    && rm -rf /var/lib/apt/lists/*

# Set working directory for Python dependencies
WORKDIR /app

# Copy only requirements to leverage Docker cache
COPY requirements.txt .

# Install Python dependencies, including testing frameworks
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source code for testing
COPY backup_script.py /app/
COPY tests/ /app/tests/
COPY config.yaml /app/


# --- Stage 2: Runtime Environment ---
FROM python:3.13-slim-bookworm

# Set Python output to be unbuffered (as requested, only in Dockerfile)
ENV PYTHONUNBUFFERED=1

# Install necessary database client tools and cron daemon for runtime.
# These tools are used to perform the actual database dumps (mysqldump, pg_dump)
# and to manage the RDB file for Redis/Valkey.
# 'sqlite3' is needed to interact with SQLite databases.
# 'gzip' is used to compress the backup files.
# 'cron' is the scheduling daemon.
# 'tzdata' is important for correct timezone handling.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        default-mysql-client \
        postgresql-client \
        sqlite3 \
        redis-tools \
        gzip \
        cron \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

# Set the working directory inside the container
WORKDIR /app

# Copy Python dependencies from the builder stage
COPY --from=builder /usr/local/lib/python3.13/site-packages/ /usr/local/lib/python3.13/site-packages/
# Copy the Python backup script, the startup script, and the config file into the container.
COPY backup_script.py /app/backup_script.py
COPY start.sh /app/start.sh
COPY config.yaml /app/config.yaml

# Create a directory for cron job logs inside the container.
RUN mkdir -p /var/log/cron

# Make the Python script and the startup script executable.
RUN chmod +x /app/backup_script.py /app/start.sh

# Define default environment variables. These can be overridden in `docker-compose.yml`.
ENV BACKUP_DIR="/backups"
 # Default: daily at 2 AM
ENV CRON_SCHEDULE="0 2 * * *"
 # Path to the YAML config file
ENV CONFIG_FILE_PATH="/app/config.yaml"

# The primary command to run when the container starts.
# It executes `start.sh`, which dynamically sets up cron and then starts the cron daemon.
CMD ["/app/start.sh"]
