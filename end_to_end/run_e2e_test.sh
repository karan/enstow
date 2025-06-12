#!/bin/bash

# run_e2e_test.sh
# End-to-end test script for the Docker DB Backup Agent.
# This script builds the backup agent image, sets up test databases,
# populates them with data, runs a backup, clears data, restores,
# validates, and cleans up.

set -e # Exit immediately if a command exits with a non-zero status

# --- Console Colors ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

log_info() { echo -e "${BLUE}[INFO]${NC} $1"; }
log_success() { echo -e "${GREEN}[SUCCESS]${NC} $1"; }
log_warning() { echo -e "${YELLOW}[WARNING]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

E2E_DIR="$(dirname "$0")"
PROJECT_ROOT="$(dirname "$E2E_DIR")"
BACKUP_DIR="${PROJECT_ROOT}/backups_e2e" # Dedicated backup dir for E2E tests

# --- Cleanup function ---
cleanup_on_exit() {
    log_info "Running cleanup..."
    log_info "Shutting down Docker Compose services..."
    docker compose -f "${E2E_DIR}/docker-compose.e2e.yml" down -v --remove-orphans || true
    
    if [ -d "$BACKUP_DIR" ]; then
        log_info "Attempting to fix permissions and remove E2E backup directory: ${BACKUP_DIR}"
        # Ensure the directory and its contents are writable by the current user for cleanup
        # This is a fallback in case files were created by root within the container
        sudo chmod -R u+rwX "$BACKUP_DIR" || true # Use sudo for a stronger attempt at fixing permissions
        rm -rf "$BACKUP_DIR"
    fi
    log_info "Cleanup complete."
}

# Register the cleanup function to be called on script exit or interruption
trap cleanup_on_exit EXIT

# --- Start Test ---
log_info "Starting End-to-End Test for DB Backup Agent..."

# 1. Build the main db-backup-agent Docker image
log_info "1. Building db-backup-agent Docker image..."
docker build -t db-backup-agent:e2e-test "${PROJECT_ROOT}"
log_success "db-backup-agent image built."

# Create dedicated backup directory for E2E test
mkdir -p "$BACKUP_DIR"
log_info "Created E2E backup directory: ${BACKUP_DIR}"

# 2. Create test containers for each DB type using docker compose
log_info "2. Bringing up test database containers and backup agent..."
docker compose -f "${E2E_DIR}/docker-compose.e2e.yml" up -d
log_success "Test containers launched."

# IMPORTANT: Add a sleep here to give the db-backup-agent-e2e container time to fully initialize
log_info "Giving db-backup-agent-e2e container a moment to settle..."
sleep 10 # Adjusted sleep time

# Wait for databases to be ready
log_info "Waiting for databases to become healthy..."
# For MariaDB: Explicitly connect to localhost via TCP
docker compose -f "${E2E_DIR}/docker-compose.e2e.yml" exec -T mariadb-test-e2e sh -c 'until mysqladmin ping -h 127.0.0.1 -u root -ptest_mariadb_password --silent; do sleep 1; done'
# For PostgreSQL
docker compose -f "${E2E_DIR}/docker-compose.e2e.yml" exec -T postgres-test-e2e sh -c 'until pg_isready -h localhost -p 5432; do sleep 1; done'
# For Valkey/Redis: Explicitly connect to localhost via TCP and authenticate
docker compose -f "${E2E_DIR}/docker-compose.e2e.yml" exec -T valkey-test-e2e sh -c 'until redis-cli -h 127.0.0.1 -a test_valkey_password ping; do sleep 1; done'
log_success "Databases are ready."

# 3. Populating each DB with some fake data
log_info "3. Populating test databases with fake data..."

# MariaDB
log_info "  Populating MariaDB..."
docker cp "${E2E_DIR}/initial_data_mariadb.sql" mariadb-test-e2e:/tmp/initial_data_mariadb.sql
# Explicitly connect to localhost via TCP
docker compose -f "${E2E_DIR}/docker-compose.e2e.yml" exec -T mariadb-test-e2e mysql -h 127.0.0.1 -u root -ptest_mariadb_password test_db < "${E2E_DIR}/initial_data_mariadb.sql"
log_success "  MariaDB populated."

# PostgreSQL
log_info "  Populating PostgreSQL..."
docker cp "${E2E_DIR}/initial_data_postgres.sql" postgres-test-e2e:/tmp/initial_data_postgres.sql
docker compose -f "${E2E_DIR}/docker-compose.e2e.yml" exec -T postgres-test-e2e psql -U pguser -d pg_test_db -f /tmp/initial_data_postgres.sql
log_success "  PostgreSQL populated."

# SQLite (copying the pre-made DB file)
log_info "  Populating SQLite by copying pre-made DB file..."
docker cp "${E2E_DIR}/initial_data_sqlite.db" sqlite-app-e2e:/app/data/e2e_sqlite.db
log_success "  SQLite populated."

# Valkey/Redis
log_info "  Populating Valkey/Redis..."
# Explicitly connect to localhost via TCP and authenticate
docker compose -f "${E2E_DIR}/docker-compose.e2e.yml" exec -T valkey-test-e2e redis-cli -h 127.0.0.1 -a test_valkey_password < "${E2E_DIR}/initial_data_redis.txt"
log_success "  Valkey/Redis populated."

# 5. Create a fake old backup for purging test
log_info "5. Creating a fake old backup file for purge test..."
DUMMY_OLD_BACKUP_PATH="${BACKUP_DIR}/mariadb/test_mariadb_backup/test_mariadb_backup-20230101_000000_UTC.sql.gz"
mkdir -p "$(dirname "$DUMMY_OLD_BACKUP_PATH")"
echo "This is a dummy old backup file." | gzip > "$DUMMY_OLD_BACKUP_PATH"
log_success "  Dummy old backup created: ${DUMMY_OLD_BACKUP_PATH}"


# 6. Use the db backup container to create backups (one-off run)
log_info "6. Triggering db-backup-agent to create backups..."
docker compose -f "${E2E_DIR}/docker-compose.e2e.yml" exec -T db-backup-agent-e2e python3 /app/backup_script.py
log_success "Backup process completed by agent."

# 7. Validating that backup files were created
log_info "7. Validating created backup files..."
# Check for files created within the last 120 seconds (approx) to account for script execution time
LATEST_BACKUP_MARIADB=$(find "${BACKUP_DIR}/mariadb/test_mariadb_backup/" -type f -name "test_mariadb_backup-*.sql.gz" -newermt "$(date -d '120 seconds ago' '+%Y-%m-%d %H:%M:%S')" | head -n 1)
LATEST_BACKUP_POSTGRES=$(find "${BACKUP_DIR}/postgres/test_postgres_backup/" -type f -name "test_postgres_backup-*.dump.gz" -newermt "$(date -d '120 seconds ago' '+%Y-%m-%d %H:%M:%S')" | head -n 1)
LATEST_BACKUP_SQLITE=$(find "${BACKUP_DIR}/sqlite/test_sqlite_backup/" -type f -name "test_sqlite_backup-*.db.gz" -newermt "$(date -d '120 seconds ago' '+%Y-%m-%d %H:%M:%S')" | head -n 1)
LATEST_BACKUP_VALKEY=$(find "${BACKUP_DIR}/valkey/test_valkey_backup/" -type f -name "test_valkey_backup-*.rdb.gz" -newermt "$(date -d '120 seconds ago' '+%Y-%m-%d %H:%M:%S')" | head -n 1)


if [ -z "$LATEST_BACKUP_MARIADB" ] || [ -z "$LATEST_BACKUP_POSTGRES" ] || \
   [ -z "$LATEST_BACKUP_SQLITE" ] || [ -z "$LATEST_BACKUP_VALKEY" ]; then
    log_error "Failed to find all latest backup files."
    log_error "  MariaDB backup: ${LATEST_BACKUP_MARIADB}"
    log_error "  PostgreSQL backup: ${LATEST_BACKUP_POSTGRES}"
    log_error "  SQLite backup: ${LATEST_BACKUP_SQLITE}"
    log_error "  Valkey backup: ${LATEST_BACKUP_VALKEY}"
    exit 1
fi
log_success "All new backup files found."

# 8. Delete all test data from all DBs
log_info "8. Deleting all test data from databases..."

# MariaDB (truncate tables)
log_info "  Clearing MariaDB data..."
# Explicitly connect to localhost via TCP
docker compose -f "${E2E_DIR}/docker-compose.e2e.yml" exec -T mariadb-test-e2e mysql -h 127.0.0.1 -u root -ptest_mariadb_password test_db -e "TRUNCATE TABLE users; TRUNCATE TABLE products;"
log_success "  MariaDB data cleared."

# PostgreSQL (DROP tables and sequences)
log_info "  Clearing PostgreSQL data by dropping tables and sequences..."
docker compose -f "${E2E_DIR}/docker-compose.e2e.yml" exec -T postgres-test-e2e psql -U pguser -d pg_test_db -c "DROP TABLE IF EXISTS orders CASCADE; DROP TABLE IF EXISTS customers CASCADE; DROP SEQUENCE IF EXISTS customers_customer_id_seq CASCADE; DROP SEQUENCE IF EXISTS orders_order_id_seq CASCADE;"
log_success "  PostgreSQL data cleared."

# SQLite (delete the DB file)
log_info "  Clearing SQLite data by removing DB file..."
docker compose -f "${E2E_DIR}/docker-compose.e2e.yml" exec -T sqlite-app-e2e rm /app/data/e2e_sqlite.db || true # Ignore if file doesn't exist
log_success "  SQLite data cleared."

# Valkey/Redis (flush all)
log_info "  Clearing Valkey/Redis data..."
# Explicitly connect to localhost via TCP and authenticate
docker compose -f "${E2E_DIR}/docker-compose.e2e.yml" exec -T valkey-test-e2e redis-cli -h 127.0.0.1 -a test_valkey_password FLUSHALL
log_success "  Valkey/Redis data cleared."


# 9. Restore the backups
log_info "9. Restoring backups to databases..."

# MariaDB
log_info "  Restoring MariaDB..."
# Explicitly connect to localhost via TCP
gunzip -c "$LATEST_BACKUP_MARIADB" | docker compose -f "${E2E_DIR}/docker-compose.e2e.yml" exec -T -i mariadb-test-e2e mysql -h 127.0.0.1 -u root -ptest_mariadb_password test_db
log_success "  MariaDB restored."

# PostgreSQL
log_info "  Restoring PostgreSQL..."
gunzip -c "$LATEST_BACKUP_POSTGRES" | docker compose -f "${E2E_DIR}/docker-compose.e2e.yml" exec -T -i postgres-test-e2e pg_restore -U pguser -d pg_test_db
log_success "  PostgreSQL restored."

# SQLite (Restore by copying the unzipped DB file into the container)
log_info "  Restoring SQLite..."
gunzip -c "$LATEST_BACKUP_SQLITE" > "${BACKUP_DIR}/temp_sqlite.db"
# Copy the unzipped backup into the container temporarily
docker cp "${BACKUP_DIR}/temp_sqlite.db" sqlite-app-e2e:/tmp/restored_e2e_sqlite.db
# Overwrite the actual DB file inside the container
docker compose -f "${E2E_DIR}/docker-compose.e2e.yml" exec -T sqlite-app-e2e sh -c "cp /tmp/restored_e2e_sqlite.db /app/data/e2e_sqlite.db"
# Clean up temporary file on host and inside container
rm "${BACKUP_DIR}/temp_sqlite.db"
docker compose -f "${E2E_DIR}/docker-compose.e2e.yml" exec -T sqlite-app-e2e rm /tmp/restored_e2e_sqlite.db || true
log_success "  SQLite restored."

# Valkey/Redis
log_info "  Restoring Valkey/Redis..."
gunzip -c "$LATEST_BACKUP_VALKEY" > "${BACKUP_DIR}/temp_valkey.rdb"
docker cp "${BACKUP_DIR}/temp_valkey.rdb" valkey-test-e2e:/data/dump.rdb
rm "${BACKUP_DIR}/temp_valkey.rdb"
# Restart Valkey/Redis to load the new RDB file
docker compose -f "${E2E_DIR}/docker-compose.e2e.yml" restart valkey-test-e2e
log_info "  Waiting for Valkey/Redis to restart and load data..."
# Explicitly connect to localhost via TCP and authenticate
docker compose -f "${E2E_DIR}/docker-compose.e2e.yml" exec -T valkey-test-e2e sh -c 'until redis-cli -h 127.0.0.1 -a test_valkey_password ping; do sleep 1; done'
log_success "  Valkey/Redis restored."

# 10. Validate that the backups were restored properly
log_info "10. Validating restored data..."
# Run the verification script inside the dedicated Docker Compose service
docker compose -f "${E2E_DIR}/docker-compose.e2e.yml" run --rm verify-restore-service
VERIFICATION_STATUS=$?

if [ "$VERIFICATION_STATUS" -ne 0 ]; then
    log_error "Data verification FAILED. Restored data does not match original."
    exit 1
fi
log_success "Data verification PASSED. All data restored correctly."

# 11. Ensure that purging worked
log_info "11. Verifying that purging worked..."
if [ -f "$DUMMY_OLD_BACKUP_PATH" ]; then
    log_error "Purging FAILED: Old dummy backup file still exists: ${DUMMY_OLD_BACKUP_PATH}"
    exit 1
fi
log_success "Purging PASSED: Old dummy backup file was removed."

# --- Final Success ---
log_success "********************************************"
log_success "*** All End-to-End Tests PASSED!        ***"
log_success "********************************************"

exit 0
