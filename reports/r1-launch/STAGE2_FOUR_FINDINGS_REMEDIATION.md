# PHOTODOC AI — ОТЧЕТ О ЗАКРЫТИИ ЧЕТЫРЕХ ЗАМЕЧАНИЙ (ЭТАП 2: H-02, H-05, M-02, M-03)

**Идентификатор задачи:** `PHOTODOC-R1-STAGE2-CLOSE-H02-H05-M02-M03`  
**Дата и время:** 2026-11-10T06:20:00+07:00  
**Исполнитель:** Google Antigravity (Advanced Agentic Coding)  
**Репозиторий:** `life2boat/photodoc-ai`  
**Рабочее дерево:** `F:\диск е\landing\.worktrees\audit-integration`  
**Целевая ветка:** `feat/r1-stage2-close-four-findings`  
**Базовая ветка для Stacked PR:** `fix/photodoc-r1-source-lifecycle-integration` (PR #37)  
**Статус развертывания:** `DEPLOYMENT_BLOCKED=true` (строго сохранено)  
**Доступ к production-хосту (79.137.196.14):** ЗАПРЕЩЕН / ОТСУТСТВУЕТ  

---

## 1. Резюме выполнения

Все четыре открытых замечания из консолидированного аудита интеграции успешно устранены и покрыты автоматическими тестами:

1. **M-02 (Canonical Consent Document Hashes): ЗАКРЫТО**
   - Плейсхолдерные строки в `backend/consents.py` заменены на криптографические дайджесты SHA-256 утвержденных канонических документов (`public-preview/offer.html` и `public-preview/privacy.html`).
   - Реализована нормализация окончаний строк (`CRLF` -> `LF`), что гарантирует идентичность хэшей на Windows и Linux.
   - Зафиксированы эталонные хэши:
     - Оферта (`public-preview/offer.html`): `3eb6615b5b5a7da42d7c4e86b5cd692b3daf20ef5b160481b4cbbed789c2c9c0`
     - Политика (`public-preview/privacy.html`): `7a4e0fde5beaf6f1ef15c5d7b8fa3c6bbab8b9d6633a1e2be5f6c5c8c87fe6cf`
   - Для условий, не имеющих утвержденного юридического текста (`third_party_images`, `minor_images`, `cross_border_transfer`), статус установлен в `BLOCKED_PENDING_LEGAL_APPROVAL` (`approved=False`). Попытка их активации или согласия завершается ошибкой (fail-closed).

2. **H-05 (Consent Enforcement in Public Order Flow): ЗАКРЫТО**
   - В эндпоинт `POST /api/order` (`backend/main.py`) внедрены параметры согласий: `consent_service_terms`, `consent_personal_data`, условные флаги, `consent_doc_version`, `consent_doc_hash` и `consents_json`.
   - При активном флаге `CONSENT_FEATURE_ENABLED=1`:
     - Обязательные согласия (оферта и обработка ПД) валидируются до выполнения операций. Отсутствие -> `HTTP 400 Bad Request`.
     - Проверяется соответствие версии и SHA-256 хэша каноническим значениям с использованием `hmac.compare_digest`.
     - Неутвержденные согласия (третьи лица, дети, трансгран) отклоняются с `HTTP 400 Bad Request`.
   - Внутри единой атомарной транзакции SQLite (`BEGIN IMMEDIATE`):
     - Фиксируются записи согласий в таблице `consent_records` (`order_ref = str(order_id)`).
     - Вызывается `consents.check_required(conn, str(order_id))`. При обнаружении недостающих согласий транзакция откатывается.
     - При любой ошибке/откате временные и финальные файлы немедленно удаляются (`unlink`), обеспечивая нулевой объем мусорных файлов и нулевое количество незарегистрированных заказов.
   - Добавлен защищенный токеном эндпоинт `GET /api/order/{order_id}/consents?order_token={token}` для инспекции юридических свидетельств согласия.

3. **H-02 (Retention, Purge, Rotation & Hold Lifecycle Integration): ЗАКРЫТО**
   - Разработан и внедрен скоординированный фоновый воркер `backend/retention_worker.py`.
   - Реализована таблица межпроцессных блокировок `retention_scheduler_lease` для предотвращения параллельного конфликтующего выполнения в multi-instance среде.
   - Реализована таблица аудита и телеметрии `retention_telemetry` (сохраняет статистику циклов без раскрытия персональных данных клиентов).
   - Скоординированный цикл выполняет:
     1. `retention.purge_expired` (очистка неоплаченных заказов старше 72 часов с помещением в карантин).
     2. `retention.purge_quarantines` (окончательное удаление файлов из карантина по истечении 24-часового льготного периода после повторной проверки статуса оплаты).
     3. `paid_hold.flag_escalations` (эскалация оплаченных, но не доставленных заказов старше 7 дней; файлы оплаченных заказов **никогда** не удаляются по таймеру).
     4. `backup_rotation.rotate` (ротация резервных копий при активном флаге `BACKUP_ROTATION_ENABLED=1`).
   - Функция проверки безопасности `validate_retention_safety` гарантирует `RETENTION_HOURS >= 24`, проверяет существование каталога загрузок и блокирует выполнение на корне файловой системы.
   - Фоновый поток воркера интегрирован в `lifespan` приложения `backend/main.py` под управлением `RETENTION_WORKER_ENABLED=1` и `threading.Event()` для чистого завершения при остановке сервера.

4. **M-03 (Rotation Safety Checks Runtime Enforcement): ЗАКРЫТО**
   - В `backend/backup_rotation.py` добавлены функции `validate_backup_rotation_params` и `validate_backup_dir_safety`, проверяющие:
     - `retention_days > 0`, `keep_min >= 0`, `max_age_days >= retention_days`.
     - Запрет выхода за пределы каталога (`..`), запрет перехода по симлинкам, запрет указания корня файловой системы.
   - Создан модуль `backend/log_rotation.py`, реализующий функцию `verify_log_retention_policy(log_dir, max_days=15)` в строгом соответствии с обязательством в `public-preview/privacy.html:48-56` и `reports/r1-launch/compliance/152FZ_PACKAGE.md:17` (максимальный срок хранения логов — не более 15 суток).
   - В `backend/main.py` внедрен стартовый хук `validate_rotation_safety()`, вызываемый при старте приложения (`lifespan`) и аварийно завершающий запуск (fail-closed) при нарушении лимитов без логирования секретных ключей.

---

## 2. Сквозной тест клиентского сценария (16 шагов)

Создан и успешно выполнен интеграционный тест `backend/tests/test_stage2_customer_journey.py`, покрывающий все 16 шагов сквозного жизненного цикла:
1. Проверка криптографических хэшей утвержденных документов (оферта, политика ПД).
2. Ознакомление клиента с условиями и формирование параметров согласий.
3. Отправка заказа через `POST /api/order` с валидными согласиями и исходным изображением.
4. Атомарное создание заказа и фиксация записей согласий в SQLite.
5. Проверка свидетельств согласия через `GET /api/order/{id}/consents` с токеном заказа.
6. Инициализация платежа через `/api/payment/create`.
7. Обработка подписанного callback от Robokassa (`POST /api/payment/robokassa/result`).
8. Переход заказа в статус `paid`.
9. **Инвариант A:** Исходное изображение (`artifact_role='source'`) исключено из очереди доставки клиенту.
10. Авторизация оператора по Bearer-токену и получение списка оплаченных заказов.
11. Скачивание исходного изображения оператором через `/api/operator/orders/{id}/source`.
12. Обработка изображения оператором и загрузка результата через `/api/operator/orders/{id}/result`.
13. Постановка результата в очередь доставки с `artifact_role='result'`.
14. **Инвариант D:** Воркер доставки берет в обработку исключительно обработанный результат.
15. **Инвариант C:** Пост-доставка удаляет только локальный файл результата; файл оригинала клиента сохраняется нетронутым.
16. Выполнение цикла ретеншн-воркера с блокировкой lease, подтверждением защиты оплаченного заказа от удаления и верификацией политик ротации.

---

## 3. Результаты тестирования

Все тесты объединенного набора выполнены локально:
- `test_consents.py` + `test_consents_integrity.py` (15 тестов) — PASSED
- `test_consent_order_flow.py` (8 тестов) — PASSED
- `test_retention.py` + `test_retention_recovery.py` + `test_retention_payment_gate.py` (33 теста) — PASSED
- `test_retention_worker.py` (4 теста) — PASSED
- `test_rotation_safety_enforcement.py` (4 теста) — PASSED
- `test_backup_rotation_naming.py` (5 тестов) — PASSED
- `test_operator_workflow.py` (10 тестов) — PASSED
- `test_order_delivery.py` + `test_delivery_concurrency.py` (22 теста) — PASSED
- `test_real_order_source_lifecycle.py` (2 теста) — PASSED
- `test_stage2_customer_journey.py` (1 тест, 16 шагов) — PASSED

**Итого:** 104 теста из 104 пройдены успешно (100% GREEN).

---

## 4. Сохранение безопасности развертывания

- `DEPLOYMENT_BLOCKED=true` сохранено в полном объеме.
- Никаких действий на production сервере (`79.137.196.14`) не производилось.
- Прямое слияние в `main` заблокировано.
- Ветка подготовлена для создания Draft PR, расположенного поверх PR #37.
