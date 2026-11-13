# PhotoDoc AI — R1 Stream Lease Liveness & Lifecycle Fix Report (Sprint 10)

**TASK_ID:** `PHOTODOC-R1-STREAM-LEASE-LIVENESS-FIX-10`  
**BRANCH:** `feat/r1-stage2-close-four-findings`  
**BASE_HEAD:** `07d16121e1a380c8f9657cfbef65836042b856a4`  
**WORKTREE:** `F:\диск е\landing\.worktrees\audit-integration`  
**STATUS:** `SUCCESS / FULL PASS`  
**PRODUCTION_UNCHANGED:** `true`  
**DEPLOYMENT_BLOCKED:** `true`  
**LEGAL_APPROVAL_STATUS:** `NOT_APPROVED`

---

## 1. Executive Summary

В рамках задачи **PHOTODOC-R1-STREAM-LEASE-LIVENESS-FIX-10** устранена liveness-регрессия, выявленная независимым аудитором в проверке №9 (`reports/r1-launch/R1_STREAM_LEASE_INDEPENDENT_RECHECK_09.md`), без малейшего ослабления защитных механизмов от дефектов HIGH-01..HIGH-04 и инвариантов Stage 7, 8 и 9.

### Ключевые результаты
1. **Воспроизведена и устранена ошибка зависания задач**: тест с 4 параллельными воркерами и искусственной блокировкой SQLite на 0.35 секунды (`test_mixed_paid_unpaid_four_workers_lock_contention_and_restart`) воспроизводил `AssertionError: [('delivered', ... 'processing')] != [('delivered',)]`. После внедрения фикса тест прошел **10 из 10 последовательных запусков (100% стабильность)**.
2. **Ликвидировано подавление ошибок**: `release_delivery_stream_lease` больше не подавляет `sqlite3.OperationalError` внутри тела функции, позволяя декоратору `@retry_busy` корректно повторять попытки при contention. В `deliver_job` ошибки освобождения lease логируются с уровнем `WARNING`, а не проглатываются через `pass`.
3. **Разделены фазы жизненного цикла передачи**:
   - Фаза 1: Передача чанков по HTTP (`httpx.put`).
   - Фаза 2: Подтверждение целостности и наличия на удаленном хранилище (`confirm_delivery_stream_completed` устанавливает `http_completed = 1, delivery_confirmed = 1`).
   - Фаза 3: Освобождение stream lease (`release_delivery_stream_lease`).
   - Фаза 4: Атомарная финализация состояния задачи и удаление lease в SQLite (`finish` с обязательной проверкой `stream_operation_id` и флагов `http_completed == 1 and delivery_confirmed == 1`).
4. **Защита от преждевременной финализации**: `finish()` отклоняет финализацию в `delivered` для активных стримов (`http_completed == 0`), даже если передан валидный токен.
5. **Предотвращение повторной передачи байтов при ретраях**: `upload_to_yandex_disk` выполняет проверку `_remote_verified` до открытия файла и отправки чанков. Если файл уже подтвержден на удаленном сервере, он немедленно подтверждает завершение через `confirm_delivery_stream_completed` и возвращает `True` без повторной отправки байтов по сети.
6. **Исправление предикатов в `claim_due` и `quarantine_expired`**: исключение задач из выборки ограничено только фактически активными стримами (`WHERE COALESCE(http_completed, 0) = 0 AND expires_at > ?`), что устраняет зависание задач в `processing` при сбоях воркеров.
7. **Анализ расхождения в коллекции тестов аудитора**:
   - Аудитор сообщал о 4 ошибках коллекции при запуске pytest из корня проекта без `PYTHONPATH`.
   - В `backend/pytest.ini` настроено `pythonpath = . ..`, рассчитанное на запуск из папки `backend/` (как в GitHub Actions CI).
   - При запуске `python -m pytest tests -q` из папки `backend/` собрано **847 тестов: 833 passed, 14 skipped, 0 failed**.

---

## 2. Root Cause Analysis

### 2.1. Механика liveness-регрессии в спринте №9
В спринте №9 была введена строгая валидация `stream_operation_id` в `finish()`: если в таблице `delivery_stream_leases` оставалась запись для живого PID, `finish()` без совпадающего `stream_operation_id` возвращал `False`.

При этом возникала следующая цепочка сбоев:
1. Во время отправки письма оплаты вызывалась блокировка `BEGIN IMMEDIATE` на 0.35 секунды (имитация contention).
2. Параллельный воркер доставки завершал загрузку файла и вызывал `release_delivery_stream_lease()`.
3. Внутри `release_delivery_stream_lease()` блок `except Exception: return False` ловил `sqlite3.OperationalError: database is locked`, возвращал `False` сразу же и **срывал работу декоратора `@retry_busy`** (декоратор не получал исключение и не делал повторных попыток).
4. В `main.py` в функции `deliver_job` в блоке `finally` вызов `release_delivery_stream_lease()` был обернут в `except Exception: pass`, что скрывало факт сбоя освобождения.
5. `deliver_job()` не возвращал `stream_operation_id` в `process_due()`.
6. `process_due()` вызывал `finish(db_path, job["id"], lease_until=lease)` **без** `stream_operation_id`.
7. `finish()` видел живой PID в `delivery_stream_leases`, отвергал финализацию (`return False`), и задача оставалась в состоянии `processing`.
8. Функция `claim_due()` содержала предикат `AND id NOT IN (SELECT job_id FROM delivery_stream_leases)`. Из-за неудаленного lease ни один воркер больше не мог взять задачу в обработку, и она зависала навсегда.

---

## 3. Детали реализации

### 3.1. Схема таблицы `delivery_stream_leases` (`backend/delivery.py`)
Добавлены колонки `http_completed` и `delivery_confirmed`:
```sql
CREATE TABLE IF NOT EXISTS delivery_stream_leases (
    job_id TEXT PRIMARY KEY,
    holder_pid INTEGER NOT NULL,
    holder_start_time REAL,
    acquired_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    gate_generation INTEGER NOT NULL DEFAULT 1,
    stream_operation_id TEXT,
    http_completed INTEGER NOT NULL DEFAULT 0,
    delivery_confirmed INTEGER NOT NULL DEFAULT 0
);
```
Миграция схемы в `ensure_gate_table()` дополнена проверкой `PRAGMA table_info` и `ALTER TABLE` для добавления колонок при работе со старыми базами данных.

### 3.2. Функция `confirm_delivery_stream_completed` (`backend/delivery.py`)
Обеспечивает подтверждение передачи с автоматическим повторением при конкуренции за базу данных:
```python
@retry_busy
def confirm_delivery_stream_completed(
    db_path_or_conn: Path | str | sqlite3.Connection,
    job_id: str,
    stream_operation_id: str | None = None,
) -> bool:
    ...
```

### 3.3. Обновление `finish()` (`backend/delivery.py`)
- Проверяет `stream_operation_id` для предотвращения несанкционированного изменения состояния чужими процессами или операциями.
- Защищает от преждевременной финализации: если `http_completed == 0` или `delivery_confirmed == 0`, перевод в состояние `delivered` отклоняется (`conn.rollback(); return False`).
- При подтвержденной передаче атомарно в одной транзакции удаляет `delivery_stream_leases` и переводит `delivery_jobs` в `delivered`.
- Если передана ошибка (`error is not None`) от владельца операции, атомарно удаляет lease и выставляет `failed` с экспоненциальной задержкой.

### 3.4. Обновление `quarantine_expired` и `claim_due` (`backend/delivery.py`)
Предикаты исключения задач со stream lease теперь учитывают реальный статус передачи:
```python
# claim_due: исключаются только задачи, где передача чанков активна и не истек срок
"AND id NOT IN ("
"SELECT job_id FROM delivery_stream_leases "
"WHERE COALESCE(http_completed, 0) = 0 AND expires_at > ?"
") "
```

### 3.5. Интеграция в `backend/main.py`
- В `upload_to_yandex_disk`: вызов `confirm_delivery_stream_completed` при успешном `_remote_verified` (как до начала передачи при повторных запусках, так и после завершения передачи).
- В `deliver_job`: инициализация `stream_lease = None` в начале функции (исключение `UnboundLocalError` для email-задач), запись `job["stream_operation_id"] = stream_lease["stream_operation_id"]`, логирование сбоев освобождения lease с уровнем `WARNING`, возврат словаря `{"ok": True, "stream_operation_id": ...}`.

---

## 4. Верификация и тестирование

### 4.1. Многопоточный E2E тест с искусственной блокировкой
- Файл: `backend/tests/test_payment_delivery_concurrency_e2e.py`
- Тест: `test_mixed_paid_unpaid_four_workers_lock_contention_and_restart`
- Конфигурация: 4 воркера, 8 заказов (смешанные оплаченные и неоплаченные), искусственная задержка SQLite `BEGIN IMMEDIATE` на 0.35 секунды при отправке SMTP, два цикла `process_due`.
- Результат: **10 из 10 последовательных прогонов PASSED (0 сбоев)**.

### 4.2. Новые регрессионные тесты 10-го этапа (`backend/tests/test_r1_concurrency_artifact_alias_04.py`)
1. `test_finish_during_active_stream_rejected_even_with_valid_token`: попытка вызвать `finish()` при активном стриме (`http_completed=0`) отклоняется, задача остается в `processing`.
2. `test_finish_after_confirm_completed_succeeds_and_atomically_cleans_lease`: после вызова `confirm_delivery_stream_completed` вызов `finish()` успешно фиксирует `delivered` и атомарно очищает lease.
3. `test_release_failure_sqlite_contention_followed_by_finish_finalizes_cleanly`: моделирование сбоя освобождения lease из-за contention — `finish()` корректно завершает задачу и удаляет lease.
4. `test_retry_after_contention_or_restart_does_not_retransmit_duplicate_bytes`: при повторном запуске после подтверждения удаленного файла отправка повторных байтов не производится (0 вызовов `httpx.put`).

Итого в `test_r1_concurrency_artifact_alias_04.py`: **41 passed из 41**.

### 4.3. Полный регрессионный прогон всего бэкенда
- Команда: `python -m pytest tests -q` (из папки `backend/`)
- Результат: **833 passed, 14 skipped, 0 failed in 273.10s (847 тестов собрано)**.

---

## 5. Environment & Collection Reproducibility Note

Независимый аудитор в отчете №9 зафиксировал `4 errors during collection` при выполнении pytest. Причина установлена:
- В `backend/pytest.ini` прописана директива:
  ```ini
  pythonpath = . ..
  ```
- Данная конфигурация ожидает, что текущая рабочая директория при запуске тестов — `backend/` (что соответствует конфигурации в `.github/workflows/backend-tests.yml`: `working-directory: backend`).
- При запуске `pytest` из корня репозитория без указания `PYTHONPATH` модули верхнего уровня импортируются с ошибками пути.
- При соблюдении проектной инструкции запуска из `backend/` все 847 тестов собираются без единой ошибки коллекции.
