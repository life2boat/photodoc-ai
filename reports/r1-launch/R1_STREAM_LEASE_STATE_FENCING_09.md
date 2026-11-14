# Отчёт по исправлению PHOTODOC-R1-STREAM-LEASE-STATE-FENCING-09

**TASK_ID:** PHOTODOC-R1-STREAM-LEASE-STATE-FENCING-09  
**Дата:** 2026-11-13  
**Проект:** PhotoDoc AI  
**Репозиторий:** `life2boat/photodoc-ai`  
**Ветка:** `feat/r1-stage2-close-four-findings`  
**Worktree:** `F:\диск е\landing\.worktrees\audit-integration`  
**Базовый HEAD:** `07d16121e1a380c8f9657cfbef65836042b856a4`  

---

## 1. Executive Summary

В ходе независимого аудита `R1_LEASE_INDEPENDENT_RECHECK_08` был обнаружен остаточный MEDIUM-дефект (PROBE2):
сторонний поток, таймер или процесс мог вызвать `finish(job_id)` (ветка `error` -> `failed` или `success` -> `delivered`) во время активной потоковой передачи. В результате:
1. `delivery_jobs.state` переходил в терминальное состояние (`failed` или `delivered`).
2. Функция `get_active_stream_leases()` трактовала lease задачи в терминальном состоянии как «орфанный» и вычищала его из базы, несмотря на то, что процесс-воркер продолжал передачу байтов.
3. Миграция схемы (`reconcile_delivery_schema`) получала эксклюзивный гейт `schema_migration_gate` и переклассифицировала файл в `quarantine` прямо во время полёта чанков.

В рамках задачи **PHOTODOC-R1-STREAM-LEASE-STATE-FENCING-09** данный дефект полностью устранён на архитектурном уровне через введение неизменяемого сессионного токена `stream_operation_id` и двухуровневого ограждения состояния (`State Fencing`).

---

## 2. Воспроизведение дефекта PROBE2: До и После исправления

### До исправления (Pre-Fix Baseline):
```text
LEASE_ACQUIRED: True
WORKER_PID_ALIVE: True
JOB_STATE_AFTER_FINISH: failed
PROBE2_STATE_FAILED_ACTIVE: 0              <-- Lease удалён из-за терминального состояния задачи!
PROBE2_STATE_CHANGE_ALLOWED_MIGRATION: True <-- Миграция получила эксклюзивный гейт во время передачи!
```

### После исправления (Post-Fix Verification):
```text
LEASE_ACQUIRED with op_id: sop_cefa0ba2f0844799af16465037a740f2
FINISH RESULT WITHOUT OP_ID: False         <-- Сторонний finish() отклонён, состояние не изменено!
JOB STATE AFTER ROGUE FINISH: processing  <-- Задача защищена от мутации состояния!
PROBE2_STATE_ACTIVE_COUNT: 1               <-- Lease остаётся активным!
PROBE2_STATE_CHANGE_ALLOWED_MIGRATION: False <-- Миграция строго заблокирована!
PROBE2_RAW_SQL_FAILED_ACTIVE: 1            <-- Даже при прямой модификации SQL lease не удаляется, если PID жив!
RAW_MIGRATION_ALLOWED: False               <-- Миграция блокируется по таймауту!
FINISH RESULT WITH VALID OP_ID: True       <-- Авторизованный воркер финализирует задачу!
JOB STATE FINAL: delivered
ACTIVE LEASES AFTER VALID FINISH: 0
MIGRATION GATE AFTER CLEAN FINISH ACQUIRED: True
```

---

## 3. Архитектура решения и ключевые инварианты

### 3.1. Неизменяемый токен сессии `stream_operation_id`
В схему таблицы `delivery_stream_leases` добавлена колонка `stream_operation_id TEXT`:
- Каждая операция потоковой передачи генерирует уникальный криптостойкий идентификатор `sop_<uuid4>`.
- Токен передаётся через весь конвейер: `acquire_delivery_stream_lease` -> `upload_to_yandex_disk` -> `check_delivery_stream_lease` -> `renew_delivery_stream_lease` -> `release_delivery_stream_lease` -> `finish()`.
- Изолирует конкурирующие потоки/операции внутри одного PID (например, при многопоточном воркере).

### 3.2. Двухуровневое ограждение (`Double-Layered Defense`)

1. **Уровень 1 — Fencing в `finish()`, `quarantine_expired()` и `claim_due()`:**
   - `finish()`: перед любой мутацией состояния проверяет наличие записи в `delivery_stream_leases`. Если активный lease удерживается живым процессом, `finish()` разрешает перевод состояния (`delivered`/`failed`) ТОЛЬКО при предоставлении совпадающего `stream_operation_id`. Любой сторонний или неавторизованный вызов `finish()` немедленно выполняет `rollback()` и возвращает `False`.
   - `quarantine_expired()`: исключает из перевода в `failed` задачи, для которых активен stream lease (`AND id NOT IN (SELECT job_id FROM delivery_stream_leases)`).
   - `claim_due()`: исключает повторный захват задач с активным stream lease другими воркерами.

2. **Уровень 2 — Иммунитет к состоянию в `get_active_stream_leases()`:**
   - Даже если в базе данных произошла прямая мутация `delivery_jobs.state` (например, внешний SQL-скрипт), `get_active_stream_leases()` **НЕ УДАЛЯЕТ** lease, пока процесс-владелец подтверждён живым (`is_pid_owner_alive(pid, start_time) == True`).
   - Пока владелец жив, lease безусловно считается активным, гарантируя блокировку эксклюзивного гейта миграции.

### 3.3. Сохранение Crash Recovery
- Если процесс-воркер аварийно завершился или был убит (`is_pid_owner_alive` возвращает `False`), и `get_active_stream_leases()`, и `finish()`, и `acquire_migration_gate()` автоматически очищают устаревший lease мертвого процесса без взаимных блокировок (deadlock).

---

## 4. Результаты тестирования и верификации

### 4.1. Целевой набор тестов конкурентности
Файл: `backend/tests/test_r1_concurrency_artifact_alias_04.py`
- Количество тестов: **37 passed, 0 failed, 0 skipped**.
- Включает все новые тесты Stage 09:
  1. `test_live_worker_stream_lease_persists_regardless_of_terminal_state` — удержание lease при терминальном состоянии.
  2. `test_probe2_state_fencing_prevents_unauthorized_finish_and_blocks_migration` — воспроизведение и ликвидация PROBE2.
  3. `test_stream_operation_id_fencing_between_threads_same_pid` — изоляция потоков внутри одного PID по `stream_operation_id`.
  4. `test_quarantine_expired_fencing_active_stream_lease` — защита активных задач от карантинного таймера.
  5. `test_claim_due_fencing_active_stream_lease` — защита активных задач от повторного клейма.
  6. `test_crashed_worker_dead_lease_recovery` — автоматическая очистка stale lease при падении процесса.
  7. `test_cross_process_stream_lease_fencing` — межпроцессное ограждение с дочерним процессом.

### 4.2. Связанные наборы тестов доставки и платежей
- `test_delivery_concurrency.py`, `test_payment_delivery_concurrency_e2e.py`, `test_consent_order_flow.py`, `test_legacy_artifact_role_migration.py`, `test_photo_cleanup_privacy.py`, `test_payment_delivery_reliability.py`, `test_real_order_source_lifecycle.py`, `test_stage2_customer_journey.py`, `test_staging_acceptance_harness.py`:
- Результат: **142 passed, 1 skipped, 0 failed**.

### 4.3. Полная backend-регрессия
- Команда: `python -m pytest backend/tests/ -q`
- Результат: **829 passed, 14 skipped, 0 failed** (100% PASS).

---

## 5. Ограничения и статус безопасности

- `PRODUCTION_UNCHANGED=true`
- `DEPLOYMENT_BLOCKED=true`
- `LEGAL_APPROVAL_STATUS=NOT_APPROVED`
- Никаких секретных ключей в коде не сохранялось.
- Использовались только синтетические тестовые файлы, изолированные базы данных SQLite и mock-заглушки.
- Операции `git commit`, `git push`, `git merge` не выполнялись (согласно регламенту).
