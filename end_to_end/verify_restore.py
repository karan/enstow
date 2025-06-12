import os
import sys
import subprocess
import json
import time

# --- Console Colors ---
GREEN='\033[0;32m'
RED='\033[0;31m'
BLUE='\033[0;34m'
YELLOW='\033[0;33m'
NC='\033[0m' # No Color

def log_info(message):
    print(f"{BLUE}[VERIFY INFO]{NC} {message}")

def log_success(message):
    print(f"{GREEN}[VERIFY SUCCESS]{NC} {message}")

def log_fail(message):
    print(f"{RED}[VERIFY FAIL]{NC} {message}")

def run_docker_exec(container_name, command_list, check_exit_code=True, capture_output=True):
    """
    Runs a command inside a Docker container using `docker exec`.
    Returns (stdout, stderr, returncode).
    """
    full_command = ["docker", "exec", "-T", container_name] + command_list
    log_info(f"Executing in container '{container_name}': {' '.join(command_list)}")
    try:
        result = subprocess.run(
            full_command,
            capture_output=capture_output,
            text=True, # Decode stdout/stderr as text
            check=False # Do not raise CalledProcessError automatically
        )
        
        # Always log stdout/stderr, even if empty, for debugging
        log_info(f"  RAW STDOUT (from {container_name}): '{result.stdout.strip()}'")
        log_info(f"  RAW STDERR (from {container_name}): '{result.stderr.strip()}'")

        if check_exit_code and result.returncode != 0:
            log_fail(f"Command failed with exit code {result.returncode}")
            return result.stdout, result.stderr, result.returncode
        return result.stdout, result.stderr, result.returncode
    except FileNotFoundError:
        log_fail(f"Error: 'docker' command not found. Is Docker installed and in PATH?")
        return "", "docker command not found", 127
    except Exception as e:
        log_fail(f"An unexpected error occurred while running docker exec: {e}")
        return "", str(e), 1

def verify_mariadb_restore():
    log_info("Verifying MariaDB restore...")
    mariadb_container_name = "mariadb-test-e2e"
    password = os.getenv("MARIADB_PASSWORD")

    # Verify table and data count
    stdout, stderr, retcode = run_docker_exec(
        mariadb_container_name,
        ["mysql", "-h", "127.0.0.1", "-u", "root", f"-p{password}", "test_db", "-e", "SELECT COUNT(*) FROM users;"]
    )
    if retcode != 0:
        log_fail("MariaDB verification failed (count query).")
        return False
    
    # Expected count: 2 users
    count = int(stdout.strip().split('\n')[-1])
    if count != 2:
        log_fail(f"MariaDB verification failed: Expected 2 users, got {count}.")
        return False
    
    log_success("MariaDB restore verified successfully.")
    return True

def verify_postgres_restore():
    log_info("Verifying PostgreSQL restore...")
    postgres_container_name = "postgres-test-e2e"
    user = "pguser"
    db_name = "pg_test_db"
    password = os.getenv("POSTGRES_PASSWORD")

    # Set PGPASSWORD env var for psql command within the container's exec context
    command = ["psql", "-U", user, "-d", db_name, "-c", "SELECT COUNT(*) FROM customers;"]
    stdout, stderr, retcode = run_docker_exec(
        postgres_container_name,
        command,
        check_exit_code=True # Changed to True to ensure exit code is checked
    )
    if retcode != 0:
        log_fail("PostgreSQL verification failed (count query).")
        return False
    
    # Example stdout: " count \n-----\n    2\n(1 row)\n"
    # Find the count line and extract the number
    count_line = [line for line in stdout.splitlines() if line.strip().isdigit()][0]
    count = int(count_line.strip())

    if count != 2:
        log_fail(f"PostgreSQL verification failed: Expected 2 customers, got {count}.")
        return False

    log_success("PostgreSQL restore verified successfully.")
    return True

def check_initial_sqlite_data():
    """
    Verifies that the initial_data_sqlite.db file is a valid SQLite database.
    This runs BEFORE backup/restore tests to ensure the source data isn't corrupted.
    """
    log_info("Checking initial SQLite data sanity...")
    sqlite_container_name = "sqlite-app-e2e"
    initial_db_path = "/app/data/e2e_sqlite.db" # Assuming this is where initial data is copied

    # Give it a moment to ensure the container is fully up and apk install finishes
    log_info("Waiting briefly for sqlite-app-e2e to ensure initial data is accessible...")
    time.sleep(5) 

    # Verify the database file exists inside the container
    stdout, stderr, retcode = run_docker_exec(
        sqlite_container_name,
        ["test", "-f", initial_db_path]
    )
    if retcode != 0:
        log_fail(f"Initial SQLite data check failed: Database file '{initial_db_path}' not found or accessible inside container.")
        return False
    log_info(f"Initial SQLite database file '{initial_db_path}' exists.")

    # Check file details
    stdout, stderr, retcode = run_docker_exec(
        sqlite_container_name,
        ["ls", "-l", initial_db_path]
    )
    if retcode != 0:
        log_fail(f"Failed to get file details for initial {initial_db_path}.")
        return False
    log_info(f"  Initial file details: {stdout.strip()}")

    # Check file header
    stdout, stderr, retcode = run_docker_exec(
        sqlite_container_name,
        ["head", "-c", "16", initial_db_path]
    )
    if retcode != 0:
        log_fail(f"Failed to read header of initial {initial_db_path}.")
        return False
    header = stdout.strip()
    log_info(f"  Initial file header (first 16 bytes): '{header}'")
    if not header.startswith("SQLite format 3"):
        log_fail(f"Initial SQLite data check failed: File header does not indicate a valid SQLite database. Found: '{header}'")
        return False
    
    # Get file size
    stdout, stderr, retcode = run_docker_exec(
        sqlite_container_name,
        ["stat", "-c", "%s", initial_db_path]
    )
    if retcode != 0:
        log_fail(f"Failed to get file size for initial {initial_db_path}.")
        return False
    file_size = int(stdout.strip())
    log_info(f"  Initial file size: {file_size} bytes")
    if file_size == 0:
        log_fail(f"Initial SQLite data check failed: Initial database file is empty ({file_size} bytes).")
        return False

    # Attempt to dump schema of initial DB
    log_info("Attempting to dump initial SQLite schema...")
    stdout, stderr, retcode = run_docker_exec(
        sqlite_container_name,
        ["sqlite3", initial_db_path, ".schema"]
    )
    if retcode != 0:
        log_fail(f"Initial SQLite data check failed: Could not read database schema. Error: {stderr.strip()}")
        return False
    log_info(f"  Initial schema dump successful (showing first 100 chars): {stdout.strip()[:100]}...")

    log_success("Initial SQLite data verified successfully.")
    return True


def verify_sqlite_restore():
    log_info("Verifying SQLite restore...")
    sqlite_container_name = "sqlite-app-e2e"
    sqlite_db_path = "/app/data/e2e_sqlite.db"

    # Add a small delay to ensure sqlite3 installation/container readiness
    log_info("Waiting for SQLite container to be fully ready (briefly)...")
    time.sleep(2) # Give it 2 seconds

    # Verify the database file exists inside the container
    stdout, stderr, retcode = run_docker_exec(
        sqlite_container_name,
        ["test", "-f", sqlite_db_path] # 'test -f' checks if file exists
    )
    if retcode != 0:
        log_fail(f"SQLite verification failed: Database file '{sqlite_db_path}' not found or accessible inside container.")
        return False
    
    log_info(f"SQLite database file '{sqlite_db_path}' exists. Checking file details of restored DB...")

    # Check file details (permissions, size)
    stdout, stderr, retcode = run_docker_exec(
        sqlite_container_name,
        ["ls", "-l", sqlite_db_path]
    )
    if retcode != 0:
        log_fail(f"Failed to get file details for {sqlite_db_path}.")
        return False
    log_info(f"  Restored File details: {stdout.strip()}")

    # Check file header (first 16 bytes for "SQLite format 3")
    stdout, stderr, retcode = run_docker_exec(
        sqlite_container_name,
        ["head", "-c", "16", sqlite_db_path]
    )
    if retcode != 0:
        log_fail(f"Failed to read header of {sqlite_db_path}.")
        return False
    header = stdout.strip()
    log_info(f"  Restored File header (first 16 bytes): '{header}'")
    if not header.startswith("SQLite format 3"):
        log_fail(f"SQLite verification failed: Restored file header does not indicate a valid SQLite database. Found: '{header}'")
        return False

    # Get file size using stat, for more robust check than ls -l parsing
    stdout, stderr, retcode = run_docker_exec(
        sqlite_container_name,
        ["stat", "-c", "%s", sqlite_db_path] # %s prints size in bytes
    )
    if retcode != 0:
        log_fail(f"Failed to get file size for {sqlite_db_path}.")
        return False
    file_size = int(stdout.strip())
    log_info(f"  Restored File size: {file_size} bytes")
    if file_size == 0:
        log_fail(f"SQLite verification failed: Restored database file is empty ({file_size} bytes).")
        return False


    # Attempt to dump schema to check if it's a valid SQLite DB
    log_info("Attempting to dump SQLite schema of restored DB...")
    stdout, stderr, retcode = run_docker_exec(
        sqlite_container_name,
        ["sqlite3", sqlite_db_path, ".schema"]
    )
    if retcode != 0:
        log_fail(f"SQLite verification failed: Could not read database schema. Error: {stderr.strip()}")
        return False
    log_info(f"  Restored Schema dump successful (showing first 100 chars): {stdout.strip()[:100]}...")


    # Verify table and data count using sqlite3 CLI
    log_info("Verifying data count in restored SQLite DB...")
    stdout, stderr, retcode = run_docker_exec(
        sqlite_container_name,
        ["sqlite3", sqlite_db_path, "SELECT COUNT(*) FROM test_table;"]
    )
    if retcode != 0:
        log_fail(f"SQLite verification failed (count query). Error: {stderr.strip()}")
        return False

    count = int(stdout.strip())
    if count != 3:
        log_fail(f"SQLite verification failed: Expected 3 entries, got {count}.")
        return False
    
    log_success("SQLite restore verified successfully.")
    return True

def verify_valkey_restore():
    log_info("Verifying Valkey/Redis restore...")
    valkey_container_name = "valkey-test-e2e"
    password = os.getenv("VALKEY_PASSWORD")

    # Verify a key exists and its value
    stdout, stderr, retcode = run_docker_exec(
        valkey_container_name,
        ["redis-cli", "-a", password, "GET", "test:key1"]
    )
    if retcode != 0:
        log_fail("Valkey/Redis verification failed (GET test:key1).")
        return False
    
    value = stdout.strip()
    if value != "Redis Value 1":
        log_fail(f"Valkey/Redis verification failed: Expected 'Redis Value 1', got '{value}'.")
        return False

    # Verify list length
    stdout, stderr, retcode = run_docker_exec(
        valkey_container_name,
        ["redis-cli", "-a", password, "LLEN", "test:list"]
    )
    if retcode != 0:
        log_fail("Valkey/Redis verification failed (LLEN test:list).")
        return False

    list_len = int(stdout.strip())
    if list_len != 3:
        log_fail(f"Valkey/Redis verification failed: Expected list length 3, got {list_len}.")
        return False

    log_success("Valkey/Redis restore verified successfully.")
    return True

if __name__ == "__main__":
    log_info("Starting data verification process...")
    
    all_passed = True

    # First, verify the integrity of the initial SQLite data itself
    if not check_initial_sqlite_data():
        log_fail("Initial SQLite data check failed. Aborting verification.")
        sys.exit(1)

    if not verify_mariadb_restore():
        all_passed = False
    
    if not verify_postgres_restore():
        all_passed = False
    
    if not verify_sqlite_restore():
        all_passed = False
    
    if not verify_valkey_restore():
        all_passed = False
    
    if all_passed:
        log_success("All data verification steps passed!")
        sys.exit(0)
    else:
        log_fail("One or more data verification steps failed.")
        sys.exit(1)
