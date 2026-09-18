# PhotoDoc AI — Production Release & Deployment Guide

This document defines the production deployment, database migration, and rollback procedures for PhotoDoc AI.

---

## 1. Prerequisites

- **Host OS**: Ubuntu 22.04+ / Debian 12+ (or compatible Linux distribution)
- **Runtime**: Docker Engine 24.0+ with Docker Compose v2 (`docker compose`)
- **Utilities**: `sqlite3`, `curl`, `git`
- **Network**: Port 80 / 443 accessible for inbound traffic, outbound HTTPS for Yandex.Disk API and SMTP

---

## 2. Required Environment Variables

All production environment variables must be placed in a host `.env` file located in the project root.

> **CRITICAL**: Never commit `.env` to version control. Set restrictive permissions: `chmod 600 .env`.

| Variable | Description | Example / Default |
| :--- | :--- | :--- |
| `DATABASE_PATH` | Path to persistent SQLite DB inside container | `/data/orders.db` |
| `ROBOKASSA_MERCHANT_LOGIN` | Merchant identifier in Robokassa | *Production Login* |
| `ROBOKASSA_PASSWORD1` | Payment initialization password (Pass1) | *Secret Pass1* |
| `ROBOKASSA_PASSWORD2` | Notification signature password (Pass2) | *Secret Pass2* |
| `ROBOKASSA_IS_TEST` | Test mode toggle (`0` for production, `1` for test) | `0` |
| `SMTP_SERVER` | SMTP host for notification emails | `smtp.yandex.ru` |
| `SMTP_PORT` | SMTP port (SSL/TLS) | `465` |
| `SMTP_USER` | SMTP username / sender address | `notifications@photodoc-ai.ru` |
| `SMTP_PASSWORD` | SMTP app password | *Secret App Password* |
| `EMAIL_TO` | Recipient address for new orders | `owner@photodoc-ai.ru` |
| `YANDEX_DISK_TOKEN` | OAuth token for Yandex.Disk upload API | *OAuth Token* |
| `PORT` | Host port for frontend Nginx | `80` |
| `VITE_API_URL` | Frontend API base route | `/api` |
| `VITE_YANDEX_METRIKA_ID` | Production Yandex Metrika counter ID | *Numeric Counter ID* |
| `VITE_SUPABASE_URL` | Supabase project URL | `https://*.supabase.co` |
| `VITE_SUPABASE_ANON_KEY` | Supabase public anonymous key | *Public Anon Key* |

---

## 3. Credential Security & Supabase Notice

> **MANUAL ACTION REQUIRED**:
> The historical Supabase service secret (`sb_secret`) was previously committed in historical revisions.
> While `sb_secret` is completely absent from canonical `main` and production source code (`SB_SECRET_SEARCH=NO_MATCHES`),
> **manual rotation/revocation of the Supabase service key must still be performed in the Supabase Cloud Console**.
> Do not use the compromised historical key.

---

## 4. Database Pre-Deployment Backup & Migration Gate

The database file resides in the Docker persistent volume mounted to `/data` (`/data/orders.db`).
When migrating an existing legacy SQLite database:

### Sequence
```text
1. Stop backend container / writer
2. Perform pre-deployment backup (PRE_DEPLOY_DB_BACKUP)
3. Run PRAGMA integrity_check before migration
4. Execute migration (001_add_payment_fields.sql)
5. Run PRAGMA integrity_check after migration
6. Start new containers
7. Execute health & smoke validation
```

### 4.1 Step-by-Step Migration Commands

```bash
# 1. Stop write traffic
docker compose stop backend

# 2. Locate persistent SQLite database
DB_FILE="/var/lib/docker/volumes/photodoc_db/_data/orders.db"
# (Or host path if bind-mounted)

# 3. Create timestamped pre-deployment backup
BACKUP_FILE="${DB_FILE}.bak.$(date +%Y%m%d%H%M%S)"
cp "$DB_FILE" "$BACKUP_FILE"
echo "Backup created at: $BACKUP_FILE"

# 4. Check integrity before migration
sqlite3 "$DB_FILE" "PRAGMA integrity_check;"
# Output MUST be: ok

# 5. Check if migration already ran (Audit: RUN_ONCE_ONLY)
# Migration 001 uses `ALTER TABLE orders ADD COLUMN ...` which is NOT idempotent in SQLite.
# Attempting to re-run on a migrated DB will result in 'duplicate column name' error.
HAS_COL=$(sqlite3 "$DB_FILE" "PRAGMA table_info(orders);" | grep -c "payment_status" || true)
if [ "$HAS_COL" -eq 0 ]; then
  echo "Applying migration 001_add_payment_fields.sql..."
  sqlite3 "$DB_FILE" < backend/migrations/001_add_payment_fields.sql
else
  echo "Columns already present. Skipping migration."
fi

# 6. Check integrity after migration
sqlite3 "$DB_FILE" "PRAGMA integrity_check;"
# Output MUST be: ok

# 7. Verify columns exist
sqlite3 "$DB_FILE" "PRAGMA table_info(orders);"
```

---

## 5. Container Deployment & Launch

```bash
# Validate Compose configuration
docker compose config

# Build containers cleanly
docker compose build --no-cache

# Start containers in detached mode
docker compose up -d

# Verify container health
docker compose ps
```

---

## 6. Health & Smoke Verification

1. **Backend Health Check**:
   ```bash
   curl -f http://localhost/api/health
   # Expected: {"status":"ok"}
   ```

2. **Frontend Root**:
   ```bash
   curl -I http://localhost/
   # Expected: HTTP/1.1 200 OK
   ```

3. **Open Graph Asset**:
   ```bash
   curl -I http://localhost/og/photodoc-og.jpg
   # Expected: HTTP/1.1 200 OK
   ```

4. **Nginx Client Max Body Size**:
   Verify client upload limit is `50M` in `frontend/nginx.conf`.

---

## 7. Rollback Plan

If health checks fail, unexpected runtime errors occur, or database integrity issues arise:

### 7.1 Application Rollback (Reverting Containers)

```bash
# 1. Stop current containers
docker compose down

# 2. Check out previous canonical release commit / tag
git checkout <PREVIOUS_RELEASE_TAG_OR_COMMIT>

# 3. Rebuild and restart previous containers
docker compose build
docker compose up -d

# 4. Verify previous health
curl -f http://localhost/api/health
```

### 7.2 Database Rollback (Restoring Backup)

If a migration failed or corrupted data:

```bash
# 1. Stop backend container
docker compose stop backend

# 2. Restore database from pre-deployment backup
cp "$BACKUP_FILE" "$DB_FILE"

# 3. Verify integrity of restored database
sqlite3 "$DB_FILE" "PRAGMA integrity_check;"
# Output MUST be: ok

# 4. Restart backend
docker compose start backend

# 5. Verify backend health
curl -f http://localhost/api/health
```
