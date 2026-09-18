# Database Migration Plan

## 1. Pre-migration Requirement: Backup
Before applying any migration to production or staging SQLite database:
1. Stop the application service.
2. Create an exact physical file backup:
   cp backend/orders.db backend/orders.db.bak_YYYYMMDD_HHMMSS
3. Validate integrity of the backup:
   sqlite3 backend/orders.db.bak_YYYYMMDD_HHMMSS "PRAGMA integrity_check;"

## 2. Schema Evolution
- CURRENT_SCHEMA: Legacy 5-column orders table (id, name, phone, comment, filename)
- TARGET_SCHEMA: 17-column orders table with payment status, server-authoritative amounts, Robokassa identifiers, and fulfillment specs.

## 3. Forward Migration
Run 001_add_payment_fields.sql:
sqlite3 backend/orders.db < backend/migrations/001_add_payment_fields.sql

## 4. Rollback Procedure
If rollback is required:
```sql
CREATE TABLE orders_backup (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    phone TEXT,
    comment TEXT,
    filename TEXT
);
INSERT INTO orders_backup (id, name, phone, comment, filename)
SELECT id, name, phone, comment, filename FROM orders;
DROP TABLE orders;
ALTER TABLE orders_backup RENAME TO orders;
```
