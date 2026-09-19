# Database Migration & Schema Reconciliation Plan

## 1. Pre-migration Requirement: Backup
Before applying any migration to production or staging SQLite database:
1. Stop the application write traffic:
   `docker compose stop backend`
2. Create an exact physical file backup:
   `cp /data/orders.db /backup/orders.db.bak_YYYYMMDD_HHMMSS`
3. Validate integrity of the backup:
   `sqlite3 /backup/orders.db.bak_YYYYMMDD_HHMMSS "PRAGMA integrity_check;"`

## 2. Schema Reconciliation (Recommended & Production Default)
For existing and production databases, use the idempotent reconciliation tool:
```bash
# Check status (dry-run, default mode)
python backend/migrations/reconcile_payment_schema.py --db /data/orders.db --check --allow-legacy-unmarked

# Apply missing columns safely
python backend/migrations/reconcile_payment_schema.py --db /data/orders.db --apply --allow-legacy-unmarked
```

`--allow-legacy-unmarked` is required only for the controlled one-time migration of an audited database whose `PRAGMA application_id` is `0`. It is not a normal startup or post-migration option. Any nonzero identity other than PhotoDoc's stable `1346650441` (`PDAI`) is always rejected.

Reconciliation runs missing DDL, `application_id` assignment, schema verification, and integrity verification under `BEGIN IMMEDIATE`. Any failure rolls back both schema and identity changes.

### Why Reconciliation Over Migration 001?
- `001_add_payment_fields.sql` is a **LEGACY_FULL_FORWARD_MIGRATION** that assumes only `(id, name, phone)` exist.
- It is **NOT_SAFE_FOR_PARTIALLY_MIGRATED_DATABASE**: running it on a database with partial columns (such as the audited production DB missing only `order_amount` and `created_at`) will fail with duplicate column errors.
- `reconcile_payment_schema.py` introspects `PRAGMA table_info(orders)`, detects only missing columns, adds them via `ALTER TABLE orders ADD COLUMN ...`, verifies schema integrity, and is strictly idempotent.

## 3. Legacy Forward Migration (Historical Reference)
`001_add_payment_fields.sql` is retained for historical purposes and test coverage against clean minimal legacy databases.

## 4. Rollback Procedure
If rollback of a database migration is required:
1. Stop backend container.
2. Restore the pre-migration physical backup:
   `cp /backup/orders.db.bak_YYYYMMDD_HHMMSS /data/orders.db`
3. Validate integrity:
   `sqlite3 /data/orders.db "PRAGMA integrity_check;"`
4. Restart backend.
