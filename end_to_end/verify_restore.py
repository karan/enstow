import os
import sys
import mysql.connector
import psycopg2

# --- Verification Logger ---
def verify_log(message, level="INFO"):
    """Simple logger for verification script."""
    if level == "FAIL":
        print(f"\n[VERIFY {level}] {message}\n", file=sys.stderr)
        sys.exit(1)
    else:
        print(f"[VERIFY {level}] {message}")

# --- Constants for Test Data ---
# MariaDB
MARIADB_HOST = "mariadb-test-e2e"
MARIADB_USER = "root"
MARIADB_PASSWORD = os.getenv("MARIADB_PASSWORD", "test_mariadb_password")
MARIADB_DATABASE = "test_db"
MARIADB_EXPECTED_USERS = [('Alice Smith', 'alice@example.com'), ('Bob Johnson', 'bob@example.com')]
MARIADB_EXPECTED_PRODUCTS = [('Test Item A', '10.99'), ('Test Item B', '25.50')] # Prices might be strings after fetch

# PostgreSQL
POSTGRES_HOST = "postgres-test-e2e"
POSTGRES_USER = "pguser"
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD", "test_postgres_password")
POSTGRES_DATABASE = "pg_test_db"
POSTGRES_EXPECTED_CUSTOMERS = [('Charlie Brown', 'New York'), ('Diana Prince', 'London')]
POSTGRES_EXPECTED_ORDERS_AMOUNTS = [150.00, 299.99] # Sorted by customer_id then order_date

# SQLite
SQLITE_HOST_CONTAINER = "sqlite-app-e2e"
SQLITE_PATH_IN_CONTAINER = "/app/data/e2e_sqlite.db"
SQLITE_EXPECTED_ITEMS = [('SQLite Test Data 1',), ('SQLite Test Data 2',)] # Tuple format from fetchall


def verify_mariadb_restore():
    verify_log("Verifying MariaDB restore...")
    try:
        conn = mysql.connector.connect(
            host=MARIADB_HOST,
            user=MARIADB_USER,
            password=MARIADB_PASSWORD,
            database=MARIADB_DATABASE
        )
        cursor = conn.cursor()

        # Verify users table
        cursor.execute("SELECT name, email FROM users ORDER BY id;")
        users = cursor.fetchall()
        if len(users) != len(MARIADB_EXPECTED_USERS) or sorted(users) != sorted(MARIADB_EXPECTED_USERS):
            verify_log(f"MariaDB users verification failed. Expected {MARIADB_EXPECTED_USERS}, got {users}", level="FAIL")

        # Verify products table
        cursor.execute("SELECT product_name, price FROM products ORDER BY id;")
        products = cursor.fetchall()
        # Convert fetched prices to string for comparison as they might come as Decimal
        products_str_price = [(p_name, str(p_price)) for p_name, p_price in products]
        if len(products_str_price) != len(MARIADB_EXPECTED_PRODUCTS) or sorted(products_str_price) != sorted(MARIADB_EXPECTED_PRODUCTS):
            verify_log(f"MariaDB products verification failed. Expected {MARIADB_EXPECTED_PRODUCTS}, got {products_str_price}", level="FAIL")

        verify_log("MariaDB restore verified successfully.")
    except Exception as e:
        verify_log(f"MariaDB verification failed: {e}", level="FAIL")
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()

def verify_postgres_restore():
    verify_log("Verifying PostgreSQL restore...")
    try:
        conn = psycopg2.connect(
            host=POSTGRES_HOST,
            user=POSTGRES_USER,
            password=POSTGRES_PASSWORD,
            database=POSTGRES_DATABASE
        )
        cursor = conn.cursor()

        # Verify customers table
        cursor.execute("SELECT customer_name, city FROM customers ORDER BY customer_id;")
        customers = cursor.fetchall()
        if len(customers) != len(POSTGRES_EXPECTED_CUSTOMERS) or sorted(customers) != sorted(POSTGRES_EXPECTED_CUSTOMERS):
            verify_log(f"PostgreSQL customers verification failed. Expected {POSTGRES_EXPECTED_CUSTOMERS}, got {customers}", level="FAIL")

        # Verify orders table
        cursor.execute("SELECT amount FROM orders ORDER BY customer_id, order_date;")
        orders_amounts = [float(row[0]) for row in cursor.fetchall()] # Convert Decimal to float for comparison
        if len(orders_amounts) != len(POSTGRES_EXPECTED_ORDERS_AMOUNTS) or sorted(orders_amounts) != sorted(POSTGRES_EXPECTED_ORDERS_AMOUNTS):
            verify_log(f"PostgreSQL orders amounts verification failed. Expected {POSTGRES_EXPECTED_ORDERS_AMOUNTS}, got {orders_amounts}", level="FAIL")

        verify_log("PostgreSQL restore verified successfully.")
    except Exception as e:
        verify_log(f"PostgreSQL verification failed: {e}", level="FAIL")
    finally:
        if 'conn' in locals():
            cursor.close()
            conn.close()

def verify_sqlite_restore():
    verify_log("Verifying SQLite restore...")
    try:
        # SQLite DB is copied into the container, so we need to exec sqlite3 within that container
        # to read its contents. The E2E script will use docker cp to put it there.
        import subprocess
        # Get data from sqlite-app-e2e container
        cmd = f'docker exec {SQLITE_HOST_CONTAINER} sqlite3 {SQLITE_PATH_IN_CONTAINER} "SELECT value FROM test_items ORDER BY id;"'
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, check=True)
        
        fetched_items = [ (item.strip(),) for item in result.stdout.strip().split('\n') if item.strip()]

        if len(fetched_items) != len(SQLITE_EXPECTED_ITEMS) or sorted(fetched_items) != sorted(SQLITE_EXPECTED_ITEMS):
            verify_log(f"SQLite items verification failed. Expected {SQLITE_EXPECTED_ITEMS}, got {fetched_items}", level="FAIL")

        verify_log("SQLite restore verified successfully.")
    except subprocess.CalledProcessError as e:
        verify_log(f"SQLite verification failed (command error): {e.stderr}", level="FAIL")
    except Exception as e:
        verify_log(f"SQLite verification failed: {e}", level="FAIL")

if __name__ == "__main__":
    verify_log("Starting data verification process...")
    verify_mariadb_restore()
    verify_postgres_restore()
    verify_sqlite_restore()
    verify_log("All data verification checks PASSED.")
