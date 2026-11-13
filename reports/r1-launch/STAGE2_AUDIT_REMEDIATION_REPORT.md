# Отчет об устранении замечаний независимого аудита Stage 2 (PR #38)

## 1. Сводка задачи и текущий статус

- **Идентификатор задачи:** `PHOTODOC-R1-STAGE2-AUDIT-REMEDIATION-01`
- **Целевой PR:** [Draft PR #38](https://github.com/life2boat/photodoc-ai/pull/38) (базовая ветка `feat/r1-stage2-close-four-findings`, надстройка над PR #37)
- **HEAD коммит PR #38:** `0c6a65f7bb33eb5d1e0cac03b720652908ec7eda`
- **SHA объединенного дерева (Combined Tree SHA):** `fbdef43996fc4bd5c62e467c67f4176cc5196f24`
- **Итоговый статус интеграции:** `READY_FOR_INDEPENDENT_RECHECK`
- **Безопасность продакшна:** `PRODUCTION_UNCHANGED=true`, `DEPLOYMENT_BLOCKED=true`

В рамках данной итерации устранены все замечания независимого аудита Codex/DeepSeek (`STAGE2_INDEPENDENT_CODEX_AUDIT.md`), классифицированные как блокирующие для Stage 2.

---

## 2. Детальный разбор устраненных замечаний

### 2.1. N-1 (HIGH / H-05): Fail-Closed валидация согласий при оформлении заказа

- **Проблема из аудита:**
  При отсутствии переменной окружения `CONSENT_FEATURE_ENABLED` эндпоинт `POST /api/order` возвращал HTTP 200, создавая заказ без записей `consent_records`. Система деградировала в небезопасный режим по умолчанию (fail-open).
- **Реализованное исправление:**
  1. В `backend/main.py::create_order` внедрена строгая проверка `consents.feature_enabled()`. При отсутствии переменной или значении `0` запрос немедленно отклоняется с кодом `400 Bad Request` и сообщением:
     `"Order creation is disabled: consent enforcement system is not configured"`.
  2. Проверка согласий выполняется **до** сохранения любых файлов на диск и создания записей в базе данных (`fail-closed` до мутации файловой системы).
  3. Обязательные согласия (`service_terms` и `personal_data_processing`) должны быть явно предоставлены клиентом (`"true"` / `"1"`). При их отсутствии возвращается HTTP 400.
  4. При передаче `doc_ver` и `doc_hash` выполняется сверка с актуальной версией документа и каноническим SHA-256 хэшем (`consents.canonical_hash()`).
  5. В `docker-compose.yml`, `docker-compose.staging.yml` и CI-конфигурациях переменная `CONSENT_FEATURE_ENABLED` по умолчанию установлена в `1`.
  6. В `backend/tests/test_consent_order_flow.py` добавлены 11 тестов:
     - `test_order_creation_rejected_when_flag_absent` (HTTP 400)
     - `test_order_creation_rejected_when_flag_disabled` (HTTP 400)
     - `test_order_creation_rejected_when_flag_absent_even_with_consents` (HTTP 400)
     - `test_order_creation_rejected_when_mandatory_consent_missing` (HTTP 400)
     - `test_order_creation_rejected_when_hash_mismatch` (HTTP 400)
     - `test_order_creation_rejected_when_version_mismatch` (HTTP 400)
     - `test_order_creation_succeeds_with_valid_consents` (HTTP 200 + проверка `consent_records`)
     - `test_failed_consent_validation_leaves_zero_filesystem_and_db_artifacts` (проверка отсутствия сиротских файлов)
     - Защищенный токеном эндпоинт `GET /api/order/{order_id}/consents`.

### 2.2. N-2: Статус юридического согласования (Legal Approval Status)

- **Контекст аудита:**
  Требовалось четко раскрыть статус текстов согласий, не фабрикуя фиктивное утверждение юристами.
- **Текущее состояние:**
  1. В `backend/consents.py` статус обязательных документов (`PURPOSE_SERVICE`, `PURPOSE_PERSONAL_DATA`) зафиксирован как:
     `legal_status: "DRAFT_PENDING_OWNER_LEGAL_FINALIZATION"`.
  2. Вторичные согласия (`third_party_images`, `minor_images`, `cross_border_transfer`) помечены `approved: False`. При попытке их передачи `create_order` возвращает HTTP 400 с блокировкой до утверждения юристами.

### 2.3. N-3 (MEDIUM / M-03): Исполнение runtime ротации логов

- **Проблема из аудита:**
  Код ротации логов существовал как утилита, но не вызывался в фоновом цикле `retention_worker`, в результате чего устаревшие логи не удалялись.
- **Реализованное исправление:**
  1. В `backend/log_rotation.py` реализована функция `rotate_application_logs(log_dir, retention_days, active_retention_days)`:
     - Защита активных логов: файлы из `ACTIVE_BASE_LOG_NAMES` (`backend.log`, `delivery.log`, `retention.log`, `app.log`) и файлы, модифицированные за последние 24 часа, никогда не удаляются.
     - Защита Legal Hold: файлы с маркерами `*.hold`, `*incident*` исключаются из удаления.
     - Четкие границы ответственности: ротация управляет файлами логов приложения в `LOG_DIR`. Системный Nginx ротируется host-демоном logrotate, логи контейнеров — Docker logging driver.
  2. Функция ротации подключена шагом 5 в `backend/retention_worker.py::run_retention_cycle` при флаге `LOG_ROTATION_ENABLED=1`.
  3. Вызов защищен распределенной блокировкой `retention_scheduler_lease` (Multi-Instance Lease Fencing), что предотвращает конкурентную ротацию при нескольких инстансах бэкенда.
  4. Телеметрия удаленных логов фиксируется в таблице `retention_telemetry` (`logs_purged`).
  5. В `backend/tests/test_rotation_safety_enforcement.py` реализовано 9 тестов, проверяющих удаление устаревших архивов, защиту активных логов, сохранение legal hold, интеграцию с воркером и отказоустойчивость при заблокированных файлах.

### 2.4. N-4: Устранение CI Gap — Выделенный CI Job для Combined Tree и Linux Symlink

- **Проблема из аудита:**
  Тесты симлинков на Linux и проверка воспроизводимого мерджа PR #34-#38 отсутствовали в качестве блокирующего верхнеуровневого шага в GitHub Actions.
- **Реализованное исправление:**
  1. В `.github/workflows/playwright.yml` добавлен независимый верхнеуровневый job `Combined Multi-PR Linux & Symlink Integration Tests` (`combined-tree-linux-tests`), запускаемый на `ubuntu-latest`.
  2. Job автоматически воспроизводит объединенное дерево мерджем веток:
     `main -> PR #34 -> PR #35 -> PR #36 -> PR #37 -> PR #38`.
  3. Выполняется Linux symlink тест `test_parent_symlink_replacement_cannot_delete_outside_uploads` с условием `fail-closed` (при пропуске теста job падает с ошибкой).
  4. Запускаются все тест-сьюты согласий, ретеншена, ротации логов, операторского интерфейса, доставки и миграций.
  5. В `scripts/test_container_upload_limits.py` добавлены дефолтные флаги согласий, а в `docker-compose.yml` передан `CONSENT_FEATURE_ENABLED=1`, благодаря чему smoke test контейнеризованного Nginx в `docker-build` успешно проходит.

---

## 3. Результаты верификации GitHub Actions CI

**CI Run URL:** https://github.com/life2boat/photodoc-ai/actions/runs/42936276443

Все 4 обязательных задания завершились успешно:
1. `Playwright Tests`: **SUCCESS** (40s, ID: 130247027692)
2. `Payment Security Tests`: **SUCCESS** (16s, ID: 130247028154)
3. `Combined Multi-PR Linux & Symlink Integration Tests`: **SUCCESS** (28s, ID: 130247028171)
   - Воспроизведено объединенное дерево: `COMBINED_TREE_SHA=fbdef43996fc4bd5c62e467c67f4176cc5196f24`
   - Linux Symlink regression test: **PASSED** (1 passed, 0 skipped)
   - Consent enforcement (fail-closed): **26 PASSED**
   - Retention & quarantine (C-01, C-02, H-02): **33 PASSED**
   - Rotation safety & log rotation (M-03): **9 PASSED**
   - Operator workflow & source delivery: **51 PASSED**
   - Migrations & 16-step customer journey: **29 PASSED**
   - Итого в комбинированном прогоне на Linux: **149 PASSED**
4. `Docker Build`: **SUCCESS** (1m31s, ID: 130247028399)
   - Контейнеризованный Nginx upload limits & smoke tests: **PASSED**

---

## 4. Сводный статус соответствия

| Пункт аудита | Описание | Статус | Подтверждение |
|---|---|---|---|
| **N-1 (H-05)** | Fail-closed consent enforcement | **RESOLVED_FAIL_CLOSED** | Тесты `test_consent_order_flow.py`, `backend/main.py:1318-1328` |
| **N-2** | Legal approval disclosure | **DRAFT_PENDING_OWNER_LEGAL_FINALIZATION** | `backend/consents.py`, отсутствие фиктивного утверждения |
| **N-3 (M-03)** | Log rotation runtime execution | **RESOLVED_EXECUTED** | `backend/log_rotation.py`, `backend/retention_worker.py:270-288` |
| **N-4** | Dedicated combined CI & Linux symlink test | **RESOLVED_CI_PASS** | Job `combined-tree-linux-tests` в run 42936276443 |
| **Безопасность** | Изоляция продакшна | **VERIFIED** | Хост 79.137.196.14 не затрагивался, `DEPLOYMENT_BLOCKED=true` |
