# PHOTODOC AI — R1 LEGACY ARTIFACT ROLE REMEDIATION REPORT (STAGE 2)
**Task ID:** `PHOTODOC-R1-LEGACY-ARTIFACT-HIGH-REMEDIATION-02`  
**Target Branch:** Draft PR #38 (`feat/r1-stage2-close-four-findings`)  
**Audited Head SHA:** `906257e454d81dd6d53a35f2d6a0f97813e41261`  
**Code Fix Head SHA:** `3022b75887aba97d4716d83b93eacf8e3e82ecd8` (Combined Tree: `32d25618b1878a0c0207ffdce36855faa631d9d2`)  
**Current PR #38 HEAD SHA:** `d5e1ff6abb5490d4a54732acbd7cb5b81281fd10`  
**Current Combined Tree SHA:** `2108fd245bbd84284c727f50169bb1bede0a6344`  
**Base Audit Source of Truth:** `reports/r1-launch/R1_LEGACY_ARTIFACT_INDEPENDENT_RECHECK.md`  
**Execution Mode:** AUTONOMOUS / MAXIMUM REASONING  
**Status:** ALL 4 HIGH AND 2 MEDIUM FINDINGS RESOLVED / 100% CI PASS  

---

## 1. ВВЕДЕНИЕ И ЦЕЛИ (EXECUTIVE SUMMARY)

В независимом аудите Codex (`reports/r1-launch/R1_LEGACY_ARTIFACT_INDEPENDENT_RECHECK.md`) для Draft PR #38 был выявлен ряд критических уязвимостей (4 HIGH и 2 MEDIUM), связанных с классификацией артефактов в устаревших схемах БД и возможным удалением или утечкой оригинальных фотографий клиентов.

Все 6 замечаний были воспроизведены на базовом коммите `906257e` с помощью независимого набора тестов (`scratch/r1-legacy-artifact-recheck-01/test_independent_roles.py`: 9 failed, 7 passed) и полностью устранены в коммите `3022b75` (16 passed, 0 failed).

Все 9 независимых сценариев перенесены в постоянный регрессионный набор `backend/tests/test_legacy_artifact_role_migration.py` (теперь содержит 19 тестов, 100% pass).

Все 4 пайплайна GitHub Actions CI на Linux/Docker завершились успешно.

---

## 2. АНАЛИЗ ПРИЧИН И РЕАЛИЗОВАННЫЕ ИСПРАВЛЕНИЯ

### HIGH-01: Отсутствие колонки `artifact_role` разрешало доставку исходных фото по манипулятивному `remote_path`
- **Первопричина:** Если колонка `artifact_role` отсутствовала в таблице `delivery_jobs`, `delivery.claim_due()` использовал эвристику `remote_path LIKE '%_result.bin' AND remote_path NOT LIKE '%_source%'`. Если злоумышленник или сбой в данных устанавливали имя назначения, оканчивающееся на `_result.bin`, воркер захватывал оригинальное фото клиента как готовый результат.
- **Исправление:** 
  - В `delivery.claim_due()`: при отсутствии колонки `artifact_role` воркер переходит в fail-closed режим для файлов: `role_predicate = "kind != 'file'"`. Ни один файл не может быть захвачен воркером до проведения миграции схемы.
  - В `main.deliver_job()`: добавлен защитный барьер defense-in-depth: при `kind == 'file'` проверяется `job.get("artifact_role") == "result"`, в противном случае вызывается `RuntimeError`.
  - В `delivery.process_due_cleanups()`: при отсутствии колонки `artifact_role` очистка файлов блокируется (`role_predicate = "AND kind != 'file'"`).

### HIGH-02: `state='delivered'` тихо конвертировал исторические оригиналы без `order_results` в `result`, вызывая их удаление
- **Первопричина:** Миграция `reconcile_delivery_schema.py` предполагала, что исторические заказы в состоянии `delivered` без записей в `order_results` являются результатами. В результате оригинальные фото маркировались как `result`, а последующий бэкфилл очистки (`reconcile`) ставил их в очередь на физическое удаление (`cleanup_state='pending'`). Повторный запуск миграции не исцелял запись.
- **Исправление:**
  - В `backend/migrations/reconcile_delivery_schema.py`: исторические доставленные задания без подтверждения в `order_results` классифицируются строго как `quarantine`.
  - Правило обновления ролей: `cur_role != new_role` гарантирует безусловное самоисцеление при повторных прогонах.
  - В бэкфилле очистки `reconcile()`: физически существующие файлы с ролью, отличной от `result` и `info`, переводятся в безопасное состояние `cleanup_state='not_applicable', cleanup_phase='none'`, исключающее их удаление.

### HIGH-03: Конфликтующие путь и хэш принимались как доказательство `result`
- **Первопричина:** Миграция проверяла условие `matches_path or matches_sha`. Если путь совпадал с `order_results`, а SHA-256 конфликтовал (или наоборот), артефакт ошибочно классифицировался как `result`.
- **Исправление:**
  - В `backend/migrations/reconcile_delivery_schema.py`: авторитетное подтверждение роли `result` требует строгого одновременного совпадения как пути, так и хэша: `path_match and sha_match and not has_source_marker`.
  - При любом расхождении, конфликте или наличии маркера `_source` артефакт маркируется как `quarantine`.

### HIGH-04: Прямой вызов `perform_job_cleanup` удалял исходные фото в фазе `deleting` и с ролью `NULL`
- **Первопричина:** Проверка в `perform_job_cleanup()` содержала исключение `and job["cleanup_phase"] != "deleting"`, что позволяло вызывать `unlink()` для исходников, если фаза уже была установлена в `deleting`. Кроме того, при повторном селекте внутри транзакции `BEGIN IMMEDIATE` отсутствовала проверка роли `artifact_role == 'result'`.
- **Исправление:**
  - В `backend/delivery.py`: если `kind == "file"` и роль не является `result`, проверяется физическое наличие файла на диске. Если файл присутствует, функция немедленно возвращает `False` и фиксирует транзакцию, гарантируя, что исходник НИКОГДА не будет удален.
  - В критической секции под `BEGIN IMMEDIATE`: повторно запрашивается `artifact_role`, и если `kind == "file"` и `artifact_role != "result"`, транзакция откатывается (`conn.rollback()`), а удаление прерывается.

### MEDIUM-01: Карантинные артефакты отдавались оператору как исходное фото для скачивания
- **Первопричина:** `operator_workflow._source_job` при отсутствии роли `source` возвращал любой файл заказа, включая находящиеся в карантине (`artifact_role='quarantine'`).
- **Исправление:**
  - В `backend/operator_workflow.py`: `_source_job` отдает предпочтение явной роли `source`. Для легаси-записей (`NULL`, пустая строка, `unknown`) проверяется отсутствие признаков результата и строгий запрет на выбор записей со статусом `quarantine` или `result`.
  - `_quarantine_for`: обнаруживает задания со статусом `artifact_role='quarantine'`, и роут `/api/operator/orders/{id}/source` возвращает статус `409 Conflict: "Source is in recovery; manual intervention required"`, полностью блокируя несанкционированную отдачу файла.

### MEDIUM-02: `delivery.finish()` переводил обрабатываемые исходники в статус `delivered`
- **Первопричина:** `delivery.finish()` проверял оплату заказа, но не валидировал роль артефакта, позволяя пометить выполняющееся задание исходника как доставленное.
- **Исправление:**
  - В `backend/delivery.py`: в методе `finish()` добавлена строгая проверка: для заданий `kind == 'file'` обязательно наличие `artifact_role == 'result'`. Любая попытка завершить задание с другой ролью возвращает `False` без изменения состояния на `delivered`.

---

## 3. ЗАТРОНУТЫЕ ФАЙЛЫ

1. `backend/delivery.py`:
   - Внедрение fail-closed изоляции в `claim_due()` при отсутствии `artifact_role`.
   - Валидация роли `result` в `finish()`.
   - Защита от удаления исходников и карантинных файлов в `perform_job_cleanup()`.
   - Изоляция `process_due_cleanups()` от файлов без роли `result`.
2. `backend/main.py`:
   - Барьер защиты в `deliver_job()`: запрет доставки файлов с ролью, отличной от `result`.
3. `backend/migrations/reconcile_delivery_schema.py`:
   - Авторитетная валидация `result` (строгое совпадение пути и хэша).
   - Безусловный перевод конфликтующих и исторических недоказанных доставленных файлов в `quarantine`.
   - Самоисцеление (`cur_role != new_role`).
   - Безопасный бэкфилл очистки (`cleanup_state='not_applicable'` для не-результатов).
4. `backend/operator_workflow.py`:
   - Изоляция `_source_job` и обработка карантинных артефактов в `_quarantine_for`.
5. `backend/tests/test_migration_cleanup_backfill.py`:
   - Синхронизация теста 1 с инвариантом обязательного наличия `order_results` для роли `result`.
6. `backend/tests/test_legacy_artifact_role_migration.py`:
   - Добавление всех 9 независимых состязательных тестов на постоянной основе (всего 19 тестов).

---

## 4. РЕЗУЛЬТАТЫ ТЕСТИРОВАНИЯ И CI

### 4.1 Локальное тестирование
- Независимый состязательный набор (`test_independent_roles.py`): **16/16 PASSED**
- Миграционный набор артефактов (`test_legacy_artifact_role_migration.py`): **19/19 PASSED**
- Бэкфилл очистки (`test_migration_cleanup_backfill.py`): **8/8 PASSED**
- Операторский воркфлоу (`test_operator_workflow.py`): **20/20 PASSED**
- Комплекс безопасности платежей (`payment-security-tests`): **116/116 PASSED**
- Комплекс retention, consents и customer journey: **78/78 PASSED** (1 skipped на Windows из-за Linux symlink)

### 4.2 GitHub Actions CI
- **Commit SHA:** `3022b75887aba97d4716d83b93eacf8e3e82ecd8`
- **Run ID:** `43009948097` (push on `feat/r1-stage2-close-four-findings`)
- **Статус джоб:**
  - `Playwright Tests`: **SUCCESS** (35s)
  - `Payment Security Tests`: **SUCCESS** (18s)
  - `Combined Multi-PR Linux & Symlink Integration Tests`: **SUCCESS** (21s)
    - Включая проверку Linux symlinks: `1 passed`
  - `Docker Build`: **SUCCESS** (1m30s)
- **Combined Tree SHA:** `32d25618b1878a0c0207ffdce36855faa631d9d2`

---

## 5. ИТОГОВЫЙ СТАТУСНЫЙ БЛОК

```
STAGE=STAGE2_ARTIFACT_REMEDIATION_02_READY
TASK_ID=PHOTODOC-R1-LEGACY-ARTIFACT-HIGH-REMEDIATION-02
PR38_HEAD=d5e1ff6abb5490d4a54732acbd7cb5b81281fd10
CODE_FIX_HEAD=3022b75887aba97d4716d83b93eacf8e3e82ecd8
COMBINED_TREE_SHA=2108fd245bbd84284c727f50169bb1bede0a6344
HIGH_FINDINGS_CLOSED=4/4
MEDIUM_FINDINGS_CLOSED=2/2
INDEPENDENT_TESTS_PASSING=16/16
LINUX_CI_STATUS=PASS
PRODUCTION_UNCHANGED=true
DEPLOYMENT_BLOCKED=true
LEGAL_APPROVAL_STATUS=NOT_APPROVED
```
