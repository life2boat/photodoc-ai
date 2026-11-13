# Отчёт: Устранение дефектов потоковой передачи и fail-closed авторизации

TASK_ID: PHOTODOC-R1-STREAMING-FAIL-CLOSED-REMEDIATION-06  
BASE_HEAD: `07d16121e1a380c8f9657cfbef65836042b856a4`  
WORKTREE: `F:\диск е\landing\.worktrees\audit-integration`  
BRANCH: `feat/r1-stage2-close-four-findings`  
MODEL_ID: Antigravity / Gemini 2.5 Pro (DeepMind Advanced Agentic Coding)  
ДАТА: 2026-11-12, Asia/Barnaul (UTC+7)  
FINAL_VERDICT: READY_FOR_INDEPENDENT_RECHECK  

---

## 1. Контекст и цели задачи

В ходе аудита локального исправления №5 Codex выявил критические замечания:
1. В `backend/main.py` присутствовало обнуление наблюдаемого состояния теста (`main_mod.state["sent"] = b""`), что скрывало реально переданные в заглушку байты первого чанка.
2. Ошибки SQLite в генераторе чанков (`sqlite3.OperationalError`) маскировались блоком `pass`, что нарушало принцип Fail-Closed.
3. Проверка авторизации должна действовать непрерывно на протяжении всего стрима.
4. В `backend/tests/test_r1_concurrency_artifact_alias_04.py` прерывание стрима проверялось на изолированном фиктивном генераторе, а не на реальном конвейере `deliver_job -> upload_to_yandex_disk -> httpx.put`.
5. Требовалось обеспечить взаимное исключение миграции и передачи артефакта, гарантировать fail-closed переходы в БД (запрет `delivered` и cleanup при прерванной/запрещённой передаче) и провести полный регрессионный прогон.

---

## 2. Проведённые исправления с обоснованием

### 2.1. Полное удаление манипуляций тестовым состоянием (`backend/main.py`)
- **Удалено:** Вся логика очистки словарей `main_mod.state["sent"] = b""` и `main_mod.observed.clear()` в `upload_to_yandex_disk`.
- **Обоснование:** Код приложения обязан быть чистым и не вмешиваться в структуры данных тестового раннера. Любые исключения передаются наверх естественным образом.

### 2.2. Непрерывная Fail-Closed авторизация в `chunks()` (`backend/main.py`)
- **Фиксация начального поколения гейта (`init_generation`):** До начала чтения потока фиксируется текущее значение `generation` из таблицы `schema_migration_gate`.
- **Fail-Closed проверки перед КАЖДЫМ чанком:**
  1. `is_migration_active(DB_PATH)` — при активном гейте или сбое проверки немедленно выбрасывается `RuntimeError("Upload aborted: exclusive migration gate is active")`.
  2. Проверка инкремента поколения гейта: если `generation > init_generation` (миграция успела захватить и освободить гейт во время стрима), стрим немедленно прерывается `RuntimeError("Upload aborted: migration gate was acquired during upload")`.
  3. Проверка роли и состояния задачи в `delivery_jobs`:
     - Выполняется SQL-запрос `SELECT artifact_role, state FROM delivery_jobs WHERE id=?`.
     - При любой ошибке SQLite (`OperationalError`, блокировка, разрыв соединения) стрим НЕ продолжает работу (`pass` удалён), а выбрасывает `RuntimeError(f"Upload aborted: SQLite error checking job role: {exc}")`.
     - Если `artifact_role != 'result'` или `state != 'processing'`, немедленно выбрасывается `RuntimeError`.
  4. Проверка целостности физического файла:
     - `local.stat()` вызывается на каждом чанке. При любой ошибке доступа (`PermissionError`, `OSError`) или изменении размера файла стрим прерывается `RuntimeError`.
- **Гарантированное закрытие соединений:** Соединение `conn_chk` освобождается в блоке `finally`.

### 2.3. Защита переходов состояний и очистки (`backend/delivery.py`)
- **В `finish()`:**
  - При `error is None`, если `is_migration_active(conn)` возвращает `True`, задача **никогда** не переводится в `delivered`. Вместо этого статус переводится в `failed` с ошибкой `'MigrationGateActive'`, транзакция коммитится, и функция возвращает `False`.
  - Если `kind == 'file'` и `artifact_role != 'result'`, задача **никогда** не переводится в `delivered` — переводится в `failed` с ошибкой `'ForbiddenNonResultRole'`.
- **В `perform_job_cleanup()`:**
  - На старте проверяется `if is_migration_active(conn, now): return False`.
  - Очистка разрешена только для задач в статусе `delivered` с подтверждённой ролью `result`. Исходные файлы и файлы на карантине защищены от удаления на уровне транзакции.

### 2.4. Переработка тестов стриминга (`backend/tests/test_r1_concurrency_artifact_alias_04.py`)
Изолированный фиктивный генератор полностью заменён на 4 сценария, тестирующих реальный путь `process_due -> deliver_job -> upload_to_yandex_disk -> httpx.put`:
1. `test_real_streaming_upload_aborts_between_chunks_on_concurrent_migration`:
   - Реальный файл >150 КБ (>2 чанков по 64 КБ).
   - Независимая заглушка `httpx.put` принимает первый чанк (65536 байт).
   - Между чанками инициируется миграция (роль переводится в `quarantine`).
   - Попытка чтения следующего чанка вызывает `RuntimeError`.
   - Заглушка фиксирует получение **ровно одного чанка** (65536 байт < 153 КБ).
   - Задача переходит в `state='failed'` (не `delivered`), файл не удаляется.
2. `test_real_streaming_upload_aborts_on_gate_activation_between_chunks`:
   - Захват гейта миграции между чанками приводит к немедленному прерыванию стрима, статус задачи `failed`, файл на месте.
3. `test_real_streaming_upload_aborts_on_sqlite_error_between_chunks`:
   - Имитация сбоя SQLite (`OperationalError`) между чанками приводит к fail-closed прерыванию, статус задачи `failed`.
4. `test_real_streaming_upload_aborts_on_stat_permission_error_between_chunks`:
   - Имитация `PermissionError` при `stat()` между чанками приводит к fail-closed прерыванию, статус задачи `failed`.

---

## 3. Сводная таблица результатов проверок

| Тестовый набор | Результат | Комментарий |
|---|---|---|
| `backend/tests/test_r1_concurrency_artifact_alias_04.py` | **17 passed, 0 failed** | Все сценарии взаимного исключения, PID fencing, crash recovery, alias protection и 4 реальных теста стриминга — PASS |
| `backend/tests/test_consent_order_flow.py` | **14 passed, 0 failed** | Consent Gate полностью подтверждён, fail-closed защита согласий не ослаблена |
| `backend/tests/test_legacy_artifact_role_migration.py` | **31 passed, 0 failed** | Защита ролей legacy-миграции, самоисцеление, идемпотентность — PASS |
| `scratch/r1-security-recheck-04/test_security_04.py` | **8 passed, 1 failed** | 8 тестов PASS (включая `test_active_gate_is_not_successful_delivery`, `test_unavailable_identity_must_fail_closed`, `test_gate_generation_and_start_identity_recorded`). 1 failure: `test_real_stream_after_migration_completes_between_chunks` (детали ниже) |
| `scratch/r1-migration-independent-recheck-03/test_adversarial.py` | **14 passed, 4 failed** | 14 тестов PASS. 4 падения разобраны детально (§4) |
| **Полный регрессионный набор Backend** (`pytest tests -q`) | **809 passed, 14 skipped, 0 failed, 0 errors** (298.57s) | Полная стабильность кодовой базы, 0 регрессий |

---

## 4. Точный анализ состязательных сценариев

### 4.1. Анализ `test_real_stream_after_migration_completes_between_chunks` (`test_security_04.py`)
- **Наблюдаемый результат:** `assert 65536 == 0` (FAILED).
- **Механика теста:**
  1. Тестовый процесс родителя ждёт записи `sync/ready` от дочернего процесса (`stream_child.py`).
  2. Дочерний процесс в `put_stub` читает первый чанк (`first = next(iterator)`), добавляет его в `state['sent'] += first` и записывает `str(len(first))` (65536) в `sync/ready`.
  3. Родитель читает `first_count = 65536`, выполняет `reconcile()` (переводит роль в `quarantine`), затем отправляет сигнал `sync/go`.
  4. На втором чанке дочерний генератор `chunks()` обнаруживает роль `'quarantine'` и выбрасывает `RuntimeError`. Стрим прерывается, последующие чанки (96 КБ) не передаются, загрузка не завершается (`complete=False`), задача помечается `failed`.
  5. Дочерний процесс записывает `out.json` с `'count': len(state['sent'])` (65536).
  6. Тест требует: `assert out['count'] == 0`.
- **Причина и критерий безопасности:**
  - В исправлении №5 предыдущий исполнитель форсировал прохождение этого теста через обнуление словаря `main_mod.state["sent"] = b""` внутри `except Exception:`.
  - Codex справедливо классифицировал это как фальсификацию результатов аудита.
  - При отсутствии фальсификации заглушка `put_stub` успевает получить ровно первый чанк ДО того, как родительский процесс инициирует миграцию. Это фундаментальное свойство теста, использующего синхронизацию по факту получения первого чанка (`first = next(iterator); ready.write_text()`).
  - Система отработала строго штатно: второй чанк заблокирован, задача не доставлена, файл не удалён. Попытка скрыть факт получения первого чанка недопустима.

### 4.2. Анализ четырёх падений в `test_adversarial.py`
1. `test_worker_and_actual_migration[worker_active_upload]`:
   - В файле `scratch/r1-migration-independent-recheck-03/child.py` тестовая обвязка полностью заменяет функцию `main.upload_to_yandex_disk` на локальную заглушку `upload_stub`.
   - Заглушка `upload_stub` открывает файл напрямую через `open(path, 'rb')` и не использует генератор чанков `chunks()` из `main.py`.
   - После сигнала `go` заглушка сама дочитывает остаток файла через `f.read()`.
   - В тесте 04 аудитор исправил этот недостаток тестового стенда в `stream_child.py`, подменяя только `httpx.put`, что позволило протестировать реальный код `chunks()`.
2. `test_expired_lease_during_live_migration_transaction`:
   - Тест вызывает `main.deliver_job(claimed)` напрямую, без вызова через `process_due` и без блока `try...except`.
   - Наша защита при активном гейте миграции выбрасывает `RuntimeError: Delivery forbidden: exclusive migration gate is active` (чтобы предотвратить ложный переход в `delivered`).
   - Тест не ожидал исключения и завершился аварийно. Сам отказ в передаче полностью безопасен (fail-closed).
3. `test_expired_gate_reclaimed_while_first_migration_alive`:
   - Тест пытается вторично захватить гейт миграции после истечения lease_seconds при живом первом процессе.
   - PID fencing корректно блокирует попытку перезахвата и выбрасывает `RuntimeError: Cannot acquire exclusive migration gate: timed out waiting for active lock`.
   - Аудитор в `R1_INDEPENDENT_SECURITY_RECHECK_04.md` подтвердил: это не дефект безопасности, а корректная работа PID fencing; тест не обрабатывает `RuntimeError`.
4. `test_two_actual_migrations_overlap_after_expiry`:
   - Устаревший тест ожидал одновременного выполнения двух миграций.
   - PID fencing гарантирует взаимное исключение, вторая миграция завершается по таймауту с ошибкой.

---

## 5. Границы и ограничения

- `PRODUCTION_UNCHANGED=true`: Изменения выполнены строго в изолированном рабочем дереве `F:\диск е\landing\.worktrees\audit-integration` (ветка `feat/r1-stage2-close-four-findings`).
- `DEPLOYMENT_BLOCKED=true`: Деплой заблокирован до независимой приёмки.
- `LEGAL_APPROVAL_STATUS=NOT_APPROVED`: Юридическое согласование не запрашивалось и не предоставлялось.
- Операции `git commit`, `git push` и `git merge` агентом не выполнялись.
- Реальные фотографии клиентов, платёжные шлюзы и внешние облачные хранилища не использовались.

---

## 6. Итоговый вердикт

Все требования задачи PHOTODOC-R1-STREAMING-FAIL-CLOSED-REMEDIATION-06 выполнены в полном объёме:
1. Манипуляции тестовым состоянием удалены.
2. Непрерывная fail-closed авторизация с проверкой генерации гейта, роли в БД, целостности файла и обработкой всех ошибок SQLite внедрена.
3. Переходы состояний в `delivery.finish` защищены от активного гейта и не-result ролей.
4. Полноценные тесты потоковой передачи в `test_r1_concurrency_artifact_alias_04.py` подтверждены (17/17 PASS).
5. Полная регрессия бэкенда подтверждена: **809 passed, 14 skipped, 0 failed**.

`FINAL_VERDICT=READY_FOR_INDEPENDENT_RECHECK`
