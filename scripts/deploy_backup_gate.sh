#!/usr/bin/env bash
# PhotoDoc AI — deployment backup gate wrapper (S6-01).
#
# Decides whether a cutover may proceed. Extracted from the deployment script so
# it can be exercised without Docker, against synthetic fixtures.
#
# Modes (auto-detected, overridable with GATE_MODE):
#   host    - LIVE_DB is a readable path on this machine; the gate runs directly.
#   docker  - LIVE_DB is inside DB_VOLUME; the gate runs in the pinned backend
#             image with the repository mounted read-only.
#   cold    - there is no database yet, so there is nothing to protect.
#
# Exit codes:
#   0  gate passed, or cold start
#   1  gate failed, configuration problem, or an unsafe skip was requested
#   2  caller asked to skip explicitly (SKIP_BACKUP_GATE=1) and was allowed to
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${REPO_ROOT:-$(cd "${SCRIPT_DIR}/.." && pwd)}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

BACKUP_ROOT="${BACKUP_ROOT:-/var/backups/photodoc}"
BACKUP_MAX_AGE_HOURS="${BACKUP_MAX_AGE_HOURS:-24}"
LIVE_DB="${LIVE_DB:-}"
DB_VOLUME="${DB_VOLUME:-}"
PHOTODOC_BACKEND_IMAGE="${PHOTODOC_BACKEND_IMAGE:-}"
GATE_MODE="${GATE_MODE:-auto}"

log() { printf '%s\n' "$*" >&2; }

if [[ "${GATE_MODE}" == "auto" ]]; then
    if [[ -n "${LIVE_DB}" && -f "${LIVE_DB}" ]]; then
        GATE_MODE="host"
    elif [[ -n "${DB_VOLUME}" ]] && command -v docker >/dev/null 2>&1; then
        GATE_MODE="docker"
    else
        GATE_MODE="cold"
    fi
fi

if [[ "${GATE_MODE}" == "cold" ]]; then
    log "       Cold start: no existing orders.db, so there is no data to back up yet."
    log "       (Once a database exists, this deployment will refuse to proceed without a verified backup.)"
    exit 0
fi

# Explicit override: allowed only when it is loud, and it does not silently pass.
if [[ "${SKIP_BACKUP_GATE:-0}" == "1" ]]; then
    log "       ####################################################################"
    log "       # WARNING: SKIP_BACKUP_GATE=1 — proceeding WITHOUT a verified backup."
    log "       # A rollback of this cutover may lose data."
    log "       ####################################################################"
    exit 2
fi

if [[ ! -d "${BACKUP_ROOT}" ]]; then
    log "FATAL: backup root '${BACKUP_ROOT}' does not exist."
    log "       A deployment with existing data requires a verified backup first."
    log "       Create one with: sudo bash ${REPO_ROOT}/scripts/run_backup.sh"
    exit 1
fi

log "       Checking '${BACKUP_ROOT}' for a verified backup set (max age ${BACKUP_MAX_AGE_HOURS}h)..."

# One-time pre-PR #26 transition. The first deployment onto a database that
# predates paid_email_outbox cannot produce a backup containing that table
# without migrating live data first, so the standard outbox requirement would
# block the very deployment that creates it. This flag relaxes exactly that one
# requirement, and backup_gate.py refuses it unless BOTH the live database and
# the backup lack the table. Once the live database has it, the flag is an error,
# so it cannot be left enabled by accident. It is independent of
# SKIP_BACKUP_GATE: nothing here skips verification.
GATE_FLAGS=()
if [[ "${BACKUP_GATE_ALLOW_PRE_OUTBOX:-0}" == "1" ]]; then
    log "       ####################################################################"
    log "       # WARNING: BACKUP_GATE_ALLOW_PRE_OUTBOX=1"
    log "       # Allowing a backup WITHOUT paid_email_outbox. This is only valid for"
    log "       # the first transition from a pre-PR#26 database; it is refused if"
    log "       # either the live database or the backup already has the table."
    log "       # Every other check (integrity, identity, freshness, deep uploads)"
    log "       # still applies."
    log "       ####################################################################"
    GATE_FLAGS+=(--allow-pre-outbox-transition)
else
    GATE_FLAGS+=(--require-outbox)
fi

GATE_STATUS=0
if [[ "${GATE_MODE}" == "host" ]]; then
    PYTHONPATH="${REPO_ROOT}/scripts" "${PYTHON_BIN}" "${REPO_ROOT}/scripts/backup_gate.py" \
        --latest-of "${BACKUP_ROOT}" \
        "${GATE_FLAGS[@]}" \
        --live-db "${LIVE_DB}" \
        --max-age-hours "${BACKUP_MAX_AGE_HOURS}" || GATE_STATUS=$?
elif [[ "${GATE_MODE}" == "docker" ]]; then
    if [[ -z "${PHOTODOC_BACKEND_IMAGE}" ]]; then
        log "FATAL: PHOTODOC_BACKEND_IMAGE is required to run the gate against a volume."
        exit 1
    fi

    # Permission model (fixes the S6-01 review finding H1).
    #
    # Backup sets are deliberately restrictive: the directory is root-owned 0700
    # and the artifacts 0600. A previous version of this wrapper dropped to
    # --user 65534:65534, which cannot traverse that directory, so a genuine
    # backup was reported as missing and every deployment was refused. Making the
    # backups world-readable to work around that would be worse.
    #
    # Instead the verifier runs as root *inside a narrowly scoped container*:
    #   --network none        no network at all (the gate performs no I/O off-host)
    #   --read-only           container filesystem immutable
    #   --tmpfs /tmp:...      the only writable location, for --deep extraction
    #   --cap-drop ALL        no capabilities
    #   no-new-privileges     cannot regain privileges
    #   every host mount is :ro, so the container cannot modify or delete backups
    # Running as root here only grants read access to the mounted paths; with all
    # three mounts read-only and no network there is nothing else it can reach.
    docker run --rm \
        --user 0:0 \
        --network none \
        --read-only \
        --tmpfs /tmp:rw,noexec,nosuid,size=64m \
        --cap-drop ALL \
        --security-opt no-new-privileges \
        -v "${REPO_ROOT}:/repo:ro" \
        -v "${BACKUP_ROOT}:/backups:ro" \
        -v "${DB_VOLUME}:/data:ro" \
        -e PYTHONPATH=/repo/scripts \
        -e PYTHONDONTWRITEBYTECODE=1 \
        "${PHOTODOC_BACKEND_IMAGE}" \
        python /repo/scripts/backup_gate.py \
            --latest-of /backups \
            "${GATE_FLAGS[@]}" \
            --live-db /data/orders.db \
            --max-age-hours "${BACKUP_MAX_AGE_HOURS}" || GATE_STATUS=$?
else
    log "FATAL: unknown GATE_MODE '${GATE_MODE}'"
    exit 1
fi

if [[ "${GATE_STATUS}" -ne 0 ]]; then
    log ""
    log "FATAL: backup gate refused this deployment (exit ${GATE_STATUS})."
    log "       There is no verified, current backup to roll back to."
    log "       Create one first: sudo bash ${REPO_ROOT}/scripts/run_backup.sh"
    log "       Inspect what is missing:"
    log "         ${PYTHON_BIN} ${REPO_ROOT}/scripts/backup_gate.py --latest-of ${BACKUP_ROOT} --require-outbox --json"
    log "       Refusing to touch running data without a rollback path."
    exit 1
fi

log "       Backup gate passed: a verified backup set newer than the live data exists."
exit 0
