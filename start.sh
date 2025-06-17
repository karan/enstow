#!/bin/bash

# Enable verbose output and exit on error for debugging
set -eux

# Set default cron schedule if not provided
CRON_SCHEDULE=${CRON_SCHEDULE:-"0 2 * * *"}
# Using the passed TIMEZONE environment variable.
# If it's empty for some reason, it will default to UTC (fallback, but should be set by docker-compose)
TIMEZONE=${TIMEZONE:-"UTC"}
BACKUP_DIR=${BACKUP_DIR:-"/backups"}
CONFIG_FILE_PATH=${CONFIG_FILE_PATH:-"/app/config.yaml"}

# Configure timezone directly using symlink, which is more reliable.
echo "$TIMEZONE" > /etc/timezone
ln -sf /usr/share/zoneinfo/"$TIMEZONE" /etc/localtime
echo "Final date/time after timezone configuration:"
date

# Verify the current timezone
FINAL_TZ_SET=$(cat /etc/timezone)
if [ "$FINAL_TZ_SET" != "$TIMEZONE" ]; then
    echo "ERROR: Timezone expected to be $TIMEZONE but found $FINAL_TZ_SET after full configuration!" >&2
fi
echo

echo "Configuring cron job for backup_script.py with schedule: '${CRON_SCHEDULE}'"

# Create the cron job file inside /etc/cron.d
# Explicitly pass necessary environment variables to the cron job.
echo "SHELL=/bin/bash" > /etc/cron.d/enstow
echo "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" >> /etc/cron.d/enstow
echo "TIMEZONE=\"${TIMEZONE}\"" >> /etc/cron.d/enstow
echo "BACKUP_DIR=\"${BACKUP_DIR}\"" >> /etc/cron.d/enstow
echo "CONFIG_FILE_PATH=\"${CONFIG_FILE_PATH}\"" >> /etc/cron.d/enstow
echo "${CRON_SCHEDULE} root /usr/local/bin/python3 /app/backup_script.py >> /var/log/cron/enstow.log 2>&1" >> /etc/cron.d/enstow

# Give appropriate permissions to the cron job file
chmod 0644 /etc/cron.d/enstow

# Ensure the log directory exists before cron starts trying to write to it
mkdir -p /var/log/cron
touch /var/log/cron/enstow.log

# Start the cron daemon in the foreground
echo "Starting cron daemon in foreground..."
exec cron -f

# The script should not reach here if `crond -f` successfully starts and keeps the container running.
# If for some reason `crond -f` fails, this provides a fallback to keep the container alive for debugging.
tail -f /dev/null
