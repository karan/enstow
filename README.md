# Enstow

This project provides a robust, containerized solution for safely backing up your databases (MariaDB/MySQL, PostgreSQL, SQLite, Valkey/Redis) running in other Docker containers. It performs periodical backups without requiring downtime for your database services and stores them in a designated directory on your host machine.

> [!NOTE]
> v1 of this project was entirely coded by Gemini Flash 2.5 (including tests). @karan ensured that the code worked, and cleaned up code comments and this README.

## Features

* **Zero-Downtime Backups:** Utilizes database-specific methods (`--single-transaction` for MariaDB/MySQL, `pg_dump` for PostgreSQL, `BGSAVE` for Valkey) to ensure consistent backups without interrupting your services.

* **Multi-Database Support:** Natively supports MariaDB/MySQL, PostgreSQL, SQLite, and Valkey (Redis).

* **Containerized Solution:** Runs as a dedicated Docker container, keeping your backup logic isolated and manageable.

* **Scheduled Backups:** Uses `cron` within the container for reliable periodic execution.

* **YAML Configuration:** Database configurations are managed via a `config.yaml` file for improved readability and structure. Passwords are now stored directly in `config.yaml`.

* **Timezone Aware:** Backups are timestamped with the specified timezone.

* **Automatic Purging:** Configurable retention policy to automatically delete old backups, specifically within each database's unique backup directory.

* **Host Persistence:** Backups are saved directly to a mounted directory on your host system, organized by `db_type/db_name`.

* **Optimized Docker Image:** Utilizes Python 3.13, multi-stage builds, and unbuffered output for smaller image size and real-time logging.

* **Comprehensive Testing:** Includes unit tests for core functionalities.

* **CI/CD Workflow:** GitHub Actions to automatically build, test, and publish the Docker image on `main` branch pushes and pull requests to GHCR.

* **Healthchecks.io Integration:** Sends pings to Healthchecks.io at the start, success, and failure of the overall backup process, and sends individual database backup statuses to the log endpoint of the global check URL.

* **Enhanced Logging:** Provides clear, indented log output with star lines to mark backup start/end, and summaries of created/purged files and their sizes.

## Requirements

* Docker and Docker Compose installed on your host machine.

* Access to the Docker socket (`/var/run/docker.sock`) for the backup container.

## Project Structure


.
├── backup_script.py        # The core Python script for database backups
├── config.yaml             # YAML configuration file for databases
├── Dockerfile              # Dockerfile for building the backup agent image
├── docker-compose.yml      # Docker Compose file to deploy the agent
├── requirements.txt        # Python dependencies
├── start.sh                # Entrypoint script for the Docker container
├── .github/workflows/
│   └── main.yml            # GitHub Actions workflow for CI/CD
├── tests/
│   └── test_backup_script.py # Unit tests for backup_script.py
└── end_to_end/             # End-to-End test suite
├── run_e2e_test.sh         # Main E2E test runner script
├── e2e_config.yaml         # Config for E2E backup agent
├── docker-compose.e2e.yml  # Docker Compose for E2E test DBs and agent
├── initial_data_mariadb.sql # Test data for MariaDB
├── initial_data_postgres.sql # Test data for PostgreSQL
├── initial_data_sqlite.db  # Test data for SQLite
├── initial_data_redis.txt  # Test data for Redis/Valkey
├── verify_restore.py       # Script to verify restored data
└── Dockerfile.verify_restore # Dockerfile for verification script


## Setup and Installation

Follow these steps to get your database backup agent running:

1.  **Clone this Repository:**

    ```bash
    git clone [https://github.com/your-repo/docker-db-backup-agent.git](https://github.com/your-repo/docker-db-backup-agent.git) # Replace with your actual repo
    cd docker-db-backup-agent
    
    ```

2.  **Create Backup Directory:**
    Create a directory on your host machine where you want to store the backups. This directory will be mounted into the backup container.

    ```bash
    mkdir -p ./backups
    
    ```

    This `backups` directory should be in the same location as your `docker-compose.yml` file.

3.  **Configure `config.yaml`:**
    Open the `config.yaml` file and carefully configure the `databases` section. You can also optionally override `timezone` and `purge_days` here, which will take precedence over the environment variables set in `docker-compose.yml`.

    **WARNING: Storing plain-text passwords directly in `config.yaml` is NOT recommended for production environments due to security risks. For production, consider using Docker secrets, Kubernetes secrets, or a dedicated secrets management solution (e.g., HashiCorp Vault, AWS Secrets Manager) and dynamically inject credentials at runtime.**

    * **`timezone`**: (Optional) Set your local timezone (e.g., `"America/Los_Angeles"`, `"Europe/London"`, `"Asia/Tokyo"`). This affects the timestamping of your backup files.

    * **`purge_days`**: (Optional) Number of days to retain backups. Files older than this will be automatically deleted. Set to `0` to disable purging.

    * **`healthcheck_url`**: The full Healthchecks.io ping URL (e.g., `https://hc-ping.com/YOUR_GLOBAL_UUID`). This single URL will be used for all pings related to the overall backup job. You will get this URL from your Healthchecks.io dashboard after creating a check.

    * **`databases`**: This is the core configuration for specifying which databases to back up. It's a list of database objects.

        * Each object must have a `"type"` (e.g., `"mariadb"`, `"postgres`", `"sqlite"`, `"valkey"`) and other specific parameters:

            * `"name"`: A unique, human-readable name for this backup configuration (e.g., `"my_blog_db"`). This name will also be used to create a subdirectory within the `db_type` folder for its backups (e.g., `./backups/mariadb/my_blog_db/`).

            * `"host"` or `"container_name"`: The Docker service name (if using `docker compose` for your DB) or the exact Docker container name of your database instance. The backup agent will use this to connect or `exec` into the container.

            * **For `mariadb`/`mysql`/`postgres`:**

                * `"user"`: The database user with backup privileges.

                * `"password"`: The actual database password. (Again, consider security implications for production.)

                * `"database"`: The name of the specific database to back up.

                * `"dump_args"`: (Optional) Additional arguments for `mysqldump` or `pg_dump`.

                    * For MariaDB/MySQL, `--single-transaction --skip-dump-date` is highly recommended for InnoDB to avoid locking tables.

                    * For PostgreSQL, `-Fc` (custom format) is often preferred for more flexible restores.

            * **For `sqlite`:**

                * `"container_name"`: The name of the Docker container where the SQLite `.db` file resides.

                * `"path_in_container"`: The **absolute path** to the SQLite `.db` file *inside that container*.

            * **For `valkey`/`redis`:**

                * `"container_name"`: The name of the Docker container running Valkey/Redis.

                * `"password"`: (Optional) The Redis password. (Again, consider security implications.)

                * `"rdb_path_in_container"`: (Optional) The absolute path to the RDB file inside the Valkey/Redis container (default is usually `/data/dump.rdb`).

4.  **Configure `docker-compose.yml`:**
    Open the `docker-compose.yml` file and review the `environment` variables.

    * **`BACKUP_DIR`**: (Default: `/backups`) - The internal path within the container. **Do not change this unless you also change the volume mount.**

    * **`CRON_SCHEDULE`**: (Default: `"0 2 * * *"`) - Set your desired cron schedule for backups.

        * Examples:

            * `"0 2 * * *"`: Daily at 2:00 AM

            * `"0 */6 * * *"`: Every 6 hours

            * `"0 0 * * 0"`: Every Sunday at midnight

    * **`TIMEZONE`**: (Default: `"America/Los_Angeles"`) - Fallback timezone. If `timezone` is set in `config.yaml`, that value will be used instead.

    * **`PURGE_DAYS`**: (Default: `"30"`) - Fallback purge days. If `purge_days` is set in `config.yaml`, that value will be used instead.

    * **`CONFIG_FILE_PATH`**: (Default: `/app/config.yaml`) - The path where `config.yaml` is mounted inside the container.

5.  **Build and Run the Backup Agent:**
    Navigate to the directory containing your `docker-compose.yml` and other project files, then run:

    ```bash
    docker compose up -d --build
    
    ```

    * `--build`: Builds the Docker image (only needed the first time or after Dockerfile changes).

    * `-d`: Runs the container in detached mode (in the background).

    You can check the logs of the backup agent to confirm it started correctly:

    ```bash
    docker logs db-backup-agent -f
    
    ```

### Important Security Note on `/var/run/docker.sock`

Mounting `/var/run/docker.sock` into a container effectively gives that container root access to your Docker host. This means the container can start, stop, and delete any other container, and even create privileged containers.

**Only use this solution if you understand and accept this security implication, and only for trusted environments.** For production, consider more isolated backup strategies if direct Docker socket access is a concern.

## Healthchecks.io Integration

This project integrates with [Healthchecks.io](https://healthchecks.io/) to provide monitoring for your backup jobs. A single **global `healthcheck_url`** from your `config.yaml` is used to monitor the overall backup process.

### Setup Healthchecks.io

1.  **Create an Account:** If you don't have one, sign up at [Healthchecks.io](https://healthchecks.io/).

2.  **Add a New Check:**

    * Click "Add Check".

    * Give it a descriptive name (e.g., "All Database Backups").

    * Set the "Period" and "Grace" intervals based on your `CRON_SCHEDULE` (e.g., if you back up daily, set Period to 1 day and Grace to 1 hour).

    * Save the check.

    * You will be given a UUID. Construct your `healthcheck_url` in the format `https://hc-ping.com/YOUR_UUID`.

3.  **Update `config.yaml`:**
    Paste the constructed `healthcheck_url` into the top-level `healthcheck_url` field in your `config.yaml` file.

### How it Works

The `backup_script.py` sends the following pings to Healthchecks.io using the single global `healthcheck_url`:

* **Start Ping (`<healthcheck_url>/start`)**: Sent at the beginning of the entire backup process. This indicates that the global backup job has started. A unique `rid` (run ID) is also sent to link this start ping to the final success/fail ping.

* **Success Ping (`<healthcheck_url>`)**: Sent if the *entire* backup process (all configured databases) completes successfully.

* **Fail Ping (`<healthcheck_url>/fail`)**: Sent if *any* database backup or other critical error occurs during the process, indicating a global failure.

* **Log Ping (`<healthcheck_url>/log`)**: For each individual database backup, a message indicating its success or failure (along with any error messages) is sent to this endpoint. This allows you to review granular backup status within the Healthchecks.io "Events" section for that single check.

If Healthchecks.io does not receive a success or fail ping within the configured "Period" + "Grace" interval, it will alert you.

## Local Development and Testing

For local development and testing, you can run the `backup_script.py` directly without spinning up the full Docker Compose stack with cron.

1.  **Clone the Repository:**

    ```bash
    git clone [https://github.com/your-repo/docker-db-backup-agent.git](https://github.com/your-repo/docker-db-backup-agent.git)
    cd docker-db-backup-agent
    
    ```

2.  **Create a Virtual Environment (Recommended):**

    ```bash
    python3 -m venv venv
    source venv/bin/activate
    
    ```

3.  **Install Python Dependencies:**

    ```bash
    pip install -r requirements.txt
    
    ```

4.  **Prepare Configuration and Backup Directory:**

    * Ensure your `config.yaml` is set up with your database details (including the global `healthcheck_url` if you're testing that integration).

    * Create a local `backups` directory:

        ```bash
        mkdir -p ./backups
        
        ```

5.  **Run Unit Tests Locally:**
    Before running the script or deploying, you can execute the unit tests to ensure everything is working as expected.

    ```bash
    pytest tests/
    
    ```

6.  **Run the Backup Script as a One-Off:**
    To perform a single backup run, you can execute the script directly. This requires your database containers to be running and the `backup_script.py` to be able to access the Docker daemon on your host.

    ```bash
    # Ensure Docker daemon is accessible for the script (e.g., you are in a Docker-enabled environment)
    # The script will try to connect to the Docker daemon.
    python3 backup_script.py
    
    ```

    You will see detailed, indented logs outputted to your console, and backups will appear in your local `./backups` directory.

## End-to-End (E2E) Testing

This project includes a comprehensive end-to-end test suite that simulates a full backup, restore, and purge cycle across all supported database types.

**Prerequisites:** You must have Docker and Docker Compose installed on your Linux system.

**How to Run the E2E Test:**

1.  **Navigate to the project root:**

    ```bash
    cd /path/to/your/docker-db-backup-agent
    
    ```

2.  **Execute the E2E test script:**

    ```bash
    ./end_to_end/run_e2e_test.sh
    
    ```

The script will:
* Build the `db-backup-agent` Docker image.
* Start up test database containers (MariaDB, PostgreSQL, SQLite app, Valkey/Redis).
* Populate these databases with dummy data.
* Create a simulated "old" backup file to test the purging logic.
* Run the `backup_script.py` inside the `db-backup-agent-e2e` container to perform backups and purging.
* Verify that new backup files were created and the old dummy file was purged.
* Delete all data from the test databases.
* Restore the data from the newly created backups.
* Validate that the restored data is correct.
* Finally, it will clean up all created containers, volumes, and temporary files, leaving your system as it was before the test.

The script will print clear `[INFO]`, `[SUCCESS]`, `[WARNING]`, and `[ERROR]` messages throughout its execution, concluding with a `PASS` or `FAIL` status.

## Restore Instructions

Restoring a database involves copying the backup file back and then importing it into a new or existing database instance. **Always exercise caution when restoring, especially in production environments.** It's often recommended to stop the database container before restoring to ensure data consistency, unless the database type supports live restore (which is rare for full backups).

The backup files are located in the `./backups` directory on your host, organized by database type and then by the database name (e.g., `./backups/mariadb/my_web_app_mariadb/my_web_app_mariadb-YYYYMMDD_HHMMSS_TZ.sql.gz`).

### General Steps for Restoration:

1.  **Stop your database container(s):**

    ```bash
    docker stop <your_db_container_name>
    
    ```

    (Or stop the entire `docker compose` stack: `docker compose stop <your_db_service_name>`)

2.  **Locate the desired backup file.**

3.  **Restore the database specific to its type:**

    #### MariaDB / MySQL

    1.  **Decompress the backup file:**

        ```bash
        gunzip ./backups/mariadb/my_web_app_mariadb/my_web_app_mariadb-YYYYMMDD_HHMMSS_TZ.sql.gz
        
        ```

    2.  **Import the SQL dump:**

        ```bash
        docker exec -i <your_mariadb_container_name> mysql -u root -p<YOUR_MARIADB_ROOT_PASSWORD> my_webapp_db < ./backups/mariadb/my_web_app_mariadb/my_web_app_mariadb-YYYYMMDD_HHMMSS_TZ.sql
        
        ```

        (Replace `root`, `YOUR_MARIADB_ROOT_PASSWORD`, `my_webapp_db`, and container name with your actual values.)

    #### PostgreSQL

    1.  **Decompress the backup file:**

        ```bash
        gunzip ./backups/postgres/my_data_service_pg/my_data_service_pg-YYYYMMDD_HHMMSS_TZ.dump.gz
        
        ```

    2.  **Restore the custom format dump:**

        ```bash
        docker exec -i <your_postgres_container_name> pg_restore -U postgres -d my_data_db < ./backups/postgres/my_data_service_pg/my_data_service_pg-YYYYMMDD_HHMMSS_TZ.dump
        
        ```

        (You may need to set `PGPASSWORD` environment variable or use the `-W` flag for a password prompt. Replace `postgres`, `my_data_db`, and container name with your actual values.)

    #### SQLite

    1.  **Decompress the backup file:**

        ```bash
        gunzip ./backups/sqlite/my_app_sqlite/my_app_sqlite-YYYYMMDD_HHMMSS_TZ.db.gz
        
        ```

    2.  **Copy the `.db` file back into your application container:**

        ```bash
        docker cp ./backups/sqlite/my_app_sqlite/my_app_sqlite-YYYYMMDD_HHMMSS_TZ.db <your_app_container_name>:/app/data/my_sqlite.db
        
        ```

        (Replace paths and container name with your actual values. Ensure your application container's volume for the SQLite file is correctly mapped.)

    #### Valkey / Redis

    1.  **Decompress the backup file:**

        ```bash
        gunzip ./backups/valkey/my_cache_valkey/my_cache_valkey-YYYYMMDD_HHMMSS_TZ.rdb.gz
        
        ```

    2.  **Copy the RDB file into the Valkey/Redis data directory:**
        Find the data directory your Valkey/Redis container uses (often `/data` or `/var/lib/redis`).

        ```bash
        docker cp ./backups/valkey/my_cache_valkey/my_cache_valkey-YYYYMMDD_HHMMSS_TZ.rdb <your_valkey_container_name>:/data/dump.rdb
        
        ```

        (Replace `your_valkey_container_name` and adjust the path to `dump.rdb` if your configuration is different. Ensure the Redis container is stopped before copying, and restart it afterward for the new RDB to be loaded.)

4.  **Restart your database container(s):**

    ```bash
    docker start <your_db_container_name>
    
    ```

    (Or `docker compose start <your_db_service_name>`)

## Troubleshooting

* **"Error connecting to Docker daemon":** Ensure `/var/run/docker.sock` is correctly mounted in your `docker-compose.yml` or that your local environment has Docker access.

* **"Container not found":** Verify that the `host` or `container_name` specified in your `config.yaml` exactly matches the name of your running Docker container or service.

* **Permissions issues:** Ensure the `backups` directory on your host has appropriate write permissions for the user running Docker.

* **Password issues:** Double-check that the `password` in your `config.yaml` is correct for the specified user.

* **SQLite/Valkey file not found:** Ensure `path_in_container` is the absolute and correct path to the `.db` or `.rdb` file inside the target container. Remember that `get_archive` returns a tar archive, and the script handles extracting the specific file from it.

* **Logs not appearing in real-time:** Ensure `ENV PYTHONUNBUFFERED=1` is present in your `Dockerfile` and `docker logs` is used with `-f`.

* **Healthchecks.io pings not working:**

    * Verify the `healthcheck_url` in `config.yaml` is correct and accessible from within the container.

    * Ensure your container has outbound internet access to `https://hc-ping.com`.

    * Check the container logs for any errors related to `requests` or network connectivity.

Feel free to open an issue or reach out if you encounter further problems!

