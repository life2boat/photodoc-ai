# PhotoDoc AI — Production Release & Deployment Guide

This document defines the production deployment, database migration, permission cutover, and rollback procedures for PhotoDoc AI on the target production server.

---

## 1. Audited Production Architecture

The production environment on `79.137.196.14` was audited and is **already containerized**:

```text
CURRENT_PRODUCTION_ALREADY_CONTAINERIZED=true
CURRENT_RUNTIME=docker compose
CURRENT_PATH=/opt/photodoc
CURRENT_ENV_FILE=/opt/photodoc/backend/.env
LEGACY_ROLLBACK_ARTIFACT=/opt/photodoc/release.zip (DO NOT USE)
ROLLBACK_FORMAT=tar.gz with POSIX paths and manifest.sha256

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

To safely reuse the existing production database, uploads, and runtime credentials without data loss or path divergence, deployment uses the production overlay:

```bash
docker compose -f docker-compose.yml -f docker-compose.production.yml config
```

### 2.1 Storage & Runtime Mapping Contract
- **Database Volume**: `photodoc_db` is an **external** volume mapping to physical volume `photodoc_backend-data` (`${PHOTODOC_DB_VOLUME:-photodoc_backend-data}`). Compose must fail if it does not already exist; it must never create an empty replacement production volume.
- **Uploads Directory**: maps to existing host bind mount `/opt/photodoc/backend/uploads` (`${PHOTODOC_UPLOADS_SOURCE:-/opt/photodoc/backend/uploads}`).
- **Backend Secrets**: loaded directly from existing `/opt/photodoc/backend/.env` via `env_file`.
- **Frontend Port**: bound strictly to `127.0.0.1:${PORT:-8080}:80`.
- **Database Identity**: production sets `PHOTODOC_REQUIRE_DB_IDENTITY=1`. SQLite `PRAGMA application_id` must equal decimal `1346650441` (big-endian ASCII `PDAI`) and the complete `orders` schema must be readable before the API starts.

---

## 3. Required Environment Variables

All backend production secrets reside in `/opt/photodoc/backend/.env`. Restrict permissions: `chmod 600 /opt/photodoc/backend/.env`.

| Variable | Description | Target Requirement |
| :--- | :--- | :--- |
| `DATABASE_PATH` | Path inside container | `/data/orders.db` (deployment managed) |
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
| `VITE_SUPABASE_URL` | Supabase URL | *Optional / Unused in Production Bundle* |
| `VITE_SUPABASE_ANON_KEY` | Public Anon Key | *Optional / Unused in Production Bundle* |

> **Note on Frontend Supabase Variables**:
> Source audit confirms `SUPABASE_CLIENT_IMPORTED_IN_PRODUCTION_BUNDLE=false`. The client `frontend/src/lib/supabase.js` is not imported anywhere in the production SPA. Therefore, missing Supabase credentials do not block the frontend build.

---

## 4. Production Upgrade Sequence (Step-by-Step)

For all production operations, consistently use the production Compose pair:
`docker compose -f docker-compose.yml -f docker-compose.production.yml ...`

### Step 1: Preflight Verification (Fail-Closed)
Before touching any services, verify that:
1. All required environment variable names are present in `/opt/photodoc/backend/.env` (without printing secret values).
2. The external database volume exists. Do this before any Compose create/start command.
3. The existing legacy database is readable, non-empty, and has an `orders` table. The one-time unmarked exception is explicit and is valid only before reconciliation.

```bash
# 1. Environment preflight
python scripts/production_preflight.py --check-env /opt/photodoc/backend/.env

# 2. External volume existence (read-only inspection; MUST succeed)
docker volume inspect "${PHOTODOC_DB_VOLUME:-photodoc_backend-data}" >/dev/null

# 3. One-time legacy database preflight (via read-only mount)
docker run --rm \
  -v photodoc_backend-data:/data:ro \
  -v "$PWD/scripts:/scripts:ro" \
  -v "$PWD/backend:/backend:ro" \
  python:3.11-alpine \
  python /scripts/production_preflight.py --db /data/orders.db --check-only --allow-legacy-unmarked
```
Expected output:
```text
ROBOKASSA_MERCHANT_LOGIN=PRESENT
ROBOKASSA_PASSWORD1=PRESENT
ROBOKASSA_PASSWORD2=PRESENT
ROBOKASSA_IS_TEST=PRESENT
SMTP_SERVER=PRESENT
SMTP_PORT=PRESENT
SMTP_USER=PRESENT
SMTP_PASSWORD=PRESENT
EMAIL_TO=PRESENT
YANDEX_DISK_TOKEN=PRESENT
ENV_SECRET_VALUES_PRINTED=false
READY_FOR_CUTOVER=true

DB_EXISTS=true
DB_NONZERO=true
ORDERS_TABLE_PRESENT=true
SCHEMA_READABLE=true
READY_FOR_MIGRATION=true
LEGACY_UNMARKED=true
```
If either check fails, **STOP IMMEDIATELY**. Do not proceed with cutover.

### Step 2: Stop Old Backend Writer
Stop the existing backend to halt writes:
```bash
docker compose -f docker-compose.yml -f docker-compose.production.yml stop backend
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
The production DB is partially migrated (missing `order_amount` and `created_at`). Run `reconcile_payment_schema.py` using the production compose overlay:

```bash
# 1. Dry run check
docker compose -f docker-compose.yml -f docker-compose.production.yml run --rm --no-deps \
  backend \
  python migrations/reconcile_payment_schema.py --db /data/orders.db --check --allow-legacy-unmarked

# 2. Apply reconciliation
docker compose -f docker-compose.yml -f docker-compose.production.yml run --rm --no-deps \
  backend \
  python migrations/reconcile_payment_schema.py --db /data/orders.db --apply --allow-legacy-unmarked
```
Expected output:
```text
COLUMNS_ADDED=order_amount,created_at
SCHEMA_STATUS=COMPLETE
MIGRATION_REQUIRED=false
DB_APPLICATION_ID=1346650441
```

The migration uses `BEGIN IMMEDIATE`; missing `ALTER TABLE` operations, the identity update, schema verification, and integrity verification are one transaction. Any failure rolls the entire transaction back.

### Step 5b: Strict Identity-Aware Preflight
The legacy flag is forbidden after reconciliation:

```bash
docker run --rm \
  -v photodoc_backend-data:/data:ro \
  -v "$PWD/scripts:/scripts:ro" \
  -v "$PWD/backend:/backend:ro" \
  python:3.11-alpine \
  python /scripts/production_preflight.py --db /data/orders.db --check-only
```

Required: `DB_IDENTITY_VALID=true`, `READY_FOR_STARTUP=true`, and `DB_APPLICATION_ID=1346650441`. Stop if any differs.

### Step 6: Permission Preparation for UID 1000 (`appuser`)
The existing database file was owned by `root:root (0644)`. The new backend runs as non-root `UID 1000`. Prepare permissions:

```bash
docker compose -f docker-compose.yml -f docker-compose.production.yml run --rm --no-deps \
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
docker compose -f docker-compose.yml -f docker-compose.production.yml exec -T backend sh -c '
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

### 5.1 Required Rollback Artifact Gate

The legacy `/opt/photodoc/release.zip` is not an acceptable rollback artifact. Build the known-good source bundle before cutover:

```bash
python scripts/build_rollback_bundle.py \
  --source /path/to/known-good-release-tree \
  --output /secure/rollback/photodoc-known-good-pre-upgrade.tar.gz \
  --manifest-output /secure/rollback/manifest.sha256

sha256sum /secure/rollback/photodoc-known-good-pre-upgrade.tar.gz \
  > /secure/rollback/photodoc-known-good-pre-upgrade.tar.gz.sha256

RESTORE_REHEARSAL="$(mktemp -d)"
python scripts/verify_rollback_bundle.py \
  --bundle /secure/rollback/photodoc-known-good-pre-upgrade.tar.gz \
  --extract-to "${RESTORE_REHEARSAL}" \
  --require docker-compose.yml \
  --require backend/main.py \
  --require frontend/nginx.conf
```

Before stopping the old production application, all of these must be true:

```text
ROLLBACK_BUNDLE_PRESENT=true
ROLLBACK_BUNDLE_HASH_VALID=true
ROLLBACK_LINUX_RESTORE_REHEARSED=true
```

The bundle and its manifest must contain no `.env`, uploads, database, backups, logs, tokens, private keys, credentials, or runtime state.

### 5.2 Mode 1: Rollback to the Verified Previous Container Release

If the new stack fails cutover:

1. **Stop new containers**:
   ```bash
   docker compose -f docker-compose.yml -f docker-compose.production.yml down
   ```
2. **Verify the immutable bundle hash, then restore into an empty staging directory**:
   ```bash
   cd /secure/rollback
   sha256sum -c photodoc-known-good-pre-upgrade.tar.gz.sha256
   RESTORE_DIR="$(mktemp -d)"
   python /opt/photodoc/scripts/verify_rollback_bundle.py \
     --bundle photodoc-known-good-pre-upgrade.tar.gz \
     --extract-to "${RESTORE_DIR}" \
     --require docker-compose.yml \
     --require backend/main.py \
     --require frontend/nginx.conf
   ```
3. Install the verified staged release files using the approved release procedure. Preserve `.env`, uploads, database volumes, backups, and logs; none are supplied by the bundle.
4. **Restore database backup (if schema rollback is required)**:
   ```bash
   docker run --rm \
     -v photodoc_backend-data:/data \
     -v "$PWD/backups:/backup" \
     alpine \
     cp "/backup/${BACKUP_NAME}" /data/orders.db
   ```
5. **Permissions compatibility on rollback**:
   The previous production backend runs as `root`. Root retains full read/write access to files owned by `UID 1000` (`OLD_ROOT_BACKEND_CAN_ACCESS_UID1000_DB=true`). No reverse chown is required.
6. **Start previous stack**:
   ```bash
   docker compose up -d
   ```
7. **Verify rollback**:
   ```bash
   curl -f http://127.0.0.1:8080/api/health
   ```

### 5.3 Mode 2: Future Git-Based Container Releases Rollback

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
