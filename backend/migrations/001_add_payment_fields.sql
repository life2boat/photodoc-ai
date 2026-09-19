-- Migration 001: Add payment and fulfillment fields to orders table
-- STATUS: LEGACY_FULL_FORWARD_MIGRATION
-- CAUTION: NOT_SAFE_FOR_PARTIALLY_MIGRATED_DATABASE
--
-- This script was designed for initial migration from minimal schema (id, name, phone).
-- It is NOT safe for databases where some columns already exist (such as production),
-- as ALTER TABLE ADD COLUMN will fail with duplicate column errors.
-- For production and safe automated upgrades, use:
-- backend/migrations/reconcile_payment_schema.py
--
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
