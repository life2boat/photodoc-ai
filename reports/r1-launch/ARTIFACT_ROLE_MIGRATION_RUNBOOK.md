# Регламент применения миграции схемы артефактов (artifact_role)

**Документ:** `reports/r1-launch/ARTIFACT_ROLE_MIGRATION_RUNBOOK.md`  
**Назначение:** Пошаговая инструкция для безопасного применения аддитивной миграции схемы `artifact_role` в базе данных PhotoDoc AI (`delivery_jobs`), классификации исторических строк и валидации целостности до перезапуска рабочего контура.  
**Контекст:** Находка F-2 независимого аудита DeepSeek (`PHOTODOC-R1-DEEPSEEK-INDEPENDENT-AUDIT-01`).  
**Статус готовности:** REQUIREMENT BEFORE MERGE / PRE-DEPLOYMENT RUNBOOK  
**Режим:** Остановленный писатель (offline migration), строгий бэкап, проверка WAL.

> [!IMPORTANT]
> **ВНИМАНИЕ: ПРИМЕНЕНИЕ НА ПРОДАКШЕНЕ ТРЕБУЕТ ОТДЕЛЬНОГО ОДОБРЕНИЯ ВЛАДЕЛЬЦА.**
> Настоящий регламент является подготовительным документом. Любое выполнение миграции или изменение базы данных на сервере продакшена **СТРОГО ТРЕБУЕТ ОТДЕЛЬНОЙ ЯВНОЙ АВТОРИЗАЦИИ ВЛАДЕЛЬЦА ПРОЕКТА** (SEPARATE OWNER AUTHORIZATION). До подтверждения владельцем статус релиза: `DEPLOYMENT_BLOCKED`.


---

## 1. Предварительные проверки (Pre-migration checks)

Перед началом любых манипуляций необходимо убедиться в идентичности базы данных и отсутствии незавершённых транзакций в журнале WAL.

1. **Проверка контракта и схемы базы данных через `inspect_database`**:
   ```bash
   python -c "
   import sys
   from pathlib import Path
   sys.path.insert(0, 'backend')
   from db_contract import inspect_database, APPLICATION_ID
   ready, details = inspect_database(Path('backend/orders.db'), require_complete_schema=True)
   print(f'READY={ready}, APP_ID_VALID={details.get(\"identity_valid\")}, APP_ID={details.get(\"application_id\")}')
   if not details.get('identity_valid'):
       sys.exit(1)
   "
   ```
   *Ожидаемый результат:* `READY=True, APP_ID_VALID=True, APP_ID=1346650953` (ASCII `"PDAI"`).

2. **Проверка режима журналирования и состояния WAL**:
   ```bash
   sqlite3 backend/orders.db "PRAGMA journal_mode; PRAGMA integrity_check;"
   ```
   *Ожидаемый результат:*
   ```text
   wal
   ok
   ```

3. **Сброс WAL на диск (Checkpoint)**:
   ```bash
   sqlite3 backend/orders.db "PRAGMA wal_checkpoint(TRUNCATE);"
   ```
   *Ожидаемый результат:* `0|0|0` (все страницы WAL сброшены в основной `.db` файл).

---

## 2. Остановка сервисов-писателей (Stop database writers)

Во избежание конкурентных блокировок `database is locked` и повреждения транзакций необходимо полностью остановить процессы бэкенда и фонового воркера доставки.

1. **Для окружения Systemd (VPS)**:
   ```bash
   sudo systemctl stop photodoc-backend
   sudo systemctl status photodoc-backend
   ```
   *Убедиться, что сервис перешёл в статус `inactive (dead)`.*

2. **Для окружения Docker Compose**:
   ```bash
   docker compose stop backend delivery-worker
   docker compose ps
   ```

3. **Контроль отсутствия открытых файловых дескрипторов**:
   ```bash
   # Linux:
   lsof backend/orders.db || fuser backend/orders.db
   # Процессы не должны выводиться
   ```

---

## 3. Верифицированное резервное копирование (Verified Backup)

Резервная копия создаётся с использованием механизма SQLite Online Backup API (`.backup`), что гарантирует атомарность снимка.

1. **Создание физического снимка**:
   ```bash
   TIMESTAMP=$(date +%Y%m%d_%H%M%S)
   BACKUP_DIR="backend/backups"
   mkdir -p "$BACKUP_DIR"
   sqlite3 backend/orders.db ".backup '${BACKUP_DIR}/orders_pre_artifact_role_${TIMESTAMP}.db'"
   ```

2. **Фиксация контрольной суммы SHA-256**:
   ```bash
   sha256sum "${BACKUP_DIR}/orders_pre_artifact_role_${TIMESTAMP}.db" > "${BACKUP_DIR}/orders_pre_artifact_role_${TIMESTAMP}.db.sha256"
   cat "${BACKUP_DIR}/orders_pre_artifact_role_${TIMESTAMP}.db.sha256"
   ```

3. **Проверка целостности созданной копии**:
   ```bash
   sqlite3 "${BACKUP_DIR}/orders_pre_artifact_role_${TIMESTAMP}.db" "PRAGMA integrity_check;"
   python -c "
   import sys
   from pathlib import Path
   sys.path.insert(0, 'backend')
   from db_contract import inspect_database
   ready, details = inspect_database(Path('${BACKUP_DIR}/orders_pre_artifact_role_${TIMESTAMP}.db'))
   assert ready and details['identity_valid'], 'Backup database identity verification failed'
   print('BACKUP_VERIFIED_OK')
   "
   ```

---

## 4. Тестовый прогон миграции (Migration Dry-Run)

Скрипт `reconcile_delivery_schema.py` по умолчанию запускается в режиме инспекции (Dry-Run), вычисляя необходимость изменений без записи в БД.

1. **Команда Dry-Run**:
   ```bash
   python backend/migrations/reconcile_delivery_schema.py --db backend/orders.db --uploads-dir backend/uploads
   ```

2. **Ожидаемый вывод**:
   ```text
   MIGRATION_REQUIRED=true
   APPLIED=false
   ```
   *(Если миграция уже была применена ранее, будет выведено `MIGRATION_REQUIRED=false`).*

---

## 5. Применение миграции (Apply Migration)

Применение выполняется под эксклюзивной блокировкой `BEGIN IMMEDIATE` с проверкой целостности `PRAGMA integrity_check`.

1. **Команда применения**:
   ```bash
   python backend/migrations/reconcile_delivery_schema.py --db backend/orders.db --uploads-dir backend/uploads --apply
   ```

2. **Ожидаемый вывод**:
   ```text
   MIGRATION_REQUIRED=true
   APPLIED=true
   ```

3. **Идемпотентная повторная проверка**:
   ```bash
   python backend/migrations/reconcile_delivery_schema.py --db backend/orders.db --uploads-dir backend/uploads
   ```
   *Ожидаемый вывод:*
   ```text
   MIGRATION_REQUIRED=false
   APPLIED=false
   ```

---

## 6. Верификация схемы и классификации строк (Schema & Classification Verification)

1. **Проверка наличия колонки `artifact_role` в таблице `delivery_jobs`**:
   ```bash
   sqlite3 backend/orders.db "PRAGMA table_info(delivery_jobs);" | grep artifact_role
   ```
   *Ожидаемый результат:*
   ```text
   3|artifact_role|TEXT|0||0
   ```

2. **Контроль распределения строк по ролям**:
   ```bash
   sqlite3 -header -column backend/orders.db "
   SELECT kind, artifact_role, state, count(*) as count 
   FROM delivery_jobs 
   GROUP BY kind, artifact_role, state;
   "
   ```
   *Критерии валидности:*
   - Все `kind = 'info'` имеют `artifact_role = 'info'`.
   - Все недоставленные файлы (`kind = 'file' AND state != 'delivered'`) имеют `artifact_role = 'source'`.
   - Задачи обработанных оператором результатов имеют `artifact_role = 'result'`.
   - Ни одна строка с `kind = 'file'` не должна иметь `artifact_role IS NULL` (за исключением случаев, когда таблица пуста).

3. **Проверка целостности индексов и базы данных**:
   ```bash
   sqlite3 backend/orders.db "PRAGMA foreign_key_check; PRAGMA integrity_check;"
   ```
   *Ожидаемый результат:* `ok`.

---

## 7. Проверка совместимости приложения (Application Compatibility Checks)

1. **Валидация через контракт базы данных и модули доставки**:
   ```bash
   python -c "
   import sys
   from pathlib import Path
   sys.path.insert(0, 'backend')
   import sqlite3
   from db_contract import inspect_database
   from delivery import claim_due, process_due

   db_path = Path('backend/orders.db')
   ready, details = inspect_database(db_path, require_complete_schema=True)
   assert ready, f'Database not ready: {details}'

   with sqlite3.connect(db_path) as conn:
       cols = [row[1] for row in conn.execute('PRAGMA table_info(delivery_jobs)').fetchall()]
       assert 'artifact_role' in cols, 'Column artifact_role is missing!'
       
       # Проверка fail-closed инварианта: исходники не должны выбираться claim_due
       conn.row_factory = sqlite3.Row
       claimed = claim_due(db_path)
       if claimed:
           assert claimed['artifact_role'] != 'source', 'CRITICAL: source claimed for delivery!'
   print('APP_COMPATIBILITY_VERIFIED=PASS')
   "
   ```

2. **Запуск синтетического интеграционного теста**:
   ```bash
   pytest backend/tests/test_migration_cleanup_backfill.py backend/tests/test_migration_reconciliation.py -v
   ```
   *Ожидаемый результат:* 100% PASS.

---

## 8. План отката и аварийного восстановления (Rollback & Recovery Plan)

Если во время шагов 5–7 возникли ошибки (`RuntimeError`, сбой целостности SQLite или несовместимость данных):

1. **Немедленно зафиксировать журнал ошибки**:
   ```bash
   echo "Migration failed at $(date)" >> backend/backups/migration_error.log
   ```

2. **Восстановление базы данных из проверенной резервной копии**:
   ```bash
   # Удаление повреждённых файлов WAL и SHM
   rm -f backend/orders.db-wal backend/orders.db-shm
   
   # Восстановление основного файла
   cp "${BACKUP_DIR}/orders_pre_artifact_role_${TIMESTAMP}.db" backend/orders.db
   ```

3. **Проверка восстановленной копии**:
   ```bash
   sqlite3 backend/orders.db "PRAGMA integrity_check;"
   python -c "
   import sys
   from pathlib import Path
   sys.path.insert(0, 'backend')
   from db_contract import inspect_database
   ready, details = inspect_database(Path('backend/orders.db'))
   assert ready and details['identity_valid']
   print('ROLLBACK_VERIFIED=SUCCESS')
   "
   ```

4. **Запуск сервиса в откатанном состоянии**:
   - Предыдущая версия бэкенда продолжает работу со старой схемой.
   - Исходные данные клиентов и заказов остаются полностью сохранными.

---

## 9. Условия безопасного перезапуска (Safe Restart Conditions)

Сервис `photodoc-backend` и воркер доставки могут быть запущены **только при выполнении всех следующих условий**:

- [x] Шаг 1: `inspect_database` и `PRAGMA integrity_check` вернули `ok`.
- [x] Шаг 3: Создана верифицированная резервная копия с проверенным SHA-256.
- [x] Шаг 5: `reconcile_delivery_schema.py --apply` выполнен без исключений с результатом `APPLIED=true`.
- [x] Шаг 6: Колонка `artifact_role` присутствует в `delivery_jobs`, отсутствуют строки с невалидными ролями.
- [x] Шаг 7: Скрипт проверки контракта подтвердил `APP_COMPATIBILITY_VERIFIED=PASS`.
- [x] Тесты: Набор тестов миграции и жизненного цикла исходников проходит успешно.

**Команда безопасного запуска**:
```bash
# Для Systemd:
sudo systemctl start photodoc-backend
sudo systemctl status photodoc-backend
sudo journalctl -u photodoc-backend -n 50 --no-pager

# Для Docker Compose:
docker compose up -d backend delivery-worker
docker compose logs -n 50 backend
```
