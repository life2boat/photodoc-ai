-- Migration 001: Add payment and fulfillment fields to orders table
-- Pre-condition: orders table exists with legacy schema (id, name, phone)
-- Note: CURRENT_TIMESTAMP is not a constant default, so created_at uses NULL default
-- for SQLite < 3.38 compatibility. The application layer sets this at insert time.

ALTER TABLE orders ADD COLUMN email TEXT;
ALTER TABLE orders ADD COLUMN format TEXT;
ALTER TABLE orders ADD COLUMN paper TEXT;
ALTER TABLE orders ADD COLUMN crop TEXT;
ALTER TABLE orders ADD COLUMN comment TEXT;
ALTER TABLE orders ADD COLUMN filename TEXT;
ALTER TABLE orders ADD COLUMN status TEXT DEFAULT 'new';
ALTER TABLE orders ADD COLUMN payment_status TEXT DEFAULT 'unpaid';
ALTER TABLE orders ADD COLUMN order_amount TEXT;
ALTER TABLE orders ADD COLUMN payment_amount TEXT;
ALTER TABLE orders ADD COLUMN payment_method TEXT DEFAULT 'robokassa';
ALTER TABLE orders ADD COLUMN robokassa_inv_id TEXT;
ALTER TABLE orders ADD COLUMN paid_at TEXT;
ALTER TABLE orders ADD COLUMN payment_email_sent_at TEXT;
ALTER TABLE orders ADD COLUMN created_at TEXT DEFAULT NULL;
