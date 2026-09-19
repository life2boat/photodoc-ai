# PhotoDoc AI — Production Release & Deployment Guide

This document defines the production deployment, database migration, permission cutover, and rollback procedures for PhotoDoc AI on the target production server.

---

## 1. Audited Production Architecture

The production environment on `79.137.196.14` was audited and is **already containerized**:

```text
CURRENT_PRODUCTION_ALREADY_CONTAINERIZED=true
CURRENT_RUNTIME=docker compose
CURRENT_PATH=/opt/photodoc
ROLLBACK_ARTIFACT=/opt/photodoc/release.zip

CURRENT_FRONTEND=photodoc-frontend (bound to 127.0.0.1:8080)
CURRENT_BACKEND=photodoc-backend (internal port 8000)

EXISTING_DB_VOLUME=photodoc_backend-data
EXISTING_DB_FILE=orders.db
HOST_DB_PATH=/var/lib/docker/volumes/photodoc_backend-data/_data/orders.db
EXISTING_DB_OWNER=root:root (mode 0644)

EXISTING_UPLOAD_PATH=/opt/photodoc/backend/uploads
```

### 1.1 Network & Infrastructure Status (Blockers)

```text
PORT_443_OWNER=XRAY (Marzban VPN - DO NOT TOUCH)
PORT_80=unused
DOMAIN=photodoc-ai.ru (NXDOMAIN)
PUBLIC_HTTPS_ROUTING=UNRESOLVED
MANUAL_SUPABASE_ROTATION_REQUIRED=YES
ROTATION_CONFIRMED=false
READY_FOR_PRODUCTION_DEPLOY=false
```

- **FAIL-CLOSED RULE**: Plaintext HTTP is never exposed publicly.
- **MARZBAN ISOLATION**: Do NOT modify `/opt/marzban`, `/var/lib/marzban`, Xray processes, certificates, or port 443 configurations during PhotoDoc AI operations.

---

## 2. Storage Strategy & Production Overlay

To safely reuse the existing production database and uploads without data loss or path divergence, deployment uses the production overlay:

```bash
docker compose -f docker-compose.yml -f docker-compose.production.yml config
```

### 2.1 Storage Mapping Contract
- **Database Volume**: `photodoc_db` maps to physical volume `photodoc_backend-data` (`${PHOTODOC_DB_VOLUME:-photodoc_backend-data}`).
- **Uploads Directory**: maps to existing host bind mount `/opt/photodoc/backend/uploads` (`${PHOTODOC_UPLOADS_SOURCE:-/opt/photodoc/backend/uploads}`).
- **Frontend Port**: bound strictly to `127.0.0.1:${PORT:-8080}:80`.

---

## 3. Required Environment Variables

All production environment variables reside in `/opt/photodoc/.env`. Restrict permissions: `chmod 600 /opt/photodoc/.env`.

| Variable | Description | Target Requirement |
| :--- | :--- | :--- |
| `DATABASE_PATH` | Path inside container | `/data/orders.db` |
| `PHOTODOC_DB_VOLUME` | Physical volume name | `photodoc_backend-data` |
| `PHOTODOC_UPLOADS_SOURCE` | Host uploads path | `/opt/photodoc/backend/uploads` |
| `ROBOKASSA_MERCHANT_LOGIN` | Robokassa Merchant Login | *Production Login* |
| `ROBOKASSA_PASSWORD1` | Payment Pass1 | *Production Pass1* |
| `ROBOKASSA_PASSWORD2` | Notification Pass2 | *Production Pass2* |
| `ROBOKASSA_IS_TEST` | Mode toggle | **`0` for real production** |
| `SMTP_SERVER` | SMTP host | `smtp.yandex.ru` |
| `SMTP_PORT` | SMTP SSL/TLS port | `465` |
| `SMTP_USER` | SMTP sender | `notifications@photodoc-ai.ru` |
| `SMTP_PASSWORD` | App Password | *Production App Password* |
| `EMAIL_TO` | Recipient | `owner@photodoc-ai.ru` |
| `YANDEX_DISK_TOKEN` | OAuth token | *OAuth Token* |
| `PORT` | Localhost bind | `8080` (bound to `127.0.0.1`) |
| `VITE_API_URL` | Frontend API route | `/api` |
| `VITE_YANDEX_METRIKA_ID` | Metrika ID | *Production ID* |
| `VITE_SUPABASE_URL` | Supabase URL | `https://*.supabase.co` |
| `VITE_SUPABASE_ANON_KEY` | Public Anon Key | *Rotated Public Anon Key* |

---

## 4. Production Upgrade Sequence (Step-by-Step)

Follow this deterministic sequence for upgrading `/opt/photodoc`:

### Step 1: Preflight Verification (Fail-Closed)
Before touching any services, verify that the existing database volume is present, readable, and non-empty:

```bash
docker run --rm \
  -v photodoc_backend-data:/data:ro \
  -v "$PWD/scripts:/scripts:ro" \
  python:3.11-alpine \
  python /scripts/production_preflight.py --db /data/orders.db --check-only
```
Expected output:
```text
DB_EXISTS=true
DB_NONZERO=true
ORDERS_TABLE_PRESENT=true
SCHEMA_READABLE=true
READY_FOR_MIGRATION=true
```
If this fails, **STOP IMMEDIATELY**. Do not proceed with cutover.

### Step 2: Stop Old Backend Writer
Stop the existing backend to halt writes:
```bash
docker compose stop backend
```
*(Do NOT run `docker compose down -v`! Managed volumes must be preserved).*

### Step 3: Create Volume-Aware Database Backup
```bash
mkdir -p "$PWD/backups"
BACKUP_NAME="orders.db.preupgrade.$(date +%Y%m%d%H%M%S)"
docker run --rm \
  -v photodoc_backend-data:/data \
  -v "$PWD/backups:/backup" \
  alpine \
  cp /data/orders.db "/backup/${BACKUP_NAME}"

test -s "$PWD/backups/${BACKUP_NAME}"
echo "BACKUP_CREATED=true"
```

### Step 4: Pre-migration Integrity Check
```bash
docker run --rm \
  -v "$PWD/backups:/backup" \
  alpine sh -c "apk add --no-cache sqlite >/dev/null && sqlite3 /backup/${BACKUP_NAME} 'PRAGMA integrity_check;'"
# Output MUST be: ok
```

### Step 5: Idempotent Schema Reconciliation
The production DB is partially migrated (missing `order_amount` and `created_at`). Run `reconcile_payment_schema.py`:

```bash
# 1. Dry run check
docker compose run --rm --no-deps \
  -v photodoc_backend-data:/data \
  backend \
  python migrations/reconcile_payment_schema.py --db /data/orders.db --check

# 2. Apply reconciliation
docker compose run --rm --no-deps \
  -v photodoc_backend-data:/data \
  backend \
  python migrations/reconcile_payment_schema.py --db /data/orders.db --apply
```
Expected output:
```text
COLUMNS_ADDED=order_amount,created_at
SCHEMA_STATUS=COMPLETE
MIGRATION_REQUIRED=false
```

### Step 6: Permission Preparation for UID 1000 (`appuser`)
The existing database file was owned by `root:root (0644)`. The new backend runs as non-root `UID 1000`. Prepare permissions:

```bash
docker compose run --rm --no-deps \
  -v photodoc_backend-data:/data \
  --user root \
  backend \
  sh -c '
    chown -R 1000:1000 /data &&
    test -w /data/orders.db
  '
```

### Step 7: Launch New Application Stack
```bash
# Validate effective compose configuration
docker compose -f docker-compose.yml -f docker-compose.production.yml config

# Build and start services
docker compose -f docker-compose.yml -f docker-compose.production.yml build
docker compose -f docker-compose.yml -f docker-compose.production.yml up -d --wait --wait-timeout 180
```

### Step 8: Post-Deployment Smoke Verification
```bash
# 1. Backend health via local proxy
curl -f http://127.0.0.1:8080/api/health
# Expected: {"status":"ok"}

# 2. Frontend SPA root
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8080/
# Expected: 200

# 3. Open Graph asset
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8080/og/photodoc-og.jpg
# Expected: 200

# 4. Backend database write verification
docker compose exec -T backend sh -c '
  test -w /data &&
  test -w /data/orders.db &&
  test -w /app/uploads
'
```

---

## 5. Rollback Procedures

> [!CAUTION]
> **CRITICAL PRODUCTION RULE**:
> NEVER run `docker compose down -v` against the production environment.
> The `-v` flag deletes named volumes (`photodoc_backend-data`), destroying order history and payment records.
> `PRODUCTION_RUNBOOK_DOWN_V=false`
> `ROLLBACK_DELETES_PRODUCTION_VOLUME=false`

### 5.1 Mode 1: Rollback to Previous Container Release (`/opt/photodoc/release.zip`)

If the new stack fails cutover:

1. **Stop new containers**:
   ```bash
   docker compose -f docker-compose.yml -f docker-compose.production.yml down
   ```
2. **Restore previous application files**:
   ```bash
   unzip -o /opt/photodoc/release.zip -d /opt/photodoc/
   ```
3. **Restore database backup (if schema rollback is required)**:
   ```bash
   docker run --rm \
     -v photodoc_backend-data:/data \
     -v "$PWD/backups:/backup" \
     alpine \
     cp "/backup/${BACKUP_NAME}" /data/orders.db
   ```
4. **Permissions compatibility on rollback**:
   The previous production backend runs as `root`. Root retains full read/write access to files owned by `UID 1000` (`OLD_ROOT_BACKEND_CAN_ACCESS_UID1000_DB=true`). No reverse chown is required.
5. **Start previous stack**:
   ```bash
   docker compose up -d
   ```
6. **Verify rollback**:
   ```bash
   curl -f http://127.0.0.1:8080/api/health
   ```

### 5.2 Mode 2: Future Git-Based Container Releases Rollback

For future releases managed via Git commits:

1. Stop current containers:
   ```bash
   docker compose -f docker-compose.yml -f docker-compose.production.yml down
   ```
2. Check out previous known-good commit:
   ```bash
   git checkout <PREVIOUS_COMMIT>
   ```
3. If database restore is needed, copy pre-deployment backup into `photodoc_backend-data`.
4. Rebuild and launch:
   ```bash
   docker compose -f docker-compose.yml -f docker-compose.production.yml up -d --build
   ```
5. Verify health:
   ```bash
   curl -f http://127.0.0.1:8080/api/health
   ```
