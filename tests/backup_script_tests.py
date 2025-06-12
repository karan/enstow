import os
import sys
import pytest
from unittest.mock import MagicMock, patch, mock_open
import datetime
import pytz
import tarfile
import io

# Add the parent directory of tests to the Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Import the script after adjusting sys.path
import backup_script

# Mock the Docker client globally for tests
@pytest.fixture(autouse=True)
def mock_docker_client():
    with patch('backup_script.docker.from_env') as mock_from_env:
        mock_client = MagicMock()
        mock_from_env.return_value = mock_client
        backup_script.client = mock_client # Ensure the script's client is the mock
        yield mock_client

@pytest.fixture
def mock_container():
    container = MagicMock()
    container.name = "test-container"
    yield container

@pytest.fixture(autouse=True)
def mock_env_vars_for_test_load():
    # Set default environment variables for tests to ensure config loading works as expected
    with patch.dict(os.environ, {
        'BACKUP_DIR': '/tmp/backups',
        'TIMEZONE': 'UTC',
        'PURGE_DAYS': '7',
        'CONFIG_FILE_PATH': '/app/config.yaml',
    }, clear=True):
        yield

@pytest.fixture
def sample_config_yaml_content():
    return """
    timezone: America/New_York
    purge_days: 14
    databases:
      - type: mariadb
        name: test_mariadb
        host: mariadb-test-container
        user: testuser
        password: "test_mariadb_password" # Direct password
        database: test_db
        dump_args: "--single-transaction"
      - type: postgres
        name: test_postgres
        host: postgres-test-container
        user: pguser
        password: "test_postgres_password" # Direct password
        database: pg_db
        dump_args: "-Fc"
      - type: sqlite
        name: test_sqlite
        container_name: sqlite-app-container
        path_in_container: /app/data/my_sqlite.db
      - type: valkey
        name: test_valkey
        container_name: valkey-test-container
        password: "test_valkey_password" # Direct password
        rdb_path_in_container: /data/dump.rdb
    """

@pytest.fixture(autouse=True)
def mock_config_file(sample_config_yaml_content):
    with patch('builtins.open', mock_open(read_data=sample_config_yaml_content)):
        with patch('os.path.exists', return_value=True):
            # Reload config after mocks are in place for each test
            import importlib
            importlib.reload(backup_script) # Reload the module to re-run top-level code (config loading)
            yield

class TestBackupScript:

    def test_get_current_timestamp(self):
        with patch('datetime.datetime') as mock_dt:
            mock_now = MagicMock()
            mock_now.strftime.return_value = "20240101_120000_UTC"
            mock_now.replace.return_value = mock_now # For tzinfo replacement
            mock_dt.now.return_value = mock_now
            mock_dt.side_effect = datetime.datetime # Allow actual datetime for other operations
            
            timestamp = backup_script.get_current_timestamp()
            assert timestamp == "20240101_120000_UTC"
            mock_dt.now.assert_called_with(pytz.timezone(backup_script.TIMEZONE))

    def test_load_config_from_yaml(self):
        # This test relies on the mock_config_file fixture already setting things up
        # We just need to assert the values are correct.
        assert len(backup_script.DATABASE_CONFIG) == 4
        assert backup_script.DATABASE_CONFIG[0]['name'] == 'test_mariadb'
        assert backup_script.TIMEZONE == 'America/New_York'
        assert backup_script.PURGE_DAYS == 14

    def test_load_config_file_not_found(self):
        with patch('os.path.exists', return_value=False):
            with pytest.raises(SystemExit) as pytest_wrapped_e:
                import importlib
                importlib.reload(backup_script)
            assert pytest_wrapped_e.type == SystemExit
            assert pytest_wrapped_e.value.code == 1

    def test_load_config_invalid_yaml(self):
        with patch('builtins.open', mock_open(read_data="databases: [ - invalid: ]")):
            with patch('os.path.exists', return_value=True):
                with pytest.raises(SystemExit) as pytest_wrapped_e:
                    import importlib
                    importlib.reload(backup_script)
                assert pytest_wrapped_e.type == SystemExit
                assert pytest_wrapped_e.value.code == 1

    @patch('subprocess.Popen')
    def test_execute_in_container_and_stream(self, mock_popen, mock_container):
        mock_exec_result = MagicMock()
        mock_exec_result.output = iter([b"chunk1", b"chunk2"])
        mock_container.exec_run.return_value = mock_exec_result
        
        mock_process = MagicMock()
        mock_popen.return_value.__enter__.return_value = mock_process # For context manager
        
        result = backup_script._execute_in_container_and_stream(
            mock_container, ['echo', 'hello'], {'ENV_VAR': 'value'}, '/tmp/backups/test.gz'
        )
        
        assert result is True
        mock_container.exec_run.assert_called_once_with(
            ['echo', 'hello'], stream=True, demux=False, environment={'ENV_VAR': 'value'}
        )
        mock_popen.assert_called_once_with(['gzip'], stdin=subprocess.PIPE, stdout=ANY)
        mock_process.stdin.write.assert_any_call(b"chunk1")
        mock_process.stdin.write.assert_any_call(b"chunk2")
        mock_process.stdin.close.assert_called_once()

    @patch('subprocess.Popen')
    def test_copy_from_container_and_gzip(self, mock_popen, mock_container):
        # Create a mock tar stream
        mock_tar_data = io.BytesIO()
        with tarfile.open(fileobj=mock_tar_data, mode='w') as tar:
            info = tarfile.TarInfo(name='my_sqlite.db')
            info.size = len(b"sqlite_file_content")
            tar.addfile(info, io.BytesIO(b"sqlite_file_content"))
        mock_tar_data.seek(0) # Rewind for reading

        mock_container.get_archive.return_value = (iter([mock_tar_data.getvalue()]), MagicMock())
        
        mock_process = MagicMock()
        mock_popen.return_value.__enter__.return_value = mock_process
        
        result = backup_script._copy_from_container_and_gzip(
            mock_container, '/app/data/my_sqlite.db', '/tmp/backups/sqlite.gz'
        )
        
        assert result is True
        mock_container.get_archive.assert_called_once_with('/app/data/my_sqlite.db')
        mock_process.stdin.write.assert_called_once_with(b"sqlite_file_content")
        mock_process.stdin.close.assert_called_once()

    def test_backup_mariadb_mysql_success(self, mock_docker_client, mock_container):
        mock_docker_client.containers.get.return_value = mock_container
        with patch('backup_script._execute_in_container_and_stream', return_value=True) as mock_exec:
            db_config = {
                'type': 'mariadb', 'name': 'test_db', 'host': 'mariadb-host',
                'user': 'root', 'password': 'test_mariadb_password', 'database': 'app_db'
            }
            result = backup_script._backup_mariadb_mysql(db_config, '/tmp/backups/mariadb', 'test_db-timestamp')
            assert result is True
            mock_exec.assert_called_once()
            assert mock_exec.call_args[0][1] == ['mysqldump', '-u', 'root', 'app_db']
            assert mock_exec.call_args[0][2] == {'MYSQL_PWD': 'test_mariadb_password'}


    def test_backup_postgres_success(self, mock_docker_client, mock_container):
        mock_docker_client.containers.get.return_value = mock_container
        with patch('backup_script._execute_in_container_and_stream', return_value=True) as mock_exec:
            db_config = {
                'type': 'postgres', 'name': 'test_pg', 'host': 'pg-host',
                'user': 'pguser', 'password': 'test_postgres_password', 'database': 'pg_db'
            }
            result = backup_script._backup_postgres(db_config, '/tmp/backups/postgres', 'test_pg-timestamp')
            assert result is True
            mock_exec.assert_called_once()
            assert mock_exec.call_args[0][1] == ['pg_dump', '-U', 'pguser', '-d', 'pg_db']
            assert mock_exec.call_args[0][2] == {'PGPASSWORD': 'test_postgres_password'}

    def test_backup_sqlite_success(self, mock_docker_client, mock_container):
        mock_docker_client.containers.get.return_value = mock_container
        with patch('backup_script._copy_from_container_and_gzip', return_value=True) as mock_copy:
            db_config = {
                'type': 'sqlite', 'name': 'test_sqlite', 'container_name': 'sqlite-app',
                'path_in_container': '/data/test.db'
            }
            result = backup_script._backup_sqlite(db_config, '/tmp/backups/sqlite', 'test_sqlite-timestamp')
            assert result is True
            mock_copy.assert_called_once_with(mock_container, '/data/test.db', ANY)

    def test_backup_valkey_redis_success(self, mock_docker_client, mock_container):
        mock_docker_client.containers.get.return_value = mock_container
        mock_container.exec_run.return_value.exit_code = 0 # Simulate successful BGSAVE
        with patch('time.sleep'), \
             patch('backup_script._copy_from_container_and_gzip', return_value=True) as mock_copy:
            db_config = {
                'type': 'valkey', 'name': 'test_valkey', 'container_name': 'valkey-cache',
                'password': 'test_valkey_password', 'rdb_path_in_container': '/data/dump.rdb'
            }
            result = backup_script._backup_valkey_redis(db_config, '/tmp/backups/valkey', 'test_valkey-timestamp')
            assert result is True
            mock_container.exec_run.assert_called_once_with(
                ['redis-cli', '-a', 'test_valkey_password', 'BGSAVE']
            )
            mock_copy.assert_called_once_with(mock_container, '/data/dump.rdb', ANY)

    def test_purge_old_backups(self):
        # Mock os.listdir, os.path.isdir, os.remove, os.path.exists
        mock_files_for_purge = [
            'db_old-20231231_235959_UTC.sql.gz', # Should be purged (older than 2024-01-01)
            'db_cutoff-20240101_000000_UTC.sql.gz', # Should NOT be purged (exactly cutoff)
            'db_recent-20240102_000000_UTC.sql.gz' # Should NOT be purged (newer than cutoff)
        ]
        
        # Patch datetime.datetime.now to control current date for cutoff
        with patch('datetime.datetime') as mock_dt:
            mock_now = MagicMock()
            # Set current date to 2024-01-08 12:00:00 UTC
            mock_now.now.return_value = datetime.datetime(2024, 1, 8, 12, 0, 0, tzinfo=pytz.timezone('UTC'))
            mock_now.side_effect = datetime.datetime # Allow real datetime for timedelta and strptime

            with patch('os.listdir', side_effect=[['mariadb_type_dir'], mock_files_for_purge]), \
                 patch('os.path.isdir', return_value=True), \
                 patch('os.remove') as mock_os_remove, \
                 patch('os.path.exists', return_value=True):
                
                # Set PURGE_DAYS to 7
                backup_script.PURGE_DAYS = 7
                backup_script.TIMEZONE = 'UTC' # Ensure consistent TZ for test

                backup_script.purge_old_backups()

                # Assert that only the file older than cutoff was removed
                mock_os_remove.assert_called_once_with(os.path.join(backup_script.BACKUP_DIR, 'mariadb_type_dir', 'db_old-20231231_235959_UTC.sql.gz'))
    
    def test_purge_disabled(self):
        # Temporarily modify PURGE_DAYS for this test
        original_purge_days = backup_script.PURGE_DAYS
        backup_script.PURGE_DAYS = 0
        try:
            with patch('os.remove') as mock_os_remove:
                backup_script.purge_old_backups()
                mock_os_remove.assert_not_called() # No files should be removed
        finally:
            backup_script.PURGE_DAYS = original_purge_days # Restore original value


# Global ANY for asserting against any value in mock calls
ANY = object()

