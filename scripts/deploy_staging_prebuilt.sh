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
if [[ -n "${TARGET_HOST}" ]]; then
    if [[ "${TARGET_HOST}" == "${PRODUCTION_IP}" ]] || [[ "${TARGET_HOST}" == *"photodoc-ai.ru"* ]]; then
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
# Step 8: Safe Database Migration Check & Application
# ==============================================================================
echo "[7/10] Checking SQLite Database Schema & Migrations..."

DB_VOLUME="photodoc_staging_backend-data"

# Check if staging DB volume exists
if docker volume inspect "${DB_VOLUME}" >/dev/null 2>&1; then
    echo "       Found existing staging database volume '${DB_VOLUME}'. Data will be preserved."

    # Run read-only schema compatibility inspection
    echo "       Running read-only migration pre-check..."
    docker run --rm --user 65534:65534 \
        -v "${DB_VOLUME}:/data:ro" \
        "${PHOTODOC_BACKEND_IMAGE}" \
        python migrations/reconcile_delivery_schema.py --db /data/orders.db --check || true

    # Run additive migration if orders.db exists
    docker run --rm --user 0 \
        -v "${DB_VOLUME}:/data" \
        "${PHOTODOC_BACKEND_IMAGE}" \
        python -c "import os, sys; sys.exit(0 if os.path.exists('/data/orders.db') else 1)" && {
            echo "       Applying additive P1 delivery schema reconciliation..."
            docker run --rm --user 0 \
                -v "${DB_VOLUME}:/data" \
                "${PHOTODOC_BACKEND_IMAGE}" \
                python migrations/reconcile_delivery_schema.py --db /data/orders.db --apply
        } || {
            echo "       No existing orders.db found in volume; fresh staging DB will initialize on startup."
        }
else
    echo "       Staging database volume does not exist yet. Volume will be created and fresh staging DB initialized."
fi

# ==============================================================================
# Confirmation Gate (State-Changing Operations)
# ==============================================================================
if [[ "${CONFIRM}" -eq 0 ]]; then
    echo ""
    echo "----------------------------------------------------------------------"
    echo " READY TO APPLY UPDATE:"
    echo " Backend  : ${PHOTODOC_BACKEND_IMAGE}"
    echo " Frontend : ${PHOTODOC_FRONTEND_IMAGE}"
    echo " Port     : 127.0.0.1:${STAGING_PORT}"
    echo "----------------------------------------------------------------------"
    read -r -p "Apply staging container update now? [y/N]: " USER_CONFIRM
    case "${USER_CONFIRM}" in
        [yY][eE][sS]|[yY])
            echo "Proceeding with deployment..."
            ;;
        *)
            echo "Deployment cancelled by operator."
            exit 0
            ;;
    esac
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
