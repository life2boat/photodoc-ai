#!/usr/bin/env bash
# PhotoDoc AI — deployment backup gate wrapper.
#
# Decides whether a cutover may proceed. Extracted from the deployment script so
# it can be exercised without Docker, against synthetic fixtures.
#
# Modes:
#   host    - LIVE_DB is a readable path on this machine; the gate runs directly.
#   docker  - the database lives in DB_VOLUME; the gate runs in the pinned image
#             with the repository mounted read-only.
#   cold    - ONLY when the caller has positively proven that no database exists
#             (DB_ABSENCE_PROVEN=1). An unknown or unreadable state is an error,
#             never a cold start.
#
# The gate itself always runs with --deep (upload archive re-verification) and
# --require-quiescent, so a cutover cannot proceed on an archive that cannot be
# restored or against a database with writers in flight.
#
# Exit codes:
#   0  gate passed
#   1  gate failed, configuration error, or unprovable state
#   2  caller explicitly asked to skip (SKIP_BACKUP_GATE=1) — never treated as a pass
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
WORKER_STATE="${DELIVERY_WORKER_ENABLED_STATE:-unknown}"

log() { printf '%s\n' "$*" >&2; }
fail() { log "FATAL: $*"; exit 1; }

# --- freshness value must be a finite positive number (a NaN threshold would
# --- silently disable the age check, because every comparison with NaN is false)
if [[ ! "${BACKUP_MAX_AGE_HOURS}" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
    fail "BACKUP_MAX_AGE_HOURS must be a plain positive number, got '${BACKUP_MAX_AGE_HOURS}'"
fi
if [[ "${BACKUP_MAX_AGE_HOURS}" =~ ^0+([.]0+)?$ ]]; then
    fail "BACKUP_MAX_AGE_HOURS must be greater than zero, got '${BACKUP_MAX_AGE_HOURS}'"
fi
_age_int="${BACKUP_MAX_AGE_HOURS%%.*}"
if (( 10#${_age_int} > 87600 )); then
    fail "BACKUP_MAX_AGE_HOURS is unreasonably large ('${BACKUP_MAX_AGE_HOURS}'); a freshness limit above ten years is a configuration error"
fi
unset _age_int

# --- an explicit skip is resolved BEFORE any cold-start reasoning, so a skip can
# --- never be mistaken for "there was nothing to protect"
if [[ "${SKIP_BACKUP_GATE:-0}" == "1" ]]; then
    log "       ####################################################################"
    log "       # WARNING: SKIP_BACKUP_GATE=1 — proceeding WITHOUT a verified backup."
    log "       # A rollback of this cutover may lose data."
    log "       ####################################################################"
    exit 2
fi

# --- resolve the mode; never assume a cold start
case "${GATE_MODE}" in
    auto)
        if [[ -n "${LIVE_DB}" && -f "${LIVE_DB}" ]]; then
            GATE_MODE="host"
        elif [[ -n "${DB_VOLUME}" ]] && command -v docker >/dev/null 2>&1; then
            GATE_MODE="docker"
        else
            fail "cannot determine the database: set LIVE_DB (host) or DB_VOLUME (docker). Refusing to assume a cold start."
        fi
        ;;
    host)
        [[ -n "${LIVE_DB}" ]] || fail "GATE_MODE=host requires LIVE_DB"
        [[ -f "${LIVE_DB}" ]] || fail "GATE_MODE=host but LIVE_DB is not a readable file: ${LIVE_DB}"
        ;;
    docker)
        [[ -n "${DB_VOLUME}" ]] || fail "GATE_MODE=docker requires DB_VOLUME"
        [[ -n "${PHOTODOC_BACKEND_IMAGE}" ]] || fail "GATE_MODE=docker requires PHOTODOC_BACKEND_IMAGE"
        command -v docker >/dev/null 2>&1 || fail "GATE_MODE=docker but docker is not available"
        ;;
    cold)
        if [[ "${DB_ABSENCE_PROVEN:-0}" != "1" ]]; then
            fail "cold start refused: the absence of an existing database has not been proven. Pass DB_ABSENCE_PROVEN=1 only after positively establishing that no database file exists."
        fi
        log "       Cold start proven: no existing database, so there is no data to protect yet."
        log "       (Once a database exists, this deployment will refuse to proceed without a verified backup.)"
        exit 0
        ;;
    *)
        fail "unknown GATE_MODE '${GATE_MODE}'"
        ;;
esac

if [[ ! -d "${BACKUP_ROOT}" ]]; then
    log "FATAL: backup root '${BACKUP_ROOT}' does not exist."
    log "       A deployment with existing data requires a verified backup first."
    log "       Create one with: sudo bash ${REPO_ROOT}/scripts/run_backup.sh"
    exit 1
fi

log "       Checking '${BACKUP_ROOT}' for a verified backup set (max age ${BACKUP_MAX_AGE_HOURS}h)..."

# One-time pre-PR #26 transition: relaxes ONLY the outbox-table requirement, and
# backup_gate.py refuses it unless both the live database and the backup lack the
# table and the backup preserves every live row. Independent of SKIP_BACKUP_GATE.
GATE_FLAGS=()
if [[ "${BACKUP_GATE_ALLOW_PRE_OUTBOX:-0}" == "1" ]]; then
    log "       ####################################################################"
    log "       # WARNING: BACKUP_GATE_ALLOW_PRE_OUTBOX=1"
    log "       # Allowing a backup WITHOUT paid_email_outbox. Valid only for the first"
    log "       # transition from a pre-PR#26 database; refused if either the live"
    log "       # database or the backup already has the table."
    log "       ####################################################################"
    GATE_FLAGS+=(--allow-pre-outbox-transition)
else
    GATE_FLAGS+=(--require-outbox)
fi

GATE_STATUS=0
if [[ "${GATE_MODE}" == "host" ]]; then
    PYTHONPATH="${REPO_ROOT}/scripts" "${PYTHON_BIN}" "${REPO_ROOT}/scripts/backup_gate.py" \
        --latest-of "${BACKUP_ROOT}" \
        --deep \
        --require-quiescent --db "${LIVE_DB}" --worker-enabled "${WORKER_STATE}" \
        "${GATE_FLAGS[@]}" \
        --live-db "${LIVE_DB}" \
        --max-age-hours "${BACKUP_MAX_AGE_HOURS}" || GATE_STATUS=$?
elif [[ "${GATE_MODE}" == "docker" ]]; then
    # Permission model: backup sets are root-owned 0700 with 0600 artifacts, so the
    # verifier runs as root inside a narrowly scoped container — no network, an
    # immutable filesystem, all capabilities dropped, and every host mount
    # read-only. Making the backups world-readable was rejected.
    #
    # The tmpfs must hold the extracted upload archive for --deep, so it is sized
    # well above the expected archive rather than at a token value.
    docker run --rm \
        --user 0:0 \
        --network none \
        --read-only \
        --tmpfs /tmp:rw,noexec,nosuid,size=1024m \
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
            --deep \
            --require-quiescent --db /data/orders.db --worker-enabled "${WORKER_STATE}" \
            "${GATE_FLAGS[@]}" \
            --live-db /data/orders.db \
            --max-age-hours "${BACKUP_MAX_AGE_HOURS}" || GATE_STATUS=$?
fi

if [[ "${GATE_STATUS}" -ne 0 ]]; then
    log ""
    log "FATAL: backup gate refused this deployment (exit ${GATE_STATUS})."
    log "       There is no verified, current backup to roll back to."
    log "       Create one first: sudo bash ${REPO_ROOT}/scripts/run_backup.sh"
    log "       Inspect what is missing:"
    log "         ${PYTHON_BIN} ${REPO_ROOT}/scripts/backup_gate.py --latest-of ${BACKUP_ROOT} --require-outbox --deep --live-db <path> --json"
    log "       Refusing to touch running data without a rollback path."
    exit 1
fi

log "       Backup gate passed: a verified backup set that matches the live data exists."
exit 0
