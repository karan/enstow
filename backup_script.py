import os
import datetime
import pytz
import docker
import subprocess
import tarfile
import io
import yaml
import sys
import requests
import uuid

# --- Global Configuration Variables ---
BACKUP_DIR = '/backups'
TIMEZONE = 'UTC'
PURGE_DAYS = 7
CONFIG_FILE_PATH = '/app/config.yaml'
GLOBAL_HEALTHCHECK_URL = None
DATABASE_CONFIG = []
client = None # Docker client will be initialized dynamically

# --- Logging Helper ---
def _log(message, level="info", indent_level=0, file=sys.stdout):
    """Prints a log message with optional indentation."""
    indent = "  " * indent_level
    print(f"{indent}{message}", file=file)

def _load_configuration():
    """Loads configuration from the YAML file and sets global variables."""
    global BACKUP_DIR, TIMEZONE, PURGE_DAYS, CONFIG_FILE_PATH, GLOBAL_HEALTHCHECK_URL, DATABASE_CONFIG

    # Environment variables act as defaults if not set in YAML
    BACKUP_DIR = os.getenv('BACKUP_DIR', '/backups')
    TIMEZONE = os.getenv('TIMEZONE', 'UTC')
    PURGE_DAYS = int(os.getenv('PURGE_DAYS', '7'))
    CONFIG_FILE_PATH = os.getenv('CONFIG_FILE_PATH', '/app/config.yaml')

    try:
        if not os.path.exists(CONFIG_FILE_PATH):
            raise FileNotFoundError(f"Configuration file not found at {CONFIG_FILE_PATH}")
        
        with open(CONFIG_FILE_PATH, 'r') as f:
            config_data = yaml.safe_load(f)
            
            DATABASE_CONFIG = config_data.get('databases', [])
            
            # Override env vars if set in config.yaml
            if 'timezone' in config_data:
                TIMEZONE = config_data['timezone']
            if 'purge_days' in config_data:
                PURGE_DAYS = int(config_data['purge_days'])
            GLOBAL_HEALTHCHECK_URL = config_data.get('healthcheck_url')

    except FileNotFoundError as e:
        _log(f"Error: {e}. Exiting.", level="error", file=sys.stderr)
        raise # Re-raise for calling context (like run_backup)
    except yaml.YAMLError as e:
        _log(f"Error parsing YAML configuration file: {e}. Exiting.", level="error", file=sys.stderr)
        raise # Re-raise for calling context
    except Exception as e:
        _log(f"An unexpected error occurred while loading config: {e}. Exiting.", level="error", file=sys.stderr)
        raise # Re-raise for calling context

def _initialize_docker_client():
    """Initializes the Docker client."""
    global client
    try:
        client = docker.from_env()
    except docker.errors.DockerException as e:
        _log(f"Error connecting to Docker daemon: {e}", level="error", file=sys.stderr)
        _log("Ensure /var/run/docker.sock is mounted correctly in your docker-compose.yml.", level="error", file=sys.stderr)
        raise # Re-raise for calling context

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
        pass # No suffix needed for success (root UUID URL)
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
        _log(f"Error pinging Healthchecks.io {url} ({endpoint_type}): {e}", level="error", indent_level=1, file=sys.stderr)

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
            # The tar archive from get_archive often contains a directory structure
            # e.g., 'path_in_container/actual_file.db'
            # We need to find the actual file within this structure
            base_filename = os.path.basename(path_in_container)
            found_member = False
            for member in tar.getmembers():
                # Check for the exact file name (e.g., 'dump.db') or the full path (e.g., 'tmp/mydatabase.db')
                if member.isfile() and (member.name == base_filename or member.name.endswith(f'/{base_filename}')):
                    extracted_file_obj = tar.extractfile(member)
                    found_member = True
                    break
            
            if extracted_file_obj:
                with open(backup_file_path, 'wb') as f_out:
                    with subprocess.Popen(['gzip'], stdin=subprocess.PIPE, stdout=f_out) as gzip_process:
                        gzip_process.stdin.write(extracted_file_obj.read())
                        gzip_process.stdin.close()
                _log(f"Backup for '{db_name}' saved to {backup_file_path}", indent_level=indent_level + 1)
                return True
            else:
                _log(f"Error: Could not find '{base_filename}' within the tar archive from '{path_in_container}'. Contents of tar: {[m.name for m in tar.getmembers()]}", level="error", indent_level=indent_level + 1, file=sys.stderr)
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
    """
    Handles SQLite backup by injecting a portable sqlite3 binary into the target container,
    using it to create a consistent backup, then copying it out and cleaning up.
    """
    path_in_container = db_config.get('path_in_container') # Path to the active DB file
    container_target = db_config.get('container_name')
    db_name = db_config.get('name', 'unknown_db')

    if not all([path_in_container, container_target]):
        raise ValueError(f"Missing required config for SQLite '{db_name}': path_in_container or container_name.")
    
    container = _get_container_object(container_target, indent_level)
    if not container:
        raise RuntimeError(f"Could not get container object for '{container_target}'.")

    sqlite_portable_binary_source = "/usr/local/bin/sqlite3_portable_backup" # Path in agent container
    sqlite_exec_path_in_container = "/tmp/sqlite3_exec" # Temporary path for binary in target
    temp_backup_path_in_container = "/tmp/temp_sqlite_backup.db" # Temporary path for backup in target

    # 1. Copy portable sqlite3 binary into the target container
    _log(f"Copying portable sqlite3 binary into '{container_target}'...", indent_level=indent_level + 1)
    try:
        with open(sqlite_portable_binary_source, 'rb') as f:
            binary_data = f.read()
        
        # Create a tar archive on the fly with the binary
        tar_io = io.BytesIO()
        tar = tarfile.open(fileobj=tar_io, mode='w')
        info = tarfile.TarInfo(name=os.path.basename(sqlite_exec_path_in_container))
        info.size = len(binary_data)
        info.mode = 0o755 # Give execute permissions
        tar.addfile(info, io.BytesIO(binary_data))
        tar.close()
        tar_io.seek(0) # Rewind to start of tar archive
        
        container.put_archive(os.path.dirname(sqlite_exec_path_in_container), tar_io) # Put into /tmp/
        _log(f"Portable sqlite3 binary copied to {sqlite_exec_path_in_container} in container.", indent_level=indent_level + 1)
    except Exception as e:
        _log(f"Error copying portable sqlite3 binary: {e}", level="error", indent_level=indent_level + 1, file=sys.stderr)
        raise RuntimeError(f"Failed to copy sqlite3 binary to {container_target}.")

    # 2. Execute the .backup command using the copied binary
    # We don't need chmod +x explicitly if mode 0o755 is set in tarinfo with put_archive
    backup_command = [sqlite_exec_path_in_container, path_in_container, f".backup '{temp_backup_path_in_container}'"]
    _log(f"Creating consistent SQLite backup inside container '{container_target}' at '{temp_backup_path_in_container}'...", indent_level=indent_level + 1)
    
    exec_result = container.exec_run(backup_command)
    if exec_result.exit_code != 0:
        error_msg = f"Error executing sqlite3 .backup command for '{db_name}': {exec_result.output.decode('utf-8', errors='ignore')} (Exit Code: {exec_result.exit_code})"
        _log(error_msg, level="error", indent_level=indent_level + 1, file=sys.stderr)
        # Attempt cleanup of the binary and temp backup even if backup fails
        container.exec_run(["rm", "-f", sqlite_exec_path_in_container, temp_backup_path_in_container])
        raise RuntimeError(error_msg)
    _log("sqlite3 .backup command executed successfully.", indent_level=indent_level + 1)
    
    # 3. Copy the created backup file from inside the container to the host
    backup_file_on_host = os.path.join(output_dir, f"{backup_filename_base}.db.gz")
    copy_success = _copy_from_container_and_gzip(container, temp_backup_path_in_container, backup_file_on_host, db_name, indent_level)
    
    # 4. Clean up the temporary binary and backup file inside the container
    _log(f"Cleaning up temporary files inside container '{container_target}'.", indent_level=indent_level + 1)
    container.exec_run(["rm", "-f", sqlite_exec_path_in_container, temp_backup_path_in_container])
    
    return copy_success

def run_backup():
    """
    Main function to orchestrate the database backup process.
    Iterates through the DATABASE_CONFIG and backs up each specified database.
    """
    # Initialize configuration and Docker client at the start of the run.
    try:
        _load_configuration()
        _initialize_docker_client()
    except (FileNotFoundError, yaml.YAMLError, docker.errors.DockerException, Exception) as e:
        # These exceptions already log an error and suggest exiting.
        # Here we ensure the process exits with a failure code.
        _log(f"Critical startup error: {e}. Exiting backup process.", level="error", file=sys.stderr)
        sys.exit(1)


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
            else:
                error_message = f"Unknown database type: {db_type}. Skipping backup for '{db_name}'."
                _log(error_message, level="error", indent_level=1, file=sys.stderr)
                backup_successful_this_db = False # Explicitly set to False
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
            all_backups_successful = False # Mark overall process as failed
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
