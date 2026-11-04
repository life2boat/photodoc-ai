#!/usr/bin/env bash
# PhotoDoc AI — scheduled backup runner (NOT installed by this PR).
#
# Sequence, each step gated on the previous one:
#   1. create a verified backup set (atomic publication)
#   2. verify it with the deployment gate
#   3. prune old local sets under the retention plan
#   4. replicate encrypted off-host (no-op until configured)
#
# Every step fails closed: a failed backup never deletes the previous one, and a
# failed gate stops the run before anything is pruned or pushed.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"

DB_PATH="${DB_PATH:-/data/orders.db}"
UPLOAD_DIR="${UPLOAD_DIR:-/app/uploads}"
BACKUP_ROOT="${BACKUP_ROOT:-/var/backups/photodoc}"
LOG_FILE="${BACKUP_LOG:-/var/log/photodoc-backup.log}"

mkdir -p "$(dirname "${LOG_FILE}")" 2>/dev/null || true
log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" | tee -a "${LOG_FILE}"; }

fail() { log "FAILED: $*"; exit 1; }

log "backup run starting (db=${DB_PATH} uploads=${UPLOAD_DIR} root=${BACKUP_ROOT})"

mkdir -p "${BACKUP_ROOT}" || fail "cannot create ${BACKUP_ROOT}"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
TARGET="${BACKUP_ROOT}/${STAMP}"

# 1. Create (atomic: builds in a sibling staging dir, publishes with a rename)
"${PYTHON_BIN}" "${REPO_DIR}/scripts/create_backup.py" \
    --db "${DB_PATH}" \
    --uploads "${UPLOAD_DIR}" \
    --output-dir "${TARGET}" \
    --json || fail "backup creation failed; the previous backup set is unchanged"

# 2. Verify with the same gate the deployment uses
"${PYTHON_BIN}" "${REPO_DIR}/scripts/backup_gate.py" \
    --backup-dir "${TARGET}" \
    --require-outbox \
    --max-age-hours 1 \
    --deep \
    --json || fail "backup gate rejected the new set at ${TARGET}"

log "backup set verified: ${TARGET}"

# 3. Prune local sets (never the newest, never the only verified one)
"${PYTHON_BIN}" "${REPO_DIR}/scripts/backup_retention.py" \
    --root "${BACKUP_ROOT}" \
    --require-outbox \
    --apply \
    --json || log "WARNING: local retention reported a problem"

# 4. Off-host replication — refuses cleanly (exit 2) until configured.
"${PYTHON_BIN}" "${REPO_DIR}/scripts/offhost_backup.py" \
    --require-outbox --execute --json
OFFHOST_RC=$?
if [ "${OFFHOST_RC}" -eq 2 ]; then
    log "off-host replication not configured (exit 2) — local backup is the only copy"
elif [ "${OFFHOST_RC}" -ne 0 ]; then
    fail "off-host replication failed with exit ${OFFHOST_RC}"
fi

log "backup run finished OK"
exit 0
