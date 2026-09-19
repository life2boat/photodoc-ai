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
- **Backend Secrets**: loaded directly from existing `/opt/photodoc/backend/.env` via
  `PHOTODOC_ENV_FILE`; the staged candidate source never receives a secret copy.
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

For candidate application operations, consistently use the staged production Compose
pair with `--project-directory "${RELEASE_DIR}" -p photodoc`. Migration and database
preflight deliberately do not use Compose or a PhotoDoc application image.

The sequence below is mandatory and ordered. Do not move permission changes ahead of
the verified backup, and stop immediately if any gate fails.

### Step 1: Stage and Verify the Candidate Release

The release task supplies `CANDIDATE_SHA` only after the PR has been merged and the
exact `main` commit has qualified. Never substitute a PR head or a short SHA. Stage
that commit beside the running release; do not copy it over `/opt/photodoc`:

```bash
ACTIVE_RELEASE_DIR=/opt/photodoc
RELEASE_DIR=/opt/photodoc-next
CANDIDATE_SHA='<full 40-character SHA supplied by the release task>'
PHOTODOC_ENV_FILE=/opt/photodoc/backend/.env
export ACTIVE_RELEASE_DIR RELEASE_DIR CANDIDATE_SHA PHOTODOC_ENV_FILE

printf '%s\n' "${CANDIDATE_SHA}" | grep -Eq '^[0-9a-f]{40}$'
test "${RELEASE_DIR}" != "${ACTIVE_RELEASE_DIR}"
test ! -e "${RELEASE_DIR}"
git -C "${ACTIVE_RELEASE_DIR}" fetch origin main
git -C "${ACTIVE_RELEASE_DIR}" worktree add --detach "${RELEASE_DIR}" "${CANDIDATE_SHA}"

test "$(git -C "${RELEASE_DIR}" rev-parse HEAD)" = "${CANDIDATE_SHA}"
test -z "$(git -C "${RELEASE_DIR}" status --porcelain=v1 --untracked-files=no)"
for required in \
  backend/db_contract.py \
  backend/migrations/reconcile_payment_schema.py \
  scripts/production_preflight.py \
  docker-compose.yml \
  docker-compose.production.yml
do
  test -f "${RELEASE_DIR}/${required}"
done
echo "CANDIDATE_SHA=${CANDIDATE_SHA}"
echo "CANDIDATE_RELEASE_STAGED=true"
```

If `/opt/photodoc-next` already exists, stop and choose a new, explicitly documented
staging path. Do not delete, reuse, or overwrite an unknown release directory during
cutover preparation.

### Step 2: Environment Preflight

```bash
python "${RELEASE_DIR}/scripts/production_preflight.py" \
  --check-env "${PHOTODOC_ENV_FILE}"
```

Required: every required variable is `PRESENT`, `ENV_SECRET_VALUES_PRINTED=false`,
and `READY_FOR_CUTOVER=true`.

### Step 3: External Volume Existence

This read-only inspection must succeed before any Compose create/start command:

```bash
docker volume inspect "${PHOTODOC_DB_VOLUME:-photodoc_backend-data}" >/dev/null
```

### Step 4: Legacy Read-Only Database Preflight

```bash
docker run --rm \
  --user 65534:65534 \
  -v "${PHOTODOC_DB_VOLUME:-photodoc_backend-data}:/data:ro" \
  -v "${RELEASE_DIR}/scripts:/candidate/scripts:ro" \
  -v "${RELEASE_DIR}/backend:/candidate/backend:ro" \
  -w /candidate \
  python:3.11-alpine \
  python scripts/production_preflight.py \
    --db /data/orders.db \
    --check-only \
    --allow-legacy-unmarked
```

Required: `DB_EXISTS=true`, `DB_NONZERO=true`, `ORDERS_TABLE_PRESENT=true`,
`SCHEMA_READABLE=true`, `READY_FOR_MIGRATION=true`, and `LEGACY_UNMARKED=true`.

### Step 5: Confirm Rollback Artifact, Hash, and Rehearsal

The rollback bundle must already have been built using Section 5.1. Verify it before
stopping the current writer:

```bash
test -s /secure/rollback/photodoc-known-good-pre-upgrade.tar.gz
cd /secure/rollback
sha256sum -c photodoc-known-good-pre-upgrade.tar.gz.sha256
RESTORE_REHEARSAL="$(mktemp -d)"
python /opt/photodoc/scripts/verify_rollback_bundle.py \
  --bundle photodoc-known-good-pre-upgrade.tar.gz \
  --extract-to "${RESTORE_REHEARSAL}" \
  --require docker-compose.yml \
  --require backend/main.py \
  --require frontend/nginx.conf
cd "${ACTIVE_RELEASE_DIR}"
```

Required: `ROLLBACK_CONTENT_SECRET_SCAN=true`, `ROLLBACK_SECRET_VALUES_PRINTED=false`,
`ROLLBACK_BUNDLE_SECRET_MATCHES=0`, `FORBIDDEN_PATH_MATCHES=0`,
`MANIFEST_HASH_VERIFICATION=PASS`, and `LINUX_EXTRACTION_REHEARSAL=PASS`.

### Step 6: Stop the Old Backend Writer

```bash
docker compose -p photodoc \
  -f "${ACTIVE_RELEASE_DIR}/docker-compose.yml" \
  -f "${ACTIVE_RELEASE_DIR}/docker-compose.production.yml" \
  stop backend
```

Never run `docker compose down -v`; the production volume must be preserved.

### Step 7: Create the Database Backup

```bash
mkdir -p "${ACTIVE_RELEASE_DIR}/backups"
BACKUP_NAME="orders.db.preupgrade.$(date +%Y%m%d%H%M%S)"
docker run --rm \
  -v "${PHOTODOC_DB_VOLUME:-photodoc_backend-data}:/data:ro" \
  -v "${ACTIVE_RELEASE_DIR}/backups:/backup" \
  alpine \
  cp /data/orders.db "/backup/${BACKUP_NAME}"
test -s "${ACTIVE_RELEASE_DIR}/backups/${BACKUP_NAME}"
echo "BACKUP_CREATED=true"
```

No ownership or permission changes are allowed before this backup exists.

### Step 8: Verify Backup Integrity

```bash
docker run --rm \
  -v "${ACTIVE_RELEASE_DIR}/backups:/backup:ro" \
  alpine sh -c "apk add --no-cache sqlite >/dev/null && sqlite3 /backup/${BACKUP_NAME} 'PRAGMA integrity_check;'"
```

The only accepted integrity result is `ok`.

### Step 9: Reconcile the Schema from the Staged Candidate

Both commands mount migration code from the SHA-verified candidate release. They use
an explicit Python runtime and never create or start an application container. The
dry-run mounts the database volume read-only. APPLY runs as root because the audited
database is `root:root` mode `0644`:

```bash
# Read-only dry-run
docker run --rm \
  --user 65534:65534 \
  -v "${PHOTODOC_DB_VOLUME:-photodoc_backend-data}:/data:ro" \
  -v "${RELEASE_DIR}/backend:/candidate/backend:ro" \
  -w /candidate/backend \
  python:3.11-alpine \
  python migrations/reconcile_payment_schema.py \
    --db /data/orders.db \
    --check \
    --allow-legacy-unmarked

# Mutating reconciliation: explicit root is mandatory
docker run --rm \
  --user 0 \
  -v "${PHOTODOC_DB_VOLUME:-photodoc_backend-data}:/data" \
  -v "${RELEASE_DIR}/backend:/candidate/backend:ro" \
  -w /candidate/backend \
  python:3.11-alpine \
  python migrations/reconcile_payment_schema.py \
    --db /data/orders.db \
    --apply \
    --allow-legacy-unmarked
echo "MIGRATION_USES_CANDIDATE_CODE=true"
echo "MIGRATION_DEPENDS_ON_EXISTING_BACKEND_IMAGE=false"
echo "PRODUCTION_MIGRATION_CAN_WRITE_ROOT_OWNED_DB=true"
```

Required: `SCHEMA_STATUS=COMPLETE`, `MIGRATION_REQUIRED=false`,
`DB_APPLICATION_ID=1346650441`, and `PRODUCTION_MIGRATION_CAN_WRITE_ROOT_OWNED_DB=true`.
The migration uses one `BEGIN IMMEDIATE` transaction for schema changes, identity,
schema verification, and integrity verification.

### Step 10: Strict Post-Migration Identity Preflight

The legacy exception is forbidden after reconciliation:

```bash
docker run --rm \
  --user 65534:65534 \
  -v "${PHOTODOC_DB_VOLUME:-photodoc_backend-data}:/data:ro" \
  -v "${RELEASE_DIR}/scripts:/candidate/scripts:ro" \
  -v "${RELEASE_DIR}/backend:/candidate/backend:ro" \
  -w /candidate \
  python:3.11-alpine \
  python scripts/production_preflight.py --db /data/orders.db --check-only
```

Required: `DB_IDENTITY_VALID=true`, `READY_FOR_STARTUP=true`, and
`DB_APPLICATION_ID=1346650441`.

### Step 11: Build the Candidate Application Images

Migration does not build application images. Build them explicitly from the staged,
SHA-verified source before any candidate application container is created or started:

```bash
docker compose --project-directory "${RELEASE_DIR}" -p photodoc \
  -f "${RELEASE_DIR}/docker-compose.yml" \
  -f "${RELEASE_DIR}/docker-compose.production.yml" \
  config --quiet
docker compose --project-directory "${RELEASE_DIR}" -p photodoc \
  -f "${RELEASE_DIR}/docker-compose.yml" \
  -f "${RELEASE_DIR}/docker-compose.production.yml" \
  build
docker image inspect photodoc-backend photodoc-frontend >/dev/null
echo "CANDIDATE_IMAGES_BUILT=true"
```

`PHOTODOC_ENV_FILE` deliberately points at the existing production env file. The
candidate staging tree contains source only and must not contain copied secrets.

### Step 12: Prepare Database and Upload Permissions for UID 1000

The audited uploads bind `/opt/photodoc/backend/uploads` is also `root:root` mode
`0755`; do not omit it and do not delete or alter existing upload contents:

```bash
docker compose --project-directory "${RELEASE_DIR}" -p photodoc \
  -f "${RELEASE_DIR}/docker-compose.yml" \
  -f "${RELEASE_DIR}/docker-compose.production.yml" \
  run --rm --no-deps \
  --user root \
  backend \
  sh -c '
    chown -R 1000:1000 /data /app/uploads &&
    test -w /data/orders.db &&
    test -w /app/uploads
  '
```

### Step 13: Verify UID 1000 Writability

These commands run as the image's default `appuser` (UID 1000):

```bash
docker compose --project-directory "${RELEASE_DIR}" -p photodoc \
  -f "${RELEASE_DIR}/docker-compose.yml" \
  -f "${RELEASE_DIR}/docker-compose.production.yml" \
  run --rm --no-deps \
  backend \
  python -c "import sqlite3; c=sqlite3.connect('/data/orders.db'); c.execute('BEGIN IMMEDIATE'); c.rollback(); c.close()"
echo "DB_APPUSER_WRITABLE=true"

docker compose --project-directory "${RELEASE_DIR}" -p photodoc \
  -f "${RELEASE_DIR}/docker-compose.yml" \
  -f "${RELEASE_DIR}/docker-compose.production.yml" \
  run --rm --no-deps \
  backend \
  sh -c 'touch /app/uploads/.appuser-write-test && rm /app/uploads/.appuser-write-test'
echo "UPLOADS_APPUSER_WRITABLE=true"
```

### Step 14: Start the New Application from the Built Candidate Images

```bash
docker compose --project-directory "${RELEASE_DIR}" -p photodoc \
  -f "${RELEASE_DIR}/docker-compose.yml" \
  -f "${RELEASE_DIR}/docker-compose.production.yml" \
  up -d --no-build --wait --wait-timeout 180
```

### Step 15: Local Smoke Verification

```bash
curl -f http://127.0.0.1:8080/api/health
curl -f -o /dev/null http://127.0.0.1:8080/
curl -f -o /dev/null http://127.0.0.1:8080/og/photodoc-og.jpg
docker compose --project-directory "${RELEASE_DIR}" -p photodoc \
  -f "${RELEASE_DIR}/docker-compose.yml" \
  -f "${RELEASE_DIR}/docker-compose.production.yml" \
  exec -T backend sh -c '
  test -w /data/orders.db && test -w /app/uploads
'
```

Required final evidence: `PRODUCTION_SEQUENCE_VERIFIED=true` and
`PRODUCTION_RUNBOOK_DOWN_V=false`.

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

The bundle and its manifest must contain no `.env`, uploads, database, backups, logs,
tokens, private keys, credentials, or runtime state. Both the builder and verifier scan
file contents for private-key headers, `sb_secret` material, and non-placeholder
password/token/secret/private-key/credential assignments. Findings report only the
filename and category; matched secret values are never printed. A successful scan must
report `ROLLBACK_CONTENT_SECRET_SCAN=true`, `ROLLBACK_SECRET_VALUES_PRINTED=false`,
`ROLLBACK_BUNDLE_SECRET_MATCHES=0`, and `FORBIDDEN_PATH_MATCHES=0`.

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
