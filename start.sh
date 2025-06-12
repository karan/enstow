#!/bin/bash
# This script is the entrypoint for the Docker container.
# It dynamically creates the crontab entry based on the CRON_SCHEDULE environment variable,
# sets the container's timezone, and then starts the cron daemon in the foreground.

# Generate crontab entry dynamically.
# The cron schedule and the path to the Python script are injected from environment variables.
# The output of the script (stdout and stderr) is redirected to a log file.
# PYTHONUNBUFFERED is now set in the Dockerfile directly.
echo "SHELL=/bin/bash" > /etc/cron.d/db-backup-cron
echo "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" >> /etc/cron.d/db-backup-cron
echo "${CRON_SCHEDULE} root /usr/local/bin/python3 /app/backup_script.py >> /var/log/cron/db_backup.log 2>&1" >> /etc/cron.d/db-backup-cron

# Ensure the crontab file has the correct permissions (readable by root).
chmod 0644 /etc/cron.d/db-backup-cron

# Set the container's timezone.
# This ensures that timestamps in backup filenames and purging logic are consistent with the desired timezone.
# The timezone is now primarily determined by the 'timezone' field in config.yaml, or the TIMEZONE env var.
# We still set tzdata here in case config.yaml doesn't specify 'timezone'.
if [ -n "$TIMEZONE" ]; then
    echo "$TIMEZONE" > /etc/timezone
    dpkg-reconfigure --frontend noninteractive tzdata
fi


# Start the cron daemon in the foreground (-f).
# This keeps the container running and allows cron to execute scheduled jobs.
cron -f
