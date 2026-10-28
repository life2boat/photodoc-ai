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
- Generates `manifest.sha256` and structured metadata `backup_manifest.json` (`zero_retention_enforced: true`).
- Automatically extracts and verifies the generated backup with `scripts/verify_backup_integrity.py`.
- Sets secure permissions (`chmod 600` on files, `chmod 700` on directory).

### 4. Historical Backup Archive Auditing & Residual Privacy Risk

Historical backup archives created prior to the zero-retention policy represent a **residual privacy risk** if they contain archived customer photos from orders that have since been delivered.

To audit existing historical archives without modifying or deleting anything, use the read-only dry-run auditor:

```bash
python3 /candidate/scripts/audit_backup_archives.py --backup-root /var/backups/photodoc
```

The audit tool inspects `orders.db` and `uploads.tar.gz` in-memory, cross-references order delivery statuses, and reports:
- Number of archives containing retained delivered customer photos (`residual_privacy_risk: HIGH`).
- Number of legitimate recovery originals for unfinished deliveries.
- Provides a safe, owner-controlled purge procedure (e.g. `tar --delete -f uploads.tar.gz uploads/<file>` and checksum recomputation) to allow an owner to redact historical customer photos without altering database audit trails.

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
