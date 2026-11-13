# PHOTODOC AI — R1 LEGACY MIGRATION HIGH REMEDIATION REPORT 03 (STAGE 2)
**Task ID:** `PHOTODOC-R1-LEGACY-MIGRATION-HIGH-REMEDIATION-03`  
**Target Branch:** Draft PR #38 (`feat/r1-stage2-close-four-findings`)  
**Verified Base HEAD:** `4617f1ed50289351785cb3bbd968ceac7ab06cf6`  
**Code Fix HEAD SHA:** `447c4c22cf9777b1b90a61a3ef21e8908122305e`  
**Audited Sources of Truth:**
- `reports/r1-launch/R1_CONTROLLED_INTEGRATION_02_REPORT.md`
- `reports/r1-launch/R1_LEGACY_ARTIFACT_INDEPENDENT_RECHECK_02.md`
- `scratch/r1-legacy-artifact-recheck-02/test_additional_roles.py`
- `scratch/r1-legacy-artifact-recheck-01/test_independent_roles.py`

**Execution Mode:** AUTONOMOUS / MAXIMUM REASONING / MINIMAL CONFIRMATIONS  
**Language:** RUSSIAN  
**Status:** BOTH HIGH VULNERABILITIES ELIMINATED / 23 INDEPENDENT TESTS PASS / SECURITY ACCEPTANCE TESTS A-K PASS  

---

## 1. КРАТКОЕ РЕЗЮМЕ (EXECUTIVE SUMMARY)

В ходе второго этапа независимого контролируемого интеграционного аудита Codex (`R1_CONTROLLED_INTEGRATION_02_REPORT.md` и `R1_LEGACY_ARTIFACT_INDEPENDENT_RECHECK_02.md`) на коммите `4617f1ed` были выявлены две критические уязвимости уровня **HIGH**, блокирующие интеграцию PR #34–38:

1. **HIGH-01 (Codex HIGH-C1) — Protected Role Escalation (Эскалация защищённых ролей):**  
   При наличии противоречивых исторических записей в `order_results`, ссылающихся на тот же синтетический оригинал, совпадающем SHA-256 и удалённом пути `_result.bin`, миграция `reconcile_delivery_schema.py` переклассифицировала явные защищённые роли `source` и `quarantine` в `result`. В результате воркер доставлял исходные байты клиента в заглушку uploader, а последующий cleanup удалял локальный файл с диска.
2. **HIGH-02 (Codex HIGH-C2) — Migration/Worker Concurrency Race (Гонка конкурентной миграции и воркера):**  
   В случае выполнения миграции при работающем delivery worker (нарушение регламента runbook) воркер успевал захватить ошибочную запись `result` в память до миграции. После того как миграция переводила запись в `quarantine`, воркер продолжал выполнение со старой копией объекта `job`, передавая оригинал в заглушку загрузки. Кроме того, отсутствовал программный барьер межпроцессного взаимоисключения между миграцией схемы и воркерами доставки/очистки.

Обе уязвимости были полностью воспроизведены на базовом HEAD `4617f1ed` с помощью набора `test_additional_roles.py` (3 падения, 4 прохода: падения в `test_a[source]`, `test_a[quarantine]`, `test_b`).

В коммите `447c4c2` обе уязвимости **полностью устранены**:
- `test_additional_roles.py`: **7 passed, 0 failed** (100% pass).
- `test_independent_roles.py`: **16 passed, 0 failed** (100% pass).
- `test_legacy_artifact_role_migration.py`: расширен до **31 теста** (добавлены dedicated Security Acceptance Tests `test_security_acceptance_a` через `k`), все **31 passed, 0 failed**.
- Комплекс 14 основных модулей регрессии бэкенда: **171 passed, 1 skipped (20.11s)**.
- Все пайплайны GitHub Actions CI успешно пройдены на Linux/Docker.

---

## 2. АНАЛИЗ ПРИЧИН И РЕАЛИЗОВАННЫЕ ИСПРАВЛЕНИЯ

### 2.1. Устранение HIGH-01: Неизменяемость защищённых ролей (Protected Role Immutability)

- **Первопричина:**  
  В `backend/migrations/reconcile_delivery_schema.py` логика классификации проверяла совпадение `ores` (из `order_results`) и наличие суффикса `_result.bin`, перезаписывая существующую роль `source` или `quarantine` на `result`, если отсутствовал маркер `_source`.
- **Архитектурное исправление:**
  1. Внедрён фундаментальный инвариант: **явная роль `quarantine` или `source` НИКОГДА не может быть повышена до `result`**.
  2. Для записей со статусом `artifact_role == 'quarantine'`: роль остаётся строго `quarantine`.
  3. Для записей со статусом `artifact_role == 'source'`: при обнаружении конфликтующих признаков (`remote_path LIKE '%_result.bin'`, совпадение хэша с `order_results` или наличие маркера `_result`) запись переводится в безопасный режим `quarantine` (fail-closed). Если конфликтов нет, сохраняется статус `source`. Повышение до `result` исключено.
  4. Запрос `unclassified_role_count` обновлён для выявления несовместимых состояний (`artifact_role = 'source' AND remote_path LIKE '%_result.bin'`), что делает миграцию самоисцеляющей и идемпотентной.
  5. Для неклассифицированных строк роль `result` назначается **исключительно** при наличии авторитетного доказательства: полное одновременное совпадение пути и хэша с `order_results`, наличие суффикса `_result.bin` и отсутствие маркеров исходника.

### 2.2. Устранение HIGH-02: Межпроцессный гейт миграции и авторитетная ревалидация

- **Первопричина:**  
  1) Воркер `main.deliver_job()` доверял словарю `job`, полученному в момент захвата (`claim_due()`), и не проверял актуальное состояние строки в SQLite перед передачей файла в uploader.
  2) Отсутствовал программный механизм межпроцессного взаимоисключения: запуск `reconcile()` не останавливал активные захваты и очистки других процессов.
- **Архитектурное исправление:**
  1. **Межпроцессный гейт миграции (`schema_migration_gate`):**
     - В `backend/delivery.py` создана таблица `schema_migration_gate (gate_name, holder_id, holder_pid, acquired_at, expires_at)`.
     - Реализованы функции `acquire_migration_gate(db_path, timeout=5.0, lease_seconds=60)` и `release_migration_gate(db_path, holder_id)`.
     - Гейт работает по принципу fail-closed: если эксклюзивный доступ не получен за `timeout`, вызывается `RuntimeError`.
     - Предусмотрено безопасное самоисцеление при сбоях (автоматический перезахват протухших блокировок `expires_at <= now` без ручного вмешательства).
     - Метод `reconcile(apply=True)` захватывает гейт в начале транзакции и гарантированно освобождает его в блоке `finally:`.
  2. **Интеграция гейта в жизненный цикл воркера:**
     - `delivery.claim_due()`: под транзакцией `BEGIN IMMEDIATE` проверяет `is_migration_active(conn)`. Если гейт активен, немедленно возвращает `None` (fail-closed).
     - `delivery.perform_job_cleanup()`: проверяет `is_migration_active(conn)` как в начале, так и в критической секции удаления. При активном гейте удаление блокируется.
     - `delivery.process_due_cleanups()` и `delivery.process_due()`: мгновенно прерывают цикл обработки при активном гейте миграции.
  3. **Авторитетная ревалидация перед выгрузкой (`main.deliver_job`):**
     - Непосредственно перед передачей файла в сетевой uploader `main.deliver_job()` выполняет прямой авторитарный запрос в SQLite под тайм-аутом.
     - Проверяются 3 строгих условия:
       1) `is_migration_active(conn)` — запрет выгрузки во время миграции.
       2) `artifact_role == "result"` — если миграция перевела задачу в `quarantine`, воркер немедленно выбрасывает `RuntimeError` до открытия файла.
       3) `state == "processing"` — проверка, что захват задачи не был сброшен или отозван.
  4. **Уточнение обработки неизвестных ролей в `operator_workflow.py`:**
     - В `operator_workflow._source_job` исключена отдача артефактов с ролью `unknown` (неклассифицированные файлы больше не выдаются оператору под видом клиентских исходников).
     - Исправлена проверка суффикса `_result`: теперь проверяется `Path(local).name` вместо полного абсолютного пути `local`, исключая ложные срабатывания при наличии `_result` в имени родительской директории.

---

## 3. ЗАТРОНУТЫЕ ФАЙЛЫ

1. `backend/delivery.py`:
   - Схема и логика гейта `schema_migration_gate`: `acquire_migration_gate()`, `release_migration_gate()`, `is_migration_active()`.
   - Защита `claim_due()`, `perform_job_cleanup()`, `process_due_cleanups()`, `process_due()` от работы во время активного гейта миграции.
2. `backend/main.py`:
   - Авторитетная SQLite-ревалидация в `deliver_job()` непосредственно перед вызовом сетевого uploader: проверка гейта, роли `result` и состояния `processing`.
3. `backend/migrations/reconcile_delivery_schema.py`:
   - Захват и освобождение эксклюзивного гейта миграции (`acquire_migration_gate` / `release_migration_gate`).
   - Запрет повышения явных ролей `source` и `quarantine` до `result`.
   - Безопасный перевод конфликтующих записей в `quarantine`.
   - Идемпотентность и самоисцеление при повторных вызовах.
4. `backend/operator_workflow.py`:
   - Запрет выдачи артефактов со статусом `unknown` через `source_for_download()`.
   - Исправление проверки имени файла через `Path(local).name`.
5. `backend/tests/test_legacy_artifact_role_migration.py`:
   - Добавление 11 специализированных тестов Security Acceptance (A–K) на постоянной основе (всего 31 тест в модуле).

---

## 4. РЕЗУЛЬТАТЫ ВЕРИФИКАЦИИ

### 4.1. Независимые проверочные наборы
- **`scratch/r1-legacy-artifact-recheck-02/test_additional_roles.py`:**
  - `test_a[source]`: **PASSED** (явный `source` не повышается до `result`, оригинал не отправлен, файл на диске).
  - `test_a[quarantine]`: **PASSED** (явный `quarantine` не повышается до `result`, оригинал не отправлен, файл на диске).
  - `test_b`: **PASSED** (устаревший захват воркера перепроверяется в БД, отправка оригинала блокирована).
  - `test_c`: **PASSED** (cleanup проверяет смену роли перед `unlink`).
  - `test_d`: **PASSED** (неклассифицированный результат не отдается оператору как оригинал).
  - `test_e`: **PASSED** (несоответствующий результат остаётся в карантине).
  - `test_f`: **PASSED** (невалидная роль не доставляется и не удаляется).
  - **Итог: 7 passed, 0 failed.**

- **`scratch/r1-legacy-artifact-recheck-01/test_independent_roles.py`:**
  - **Итог: 16 passed, 0 failed.**

### 4.2. Постоянный миграционный набор `test_legacy_artifact_role_migration.py`
- Содержит 31 тест, включая:
  - 19 базовых состязательных тестов на роли, транзакции и очистку.
  - `test_security_acceptance_a` (source / quarantine): защита от эскалации при наличии `order_results`.
  - `test_security_acceptance_b`: ревалидация устаревшего in-memory claim перед upload.
  - `test_security_acceptance_c`: блокировка `claim_due` во время активного гейта миграции.
  - `test_security_acceptance_d`: блокировка `perform_job_cleanup` во время активного гейта.
  - `test_security_acceptance_e`: межпроцессное взаимоисключение (тестирование через отдельный процесс `subprocess.Popen`).
  - `test_security_acceptance_f`: автоматическое восстановление протухшего гейта без дедлока.
  - `test_security_acceptance_g`: pre-unlink hook и блокировка удаления при смене роли.
  - `test_security_acceptance_h`: корректный выход `process_due` при активном гейте.
  - `test_security_acceptance_i`: защита операторского скачивания от роли `unknown`.
  - `test_security_acceptance_j`: изоляция несовпадающего пути результата в карантине.
  - `test_security_acceptance_k`: блокировка доставки и удаления невалидных ролей.
- **Итог: 31 passed, 0 failed.**

### 4.3. Регрессия бэкенда (14 ключевых модулей)
- `test_legacy_artifact_role_migration.py`
- `test_migration_cleanup_backfill.py`
- `test_migration_reconciliation.py`
- `test_order_delivery.py`
- `test_real_order_source_lifecycle.py`
- `test_operator_workflow.py`
- `test_stage2_customer_journey.py`
- `test_consents.py`
- `test_consents_integrity.py`
- `test_consent_order_flow.py`
- `test_retention.py`
- `test_retention_adversarial.py`
- `test_retention_recovery.py`
- `test_rotation_safety_enforcement.py`
- **Итог: 171 passed, 1 skipped (Linux symlink skip на платформе Windows) за 20.11s.**

---

## 5. ИТОГОВЫЙ СТАТУСНЫЙ БЛОК

```
STAGE=STAGE2_ARTIFACT_REMEDIATION_03_READY
TASK_ID=PHOTODOC-R1-LEGACY-MIGRATION-HIGH-REMEDIATION-03
PR38_HEAD=16194541cb83eb5a539b6623e104f69ee73b404d
CODE_FIX_HEAD=447c4c22cf9777b1b90a61a3ef21e8908122305e
HIGH_FINDINGS_CLOSED=2/2
INDEPENDENT_TESTS_PASSING=23/23
SECURITY_ACCEPTANCE_TESTS=11/11
LINUX_CI_STATUS=PASS
PRODUCTION_UNCHANGED=true
DEPLOYMENT_BLOCKED=true
LEGAL_APPROVAL_STATUS=NOT_APPROVED
FINAL_VERDICT=READY_FOR_INDEPENDENT_RECHECK
```
