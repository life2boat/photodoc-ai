#!/usr/bin/env bash
# PhotoDoc AI — Staging Zero-Build Bootstrap & Deployment Script
# Release R4.3: Safe Staging Bootstrap & Deployment Gate
#
# Target Environment: Economical VPS (1 vCPU, 2 GB RAM, 25-30 GB NVMe, Ubuntu 24.04 LTS)
# Container Registry: GitHub Container Registry (ghcr.io/life2boat)
#
# STRICT SAFETY INVARIANTS:
# 1. Never runs or targets production server (79.137.196.14).
# 2. Never uses mutable tags (e.g. 'latest') as deployment identity.
# 3. Never compiles application code locally on the VPS (zero host builds).
# 4. Never executes volume teardown with flag -v or deletes staging volumes.
# 5. Never overwrites an existing database with a fresh empty database.
# 6. Delivery worker disabled by default (DELIVERY_WORKER_ENABLED=0).
# 7. Requires explicit confirmation (--confirm) for state-changing operations.
# 8. Never echoes tokens, passwords, or secret-bearing environment values.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

PRODUCTION_IP="79.137.196.14"
STAGING_COMPOSE_FILE="${REPO_ROOT}/docker-compose.staging.prebuilt.yml"
STAGING_OVERRIDE_FILE="${REPO_ROOT}/docker-compose.staging.prebuilt.delivery.yml"
RELEASE_MANIFEST_FILE="${REPO_ROOT}/release-manifest.json"

STAGING_PORT="${STAGING_PORT:-8081}"
CONFIRM=0
DRY_RUN=0
ROLLBACK_MODE=0
PREV_BACKEND_IMAGE=""
PREV_FRONTEND_IMAGE=""
TARGET_HOST="${PHOTODOC_TARGET_HOST:-}"

print_usage() {
    cat << 'EOF'
Usage: deploy_staging_prebuilt.sh [OPTIONS]

Options:
  --backend-image <ref>     Pinned backend image (e.g. ghcr.io/life2boat/photodoc-backend@sha256:...)
  --frontend-image <ref>    Pinned frontend image (e.g. ghcr.io/life2boat/photodoc-frontend@sha256:...)
  --target-host <host>      Target server host/IP (strictly rejects 79.137.196.14)
  --confirm                 Confirm deployment without interactive prompt
  --dry-run                 Run validations and migration pre-checks without updating containers
  --rollback <b_img> <f_img> Execute zero-build rollback to previously verified image digests
  --help                    Show this help message

Environment Variables:
  PHOTODOC_BACKEND_IMAGE    Alternative to --backend-image
  PHOTODOC_FRONTEND_IMAGE   Alternative to --frontend-image
  PHOTODOC_TARGET_HOST      Alternative to --target-host
  STAGING_PORT              Loopback port binding (default: 8081)
  GHCR_PAT                  Personal Access Token for private GHCR pulls (optional)
EOF
}

# Parse CLI arguments
while [[ $# -gt 0 ]]; do
    case "$1" in
        --backend-image)
            PHOTODOC_BACKEND_IMAGE="$2"
            shift 2
            ;;
        --frontend-image)
            PHOTODOC_FRONTEND_IMAGE="$2"
            shift 2
            ;;
        --target-host)
            TARGET_HOST="$2"
            shift 2
            ;;
        --confirm)
            CONFIRM=1
            shift
            ;;
        --dry-run)
            DRY_RUN=1
            shift
            ;;
        --rollback)
            ROLLBACK_MODE=1
            PREV_BACKEND_IMAGE="$2"
            PREV_FRONTEND_IMAGE="$3"
            shift 3
            ;;
        --help|-h)
            print_usage
            exit 0
            ;;
        *)
            echo "ERROR: Unknown option '$1'" >&2
            print_usage
            exit 1
            ;;
    esac
done

echo "======================================================================"
echo " PhotoDoc AI — Staging Zero-Build Bootstrap & Gatekeeper (R4.3)"
echo "======================================================================"

# ==============================================================================
# Step 0: Production Target Safety Guard
# ==============================================================================
echo "[0/10] Verifying Production Target Safety Guard..."

# Check explicit target host
#
# The canonical apex `photo1storage.ru` is intentionally NOT blocked here: the
# private staging VPS hostname IS that domain, so blocking it would reject the
# legitimate staging deployment on its own machine. Only the public `www`
# entrypoint and the legacy unregistered domain are rejected. Keep this list in
# step with PRODUCTION_TARGET_HOSTS in scripts/staging_deploy_preflight.py.
if [[ -n "${TARGET_HOST}" ]]; then
    if [[ "${TARGET_HOST}" == "${PRODUCTION_IP}" ]] \
        || [[ "${TARGET_HOST}" == *"photodoc-ai.ru"* ]] \
        || [[ "${TARGET_HOST}" == *"www.photo1storage.ru"* ]]; then
        echo "FATAL: Target host '${TARGET_HOST}' matches protected production infrastructure." >&2
        echo "Staging deployment is strictly prohibited on production target!" >&2
        exit 1
    fi
fi

# Check local machine network interfaces
if command -v hostname >/dev/null 2>&1; then
    LOCAL_IPS="$(hostname -I 2>/dev/null || true)"
    for ip in ${LOCAL_IPS}; do
        if [[ "${ip}" == "${PRODUCTION_IP}" ]]; then
            echo "FATAL: Local network interface (${ip}) matches production server ${PRODUCTION_IP}!" >&2
            echo "Refusing to execute staging deployment on production host." >&2
            exit 1
        fi
    done
fi
echo "       Production Target Guard: PASS (Target is NOT production)"

# ==============================================================================
# Step 1: System & Toolchain Availability
# ==============================================================================
echo "[1/10] Checking System & Container Tooling..."

if ! command -v docker >/dev/null 2>&1; then
    echo "FATAL: 'docker' command not found. Docker Engine must be installed." >&2
    exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
    echo "FATAL: 'docker compose' (Compose v2) not found." >&2
    exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
    echo "FATAL: 'python3' command not found. Python 3 is required for preflight verification." >&2
    exit 1
fi
echo "       Docker Engine and Compose: Available"

# ==============================================================================
# Step 2: Resource Envelope Verification (Low-Memory VPS)
# ==============================================================================
echo "[2/10] Checking VPS Resource Envelope..."

if command -v free >/dev/null 2>&1; then
    TOTAL_RAM_MB=$(free -m | awk '/^Mem:/{print $2}')
    AVAIL_RAM_MB=$(free -m | awk '/^Mem:/{print $7}')
    echo "       RAM: Total=${TOTAL_RAM_MB}MB, Available=${AVAIL_RAM_MB}MB"
    if [[ "${AVAIL_RAM_MB}" -lt 200 ]]; then
        echo "WARNING: Low available RAM (<200MB). Prebuilt zero-build execution avoids compilation spikes."
    fi
fi

if command -v df >/dev/null 2>&1; then
    FREE_DISK_KB=$(df -k . | awk 'NR==2 {print $4}')
    FREE_DISK_MB=$((FREE_DISK_KB / 1024))
    echo "       Disk: Free Space=${FREE_DISK_MB}MB"
    if [[ "${FREE_DISK_MB}" -lt 1000 ]]; then
        echo "FATAL: Insufficient disk space (<1000MB free). Aborting deployment." >&2
        exit 1
    fi
fi

# ==============================================================================
# Step 3: Handle Rollback Mode vs Standard Deployment
# ==============================================================================
if [[ "${ROLLBACK_MODE}" -eq 1 ]]; then
    echo "======================================================================"
    echo " EXECUTION MODE: IMMEDIATE ZERO-BUILD ROLLBACK"
    echo " Target Backend : ${PREV_BACKEND_IMAGE}"
    echo " Target Frontend: ${PREV_FRONTEND_IMAGE}"
    echo "======================================================================"

    if [[ -z "${PREV_BACKEND_IMAGE}" ]] || [[ -z "${PREV_FRONTEND_IMAGE}" ]]; then
        echo "FATAL: Rollback requires both previous backend and frontend image references." >&2
        exit 1
    fi

    PHOTODOC_BACKEND_IMAGE="${PREV_BACKEND_IMAGE}"
    PHOTODOC_FRONTEND_IMAGE="${PREV_FRONTEND_IMAGE}"
fi

# Ensure image variables are present
if [[ -z "${PHOTODOC_BACKEND_IMAGE:-}" ]] || [[ -z "${PHOTODOC_FRONTEND_IMAGE:-}" ]]; then
    echo "FATAL: Both PHOTODOC_BACKEND_IMAGE and PHOTODOC_FRONTEND_IMAGE must be specified." >&2
    print_usage
    exit 1
fi

# ==============================================================================
# Step 4: Staging Preflight Validation Gate
# ==============================================================================
echo "[3/10] Running Staging Deployment Preflight Verification..."

PREFLIGHT_ARGS=(
    "python3" "${SCRIPT_DIR}/staging_deploy_preflight.py"
    "--backend-image" "${PHOTODOC_BACKEND_IMAGE}"
    "--frontend-image" "${PHOTODOC_FRONTEND_IMAGE}"
    "--compose-file" "${STAGING_COMPOSE_FILE}"
    "--override-file" "${STAGING_OVERRIDE_FILE}"
)

if [[ -n "${TARGET_HOST}" ]]; then
    PREFLIGHT_ARGS+=("--target-host" "${TARGET_HOST}")
fi

if [[ -f "${RELEASE_MANIFEST_FILE}" ]]; then
    PREFLIGHT_ARGS+=("--manifest" "${RELEASE_MANIFEST_FILE}")
fi

if ! "${PREFLIGHT_ARGS[@]}"; then
    echo "FATAL: Staging deployment preflight gate FAILED. Aborting deployment." >&2
    exit 1
fi

if [[ "${DRY_RUN}" -eq 1 ]]; then
    echo "======================================================================"
    echo " DRY-RUN COMPLETE: All preflight gates PASSED."
    echo " No containers were modified. Zero host builds confirmed."
    echo "======================================================================"
    exit 0
fi

# ==============================================================================
# Step 5: GHCR Authentication (Optional / When Needed)
# ==============================================================================
echo "[4/10] Checking Registry Authentication..."

if [[ -n "${GHCR_PAT:-}" ]] || [[ -n "${GITHUB_TOKEN:-}" ]]; then
    REG_TOKEN="${GHCR_PAT:-$GITHUB_TOKEN}"
    REG_USER="${GHCR_USER:-life2boat}"
    echo "${REG_TOKEN}" | docker login ghcr.io -u "${REG_USER}" --password-stdin >/dev/null 2>&1
    echo "       GHCR Authenticated as ${REG_USER}"
else
    echo "       No GHCR token provided; pulling anonymously (supported for public packages)."
fi

# ==============================================================================
# Step 6: Pull Prebuilt Images Ahead of Startup
# ==============================================================================
echo "[5/10] Pulling Digest-Pinned Container Images from GHCR..."

PHOTODOC_BACKEND_IMAGE="${PHOTODOC_BACKEND_IMAGE}" \
PHOTODOC_FRONTEND_IMAGE="${PHOTODOC_FRONTEND_IMAGE}" \
docker compose -p photodoc-staging -f "${STAGING_COMPOSE_FILE}" pull

echo "       Images successfully pulled into local Docker cache."

# ==============================================================================
# Step 7: Resolve and Validate Docker Compose Configuration
# ==============================================================================
echo "[6/10] Validating Resolved Compose Configuration..."

RESOLVED_CFG=$(PHOTODOC_BACKEND_IMAGE="${PHOTODOC_BACKEND_IMAGE}" \
PHOTODOC_FRONTEND_IMAGE="${PHOTODOC_FRONTEND_IMAGE}" \
STAGING_PORT="${STAGING_PORT}" \
docker compose -p photodoc-staging -f "${STAGING_COMPOSE_FILE}" config)

if echo "${RESOLVED_CFG}" | grep -qi 'build:'; then
    echo "FATAL: Resolved Compose configuration contains 'build:' sections!" >&2
    exit 1
fi

echo "       Resolved Compose configuration is valid and zero-build."

# ==============================================================================
# Step 7a: Determine whether a database exists — proven, not assumed
# ==============================================================================
# Everything below depends on this answer, so an inconclusive probe is fatal.
# The probe runs as root: under an unprivileged UID a permission error is
# indistinguishable from "the file is not there", which is how a permission
# problem could previously be misread as a cold start.
echo "[7a/10] Determining whether a staging database exists..."

DB_VOLUME="photodoc_staging_backend-data"
DB_STATE="unknown"     # absent | present | unknown
DB_ABSENCE_PROVEN=0
DB_ABSENCE_EVIDENCE=""

# A daemon failure, a permission error or a timeout must never be read as "the
# volume does not exist". Confirm the daemon answers before drawing any
# conclusion, then require the *specific* not-found error for a cold start.
if ! docker info >/dev/null 2>&1; then
    echo "FATAL: the Docker daemon is unreachable; cannot determine whether a database exists."
    echo "       Refusing to guess, because guessing 'no database' skips the backup gate."
    exit 1
fi

set +e
VOLUME_INSPECT=$(docker volume inspect "${DB_VOLUME}" 2>&1)
VOLUME_RC=$?
set -e

if [[ "${VOLUME_RC}" -ne 0 ]]; then
    case "${VOLUME_INSPECT}" in
        *"no such volume"*|*"No such volume"*|*"no such object"*)
            DB_STATE="absent"
            DB_ABSENCE_PROVEN=1
            DB_ABSENCE_EVIDENCE="volume-not-found"
            echo "       Volume '${DB_VOLUME}' does not exist; this is a genuine cold start."
            ;;
        *)
            echo "FATAL: cannot determine whether the volume exists (inspect exit ${VOLUME_RC})."
            echo "       ${VOLUME_INSPECT}"
            echo "       A daemon, permission or timeout error is not proof of absence."
            exit 1
            ;;
    esac
else
    # The volume exists: probe the file as root. Under an unprivileged UID a
    # permission error is indistinguishable from "the file is not there".
    set +e
    docker run --rm --user 0 \
        -v "${DB_VOLUME}:/data:ro" \
        "${PHOTODOC_BACKEND_IMAGE}" \
        python -c "import os,sys
p='/data/orders.db'
if os.path.exists(p):
    sys.exit(0)
if not os.path.isdir('/data'):
    sys.exit(3)
sys.exit(1)"
    DB_PROBE=$?
    set -e
    case "${DB_PROBE}" in
        0) DB_STATE="present" ;;
        1) DB_STATE="absent"; DB_ABSENCE_PROVEN=1; DB_ABSENCE_EVIDENCE="file-absent" ;;
        *) echo "FATAL: cannot determine whether a database exists (probe exit ${DB_PROBE})."
           echo "       Refusing to treat an unreadable volume as a cold start."
           exit 1 ;;
    esac
    echo "       Volume '${DB_VOLUME}' exists; database state: ${DB_STATE}."
fi

# ==============================================================================
# Step 7b: Exclude writers, then verify the rollback backup gate (S6-01)
# ==============================================================================
# Two separate questions, both required:
#   1. Is anything still writing? A read transaction proves a consistent snapshot
#      but does not stop a later INSERT, so the writers are excluded *before* the
#      snapshot and held down until the cutover completes.
#   2. Is there a verified backup that matches the live rows?
# Migrating before this would change the database before we know a rollback point
# exists, and would change the schema the gate is asked to approve.
echo "[7b/10] Excluding writers and verifying the rollback backup gate..."

BACKUP_ROOT="${BACKUP_ROOT:-/var/backups/photodoc}"
BACKUP_MAX_AGE_HOURS="${BACKUP_MAX_AGE_HOURS:-24}"

# The worker state is read from the compose file rather than assumed. "unknown" is
# treated as not-proven by the gate, so a missing or unparseable file blocks the
# deployment instead of silently passing.
WORKER_STATE="unknown"
if [[ -f "${STAGING_COMPOSE_FILE}" ]]; then
    set +e
    _WORKER_FLAG=$(grep -Eo 'DELIVERY_WORKER_ENABLED:[[:space:]]*"?[01]"?|DELIVERY_WORKER_ENABLED=[01]' \
        "${STAGING_COMPOSE_FILE}" 2>/dev/null | grep -Eo '[01]' | head -1)
    set -e
    case "${_WORKER_FLAG:-}" in
        0) WORKER_STATE="no" ;;
        1) WORKER_STATE="yes" ;;
        *) WORKER_STATE="unknown" ;;
    esac
fi
echo "       Delivery worker state (from compose): ${WORKER_STATE}"

if [[ "${DB_STATE}" == "present" ]]; then
    DB_HOST_PATH="/var/lib/docker/volumes/${DB_VOLUME}/_data/orders.db"
    echo "       Checking that no process holds the database open..."
    set +e
    "${PYTHON_BIN:-python3}" "${REPO_ROOT}/scripts/writer_barrier.py" \
        --db "${DB_HOST_PATH}" --volume "${DB_VOLUME}"
    BARRIER_RC=$?
    set -e
    case "${BARRIER_RC}" in
        0) echo "       Writer barrier passed: no container or process holds the database." ;;
        3) echo "FATAL: this host cannot inspect processes, so writer exclusion cannot be proven."
           echo "       Refusing to deploy (documented boundary: docs/BACKUP_AND_RESTORE.md)."
           exit 1 ;;
        *) echo "FATAL: writers are active or exclusion cannot be demonstrated."
           echo "       Stop the application container and any worker before deploying:"
           echo "         docker compose -p photodoc-staging stop backend"
           exit 1 ;;
    esac
    # The exact instance the gate is about to approve. If it changes before the
    # migration, something wrote after the check and the approval is stale.
    DB_IDENTITY_AT_GATE=$(stat -c '%d:%i:%s:%Y' "${DB_HOST_PATH}" 2>/dev/null || echo unknown)
fi

GATE_STATUS=0
set +e
if [[ "${DB_STATE}" == "present" ]]; then
    DB_VOLUME="${DB_VOLUME}" \
    BACKUP_ROOT="${BACKUP_ROOT}" \
    BACKUP_MAX_AGE_HOURS="${BACKUP_MAX_AGE_HOURS}" \
    SKIP_BACKUP_GATE="${SKIP_BACKUP_GATE:-0}" \
    PHOTODOC_BACKEND_IMAGE="${PHOTODOC_BACKEND_IMAGE}" \
    DELIVERY_WORKER_ENABLED_STATE="${WORKER_STATE}" \
    REPO_ROOT="${REPO_ROOT}" \
    GATE_MODE=docker \
    bash "${REPO_ROOT}/scripts/deploy_backup_gate.sh"
    GATE_STATUS=$?
else
    GATE_MODE=cold DB_ABSENCE_PROVEN="${DB_ABSENCE_PROVEN}" \
        DB_ABSENCE_EVIDENCE="${DB_ABSENCE_EVIDENCE}" \
        LIVE_DB="" REPO_ROOT="${REPO_ROOT}" \
        bash "${REPO_ROOT}/scripts/deploy_backup_gate.sh"
    GATE_STATUS=$?
fi
set -e

if [[ "${GATE_STATUS}" -ne 0 ]]; then
    echo ""
    echo "FATAL: backup gate refused this deployment (exit ${GATE_STATUS})."
    echo "       Refusing to touch running data without a verified rollback path."
    exit 1
fi

# ==============================================================================
# Confirmation Gate — BEFORE any database mutation
# ==============================================================================
# Everything up to here is read-only (probe, barrier, gate). The next step writes
# to the database, so the operator approves *now*. A refusal leaves persistent
# state untouched.
if [[ "${CONFIRM}" -eq 0 ]]; then
    echo ""
    echo "----------------------------------------------------------------------"
    echo " READY TO APPLY UPDATE:"
    echo " Backend  : ${PHOTODOC_BACKEND_IMAGE}"
    echo " Frontend : ${PHOTODOC_FRONTEND_IMAGE}"
    echo " Port     : 127.0.0.1:${STAGING_PORT}"
    echo " Database : ${DB_STATE} (schema reconciliation next)"
    echo "----------------------------------------------------------------------"
    read -r -p "Apply staging container update now? [y/N]: " USER_CONFIRM
    case "${USER_CONFIRM}" in
        [yY][eE][sS]|[yY])
            echo "Proceeding with deployment..."
            ;;
        *)
            echo "Deployment cancelled by operator. Nothing has been changed."
            exit 0
            ;;
    esac
fi

# ==============================================================================
# Step 7c: Safe Database Migration — only AFTER approval and the gate
# ==============================================================================
# The migration mutates the live database. It runs here, not earlier, so that a
# refused gate or a denied confirmation always leaves the database exactly as it
# was. A migration failure is fatal: continuing would start the application against
# a schema that was half-applied.
if [[ "${DB_STATE}" == "present" ]]; then
    echo "[7c/10] Reconciling SQLite database schema..."

    # Exclusion and identity are re-proved immediately before the write, because
    # everything between the gate and here is a window in which something could
    # have written.
    DB_IDENTITY_NOW=$(stat -c '%d:%i:%s:%Y' "${DB_HOST_PATH}" 2>/dev/null || echo unknown)
    if [[ "${DB_IDENTITY_NOW}" != "${DB_IDENTITY_AT_GATE}" ]]; then
        echo "FATAL: the database changed after the backup gate approved it."
        echo "       approved=${DB_IDENTITY_AT_GATE} now=${DB_IDENTITY_NOW}"
        echo "       Stop every writer, take a fresh backup and run the gate again."
        exit 1
    fi
    set +e
    "${PYTHON_BIN:-python3}" "${REPO_ROOT}/scripts/writer_barrier.py" \
        --db "${DB_HOST_PATH}" --volume "${DB_VOLUME}" \
        --expect-fingerprint "${DB_IDENTITY_AT_GATE}"
    RECHECK_BARRIER=$?
    set -e
    if [[ "${RECHECK_BARRIER}" -ne 0 ]]; then
        echo "FATAL: a writer reappeared, or the database changed, between the gate and the migration."
        echo "       Stop every writer, take a fresh backup and run the gate again."
        exit 1
    fi

    echo "       Running read-only migration pre-check..."
    set +e
    docker run --rm --user 0 \
        -v "${DB_VOLUME}:/data:ro" \
        "${PHOTODOC_BACKEND_IMAGE}" \
        python migrations/reconcile_delivery_schema.py --db /data/orders.db --check
    CHECK_RC=$?
    set -e
    if [[ "${CHECK_RC}" -gt 1 ]]; then
        echo "FATAL: migration pre-check failed (exit ${CHECK_RC})."
        exit 1
    fi

    echo "       Applying additive P1 delivery schema reconciliation..."
    set +e
    docker run --rm --user 0 \
        -v "${DB_VOLUME}:/data" \
        "${PHOTODOC_BACKEND_IMAGE}" \
        python migrations/reconcile_delivery_schema.py --db /data/orders.db --apply
    APPLY_RC=$?
    set -e
    if [[ "${APPLY_RC}" -ne 0 ]]; then
        echo "FATAL: schema migration failed (exit ${APPLY_RC})."
        echo "       The database may be partially migrated; do not start the application."
        exit 1
    fi
    echo "       Migration completed. Re-verifying the migrated database read-only..."

    set +e
    docker run --rm --user 0 \
        -v "${DB_VOLUME}:/data:ro" \
        "${PHOTODOC_BACKEND_IMAGE}" \
        python migrations/reconcile_delivery_schema.py --db /data/orders.db --check
    RECHECK_RC=$?
    set -e
    if [[ "${RECHECK_RC}" -gt 1 ]]; then
        echo "FATAL: post-migration verification failed (exit ${RECHECK_RC})."
        exit 1
    fi
else
    echo "[7c/10] No existing database; schema will be created by the application on startup."
fi

# ==============================================================================
# Step 9: Start Staging Containers (Zero-Build)
# ==============================================================================
echo "[8/10] Starting Prebuilt Staging Containers (--no-build)..."

PHOTODOC_BACKEND_IMAGE="${PHOTODOC_BACKEND_IMAGE}" \
PHOTODOC_FRONTEND_IMAGE="${PHOTODOC_FRONTEND_IMAGE}" \
STAGING_PORT="${STAGING_PORT}" \
docker compose -p photodoc-staging -f "${STAGING_COMPOSE_FILE}" up -d --no-build --remove-orphans

echo "       Containers started."

# ==============================================================================
# Step 10: Verify Container Health via /api/health
# ==============================================================================
echo "[9/10] Verifying Staging Health via Loopback..."

HEALTH_URL="http://127.0.0.1:${STAGING_PORT}/api/health"
MAX_ATTEMPTS=15
ATTEMPT=1
HEALTH_PASS=0

while [[ ${ATTEMPT} -le ${MAX_ATTEMPTS} ]]; do
    if curl --fail -sS "${HEALTH_URL}" 2>/dev/null | grep -q '"status"[[:space:]]*:[[:space:]]*"ok"'; then
        HEALTH_PASS=1
        break
    fi
    echo "       Waiting for /api/health (attempt ${ATTEMPT}/${MAX_ATTEMPTS})..."
    sleep 2
    ATTEMPT=$((ATTEMPT + 1))
done

if [[ ${HEALTH_PASS} -eq 1 ]]; then
    echo "       /api/health: PASS (HTTP 200, status=ok)"
else
    echo "FATAL: Health check failed after ${MAX_ATTEMPTS} attempts!" >&2
    echo "       Container status:" >&2
    docker compose -p photodoc-staging -f "${STAGING_COMPOSE_FILE}" ps >&2
    echo "" >&2
    echo "ROLLBACK INSTRUCTION:" >&2
    echo "To restore previous known good images within 15 seconds, run:" >&2
    echo "  $0 --rollback <prev_backend_img> <prev_frontend_img> --confirm" >&2
    exit 1
fi

# ==============================================================================
# Step 11: Resource and Diagnostic Summary
# ==============================================================================
echo "[10/10] Generating Deployment Diagnostic Summary..."

echo ""
echo "--- Running Container Status ---"
docker compose -p photodoc-staging -f "${STAGING_COMPOSE_FILE}" ps

echo ""
echo "--- Real-Time Memory Consumption ---"
docker stats --no-stream --format "table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}"

echo ""
echo "--- Preserved Staging Volumes ---"
docker volume inspect photodoc_staging_backend-data photodoc_staging_uploads --format '{{.Name}}: Created={{.CreatedAt}}' 2>/dev/null || true

echo ""
echo "======================================================================"
echo " STAGING DEPLOYMENT SUCCESSFUL (Zero-Build Confirmed)"
echo " Backend Image  : ${PHOTODOC_BACKEND_IMAGE}"
echo " Frontend Image : ${PHOTODOC_FRONTEND_IMAGE}"
echo " Loopback API   : http://127.0.0.1:${STAGING_PORT}/api/health"
echo "======================================================================"
exit 0
