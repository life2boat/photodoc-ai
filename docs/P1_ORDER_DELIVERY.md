# P1 order delivery: migration and recovery

This change builds on PR #8. It does not replace the Yandex Disk OAuth or SMTP
integrations. The production database and upload directory must remain on their
existing persistent volume/bind mount.

## Before starting the P1 backend

Stop the old backend writer and execute the verified backup protocol detailed below
for `orders.db` and the uploads directory. Keep the backup outside the release tree.
On the backed-up, identified PhotoDoc database, run:

```bash
python backend/migrations/reconcile_delivery_schema.py --db /data/orders.db
python backend/migrations/reconcile_delivery_schema.py --db /data/orders.db --apply
python backend/migrations/reconcile_delivery_schema.py --db /data/orders.db
```

The script requires the P0 `PDAI` application identity and complete P0 schema.
It adds `order_token_hash` and `delivery_jobs` transactionally; existing orders
and local files are not rewritten. The P1 backend refuses to start against a
database missing these additions. Run these commands in an isolated container
with the real persistent volume mounted only during an approved release. This
document is not an instruction to deploy from a draft PR.

## Hardened Backup and Recovery Protocol

Copying an active SQLite database file using `cp` while writers are active or before WAL
checkpointing can produce torn pages or lose uncheckpointed transactions, and can cause
desynchronization between database records and customer photo uploads on disk.

To guarantee consistent, verifiable backups prior to schema migration or deployment cutover,
follow this strict procedure:

### 1. Writer Shutdown and SQLite WAL Checkpoint

Before snapshotting, guarantee zero write concurrency by stopping the backend container:

```bash
docker compose -p photodoc \
  -f "${ACTIVE_RELEASE_DIR}/docker-compose.yml" \
  -f "${ACTIVE_RELEASE_DIR}/docker-compose.production.yml" \
  stop backend
```

Once the writer is stopped, flush and truncate any SQLite Write-Ahead Log (`orders.db-wal`):

```bash
docker run --rm \
  -v "${PHOTODOC_DB_VOLUME:-photodoc_backend-data}:/data" \
  alpine sh -c "apk add --no-cache sqlite >/dev/null && sqlite3 /data/orders.db 'PRAGMA wal_checkpoint(TRUNCATE);'"
```

Verify that the command returns `0` (not busy) and that the database integrity check returns `ok`:

```bash
docker run --rm \
  -v "${PHOTODOC_DB_VOLUME:-photodoc_backend-data}:/data:ro" \
  alpine sh -c "apk add --no-cache sqlite >/dev/null && sqlite3 /data/orders.db 'PRAGMA integrity_check;'"
```

### 2. Transactionally Consistent SQLite Snapshot via Backup API

Target backup storage must reside completely outside the active release tree (`/opt/photodoc`)
and outside any web server document root to prevent accidental exposure via Nginx.
Default target: `/var/backups/photodoc`.

Ensure sufficient disk space and overwrite protection before snapshotting:

```bash
PHOTODOC_BACKUP_ROOT="${PHOTODOC_BACKUP_ROOT:-/var/backups/photodoc}"
mkdir -p "${PHOTODOC_BACKUP_ROOT}"

# Disk space check: require at least 500 MB free space
AVAILABLE_KB=$(df -k "${PHOTODOC_BACKUP_ROOT}" | awk 'NR==2 {print $4}')
if [ "${AVAILABLE_KB}" -lt 512000 ]; then
  echo "ERROR: Insufficient disk space for backup in ${PHOTODOC_BACKUP_ROOT}" >&2
  exit 1
fi

BACKUP_DIR="${PHOTODOC_BACKUP_ROOT}/release_$(date -u +%Y%m%d_%H%M%S)"
test ! -e "${BACKUP_DIR}"
mkdir -p "${BACKUP_DIR}"
chmod 700 "${BACKUP_DIR}"

docker run --rm \
  -v "${PHOTODOC_DB_VOLUME:-photodoc_backend-data}:/data:ro" \
  -v "${BACKUP_DIR}:/backup" \
  alpine sh -c "apk add --no-cache sqlite >/dev/null && sqlite3 /data/orders.db '.backup /backup/orders.db'"
```

Verify integrity of the backup file:

```bash
docker run --rm \
  -v "${BACKUP_DIR}:/backup:ro" \
  alpine sh -c "apk add --no-cache sqlite >/dev/null && sqlite3 /backup/orders.db 'PRAGMA integrity_check;'"
```
The result must be strictly `ok`.

### 3. Zero-Retention Backup Generation & Uploads Archive

To enforce the **Zero-Retention Privacy Gate**, newly generated backups must NEVER retain customer photos from orders whose delivery is complete. However, backups must preserve all local originals required for unfinished deliveries (`pending`, `processing`, or `failed`) to ensure disaster recovery.

Use the automated zero-retention backup generator (`scripts/create_backup.py`), which transactionally snapshots SQLite, verifies application identity `PDAI` (1346650441), sweeps pending cleanups, archives strictly the required recovery files, records cryptographic manifests, and verifies the backup:

```bash
python3 /candidate/scripts/create_backup.py \
  --db /opt/photodoc/backend/orders.db \
  --uploads /opt/photodoc/backend/uploads \
  --output-dir "${BACKUP_DIR}"
```

This automated tool:
- Sweeps pending cleanups for delivered orders before snapshotting to establish durable deletion state.
- Snapshots SQLite `orders.db` via SQLite Online Backup API.
- Archives **ONLY** local original files required for unfinished deliveries (`state IN ('pending', 'processing', 'failed')`) into `uploads.tar.gz`.
- Strictly excludes photos from delivered orders (`state == 'delivered'`), guaranteeing zero delivered customer photo content in new backups.
- **Fail-Closed Legacy P0 Recovery Protection (`--legacy-p0-policy [fail|include|exclude]`, default: `fail`):**
  If physical files exist on disk for legacy P0 orders that lack `delivery_jobs` tracking records, their delivery and retention status cannot be verified with cryptographic certainty. The backup generator fails closed by default rather than silently omitting them or leaking delivered photos.
  - **Reconciliation Runbook:**
    1. *Active Legacy Orders:* Reconcile them into `delivery_jobs` so their lifecycle is tracked with P1 hashes and delivery states.
    2. *Fulfilled Historical Orders:* If photos are obsolete copies, remove unindexed files or pass `--legacy-p0-policy exclude`.
    3. *Preservation for Disaster Recovery:* If legacy files must be preserved in backup, explicitly pass `--legacy-p0-policy include`.
- Generates `manifest.sha256` and structured metadata `backup_manifest.json` (`zero_retention_enforced: true`).
- Automatically extracts and verifies the generated backup with `scripts/verify_backup_integrity.py`.
- Sets secure permissions (`chmod 600` on files, `chmod 700` on directory).

### 4. Historical Backup Archive Auditing & Residual Privacy Risk

Historical backup archives created prior to the zero-retention policy represent a **residual privacy risk** if they contain archived customer photos from orders that have since been delivered.

To audit existing historical archives without modifying or deleting anything, use the read-only dry-run auditor:

```bash
python3 /candidate/scripts/audit_backup_archives.py --backup-root /var/backups/photodoc
```

The audit tool inspects `orders.db` and `uploads.tar.gz` in-memory, cross-references order delivery statuses, and adheres to a strict fail-closed contract:
- **Fail-Closed Risk Classification:**
  - `HIGH`: Confirmed retained customer photos from delivered orders.
  - `UNKNOWN` / `BLOCKED`: Untracked or unclassified archive files present, or unsupported archive formats detected. Never falsely classified as `ZERO`.
  - `ERROR`: Corrupted archives, unreadable SQLite databases, or missing required backup directories.
  - `ZERO`: Strictly all archives are readable, valid, fully accounted for, with 0 delivered customer photos and 0 untracked files.
- **Deterministic CLI Exit Codes:**
  - Exit `0`: All archives inspected have confirmed `ZERO` residual privacy risk.
  - Exit `1`: Confirmed privacy risk (`HIGH`).
  - Exit `2`: Inconclusive / unclassified files, empty inventory, or unsupported archive formats (`UNKNOWN` / `BLOCKED`).
  - Exit `3`: Read or integrity errors (`ERROR`).
- **Privacy Sanitization in Default Diagnostics:**
  Default diagnostic output withholds customer PII and raw original filenames. Full detailed itemization for owner remediation is explicitly opt-in via `--show-details`.

#### Safe Two-Phase Owner-Controlled Remediation Workflow for Compressed Archives (R4.4.6 Commit Failure Recovery & Full-State Consistency)

> [!IMPORTANT]
> GNU tar does not support in-place deletion (`tar --delete`) on compressed archives (`.tar.gz`). Attempting to run `tar --delete` on a `.tar.gz` file fails with `tar: Cannot update compressed archives`.
> Furthermore, two independent filesystem renames (replacing `uploads.tar.gz` and `manifest.sha256`) **are not collectively atomic at the filesystem level**. If execution fails or terminates between the two renames, the archive content and manifest diverge.
> To guarantee full failure-atomicity and recovery integrity, PhotoDoc AI implements a recoverable transaction state machine with explicit rollback guarantees:
> - **Recoverable Transaction Journal State Machine:** The remediation engine tracks explicit phases: `PRE_COMMIT` -> `ARCHIVE_MUTATED` -> `MANIFEST_MUTATED` -> `VERIFYING` -> `COMMITTED` (or `ROLLING_BACK` -> `ROLLED_BACK` / `FAILED`). Recorded in `${ARCHIVE_DIR}/.transaction_journal.json`.
> - **Commit Boundary Protection:** Every failure after the archive rename (including a failed manifest rename) enters the recovery handler rather than terminating with an inconsistent state.
> - **Exact Manifest State Restoration:** If a manifest originally existed, its exact bytes and SHA-256 are restored on rollback and verified. If no manifest existed, any newly created manifest is removed, and its absence is verified.
> - **Full-State Source Consistency Gate:** Before any mutation during Phase 2, active `uploads.tar.gz`, active `orders.db`, active `manifest.sha256` (or confirmed absence), candidate files, and preserved pre-purge baselines are verified. Any mismatch aborts before any file mutation.
> - **Signal Safety & Dedicated Exit Handler:** Traps `INT` and `TERM` safely, cleanly aborts before commit, and invokes recovery if interrupted during or after mutation. Lock directory `${ARCHIVE_DIR}/.remediation.lock` is released only after clean completion or verified rollback; if rollback fails (`FAILED` / `RECOVERY_REQUIRED`), the lock and all recovery artifacts are preserved.
> - **TOCTOU Temporary File Protection:** Temporary candidate and rollback files are written directly through open file descriptors with mode `0o600`, flushed, fsynced, verified non-symlink, and checksum-validated before replacement.

##### Phase 1: Candidate Preparation & Full-State Baseline Preservation (`--prepare`)

1. **Acquire Exclusive Transaction Lock & Validate Path Boundaries:**
   ```bash
   ARCHIVE_DIR="/var/backups/photodoc/release_YYYYMMDD_HHMMSS"
   REPO_ROOT="/opt/photodoc"
   LOCK_DIR="${ARCHIVE_DIR}/.remediation.lock"

   # Cross-process mutex with PID ownership tracking:
   mkdir "${LOCK_DIR}" || { echo "ERROR: Remediation transaction lock is held by another process: ${LOCK_DIR}" >&2; exit 1; }
   echo $$ > "${LOCK_DIR}/pid"
   LOCK_ACQUIRED=1

   # Symlink rejection without dereferencing:
   [ ! -L "${ARCHIVE_DIR}" ] || { echo "ERROR: Symlink archive directory rejected" >&2; exit 1; }
   ```

2. **Preserve Existing Staging Artifacts & Record Full Source State:**
   ```bash
   STAGING_DIR="${ARCHIVE_DIR}/.purge_staging"
   # Fail-closed if staging workspace already exists to protect diagnostics:
   [ ! -e "${STAGING_DIR}" ] || {
     echo "ERROR: Staging directory ${STAGING_DIR} already exists. Inspect or remove recovery artifacts before preparing a new candidate." >&2
     exit 1
   }

   # Record active archive & active database checksums:
   ORIG_ARCH_SHA=$(sha256sum "${ARCHIVE_DIR}/uploads.tar.gz" | awk '{print $1}')
   ORIG_DB_SHA=$(sha256sum "${ARCHIVE_DIR}/orders.db" | awk '{print $1}')
   ```

3. **Race-Safe Atomic Baseline Preservation (`uploads.tar.gz.pre-purge` and `manifest.sha256.pre-purge`):**
   ```bash
   # Exclusive creation via O_CREAT | O_EXCL prevents concurrent clobbering:
   python3 -c '
   import sys, shutil
   src, dst = sys.argv[1], sys.argv[2]
   try:
       with open(src, "rb") as f_in, open(dst, "xb") as f_out:
           shutil.copyfileobj(f_in, f_out)
   except FileExistsError:
       sys.stderr.write("ERROR: Pre-purge baseline already exists; aborting to prevent overwrite\n")
       sys.exit(1)
   ' "${ARCHIVE_DIR}/uploads.tar.gz" "${ARCHIVE_DIR}/uploads.tar.gz.pre-purge"

   # Preserve and record exact original manifest state (if existed):
   if [ -f "${ARCHIVE_DIR}/manifest.sha256" ]; then
     # Verify manifest integrity:
     (cd "${ARCHIVE_DIR}" && sha256sum -c manifest.sha256 >/dev/null)
     # Preserve manifest baseline via atomic exclusive open:
     python3 -c '...' "${ARCHIVE_DIR}/manifest.sha256" "${ARCHIVE_DIR}/manifest.sha256.pre-purge"
     ORIG_MAN_SHA=$(sha256sum "${ARCHIVE_DIR}/manifest.sha256" | awk '{print $1}')
     HAS_MANIFEST="true"
   else
     HAS_MANIFEST="false"
   fi
   ```

4. **Rebuild Candidate in Isolated Staging Workspace:**
   ```bash
   mkdir -p "${STAGING_DIR}/rebuild" "${STAGING_DIR}/candidate"
   cp "${ARCHIVE_DIR}/uploads.tar.gz" "${STAGING_DIR}/uploads.tar.gz.orig"
   cp "${ARCHIVE_DIR}/orders.db" "${STAGING_DIR}/candidate/orders.db"

   tar -xzf "${STAGING_DIR}/uploads.tar.gz.orig" -C "${STAGING_DIR}/rebuild"
   # Remove only owner-approved delivered customer photo files:
   rm -f "${STAGING_DIR}/rebuild/uploads/<approved_delivered_filename>"

   tar --numeric-owner -czf "${STAGING_DIR}/candidate/uploads.tar.gz" -C "${STAGING_DIR}/rebuild" uploads
   (cd "${STAGING_DIR}/candidate" && sha256sum orders.db uploads.tar.gz > manifest.sha256)
   CANDIDATE_ARCH_SHA=$(sha256sum "${STAGING_DIR}/candidate/uploads.tar.gz" | awk '{print $1}')
   CANDIDATE_DB_SHA=$(sha256sum "${STAGING_DIR}/candidate/orders.db" | awk '{print $1}')
   CANDIDATE_MAN_SHA=$(sha256sum "${STAGING_DIR}/candidate/manifest.sha256" | awk '{print $1}')
   ```

5. **Verify Candidate via Integrity Verifier and Privacy Auditor:**
   ```bash
   python3 "${REPO_ROOT}/scripts/verify_backup_integrity.py"      --db "${STAGING_DIR}/candidate/orders.db"      --uploads "${STAGING_DIR}/rebuild/uploads"

   python3 "${REPO_ROOT}/scripts/audit_backup_archives.py"      --archive-dir "${STAGING_DIR}/candidate"
   ```

6. **Verify Original Archive & Database Immutability & Record Metadata:**
   ```bash
   test "$(sha256sum "${ARCHIVE_DIR}/uploads.tar.gz" | awk '{print $1}')" = "${ORIG_ARCH_SHA}"
   test "$(sha256sum "${ARCHIVE_DIR}/orders.db" | awk '{print $1}')" = "${ORIG_DB_SHA}"
   # Save candidate_metadata.json with all active and candidate checksums
   ```
   At the end of Phase 1, the historical archive remains completely untouched.

##### Phase 2: Operator-Confirmed Replacement with Commit Failure Recovery (`--confirm <CANDIDATE_SHA>`)

7. **Operator Confirmation & Full-State Pre-Commit Verification:**
   ```bash
   # Phase 2 command:
   # ./remediation_runbook.sh --confirm <CANDIDATE_SHA>
   ```
   Before any active mutation, the runbook verifies:
   - Provided SHA-256 is 64 hex characters and matches `CANDIDATE_ARCH_SHA`.
   - Active `uploads.tar.gz` matches `ORIG_ARCH_SHA`.
   - Active `orders.db` matches `ORIG_DB_SHA`.
   - Active `manifest.sha256` matches `ORIG_MAN_SHA` if manifest originally existed, or confirms absence if originally absent.
   - Preserved `uploads.tar.gz.pre-purge` matches `ORIG_ARCH_SHA`.
   - Candidate `orders.db` and `manifest.sha256` match recorded candidate hashes.
   - Candidate passes integrity verification and zero-retention privacy audit.

8. **TOCTOU-Safe Temporary File Creation:**
   - Temporary replacement files for archive and manifest are created exclusively (`NamedTemporaryFile(..., delete=False)`), written directly through open file descriptors with mode `0o600`, flushed, fsynced, verified non-symlink, and verified against candidate checksums.

9. **Commit Point & Recoverable Mutating Lifecycle:**
   ```bash
   TRANSACTION_PHASE="ARCHIVE_MUTATED"
   update_journal "ARCHIVE_MUTATED"
   mv "${TMP_ARCHIVE}" "${ARCHIVE_DIR}/uploads.tar.gz"

   TRANSACTION_PHASE="MANIFEST_MUTATING"
   if ! mv "${TMP_MANIFEST}" "${ARCHIVE_DIR}/manifest.sha256"; then
       echo "ERROR: Manifest replacement rename failed after archive replacement" >&2
       execute_rollback
       exit 1
   fi
   TRANSACTION_PHASE="MANIFEST_MUTATED"
   update_journal "MANIFEST_MUTATED"
   ```

10. **Post-Replacement Double Verification & Automatic Atomic Rollback:**
    ```bash
    # Post-replacement verification:
    (cd "${ARCHIVE_DIR}" && sha256sum -c manifest.sha256)
    python3 "${REPO_ROOT}/scripts/audit_backup_archives.py" --archive-dir "${ARCHIVE_DIR}"
    python3 "${REPO_ROOT}/scripts/verify_backup_integrity.py" ...
    ```
    - **On Success:** `TRANSACTION_PHASE="COMMITTED"`, staging directory and transaction journal are cleanly removed, lock is released on exit.
    - **On Any Failure (Manifest Rename or Post-Verification):**
      1. Sets `TRANSACTION_PHASE="ROLLING_BACK"`.
      2. Restores `uploads.tar.gz` from `.pre-purge` baseline via temporary file and atomic `mv`.
      3. If manifest originally existed, restores `manifest.sha256` from `.pre-purge` baseline.
      4. If manifest was originally absent, removes `manifest.sha256` from `${ARCHIVE_DIR}` and verifies absence.
      5. Verifies restored archive and manifest checksums against expected original baselines.
      6. Emits `ROLLBACK_SUCCESS` and preserves `${STAGING_DIR}` for diagnostic forensics.
      7. If rollback restored checksum fails: sets `TRANSACTION_PHASE="FAILED"`, emits `RECOVERY_REQUIRED`, and preserves `${LOCK_DIR}` and `${STAGING_DIR}`.

##### Stale-Lock Recovery Protocol (After Uncatchable SIGKILL)

If a process running Phase 2 terminates uncatchably (`SIGKILL`, host kernel panic, power loss), the directory lock `${ARCHIVE_DIR}/.remediation.lock` remains on disk:

1. **Check Caller PID:**
   ```bash
   PID=$(cat "${ARCHIVE_DIR}/.remediation.lock/pid" 2>/dev/null)
   # If PID is still running, do NOT touch the lock:
   kill -0 "${PID}" 2>/dev/null && { echo "Process ${PID} is still active!"; exit 1; }
   ```
2. **Inspect Transaction Journal (`${ARCHIVE_DIR}/.transaction_journal.json`):**
   - **Phase `PRE_COMMIT`:** No active files were mutated. Active archive, database, and manifest are intact. Safely remove lock:
     ```bash
     rm -rf "${ARCHIVE_DIR}/.remediation.lock"
     ```
   - **Phase `ARCHIVE_MUTATED` or `MANIFEST_MUTATED` or `VERIFYING`:** A mutation occurred. Restore original baseline immediately:
     ```bash
     cp "${ARCHIVE_DIR}/uploads.tar.gz.pre-purge" "${ARCHIVE_DIR}/uploads.tar.gz"
     if [ -f "${ARCHIVE_DIR}/manifest.sha256.pre-purge" ]; then
       cp "${ARCHIVE_DIR}/manifest.sha256.pre-purge" "${ARCHIVE_DIR}/manifest.sha256"
     else
       rm -f "${ARCHIVE_DIR}/manifest.sha256"
     fi
     sha256sum "${ARCHIVE_DIR}/uploads.tar.gz"
     rm -rf "${ARCHIVE_DIR}/.remediation.lock"
     ```
   - **Phase `COMMITTED`:** Replacement completed successfully before crash. Verify `sha256sum -c manifest.sha256`, then remove lock:
     ```bash
     rm -rf "${ARCHIVE_DIR}/.remediation.lock" "${ARCHIVE_DIR}/.purge_staging" "${ARCHIVE_DIR}/.transaction_journal.json"
     ```

### 5. Independent Sandbox Restore Rehearsal

Before modifying the active production database, verify the restorable state in an isolated scratch environment using the standalone verification CLI (`scripts/verify_backup_integrity.py`).

The verifier:
- Reconciles P1 attachments via `delivery_jobs` (safe path confinement, physical existence, exact size, and SHA-256).
- Supports historical P0 orders without false-positive failures if old local files were not retained.
- Blocks directory traversal (`..` escape attempts).
- Returns non-zero exit code on corrupted/missing data.
- Emits a sanitized report free of customer PII.

```bash
RESTORE_DIR=$(mktemp -d /tmp/photodoc_restore_XXXXXX)

# 1. Verify checksums against manifest
cd "${BACKUP_DIR}"
sha256sum -c manifest.sha256

# 2. Extract into sandbox
cp "${BACKUP_DIR}/orders.db" "${RESTORE_DIR}/orders.db"
tar -xzf "${BACKUP_DIR}/uploads.tar.gz" -C "${RESTORE_DIR}"

# 3. Execute integrity and storage verification CLI
python3 /candidate/scripts/verify_backup_integrity.py --backup-dir "${RESTORE_DIR}"

# 4. Clean up sandbox
rm -rf "${RESTORE_DIR}"
```

### 6. Rollback Protocol and Migration Compatibility

- **Rollback before new orders are accepted:**
  If migration fails or candidate verification aborts before opening public traffic:
  1. Stop candidate containers.
  2. Restore `/data/orders.db` from `${BACKUP_DIR}/orders.db`.
  3. Ensure ownership and mode: `chmod 644 /data/orders.db`.
  4. Restart the verified previous release backend.
- **Rollback after new orders are accepted (Additive Migration Compatibility):**
  P1 migration is strictly additive:
  - Adds `order_token_hash` column to `orders`.
  - Creates `delivery_jobs` table and indexes.
  The P0 backend inspection (`inspect_database` in `backend/db_contract.py`) verifies required columns and application identity `1346650441`, and gracefully tolerates additional columns or tables.
  However, orders created while running P1 will have their delivery tasks queued in `delivery_jobs` rather than executed via P0's legacy synchronous path.
  Therefore:
  - In a catastrophic rollback to P0 while preserving new customer orders, run an operator script to inspect pending `delivery_jobs` and dispatch them manually.
  - If reverting completely to the pre-upgrade snapshot, any orders placed during the failed P1 window must be exported before database restoration.

## Recovery model

New uploads are streamed into temporary files on the persistent uploads mount.
The API assigns each attachment a random stored name, then commits the order,
its token hash, and file/info/email jobs in one SQLite transaction after the
local sources exist. A crash before commit can leave an orphan source file, but
cannot leave a committed order without delivery jobs. Orphans may be inspected
and removed only after checking the database; no automatic cleanup is performed.

The worker claims due jobs with a SQLite write transaction and a three-minute
lease. Failures retain local files and retry with bounded exponential backoff.
Expired leases are eligible again after a process crash. Lease fencing ensures
that a delayed worker whose lease expired cannot clobber a newly claimed active lease.
Retries are capped at a maximum attempt limit (default: 10, configurable via
`DELIVERY_MAX_ATTEMPTS`) to quarantine unrecoverable tasks for inspection.
Delivery state is in the same persistent database as the order. The Yandex upload uses
the existing OAuth token, `overwrite=false`, and verifies remote SHA-256 and size before
marking a job delivered. A matching remote file makes retries idempotent; a
different file at that path is an error. Local sources are automatically and safely cleaned up immediately upon confirmed
delivery to Yandex.Disk according to the Privacy by Design model (see Section 7 below).
Temporary storage on the VPS is strictly ephemeral; Yandex.Disk is the authoritative persistent storage.

Upload limits are aligned across the containerized reverse proxy (`client_max_body_size 100M`)
and backend (20 MiB per file, 80 MiB per order) to guarantee that multipart requests within
backend constraints are accepted.

Old orders have no token hash. They remain readable in the database, and
existing signed Robokassa ResultURL callbacks can still complete initiated
payments. Creating a new payment link for an old order is blocked until an
operator has authenticated the customer through a separate, approved flow.
Old files are not renamed or requeued automatically.

SMTP jobs also persist. An SMTP failure leaves the job retryable. Delivery is
at least once: if the SMTP server accepts a message and the process crashes
before the success record is committed, a duplicate message remains possible.
Payment status itself remains terminal and is never retried as a new charge.


---

## 7. Automated Photo Cleanup & Privacy by Design Architecture

### 7.1 Core Storage Principle
**Яндекс.Диск является постоянным защищённым хранилищем заказа; локальный VPS выполняет роль временного буфера до подтверждения доставки.**

PhotoDoc AI реализует принцип *Privacy by Design* и минимизации хранения чувствительных персональных данных (фотографий клиентов). Клиентские фотографии и сопроводительные карточки (`info.txt`) удаляются с локального диска VPS автоматически, сразу же после гарантированной доставки в облако лаборатории.

### 7.2 Детерминированный жизненный цикл клиентской фотографии
Для каждого входящего файла:
1. **Приём файла:** Поток загрузки сохраняется во временный файл `.incoming-*` в `/app/uploads` с вычислением SHA-256 и размера.
2. **Атомарная фиксация заказа:** Файл перемещается в `{order_id}_{uuid}.{ext}`, и в одной SQLite-транзакции создаётся запись `delivery_jobs` (`kind='file'`) с исходными `content_sha256` и `content_size`.
3. **Загрузка на Яндекс.Диск:** Фоновый worker забирает задачу по SQLite-лизингу (`claim_due`) и выгружает файл по OAuth API с параметром `overwrite=false`.
4. **Удалённая сверка метаданных:** По завершении выгрузки worker запрашивает ресурсные метаданные Яндекс.Диска и сверяет удалённые `sha256` и `size` **строго с сохранёнными в `delivery_jobs`**, а не только с локальным файлом на диске.
5. **Фиксация доставки:** SQLite транзакционно фиксирует `state='delivered'` с проверкой действующего лизинга (`finish`).
6. **Независимая очистка:** Сразу после успешного COMMIT планируется очистка (`perform_job_cleanup`). **Каждый доставленный файл удаляется независимо**, не дожидаясь оплаты заказа, отправки email или доставки остальных вложений.
7. **Верификация локального файла перед удалением:** Перед вызовом `unlink()` система проверяет:
   - Путь строго внутри `/app/uploads` (защита от directory traversal `..`).
   - Отсутствие символьных ссылок (защита от symlink-атак).
   - Размер и SHA-256 локального файла точно совпадают с метаданными задачи (защита от подмены файла).
   - Никакая другая незавершённая задача в `delivery_jobs` не ссылается на этот файл.
8. **Фиксация очистки в SQLite:** Локальный файл удаляется (`unlink()`), и в БД фиксируется `cleanup_state='completed'`, `local_deleted_at=utc_now()`.

Аналогичный цикл выполняется для файла `info.txt` (`kind='info'`).

### 7.3 Устойчивость к сбоям (Crash Recovery State Machine)
Состояние очистки отслеживается отдельным конечным автоматом в `delivery_jobs`:
- `pending`: файл ожидает очистки после доставки.
- `processing`: очистка выполняется конкретным worker под лизингом `cleanup_lease_until`.
- `completed`: файл успешно удалён с локального диска, зафиксирован `local_deleted_at`.
- `failed`: ошибка удаления (например, временная блокировка файла ОС), инкрементирован `cleanup_attempts`.
- `not_applicable`: для почтовых задач (`admin_email`, `paid_email`).

**Гарантии отказоустойчивости:**
- **Идемпотентность:** Повторная очистка уже удалённого файла штатно возвращает успех и обновляет статус до `completed`.
- **Никаких повторных отправок:** Сбой очистки **никогда не переводит** задачу обратно в `state='pending'` для повторной загрузки на Яндекс.Диск.
- **Автоматическое восстановление при рестарте:** При перезапуске backend фоновый процесс `process_due_cleanups()` находит все доставленные задачи с `cleanup_state IN ('pending', 'failed')` и завершает очистку.
- **Сбой между unlink и commit:** Если процесс аварийно завершился сразу после удаления файла до коммита в БД, при следующем запуске система видит отсутствие файла на диске при подтверждённой доставке и корректно фиксирует `cleanup_state='completed'`.

### 7.4 Уборка временных файлов
Функция `cleanup_orphaned_temporary_files()` безопасно удаляет остаточные файлы:
- Осиротевшие `.incoming-*` после аварийного отключения клиентов при загрузке.
- Временные файлы `.tmp*`.
- **Защитный возрастной порог:** Файлы удаляются только при возрасте более 1 часа (`max_age_seconds=3600`) и при отсутствии ссылок из активных задач `delivery_jobs`.
- **Запрет удаления по возрасту:** Файлы заказов (фотографии и `info.txt`) **категорически запрещено удалять по одному лишь возрасту** без подтверждения доставки в `delivery_jobs`.

### 7.5 Физическое удаление на твердотельных накопителях (SSD/NVMe Disclaimer)
> **Техническое уточнение о приватности:**
> Стандартный системный вызов `unlink()` удаляет дескриптор файла и возвращает блоки файловой системе, передавая команду TRIM контроллеру диска. Однако из-за аппаратных особенностей SSD/NVMe (алгоритмы выравнивания износа Wear-Leveling, трансляция адресов FTL, резервные пулы Over-Provisioning) стандартное удаление файлов операционной системой **не даёт криптографической гарантии немедленного физического зануления ячеек flash-памяти**.
> PhotoDoc AI обеспечивает защиту персональных данных за счёт минимизации времени жизни файлов на сервере (буферизация на несколько минут до выгрузки в облако) и запрета логирования содержимого. Для систем с абсолютными требованиями к криптографическому уничтожению рекомендуется применение полнодискового шифрования LUKS/dm-crypt с уничтожением эфемерных ключей при выводе дисков из эксплуатации.

### 7.6 Регламент хранения существующих архивных копий
1. **Исторические архивы:** Существующие архивные дампы (`photodoc_cutover.tar.gz`, снапшоты до внедрения очистки), содержащие клиентские фотографии, подлежат безопасному хранению в изолированном каталоге с правами `chmod 600` в течение 30-дневного скользящего периода (rolling retention) для целей аудита и аварийного восстановления.
2. **Запрет автоматического удаления:** Автоматические скрипты **НЕ имеют права удалять существующие архивные дампы** без отдельного утверждённого регламента и прямого подтверждения владельца.
3. **Новые инкрементальные бэкапы:** Все новые резервные копии, создаваемые после подтверждения доставки и очистки, содержат только базу данных SQLite (`orders.db`), метаданные и ещё не доставленные файлы, что минимизирует объём бэкапа и исключает накопление персональных фотографий.
