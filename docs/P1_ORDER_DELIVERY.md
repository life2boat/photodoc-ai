# P1 order delivery: migration and recovery

This change builds on PR #8. It does not replace the Yandex Disk OAuth or SMTP
integrations. The production database and upload directory must remain on their
existing persistent volume/bind mount.

## Before starting the P1 backend

Stop the old backend writer and execute the verified backup protocol detailed below
for `orders.db` and the uploads directory. Keep the backup outside the release tree.
On the backed-up, identified PhotoDoc database, run:

```bash
python backend/migrations/reconcile_delivery_schema.py --db /data/orders.db
python backend/migrations/reconcile_delivery_schema.py --db /data/orders.db --apply
python backend/migrations/reconcile_delivery_schema.py --db /data/orders.db
```

The script requires the P0 `PDAI` application identity and complete P0 schema.
It adds `order_token_hash` and `delivery_jobs` transactionally; existing orders
and local files are not rewritten. The P1 backend refuses to start against a
database missing these additions. Run these commands in an isolated container
with the real persistent volume mounted only during an approved release. This
document is not an instruction to deploy from a draft PR.

## Hardened Backup and Recovery Protocol

Copying an active SQLite database file using `cp` while writers are active or before WAL
checkpointing can produce torn pages or lose uncheckpointed transactions, and can cause
desynchronization between database records and customer photo uploads on disk.

To guarantee consistent, verifiable backups prior to schema migration or deployment cutover,
follow this strict procedure:

### 1. Writer Shutdown and SQLite WAL Checkpoint

Before snapshotting, guarantee zero write concurrency by stopping the backend container:

```bash
docker compose -p photodoc \
  -f "${ACTIVE_RELEASE_DIR}/docker-compose.yml" \
  -f "${ACTIVE_RELEASE_DIR}/docker-compose.production.yml" \
  stop backend
```

Once the writer is stopped, flush and truncate any SQLite Write-Ahead Log (`orders.db-wal`):

```bash
docker run --rm \
  -v "${PHOTODOC_DB_VOLUME:-photodoc_backend-data}:/data" \
  alpine sh -c "apk add --no-cache sqlite >/dev/null && sqlite3 /data/orders.db 'PRAGMA wal_checkpoint(TRUNCATE);'"
```

Verify that the command returns `0` (not busy) and that the database integrity check returns `ok`:

```bash
docker run --rm \
  -v "${PHOTODOC_DB_VOLUME:-photodoc_backend-data}:/data:ro" \
  alpine sh -c "apk add --no-cache sqlite >/dev/null && sqlite3 /data/orders.db 'PRAGMA integrity_check;'"
```

### 2. Transactionally Consistent SQLite Snapshot via Backup API

Target backup storage must reside completely outside the active release tree (`/opt/photodoc`)
and outside any web server document root to prevent accidental exposure via Nginx.
Default target: `/var/backups/photodoc`.

Ensure sufficient disk space and overwrite protection before snapshotting:

```bash
PHOTODOC_BACKUP_ROOT="${PHOTODOC_BACKUP_ROOT:-/var/backups/photodoc}"
mkdir -p "${PHOTODOC_BACKUP_ROOT}"

# Disk space check: require at least 500 MB free space
AVAILABLE_KB=$(df -k "${PHOTODOC_BACKUP_ROOT}" | awk 'NR==2 {print $4}')
if [ "${AVAILABLE_KB}" -lt 512000 ]; then
  echo "ERROR: Insufficient disk space for backup in ${PHOTODOC_BACKUP_ROOT}" >&2
  exit 1
fi

BACKUP_DIR="${PHOTODOC_BACKUP_ROOT}/release_$(date -u +%Y%m%d_%H%M%S)"
test ! -e "${BACKUP_DIR}"
mkdir -p "${BACKUP_DIR}"
chmod 700 "${BACKUP_DIR}"

docker run --rm \
  -v "${PHOTODOC_DB_VOLUME:-photodoc_backend-data}:/data:ro" \
  -v "${BACKUP_DIR}:/backup" \
  alpine sh -c "apk add --no-cache sqlite >/dev/null && sqlite3 /data/orders.db '.backup /backup/orders.db'"
```

Verify integrity of the backup file:

```bash
docker run --rm \
  -v "${BACKUP_DIR}:/backup:ro" \
  alpine sh -c "apk add --no-cache sqlite >/dev/null && sqlite3 /backup/orders.db 'PRAGMA integrity_check;'"
```
The result must be strictly `ok`.

### 3. Synchronized Uploads Directory Archive

With the backend writer stopped, no new files can be written to `/opt/photodoc/backend/uploads`. Archive the uploads directory while preserving directory structure, permissions, and numeric ownership:

```bash
tar --numeric-owner -czvf "${BACKUP_DIR}/uploads.tar.gz" -C /opt/photodoc/backend uploads
```

### 4. Cryptographic Manifest, Metadata, and Accidental Overwrite Protection

Generate SHA-256 checksums for both artifacts:

```bash
cd "${BACKUP_DIR}"
sha256sum orders.db uploads.tar.gz > manifest.sha256
```

Record the metadata manifest `backup_manifest.json` capturing database application identity, record counts, and file counts:

```bash
DB_APP_ID=$(sqlite3 "${BACKUP_DIR}/orders.db" "PRAGMA application_id;")
ORDER_COUNT=$(sqlite3 "${BACKUP_DIR}/orders.db" "SELECT COUNT(*) FROM orders;")
MAX_ORDER_ID=$(sqlite3 "${BACKUP_DIR}/orders.db" "SELECT COALESCE(MAX(id), 0) FROM orders;")
FILE_COUNT=$(find /opt/photodoc/backend/uploads -type f | wc -l)
DB_SHA256=$(sha256sum "${BACKUP_DIR}/orders.db" | awk '{print $1}')
UPLOADS_SHA256=$(sha256sum "${BACKUP_DIR}/uploads.tar.gz" | awk '{print $1}')

cat <<EOF > "${BACKUP_DIR}/backup_manifest.json"
{
  "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "application_id": ${DB_APP_ID},
  "application_id_tag": "PDAI",
  "orders_count": ${ORDER_COUNT},
  "max_order_id": ${MAX_ORDER_ID},
  "uploads_files_count": ${FILE_COUNT},
  "orders_db_sha256": "${DB_SHA256}",
  "uploads_tar_sha256": "${UPLOADS_SHA256}"
}
EOF
```

Confirm that `application_id` matches `1346650441` (`PDAI`).
Protect completed backup artifacts from accidental modification or deletion:

```bash
chmod 600 "${BACKUP_DIR}/orders.db" "${BACKUP_DIR}/uploads.tar.gz" "${BACKUP_DIR}/manifest.sha256"
chmod a-w "${BACKUP_DIR}"
```

### 5. Independent Sandbox Restore Rehearsal

Before modifying the active production database, verify the restorable state in an isolated scratch environment using the standalone verification CLI (`scripts/verify_backup_integrity.py`).

The verifier:
- Reconciles P1 attachments via `delivery_jobs` (safe path confinement, physical existence, exact size, and SHA-256).
- Supports historical P0 orders without false-positive failures if old local files were not retained.
- Blocks directory traversal (`..` escape attempts).
- Returns non-zero exit code on corrupted/missing data.
- Emits a sanitized report free of customer PII.

```bash
RESTORE_DIR=$(mktemp -d /tmp/photodoc_restore_XXXXXX)

# 1. Verify checksums against manifest
cd "${BACKUP_DIR}"
sha256sum -c manifest.sha256

# 2. Extract into sandbox
cp "${BACKUP_DIR}/orders.db" "${RESTORE_DIR}/orders.db"
tar -xzf "${BACKUP_DIR}/uploads.tar.gz" -C "${RESTORE_DIR}"

# 3. Execute integrity and storage verification CLI
python3 /candidate/scripts/verify_backup_integrity.py --backup-dir "${RESTORE_DIR}"

# 4. Clean up sandbox
rm -rf "${RESTORE_DIR}"
```

### 6. Rollback Protocol and Migration Compatibility

- **Rollback before new orders are accepted:**
  If migration fails or candidate verification aborts before opening public traffic:
  1. Stop candidate containers.
  2. Restore `/data/orders.db` from `${BACKUP_DIR}/orders.db`.
  3. Ensure ownership and mode: `chmod 644 /data/orders.db`.
  4. Restart the verified previous release backend.
- **Rollback after new orders are accepted (Additive Migration Compatibility):**
  P1 migration is strictly additive:
  - Adds `order_token_hash` column to `orders`.
  - Creates `delivery_jobs` table and indexes.
  The P0 backend inspection (`inspect_database` in `backend/db_contract.py`) verifies required columns and application identity `1346650441`, and gracefully tolerates additional columns or tables.
  However, orders created while running P1 will have their delivery tasks queued in `delivery_jobs` rather than executed via P0's legacy synchronous path.
  Therefore:
  - In a catastrophic rollback to P0 while preserving new customer orders, run an operator script to inspect pending `delivery_jobs` and dispatch them manually.
  - If reverting completely to the pre-upgrade snapshot, any orders placed during the failed P1 window must be exported before database restoration.

## Recovery model

New uploads are streamed into temporary files on the persistent uploads mount.
The API assigns each attachment a random stored name, then commits the order,
its token hash, and file/info/email jobs in one SQLite transaction after the
local sources exist. A crash before commit can leave an orphan source file, but
cannot leave a committed order without delivery jobs. Orphans may be inspected
and removed only after checking the database; no automatic cleanup is performed.

The worker claims due jobs with a SQLite write transaction and a three-minute
lease. Failures retain local files and retry with bounded exponential backoff.
Expired leases are eligible again after a process crash. Lease fencing ensures
that a delayed worker whose lease expired cannot clobber a newly claimed active lease.
Retries are capped at a maximum attempt limit (default: 10, configurable via
`DELIVERY_MAX_ATTEMPTS`) to quarantine unrecoverable tasks for inspection.
Delivery state is in the same persistent database as the order. The Yandex upload uses
the existing OAuth token, `overwrite=false`, and verifies remote SHA-256 and size before
marking a job delivered. A matching remote file makes retries idempotent; a
different file at that path is an error. Local sources remain available after
delivery for manual recovery and backup; plan storage monitoring and a separate
retention policy before deleting them.

Upload limits are aligned across the containerized reverse proxy (`client_max_body_size 100M`)
and backend (20 MiB per file, 80 MiB per order) to guarantee that multipart requests within
backend constraints are accepted.

Old orders have no token hash. They remain readable in the database, and
existing signed Robokassa ResultURL callbacks can still complete initiated
payments. Creating a new payment link for an old order is blocked until an
operator has authenticated the customer through a separate, approved flow.
Old files are not renamed or requeued automatically.

SMTP jobs also persist. An SMTP failure leaves the job retryable. Delivery is
at least once: if the SMTP server accepts a message and the process crashes
before the success record is committed, a duplicate message remains possible.
Payment status itself remains terminal and is never retried as a new charge.
