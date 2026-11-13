# Отчёт об устранении дефекта удержания lease при живом воркере (Stage 08)

**TASK_ID:** `PHOTODOC-R1-LIVE-LEASE-SAFETY-FIX-08`  
**Дата:** 2026-11-13  
**Исполнитель:** Google Antigravity (Advanced Agentic Coding)  
**Репозиторий:** `life2boat/photodoc-ai`  
**Worktree:** `F:\диск е\landing\.worktrees\audit-integration`  
**Branch:** `feat/r1-stage2-close-four-findings`  
**Base Head:** `07d16121e1a380c8f9657cfbef65836042b856a4`  
**Статус:** `RESOLVED / ALL PASS`  

---

## 1. Исходная проблема и Root Cause

### 1.1. Подтверждённый дефект из Recheck 07
В отчёте независимого аудита `R1_STREAMING_ARCHITECTURE_INDEPENDENT_RECHECK_07.md` был зафиксирован воспроизводимый HIGH-дефект:
```text
LEASE_ACQUIRED=6  WORKER_PID_ALIVE=True
ACTIVE_LEASES_AFTER_EXPIRY=[]          ← lease удалён, хотя PID жив
MIGRATION_ACQUIRED_AFTER_EXPIRY=True    ← миграция прошла
EXPIRY_ALONE_ALLOWED_MIGRATION=True
```

### 1.2. Root Cause
1. **Преждевременная очистка в `get_active_stream_leases()` (`backend/delivery.py`):**  
   Функция очищала запись lease исключительно по условию `exp_dt <= cur_dt`, даже когда владелец lease (`holder_pid` и `holder_start_time`) был подтверждённо жив (`is_pid_owner_alive(...) == True`). В результате, миграция считала список активных передач пустым (`[]`), захватывала эксклюзивный `schema_migration_gate` и переклассифицировала артефакты во время активной передачи.
2. **Отсутствие heartbeat/renew в потоковом цикле воркера:**  
   Функция `check_delivery_stream_lease()` выполняла только проверку на чтение, не обновляя `expires_at`. Любая сетевая передача дольше `DEFAULT_STREAM_LEASE_SECONDS=60` неизбежно приводила к истечению lease, его очистке и разрыву взаимного исключения.

---

## 2. Воспроизведение дефекта до исправления

До внесения изменений был запущен прямой воспроизводящий зонд:
```python
conn = sqlite3.connect(db)
lease = delivery.acquire_delivery_stream_lease(conn, 'job-1', lease_seconds=1)
conn.close()

# Прошло время > expires_at при живом процессе воркера
expired_now = (datetime.now(timezone.utc) + timedelta(seconds=10)).isoformat()
conn2 = sqlite3.connect(db)
active = delivery.get_active_stream_leases(conn2, now=expired_now)
conn2.commit()
conn2.close()

holder = delivery.acquire_migration_gate(db, timeout=0.2, clock=lambda: expired_now)
```
**Результат на старом коде:**
```text
Active count: 0
Migration acquired gate: True
```
Дефект подтверждён: истечение `expires_at` само по себе открывало миграции доступ к эксклюзивному гейту при живом воркере передачи.

---

## 3. Архитектурное решение и протокол безопасности

### 3.1. Защита живого воркера в `get_active_stream_leases()`
В `backend/delivery.py` переработан алгоритм классификации lease:
1. **Проверка процесса (`is_pid_owner_alive(pid, start_time)`):**
   - Если PID мёртв или start time не совпадает (PID reuse) → lease признаётся stale (аварийно завершившийся воркер) и подлежит очистке.
2. **Проверка состояния операции (`delivery_jobs`):**
   - Если задача уже завершена (`state in ('delivered', 'failed')`) или удалена → операция завершена, оставшийся lease безопасно очищается.
   - Если задача находится в состоянии `state == 'processing'` и `artifact_role == 'result'`:
     **КРИТИЧЕСКИЙ ИНВАРИАНТ:** Истечение `expires_at` **НЕ** даёт права удалять lease или захватывать гейт миграции. Lease безусловно признаётся **ACTIVE**. Миграция блокируется и завершается таймаутом (fail-closed).

### 3.2. Heartbeat-протокол продления (`renew_delivery_stream_lease`)
Реализована функция `renew_delivery_stream_lease(db_path_or_conn, job_id, lease_seconds=60, clock=None)`:
- Защищена декоратором `@retry_busy` для устойчивости к конкуренции SQLite.
- Проверяет отсутствие активного гейта миграции и неизменность generation.
- Проверяет совпадение PID и start time текущего процесса.
- Проверяет валидность роли `result` и состояния `processing` в `delivery_jobs`.
- Продлевает `expires_at = now + lease_seconds`.

### 3.3. Автоматическое продление в чанковом цикле (`backend/main.py`)
- В функции `check_delivery_stream_lease()` добавлен параметр `renew: bool = True`.
- На каждом отправляемом чанке (64KB) воркер проверяет авторизацию и мгновенно сдвигает `expires_at` в будущее с немедленным `conn.commit()`.
- **Исключение долгих блокировок:** Транзакция обновления выполняется за микросекунды, после чего SQLite освобождает блокировку на время сетевой передачи чанка (`httpx.put`).
- При сбое обновления или изменении роли/гейта чанковый генератор немедленно выбрасывает `RuntimeError` (fail-closed), прерывая передачу без утечки байтов.

### 3.4. Защита от сопутствующих сценариев
- **Медленная передача (> 60 сек):** Непрерывный heartbeat удерживает lease активным на всём протяжении передачи.
- **Зависший воркер:** Миграция блокируется до явного завершения процесса или вмешательства оператора/watchdog. Переклассификация артефакта за спиной воркера исключена.
- **Аварийное завершение (Crash Recovery):** При гибели воркера (`kill -9`, OOM, SIGKILL) `is_pid_owner_alive()` возвращает `False`, миграция безопасно очищает заброшенный lease и захватывает гейт без дедлока.
- **PID Reuse Fencing:** Сверка `holder_start_time` с системным временем старта процесса предотвращает захват прав новым процессом с переиспользованным PID.
- **Параллельная работа нескольких воркеров:** Каждая задача изолирована по `job_id`, завершение одной передачи не влияет на удержание гейта другой.
- **Восстановление после перезапуска:** После рестарта сервера все старые PID распознаются как мёртвые и очищаются при первом обращении.

---

## 4. Верификация после исправления

### 4.1. Зонд воспроизведения после фикса
```text
Active count after expiry: 1
Migration correctly blocked: RuntimeError Cannot acquire exclusive migration gate: timed out waiting for active lock
```
Дефект полностью устранён.

### 4.2. Набор тестов `backend/tests/test_r1_concurrency_artifact_alias_04.py`
Добавлены 8 новых целевых тестов (всего 31 тест):
1. `test_expired_lease_with_live_worker_blocks_migration` — **PASSED**
2. `test_worker_heartbeat_renewal_protocol` — **PASSED**
3. `test_slow_upload_continuous_stream_lease_renewal` — **PASSED**
4. `test_heartbeat_failure_halts_streaming_fail_closed` — **PASSED**
5. `test_dead_worker_reclaimed_by_migration` — **PASSED**
6. `test_terminal_job_orphaned_lease_cleaned_up` — **PASSED**
7. `test_sqlite_contention_during_stream_renewal_retries_successfully` — **PASSED**
8. `test_multiple_concurrent_workers_stream_leases_isolated` — **PASSED**

**Итог набора:** `31 passed in 6.06s` (100% PASS).

### 4.3. Регрессия смежных компонентов
- `test_payment_delivery_concurrency_e2e.py`: **1 passed in 2.18s**
- `test_consent_order_flow.py` + `test_legacy_artifact_role_migration.py`: **45 passed in 4.95s**
- Независимые аудиторские наборы `test_independent_roles.py` + `test_additional_roles.py`: **23 passed in 1.80s**

### 4.4. Полная регрессия backend
```text
pytest backend/tests/ -q
823 passed, 14 skipped in 266.16s (0:04:26)
```
0 failed, 0 errors.

---

## 5. Итоговый статус и инварианты

```text
PRODUCTION_UNCHANGED=true
DEPLOYMENT_BLOCKED=true
LEGAL_APPROVAL_STATUS=NOT_APPROVED
```
Никакие production-окружения, реальные фотографии, платежные шлюзы или внешние хранилища не затрагивались. Все тесты выполнены строго на изолированных синтетических данных.
