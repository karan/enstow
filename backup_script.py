import os
import datetime
import pytz
import docker
import subprocess
import time
import tarfile
import io
import yaml
import sys
import requests
import uuid # For generating run_id for Healthchecks.io

# --- Configuration (loaded from Environment Variables and YAML) ---
# BACKUP_DIR: The directory inside the container where backups will be stored.
#             This should be mounted to a host directory.
BACKUP_DIR = os.getenv('BACKUP_DIR', '/backups')

# TIMEZONE: The timezone to use for timestamps in backup filenames and for purging.
#           e.g., "America/Los_Angeles", "UTC", "Europe/Berlin".
TIMEZONE = os.getenv('TIMEZONE', 'UTC')

# PURGE_DAYS: Number of days to keep old backups. Backups older than this will be deleted.
#             Set to '0' to disable purging.
PURGE_DAYS = int(os.getenv('PURGE_DAYS', '7'))

# CONFIG_FILE_PATH: Path to the YAML configuration file inside the container.
CONFIG_FILE_PATH = os.getenv('CONFIG_FILE_PATH', '/app/config.yaml')

# Global Healthchecks.io URL (loaded from config.yaml)
GLOBAL_HEALTHCHECK_URL = None

# --- Logging Helper ---
def _log(message, level="info", indent_level=0, file=sys.stdout):
    """Prints a log message with optional indentation."""
    indent = "  " * indent_level
    print(f"{indent}{message}", file=file)

# --- Load DATABASE_CONFIG from YAML file ---
DATABASE_CONFIG = []
try:
    if not os.path.exists(CONFIG_FILE_PATH):
        _log(f"Error: Configuration file not found at {CONFIG_FILE_PATH}. Exiting.", level="error", file=sys.stderr)
        sys.exit(1)
    with open(CONFIG_FILE_PATH, 'r') as f:
        config_data = yaml.safe_load(f)
        DATABASE_CONFIG = config_data.get('databases', [])
        # Override env vars if set in config.yaml
        if 'timezone' in config_data:
            TIMEZONE = config_data['timezone']
        if 'purge_days' in config_data:
            PURGE_DAYS = int(config_data['purge_days'])
        # New: Load global Healthchecks.io URL
        GLOBAL_HEALTHCHECK_URL = config_data.get('healthcheck_url')

except yaml.YAMLError as e:
    _log(f"Error parsing YAML configuration file: {e}. Exiting.", level="error", file=sys.stderr)
    sys.exit(1)
except Exception as e:
    _log(f"An unexpected error occurred while loading config: {e}. Exiting.", level="error", file=sys.stderr)
    sys.exit(1)

# --- Docker Client Setup ---
try:
    client = docker.from_env()
except docker.errors.DockerException as e:
    _log(f"Error connecting to Docker daemon: {e}", level="error", file=sys.stderr)
    _log("Ensure /var/run/docker.sock is mounted correctly in your docker-compose.yml.", level="error", file=sys.stderr)
    sys.exit(1)

def _ping_healthchecks(base_url: str, endpoint_type: str = "success", message: str = "", run_id: str = None):
    """
    Sends a ping to Healthchecks.io.
    base_url: The full Healthchecks.io URL (e.g., https://hc-ping.com/<uuid>)
    endpoint_type: "start", "success", "fail", "log"
    message: Optional message for "fail" or "log" endpoints.
    run_id: Optional UUID to link start/end pings.
    """
    if not base_url:
        return
    
    url = base_url
    
    if endpoint_type == "start":
        url += "/start"
    elif endpoint_type == "success":
        # No suffix needed for success (root UUID URL)
        pass
    elif endpoint_type == "fail":
        url += "/fail"
    elif endpoint_type == "log":
        url += "/log"
    else:
        _log(f"Warning: Invalid Healthchecks endpoint type '{endpoint_type}'. Skipping ping.", level="warning", file=sys.stderr)
        return

    params = {'rid': run_id} if run_id else {}

    try:
        if endpoint_type in ["fail", "log"] and message:
            response = requests.post(url, data=message.encode('utf-8'), params=params, timeout=10)
        else:
            response = requests.get(url, params=params, timeout=10)
        
        response.raise_for_status() # Raise HTTPError for bad responses (4xx or 5xx)
        _log(f"Healthchecks.io '{endpoint_type}' ping to {url} successful.", indent_level=1)
    except requests.exceptions.RequestException as e:
        _log(f"Error pinging Healthchecks.io {url} ({endpoint_type}): {e}", level="error", file=sys.stderr)

def get_current_timestamp():
    """
    Returns a timezone-aware timestamp string for use in backup filenames.
    The timezone is determined by the TIMEZONE environment variable/config.
    """
    try:
        tz = pytz.timezone(TIMEZONE)
    except pytz.UnknownTimeZoneError:
        _log(f"Warning: Unknown timezone '{TIMEZONE}'. Defaulting to 'UTC'.", level="warning", file=sys.stderr)
        tz = pytz.timezone('UTC')
    now = datetime.datetime.now(tz)
    return now.strftime("%Y%m%d_%H%M%S_%Z")

def _get_file_size_mb(filepath):
    """Returns file size in megabytes."""
    try:
        return os.path.getsize(filepath) / (1024 * 1024)
    except OSError:
        return 0

def _execute_in_container_and_stream(container, command, environment, backup_file_path, db_name, indent_level):
    """
    Executes a command inside a Docker container and streams its output to a gzipped file.
    """
    try:
        _log(f"Executing command in container '{container.name}': {' '.join(command)}", indent_level=indent_level + 1)
        exec_result = container.exec_run(command, stream=True, demux=False, environment=environment)
        
        with open(backup_file_path, 'wb') as f_out:
            with subprocess.Popen(['gzip'], stdin=subprocess.PIPE, stdout=f_out) as gzip_process:
                for chunk in exec_result.output:
                    gzip_process.stdin.write(chunk)
                gzip_process.stdin.close()
        _log(f"Backup for '{db_name}' saved to {backup_file_path}", indent_level=indent_level + 1)
        return True
    except Exception as e:
        _log(f"Error executing command or compressing output for '{db_name}': {e}", level="error", indent_level=indent_level + 1, file=sys.stderr)
        return False

def _copy_from_container_and_gzip(container, path_in_container, backup_file_path, db_name, indent_level):
    """
    Copies a file from a Docker container, decompresses if it's a tar stream,
    and then gzipps and saves it to the host.
    """
    try:
        _log(f"Copying file from container '{container.name}': {path_in_container}", indent_level=indent_level + 1)
        st, stat = container.get_archive(path_in_container)
        tar_stream = io.BytesIO(b"".join(st))
        
        with tarfile.open(fileobj=tar_stream, mode='r') as tar:
            extracted_file_obj = None
            for member in tar.getmembers():
                if member.isfile() and member.name.endswith(os.path.basename(path_in_container)):
                    extracted_file_obj = tar.extractfile(member)
                    break
            
            if extracted_file_obj:
                with open(backup_file_path, 'wb') as f_out:
                    with subprocess.Popen(['gzip'], stdin=subprocess.PIPE, stdout=f_out) as gzip_process:
                        gzip_process.stdin.write(extracted_file_obj.read())
                        gzip_process.stdin.close()
                _log(f"Backup for '{db_name}' saved to {backup_file_path}", indent_level=indent_level + 1)
                return True
            else:
                _log(f"Error: Could not find '{os.path.basename(path_in_container)}' within the tar archive from '{path_in_container}'.", level="error", indent_level=indent_level + 1, file=sys.stderr)
                return False

    except Exception as e:
        _log(f"Error copying or compressing file from container for '{db_name}': {e}", level="error", indent_level=indent_level + 1, file=sys.stderr)
        return False

def _get_container_object(container_target, indent_level):
    """Retrieves a Docker container object by name."""
    try:
        return client.containers.get(container_target)
    except docker.errors.NotFound:
        _log(f"Error: Target container '{container_target}' not found.", level="error", indent_level=indent_level + 1, file=sys.stderr)
        return None
    except docker.errors.APIError as e:
        _log(f"Error accessing Docker API for container '{container_target}': {e}", level="error", indent_level=indent_level + 1, file=sys.stderr)
        return None
    except Exception as e:
        _log(f"An unexpected error occurred while getting container '{container_target}': {e}", level="error", indent_level=indent_level + 1, file=sys.stderr)
        return None

def _backup_mariadb_mysql(db_config, output_dir, backup_filename_base, indent_level):
    """Handles MariaDB/MySQL backup using mysqldump."""
    user = db_config.get('user')
    password = db_config.get('password')
    database = db_config.get('database')
    dump_args = db_config.get('dump_args', '--single-transaction --skip-dump-date')
    container_target = db_config.get('container_name') or db_config.get('host')
    db_name = db_config.get('name', 'unknown_db')

    if not all([user, password, database, container_target]):
        raise ValueError(f"Missing required config for MariaDB/MySQL '{db_name}': user, password, database, or container_name/host.")

    container = _get_container_object(container_target, indent_level)
    if not container:
        raise RuntimeError(f"Could not get container object for '{container_target}'.")

    backup_file = os.path.join(output_dir, f"{backup_filename_base}.sql.gz")
    command = ['mysqldump'] + dump_args.split() + ['-u', user, database]
    environment = {'MYSQL_PWD': password}
    return _execute_in_container_and_stream(container, command, environment, backup_file, db_name, indent_level)

def _backup_postgres(db_config, output_dir, backup_filename_base, indent_level):
    """Handles PostgreSQL backup using pg_dump."""
    user = db_config.get('user')
    password = db_config.get('password')
    database = db_config.get('database')
    dump_args = db_config.get('dump_args', '')
    container_target = db_config.get('container_name') or db_config.get('host')
    db_name = db_config.get('name', 'unknown_db')

    if not all([user, password, database, container_target]):
        raise ValueError(f"Missing required config for PostgreSQL '{db_name}': user, password, database, or container_name/host.")

    container = _get_container_object(container_target, indent_level)
    if not container:
        raise RuntimeError(f"Could not get container object for '{container_target}'.")

    backup_file = os.path.join(output_dir, f"{backup_filename_base}.dump.gz")
    command = ['pg_dump'] + dump_args.split() + ['-U', user, '-d', database]
    environment = {'PGPASSWORD': password}
    return _execute_in_container_and_stream(container, command, environment, backup_file, db_name, indent_level)

def _backup_sqlite(db_config, output_dir, backup_filename_base, indent_level):
    """Handles SQLite backup by copying the .db file."""
    path_in_container = db_config.get('path_in_container')
    container_target = db_config.get('container_name')
    db_name = db_config.get('name', 'unknown_db')

    if not all([path_in_container, container_target]):
        raise ValueError(f"Missing required config for SQLite '{db_name}': path_in_container or container_name.")
    
    container = _get_container_object(container_target, indent_level)
    if not container:
        raise RuntimeError(f"Could not get container object for '{container_target}'.")

    backup_file = os.path.join(output_dir, f"{backup_filename_base}.db.gz")
    return _copy_from_container_and_gzip(container, path_in_container, backup_file, db_name, indent_level)

def _backup_valkey_redis(db_config, output_dir, backup_filename_base, indent_level):
    """Handles Valkey/Redis backup using BGSAVE and copying the RDB file."""
    password = db_config.get('password')
    rdb_path_in_container = db_config.get('rdb_path_in_container', '/data/dump.rdb')
    container_target = db_config.get('container_name')
    db_name = db_config.get('name', 'unknown_db')

    if not container_target:
        raise ValueError(f"Missing required config for Valkey/Redis '{db_name}': container_name.")

    container = _get_container_object(container_target, indent_level)
    if not container:
        raise RuntimeError(f"Could not get container object for '{container_target}'.")

    # Trigger BGSAVE
    redis_cli_cmd = ['redis-cli']
    if password:
        redis_cli_cmd.extend(['-a', password])
    redis_cli_cmd.append('BGSAVE')

    try:
        _log(f"Triggering BGSAVE in container '{container_target}'...", indent_level=indent_level + 1)
        exec_result = container.exec_run(redis_cli_cmd)
        if exec_result.exit_code != 0:
            error_msg = f"Error triggering BGSAVE for '{db_name}': {exec_result.output.decode()}"
            _log(error_msg, level="error", indent_level=indent_level + 1, file=sys.stderr)
            raise RuntimeError(error_msg)
        _log(f"BGSAVE command executed. Waiting for RDB file to be ready (5 seconds).", indent_level=indent_level + 1)
        time.sleep(5) # Give Valkey/Redis some time to finish writing the RDB file

        backup_file = os.path.join(output_dir, f"{backup_filename_base}.rdb.gz")
        return _copy_from_container_and_gzip(container, rdb_path_in_container, backup_file, db_name, indent_level)

    except Exception as e:
        raise RuntimeError(f"Error during Valkey/Redis BGSAVE or file copy for '{db_name}': {e}")

def run_backup():
    """
    Main function to orchestrate the database backup process.
    Iterates through the DATABASE_CONFIG and backs up each specified database.
    """
    timestamp = get_current_timestamp()
    run_id = str(uuid.uuid4()) # Generate a unique run ID for Healthchecks.io
    
    _log("**************************************************")
    _log(f"*** Starting database backup process at {timestamp} ***")
    _log("**************************************************")

    # Ping Healthchecks.io start for the entire backup process
    _ping_healthchecks(GLOBAL_HEALTHCHECK_URL, "start", f"Starting global backup process at {timestamp}.", run_id)

    all_backups_successful = True # Track overall success
    new_files_created = []
    total_new_size_mb = 0

    for db_config in DATABASE_CONFIG:
        db_type = db_config.get('type')
        db_name = db_config.get('name', 'unknown_db')
        
        _log(f"\n--- Processing database: {db_name} ({db_type}) ---", indent_level=0)

        if not db_type:
            _log(f"Skipping malformed DB config: {db_config}. Missing 'type'.", level="error", indent_level=1, file=sys.stderr)
            all_backups_successful = False
            continue

        output_dir = os.path.join(BACKUP_DIR, db_type, db_name)
        os.makedirs(output_dir, exist_ok=True)

        backup_filename_base = f"{db_name}-{timestamp}"
        
        backup_successful_this_db = False
        error_message = ""
        current_backup_file = None

        try:
            if db_type == 'mariadb' or db_type == 'mysql':
                backup_successful_this_db = _backup_mariadb_mysql(db_config, output_dir, backup_filename_base, 1)
                current_backup_file = os.path.join(output_dir, f"{backup_filename_base}.sql.gz")
            elif db_type == 'postgres':
                backup_successful_this_db = _backup_postgres(db_config, output_dir, backup_filename_base, 1)
                current_backup_file = os.path.join(output_dir, f"{backup_filename_base}.dump.gz")
            elif db_type == 'sqlite':
                backup_successful_this_db = _backup_sqlite(db_config, output_dir, backup_filename_base, 1)
                current_backup_file = os.path.join(output_dir, f"{backup_filename_base}.db.gz")
            elif db_type == 'valkey' or db_type == 'redis':
                backup_successful_this_db = _backup_valkey_redis(db_config, output_dir, backup_filename_base, 1)
                current_backup_file = os.path.join(output_dir, f"{backup_filename_base}.rdb.gz")
            else:
                error_message = f"Unknown database type: {db_type}. Skipping backup for '{db_name}'."
                _log(error_message, level="error", indent_level=1, file=sys.stderr)
                backup_successful_this_db = False
        except (ValueError, RuntimeError) as e:
            error_message = f"Backup for {db_name} failed: {e}"
            _log(error_message, level="error", indent_level=1, file=sys.stderr)
            backup_successful_this_db = False
        except Exception as e:
            error_message = f"An unexpected error occurred during backup for {db_name}: {e}"
            _log(error_message, level="error", indent_level=1, file=sys.stderr)
            backup_successful_this_db = False
        
        if backup_successful_this_db:
            if current_backup_file and os.path.exists(current_backup_file):
                new_files_created.append(current_backup_file)
                total_new_size_mb += _get_file_size_mb(current_backup_file)
            _log(f"Backup for '{db_name}' finished successfully.", indent_level=1)
            _ping_healthchecks(GLOBAL_HEALTHCHECK_URL, "log", f"SUCCESS: Backup for {db_name} completed.", run_id)
        else:
            all_backups_successful = False
            _log(f"Backup for '{db_name}' failed.", level="error", indent_level=1, file=sys.stderr)
            _ping_healthchecks(GLOBAL_HEALTHCHECK_URL, "log", f"FAILURE: Backup for {db_name} failed with error: {error_message}", run_id)


    _log(f"\n[{timestamp}] Backup execution phase finished. Starting purge phase...", indent_level=0)
    purged_files, total_purged_size_mb = purge_old_backups(run_id)

    _log("\n**************************************************")
    _log(f"*** Database backup process completed at {timestamp} ***")
    _log("**************************************************")

    _log("\n--- Backup Summary ---")
    _log(f"New files created ({len(new_files_created)}):", indent_level=1)
    if new_files_created:
        for f in new_files_created:
            _log(f"- {os.path.basename(f)} ({_get_file_size_mb(f):.2f} MB)", indent_level=2)
        _log(f"Total new files size: {total_new_size_mb:.2f} MB", indent_level=1)
    else:
        _log("No new files created.", indent_level=2)

    _log(f"\nPurged files ({len(purged_files)}):", indent_level=1)
    if purged_files:
        for f in purged_files:
            _log(f"- {os.path.basename(f)}", indent_level=2)
        _log(f"Total purged files size: {total_purged_size_mb:.2f} MB", indent_level=1)
    else:
        _log("No files purged.", indent_level=2)
    _log("--------------------------------------------------")

    # Ping Healthchecks.io success/fail for the entire backup process
    if all_backups_successful:
        _ping_healthchecks(GLOBAL_HEALTHCHECK_URL, "success", f"Global backup process completed successfully at {timestamp}. New: {len(new_files_created)} files ({total_new_size_mb:.2f} MB). Purged: {len(purged_files)} files ({total_purged_size_mb:.2f} MB).", run_id)
    else:
        _ping_healthchecks(GLOBAL_HEALTHCHECK_URL, "fail", f"Global backup process failed for one or more databases at {timestamp}. New: {len(new_files_created)} files ({total_new_size_mb:.2f} MB). Purged: {len(purged_files)} files ({total_purged_size_mb:.2f} MB).", run_id)


def purge_old_backups(run_id: str = None):
    """
    Removes backup files older than PURGE_DAYS from each configured database's specific backup directory.
    Returns a tuple of (list of purged files, total size in MB).
    """
    purged_files = []
    total_purged_size_mb = 0

    if PURGE_DAYS <= 0:
        _log("Backup purging is disabled (PURGE_DAYS is 0 or less).", indent_level=1)
        return purged_files, total_purged_size_mb

    _log(f"Purging backups older than {PURGE_DAYS} days...", indent_level=1)
    
    try:
        tz = pytz.timezone(TIMEZONE)
    except pytz.UnknownTimeZoneError:
        _log(f"Warning: Unknown timezone '{TIMEZONE}' for purging. Defaulting to 'UTC'.", level="warning", indent_level=1, file=sys.stderr)
        tz = pytz.timezone('UTC')

    cutoff_date = datetime.datetime.now(tz) - datetime.timedelta(days=PURGE_DAYS)

    for db_config in DATABASE_CONFIG:
        db_type = db_config.get('type')
        db_name = db_config.get('name')

        if not db_type or not db_name:
            _log(f"Skipping purge for malformed DB config: {db_config}. Missing 'type' or 'name'.", level="error", indent_level=2, file=sys.stderr)
            continue
        
        specific_backup_dir = os.path.join(BACKUP_DIR, db_type, db_name)

        if not os.path.exists(specific_backup_dir):
            _log(f"Backup directory '{specific_backup_dir}' for '{db_name}' does not exist. Skipping purge for this database.", indent_level=2)
            continue

        _log(f"Processing '{db_name}' in '{specific_backup_dir}'...", indent_level=2)
        for filename in os.listdir(specific_backup_dir):
            filepath = os.path.join(specific_backup_dir, filename)
            if not os.path.isfile(filepath):
                continue

            try:
                filename_no_gz = filename.removesuffix('.gz')
                last_dot_index = filename_no_gz.rfind('.')
                filename_no_ext = filename_no_gz[:last_dot_index] if last_dot_index != -1 else filename_no_gz

                last_hyphen_index = filename_no_ext.rfind('-')
                
                if last_hyphen_index != -1:
                    datetime_tz_part = filename_no_ext[last_hyphen_index+1:]
                    datetime_parts_split_by_underscore = datetime_tz_part.split('_')
                    
                    if len(datetime_parts_split_by_underscore) >= 2:
                        datetime_part = '_'.join(datetime_parts_split_by_underscore[:2])
                        file_datetime = datetime.datetime.strptime(datetime_part, "%Y%m%d_%H%M%S").replace(tzinfo=tz)

                        if file_datetime < cutoff_date:
                            _log(f"Purging old backup: {filename}", indent_level=3)
                            size_before_delete = _get_file_size_mb(filepath)
                            os.remove(filepath)
                            purged_files.append(filepath)
                            total_purged_size_mb += size_before_delete
                    else:
                        _log(f"Warning: Filename format for timestamp (YYYYMMDD_HHMMSS_TZ) is unexpected in '{filename}'. Skipping purge for this file.", level="warning", indent_level=3, file=sys.stderr)
                else:
                    _log(f"Warning: Filename does not contain expected timestamp separator '-' in '{filename}'. Skipping purge for this file.", level="warning", indent_level=3, file=sys.stderr)

            except (ValueError, IndexError) as e:
                _log(f"Warning: Could not parse date from filename '{filename}' for purging: {e}. Skipping purge for this file.", level="warning", indent_level=3, file=sys.stderr)
            except Exception as e:
                _log(f"Error purging file '{filepath}': {e}", level="error", indent_level=3, file=sys.stderr)
    
    _log(f"Purge phase completed.", indent_level=1)
    return purged_files, total_purged_size_mb

if __name__ == '__main__':
    run_backup()
