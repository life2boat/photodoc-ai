# PhotoDoc AI — Production Release & Deployment Guide

This document defines the production deployment, database migration, and rollback procedures for PhotoDoc AI.

---

## 1. Prerequisites & Architecture

- **Host OS**: Ubuntu 22.04+ / Debian 12+ (or compatible Linux distribution)
- **Runtime**: Docker Engine 24.0+ with Docker Compose v2 (`docker compose`)
- **Utilities**: `sqlite3`, `curl`, `git`
- **Reverse Proxy / TLS**: Host-level Nginx, Caddy, Cloudflare Tunnel, or cloud load balancer terminating TLS for `https://photodoc-ai.ru/` and forwarding traffic to `127.0.0.1:8080`.

### 1.1 Network & TLS Architecture (Fail-Closed Default)

The production canonical URL is:
```text
https://photodoc-ai.ru/
```

The Docker Compose baseline binds the frontend container strictly to `localhost` to prevent plaintext public HTTP exposure:
```yaml
ports:
  - "127.0.0.1:${PORT:-8080}:80"
```

Deployment metadata:
```text
APPLICATION_LISTEN=127.0.0.1:8080
TLS_TERMINATION=EXTERNAL_REQUIRED
PUBLIC_HTTPS_ENTRYPOINT=UNRESOLVED_UNTIL_SERVER_AUDIT
PUBLIC_PLAINTEXT_HTTP_EXPOSURE=false
TLS_PRODUCTION_ARCHITECTURE_RESOLVED=false
READY_FOR_PRODUCTION_DEPLOY=false
```

Plaintext HTTP is never exposed publicly to ensure user uploads, payment callbacks, and session data remain secure.

---

## 2. Required Environment Variables

All production environment variables must be placed in a host `.env` file located in the project root.

> **CRITICAL**: Never commit `.env` to version control. Set restrictive permissions: `chmod 600 .env`.

| Variable | Description | Default / Production Requirement |
| :--- | :--- | :--- |
| `DATABASE_PATH` | Path to persistent SQLite DB inside container | `/data/orders.db` |
| `ROBOKASSA_MERCHANT_LOGIN` | Merchant identifier in Robokassa | *Production Login* |
| `ROBOKASSA_PASSWORD1` | Payment initialization password (Pass1) | *Secret Pass1* |
| `ROBOKASSA_PASSWORD2` | Notification signature password (Pass2) | *Secret Pass2* |
| `ROBOKASSA_IS_TEST` | Test mode toggle (`0` for production, `1` for test) | **Must be set to `0` for real production payments** (Default: `1`) |
| `SMTP_SERVER` | SMTP host for notification emails | `smtp.yandex.ru` |
| `SMTP_PORT` | SMTP port (SSL/TLS) | `465` |
| `SMTP_USER` | SMTP username / sender address | `notifications@photodoc-ai.ru` |
| `SMTP_PASSWORD` | SMTP app password | *Secret App Password* |
| `EMAIL_TO` | Recipient address for new orders | `owner@photodoc-ai.ru` |
| `YANDEX_DISK_TOKEN` | OAuth token for Yandex.Disk upload API | *OAuth Token* |
| `PORT` | Localhost bind port for frontend Nginx | `8080` (bound to `127.0.0.1`) |
| `VITE_API_URL` | Frontend API base route | `/api` |
| `VITE_YANDEX_METRIKA_ID` | Production Yandex Metrika counter ID | *Numeric Counter ID* |
| `VITE_SUPABASE_URL` | Supabase project URL | `https://*.supabase.co` |
| `VITE_SUPABASE_ANON_KEY` | Supabase public anonymous key | *Public Anon Key* |

---

## 3. Credential Security & Supabase Gate

> **MANUAL ACTION REQUIRED**:
> The historical Supabase service secret (`sb_secret`) was previously committed in historical revisions.
> While `sb_secret` is completely absent from canonical `main` and production source code (`SB_SECRET_SEARCH=NO_MATCHES`),
> **manual rotation/revocation of the Supabase service key must still be performed in the Supabase Cloud Console**.
>
> Gate status:
> ```text
> MANUAL_SUPABASE_ROTATION_REQUIRED=YES
> ROTATION_CONFIRMED=false
> READY_FOR_PRODUCTION_DEPLOY=false
> ```
> Production deployment remains blocked until the operator confirms key rotation.

---

## 4. Database Pre-Deployment Backup & Migration Gate

The database file resides in the Docker persistent named volume `photodoc_db` mounted to `/data/orders.db`.

### 4.1 Volume-Aware Database Backup

Do NOT rely on host-internal Docker volume storage paths (e.g. `/var/lib/docker/volumes/...`), which are implementation-specific. Use a clean, volume-aware backup container:

```bash
# 1. Ensure backup directory exists on host
mkdir -p "$PWD/backups"

# 2. Stop write traffic
docker compose stop backend

# 3. Perform volume-aware pre-deployment backup
BACKUP_NAME="orders.db.predeploy.$(date +%Y%m%d%H%M%S)"
docker run --rm \
  -v photodoc_db:/data \
  -v "$PWD/backups:/backup" \
  alpine \
  sh -c "cp /data/orders.db /backup/${BACKUP_NAME}"

# 4. Verify backup file exists and is non-empty
test -s "$PWD/backups/${BACKUP_NAME}"
echo "BACKUP_CREATED=true"
echo "BACKUP_NONZERO=true"

# 5. Check pre-migration database integrity
sqlite3 "$PWD/backups/${BACKUP_NAME}" "PRAGMA integrity_check;"
# Output MUST be: ok
echo "PRE_MIGRATION_INTEGRITY=ok"
```

### 4.2 Migration Execution (Audit: RUN_ONCE_ONLY)

Migration `001_add_payment_fields.sql` uses `ALTER TABLE orders ADD COLUMN ...`, which is **not idempotent** in SQLite (`MIGRATION_IDEMPOTENT=false`, `RUN_ONCE_ONLY=true`). Attempting to re-run it on an already-migrated database will error with `duplicate column name`.

Execute the migration inside a temporary container or against the verified volume:

```bash
# Check if columns are already present
HAS_COL=$(docker run --rm -v photodoc_db:/data alpine sh -c '
  apk add --no-cache sqlite >/dev/null 2>&1
  sqlite3 /data/orders.db "PRAGMA table_info(orders);" | grep -c "payment_status" || true
')

if [ "$HAS_COL" -eq 0 ]; then
  echo "Applying migration 001_add_payment_fields.sql..."
  docker run --rm \
    -v photodoc_db:/data \
    -v "$PWD/backend/migrations:/migrations:ro" \
    alpine \
    sh -c '
      apk add --no-cache sqlite >/dev/null 2>&1
      sqlite3 /data/orders.db < /migrations/001_add_payment_fields.sql
    '
else
  echo "Payment columns already present. Skipping migration."
fi

# Check post-migration integrity
docker run --rm -v photodoc_db:/data alpine sh -c '
  apk add --no-cache sqlite >/dev/null 2>&1
  sqlite3 /data/orders.db "PRAGMA integrity_check;"
'
# Output MUST be: ok
echo "POST_MIGRATION_INTEGRITY=ok"
```

---

## 5. Container Deployment & Launch

```bash
# 1. Validate Compose configuration
docker compose config

# 2. Build containers cleanly
docker compose build --no-cache

# 3. Start containers in detached mode and wait for health
docker compose up -d --wait --wait-timeout 180

# 4. Verify container status and health
docker compose ps
```

---

## 6. Health & Smoke Verification

Execute smoke tests against the local binding `http://127.0.0.1:8080`:

1. **Backend Health Check via Nginx Proxy**:
   ```bash
   curl -f http://127.0.0.1:8080/api/health
   # Expected: {"status":"ok"}
   ```

2. **Frontend Root SPA**:
   ```bash
   curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8080/
   # Expected: 200
   ```

3. **Open Graph Asset**:
   ```bash
   curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8080/og/photodoc-og.jpg
   # Expected: 200
   ```

4. **Named Volume Writability & Persistence**:
   ```bash
   docker compose exec -T backend sh -c '
     test -w /data &&
     test -w /app/uploads &&
     touch /data/.write-test &&
     touch /app/uploads/.write-test &&
     rm /data/.write-test /app/uploads/.write-test
   '
   docker compose exec -T backend test -f /data/orders.db
   # Expected: exit code 0
   ```

5. **Nginx Client Max Body Size**:
   Verify client upload limit is `50M` in `frontend/nginx.conf`.

---

## 7. Rollback Procedures

### 7.1 Mode 1: First Containerized Release Rollback

Because the previous canonical production release did NOT use this container deployment layer, a container rollback cannot simply check out the previous commit.

Before production cutover, a production server audit must discover and record:
```text
CURRENT_PRODUCTION_RUNTIME
CURRENT_PRODUCTION_PATH
CURRENT_PRODUCTION_START_COMMAND
CURRENT_PRODUCTION_STOP_COMMAND
CURRENT_PRODUCTION_SERVICE
```

Current qualification state:
```text
FIRST_CONTAINER_RELEASE_ROLLBACK_RESOLVED=false
READY_FOR_PRODUCTION_DEPLOY=false
```

If the first container deployment fails cutover:
1. Stop Docker containers: `docker compose down -v`
2. If database was migrated, restore pre-deployment backup (see 7.3)
3. Resume previous native service using documented `CURRENT_PRODUCTION_START_COMMAND`

### 7.2 Mode 2: Future Container Releases Rollback

For subsequent container releases:

```bash
# 1. Stop current containers
docker compose down

# 2. Check out previous known-good release tag / commit
git checkout <PREVIOUS_KNOWN_GOOD_TAG_OR_COMMIT>

# 3. Rebuild and launch previous release
docker compose build --no-cache
docker compose up -d --wait --wait-timeout 180

# 4. Verify health
curl -f http://127.0.0.1:8080/api/health
```

### 7.3 Database Rollback (Restoring Volume-Aware Backup)

If a schema migration failed or corrupted data:

```bash
# 1. Stop backend container
docker compose stop backend

# 2. Restore database from pre-deployment backup using volume container
docker run --rm \
  -v photodoc_db:/data \
  -v "$PWD/backups:/backup" \
  alpine \
  sh -c "cp /backup/${BACKUP_NAME} /data/orders.db"

# 3. Verify integrity of restored database
docker run --rm -v photodoc_db:/data alpine sh -c '
  apk add --no-cache sqlite >/dev/null 2>&1
  sqlite3 /data/orders.db "PRAGMA integrity_check;"
'
# Output MUST be: ok

# 4. Restart backend
docker compose start backend

# 5. Verify backend health
curl -f http://127.0.0.1:8080/api/health
```
