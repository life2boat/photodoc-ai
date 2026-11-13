# Отчёт о готовности к интеграции жизненного цикла исходников (Source Lifecycle Integration Readiness)

**Идентификатор задачи:** `PHOTODOC-R1-SOURCE-LIFECYCLE-INTEGRATION-READINESS-01`  
**Исполнитель:** Google Antigravity  
**Дата:** 2026-11-09  
**Репозиторий:** `life2boat/photodoc-ai`  
**Целевая ветка:** `fix/photodoc-r1-real-order-source-lifecycle` (PR #36)  
**Итоговый вердикт:** `READY_FOR_INTEGRATION_REVIEW`

---

## 1. Резюме и статус находок независимого аудита DeepSeek

В рамках независимого аудита DeepSeek (`PHOTODOC-R1-DEEPSEEK-INDEPENDENT-AUDIT-01`) фикс разделения ролей артефактов (`artifact_role='source'` / `'result'` / `'info'`) получил статус `APPROVE_FOR_NEXT_STAGE` с тремя замечаниями к этапу интеграции. Все три замечания полностью закрыты:

### F-1 (MEDIUM) — Устаревшие тесты доставки переведены на утверждённый контракт
- **Проблема:** Три теста в `backend/tests/test_order_delivery.py` (`test_failed_partial_delivery_and_restart_retry_keep_sources`, `test_expired_lease_recovered_after_crash`, `test_backend_startup_worker_resumes_persisted_jobs`) изначально ожидали, что воркер доставки будет отправлять клиентские фото-исходники на Яндекс.Диск сразу после оплаты.
- **Исправление:** Тесты обновлены в строгом соответствии с контрактом R1 и инвариантами A–F:
  1. Клиентские файлы-исходники получают `artifact_role='source'` и остаются в состоянии `state='pending'`.
  2. Колбэк оплаты **не ставит** исходники в очередь доставки (`claim_due` не забирает их).
  3. Воркер доставки забирает и доставляет **только** обработанный оператором результат (`artifact_role='result'`), текстовое описание (`info`) и email-уведомления.
  4. Очистка воркера (`perform_job_cleanup`) не удаляет исходники клиента с диска.
- **Результат:** Модуль `backend/tests/test_order_delivery.py` проходит со 100% успехом: **12 passed**.
- **Коммит:** `1786c1a4204fa979df942520ffe53b65e0e24fd6`.

### F-2 (NON-BLOCKING) — Регламент применения миграции схемы
- **Проблема:** Внедрение колонки `artifact_role` в `delivery_jobs` реализовано через аддитивный скрипт `backend/migrations/reconcile_delivery_schema.py`, требующий ручного применения при остановленном сервисе-писателе.
- **Решение:** Разработан исчерпывающий пошаговый регламент: [`reports/r1-launch/ARTIFACT_ROLE_MIGRATION_RUNBOOK.md`](file:///F:/диск%20е/landing/reports/r1-launch/ARTIFACT_ROLE_MIGRATION_RUNBOOK.md).
- **Содержание регламента:**
  1. Предварительные проверки контракта (`inspect_database`, SQLite WAL status).
  2. Остановка сервисов-писателей (`systemctl stop photodoc-backend` / `docker compose stop`).
  3. Верифицированное резервное копирование (`sqlite3 ... ".backup ..."` + SHA-256).
  4. Тестовый холостой прогон миграции (`--db ...` без `--apply`).
  5. Применение изменений под блокировкой `BEGIN IMMEDIATE` (`--apply`).
  6. Верификация схемы (`PRAGMA table_info`) и корректности классификации существующих строк.
  7. Контроль совместимости приложения (`delivery_schema_ready`, `health_check`).
  8. План отката и аварийного восстановления.
  9. Чёткие критерии безопасного запуска сервиса.

### F-3 (DOCUMENTED) — Анализ противоречивых тестов аудита Codex
- **Проблема:** В файле `scratch/pr36-audit/test_operator_adversarial.py` два теста содержали взаимоисключающие утверждения для одной и той же исходной фикстуры заказа:
  - Строка 82: `test_original_cannot_be_delivered_before_processed_result` утверждает `assert claim_due(db) is None` (исходник не может быть доставлен до результата).
  - Строка 89: `test_delivered_original_does_not_block_result_upload` утверждает `assert claimed["id"] == "source-1"` (исходник обязан быть захвачен и доставлен воркером).
- **Разрешение:**
  - Согласно фундаментальному инварианту A, клиентские исходники **никогда не доставляются** воркером клиенту. Поэтому строка 89 противоречит утверждённой архитектуре.
  - Намерение проверки (загрузка результата оператором не блокируется историческими доставленными записями) корректно валидируется утверждённым тестом `test_delivered_source_does_not_block_result_upload` в `backend/tests/test_operator_workflow.py:173`.
  - Оригинальный файл Codex в `scratch/` сохранён неизменным; противоречивый тест признан устаревшим и не выдаётся за прошедший.

---

## 2. Идентификация коммитов и деревьев Git

| Роль | SHA | Описание |
|---|---|---|
| **AUDITED_HEAD** | `80b2a2b42c24c13e426cb97776e9fe244f1bbe12` | Базовый коммит до фикса разделения ролей артефактов |
| **VERIFIED_FIX_COMMIT** | `614b0ae1ca4b91dde7b90049c42a91da7ca5025d` | Фикс жизненного цикла исходников (инварианты A–F) |
| **NEW_HEAD** | `1786c1a4204fa979df942520ffe53b65e0e24fd6` | Фикс F-1: согласование тестов доставки `test_order_delivery.py` |
| **COMBINED_TREE_REF** | `88a738d96846e95ac3bff6df5005b2b1289dac43` | Слияние PR #36 + PR #34 (локальный коммит `08c44bc`) |

---

## 3. Результаты полного регрессионного тестирования

Все необходимые тестовые наборы выполнены на актуальном рабочем дереве `NEW_HEAD` и объединённом дереве `COMBINED_TREE`:

| Тестовый набор | Файлы тестов | Passed | Failed | Skipped | Статус |
|---|---|---|---|---|---|
| **Доставка заказов (F-1)** | `backend/tests/test_order_delivery.py` | 12 | 0 | 0 | **PASS** |
| **Жизненный цикл исходников** | `backend/tests/test_real_order_source_lifecycle.py` | 8 | 0 | 0 | **PASS** |
| **Операторский воркфлоу** | `backend/tests/test_operator_workflow.py` | 20 | 0 | 0 | **PASS** |
| **Безопасность платежей** | `backend/tests/test_payment_security.py` | 23 | 0 | 0 | **PASS** |
| **Конкурентность доставки** | `backend/tests/test_delivery_concurrency.py` | 5 | 0 | 0 | **PASS** |
| **Миграции и бэкфилл** | `backend/tests/test_migration_cleanup_backfill.py`<br>`backend/tests/test_migration_reconciliation.py` | 28 | 0 | 0 | **PASS** |
| **Ретеншн C-01 / C-02 (Combined)** | `backend/tests/test_retention_recovery.py`<br>`backend/tests/test_retention.py`<br>`backend/tests/test_retention_adversarial.py`<br>`backend/tests/test_retention_payment_gate.py` | 33 | 0 | 1* | **PASS** |
| **ИТОГО** | **Все актуальные наборы регрессии** | **129** | **0** | **1** | **PASS** |

*\* Примечание: 1 тест (`test_parent_symlink_replacement_cannot_delete_outside_uploads`) пропущен из-за отсутствия привилегий создания symlink в среде Windows (`WinError 1314`).*

---

## 4. Проверка гарантий C-01 и C-02 на объединённом дереве

На ветке слияния PR #36 и PR #34 (`08c44bc9153a572dd38e9beb92f2b8999db92fe9`):
1. **Инвариант C-01 (Quarantine on Restore Failure):**
   - При сбое восстановления повреждённый файл или ошибка не приводят к потере единственной копии.
   - Тесты `test_c01_failed_restore_keeps_the_only_copy` и `test_c01_restart_with_paid_order_keeps_quarantine` прошли успешно.
2. **Инвариант C-02 (Quarantine Restoration on Late Payment):**
   - Поздняя оплата восстанавливает клиентский исходник из карантина, сохраняя непрерывность выполнения заказа.
   - Тесты `test_c02_payment_after_deletion_restores_from_quarantine`, `test_c02_unpaid_quarantine_is_released` и `test_c02_quarantine_is_kept_within_the_grace_window` прошли успешно.

---

## 5. Статус проверки Linux Symlinks

- **Окружение хоста:** Windows 11 (NTFS).
- **Попытка запуска:** Создание символических ссылок в Windows требует повышенных привилегий SeCreateSymbolicLinkPrivilege (`WinError 1314`).
- **Попытка WSL2:** Дистрибутив WSL2 Ubuntu присутствует, однако в системном Python WSL отсутствуют необходимые зависимости проекта (`fastapi`, `httpx` и др.).
- **Статус согласно инструкции:** `NOT_EXECUTED` (не блокирует слияние, валидация должна быть выполнена в CI Linux или на VPS в изолированном контейнере).

---

## 6. Оставшиеся блокеры и рекомендации по развёртыванию

1. **Режим развёртывания:**
   - Репозиторий сохраняет флаг `DEPLOYMENT_BLOCKED` до консолидированного одобрения владельцем проекта.
   - Прямой `git push` и автоматический мерж запрещены регламентом проекта.
2. **Порядок слияния:**
   - PR #34 (ретеншн и карантин C-01/C-02) и PR #36 (жизненный цикл исходников) проверены на совместимость без конфликтов.
   - Рекомендуется слияние PR #34, затем PR #36.
3. **Применение на продакшене:**
   - Обязательно строго следовать [`reports/r1-launch/ARTIFACT_ROLE_MIGRATION_RUNBOOK.md`](file:///F:/диск%20е/landing/reports/r1-launch/ARTIFACT_ROLE_MIGRATION_RUNBOOK.md): остановка `photodoc-backend` -> верифицированный бэкап -> `reconcile_delivery_schema.py --apply` -> запуск.
