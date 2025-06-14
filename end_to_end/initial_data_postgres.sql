-- end_to_end/initial_data_postgres.sql
-- Test data for PostgreSQL
CREATE TABLE IF NOT EXISTS customers (
    customer_id SERIAL PRIMARY KEY,
    customer_name VARCHAR(100) NOT NULL,
    city VARCHAR(100)
);

CREATE TABLE IF NOT EXISTS orders (
    order_id SERIAL PRIMARY KEY,
    customer_id INT REFERENCES customers(customer_id),
    order_date DATE,
    amount DECIMAL(10, 2)
);

INSERT INTO customers (customer_name, city) VALUES
('Charlie Brown', 'New York'),
('Diana Prince', 'London');

INSERT INTO orders (customer_id, order_date, amount) VALUES
((SELECT customer_id FROM customers WHERE customer_name = 'Charlie Brown'), '2024-01-15', 150.00),
((SELECT customer_id FROM customers WHERE customer_name = 'Diana Prince'), '2024-01-20', 299.99);