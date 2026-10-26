# PhotoDoc AI — R2.1 Deployment & HTTPS Architecture Specification

This document defines the corrected deployment architecture, SQLite migration lifecycle, Nginx HTTPS bootstrap procedure, request buffering analysis, staging and production cutover plans, and factual infrastructure requirements for PhotoDoc AI.

---

## 1. Audited Target Architecture

### 1.1 Architecture Topology
```
[ Клиент (Браузер) ]
        │ HTTPS (Порт 443) / TLS 1.2 & 1.3
        │ Let's Encrypt (Certbot автоматическое обновление)
        ▼
[ Edge Nginx на новом Web VPS ]
        │ client_max_body_size 100M; proxy_request_buffering off;
        │ limit_req zone=api_limit burst=20 nodelay (для /api/);
        │ X-Forwarded-Proto: https
        ├─────────────────────────────────────────┐
        │                                         │
        ▼ GET / статика и SPA-маршруты            ▼ POST /api/* (потоковая передача)
[ Frontend Docker (Порт 8080) ]           [ Backend Docker (Порт 8000) ]
(Internal Nginx, preserves Proto)                 │
                                                  ├──────────────────────────────┐
                                                  ▼                              ▼
                                     [ Локальное хранилище Web VPS ]   [ Внешние интеграции ]
                                     - SQLite WAL (/data/orders.db)    - Robokassa (POST /api/payment/create)
                                     - Uploads (/app/uploads)          - Robokassa ResultURL callback
                                     - PRAGMA application_id=PDAI      - Яндекс.Диск API (OAuth)
                                     - Durable delivery queue          - SMTP (smtp.yandex.ru:465)
```

### 1.2 Принципы разделения и изоляции
1. **Полная независимость от старого VPS:**
   - Новый Web VPS содержит весь стек приложения: Edge Nginx, Docker-контейнеры `photodoc-frontend` и `photodoc-backend`, локальные тома SQLite и uploads.
   - Запрещено подключать новый Web VPS к SQLite старого сервера через публичные порты или туннели.
   - Старый VPS `79.137.196.14` с Xray/Marzban остаётся полностью нетронутым и выполняет исключительно функции VPN-ноды.
2. **Прямое взаимодействие с внешними сервисами:**
   - Новый backend подключается к Яндекс.Диску, SMTP Яндекса и платёжному шлюзу Robokassa напрямую со своего IP-адреса.

---

## 2. Анализ и устранение несовместимости SQLite

### 2.1 Исходное состояние базы на `79.137.196.14`
Read-only диагностика существующего файла `/var/lib/docker/volumes/photodoc_backend-data/_data/orders.db`:
- Размер: 12,288 байт (12 KB).
- `PRAGMA application_id`: `0` (Legacy unmarked).
- Таблицы: `orders`, `sqlite_sequence`.
- Записей в таблице `orders`: **0**.
- Колонки: `id`, `name`, `phone`, `email`, `format`, `paper`, `crop`, `comment`, `filename`, `status`, `payment_status`, `payment_amount`, `payment_method`, `robokassa_inv_id`, `paid_at`, `payment_email_sent_at`.
- Подтверждение принадлежности: Состав полей строго соответствует исходной структуре PhotoDoc P0 (`orders`), однако `application_id=0` **не считается валидным** для запуска P1 backend, так как P1 требует строгой идентификации `PDAI` (`1346650441`) и колонок `order_amount`, `created_at`, `order_token_hash` и таблицы `delivery_jobs`.

### 2.2 Проверенная цепочка миграции (Воспроизведена на синтетической копии)
1. **Шаг 1: P0 Payment Schema Reconciliation**
   ```bash
   python backend/migrations/reconcile_payment_schema.py --db /data/orders.db --allow-legacy-unmarked --apply
   ```
   - Добавляет колонки `order_amount TEXT`, `created_at TEXT`.
   - Устанавливает `PRAGMA application_id = 1346650441` (`PDAI`).
2. **Шаг 2: P1 Delivery Schema Reconciliation**
   ```bash
   python backend/migrations/reconcile_delivery_schema.py --db /data/orders.db --apply
   ```
   - Добавляет колонку `order_token_hash TEXT` в `orders`.
   - Создаёт таблицу `delivery_jobs` с индексами.
3. **Шаг 3: Верификация целостности**
   ```bash
   python scripts/production_preflight.py --db /data/orders.db --check-only
   python scripts/verify_backup_integrity.py --db /data/orders.db --uploads /app/uploads
   ```
   - Результат: `DB_IDENTITY_VALID=true`, `VERDICT: INTEGRITY_CHECK_PASSED`.

### 2.3 База данных для Staging
- Для Staging-окружения не требуется переносить старую пустую базу.
- Функция `main.init_db()` автоматически инициализирует чистую независимую базу данных с `PRAGMA application_id = 1346650441` (`PDAI`) и полным набором таблиц P1 (`orders`, `delivery_jobs`).

---

## 3. HTTPS, ACME Bootstrap и анализ буферизации Nginx

### 3.1 Двухфазный процесс ACME/Bootstrap
При первоначальном развертывании на новом Web VPS сертификаты Let's Encrypt ещё не существуют. Попытка запустить Nginx с конфигурацией HTTPS приводит к фатальной ошибке `cannot load certificate` и отказу старта.
Решение — двухфазный запуск:

- **Фаза 1: Bootstrap HTTP (`infra/nginx/photodoc-http-bootstrap.conf`)**
  1. Nginx слушает только порт 80.
  2. Маршрут `/.well-known/acme-challenge/` обслуживается из `/var/www/certbot`.
  3. Все остальные маршруты возвращают текстовую заглушку 200 Setup in progress.
  4. Запуск Certbot:
     ```bash
     certbot certonly --webroot -w /var/www/certbot -d photodoc-ai.ru -d www.photodoc-ai.ru
     ```
- **Фаза 2: Production HTTPS (`infra/nginx/photodoc-https.conf`)**
  1. После успешного выпуска сертификатов в `/etc/letsencrypt/live/photodoc-ai.ru/` активируется боевая конфигурация.
  2. Проверка синтаксиса: `nginx -t`.
  3. Перезагрузка: `systemctl reload nginx`.
  4. Порт 80 постоянно выполняет 301-редирект на HTTPS, сохраняя маршрут `acme-challenge` для автопродления каждые 60 дней.

### 3.2 Трассировка буферизации Multipart-запросов (80 MiB)
Для исключения падений по памяти и задержек ввода-вывода трафик заказов обрабатывается следующим образом:
1. **Edge Nginx (Web VPS):**
   - `client_max_body_size 100M;` — разрешает передачу тела заказа до 80 MiB с учетом multipart boundary.
   - `client_body_buffer_size 1M;` — небольшие фрагменты удерживаются в памяти.
   - `proxy_request_buffering off;` в блоке `/api/` — отключает буферизацию тела запроса на диск Edge Nginx; байты немедленно потоком передаются в контейнер.
2. **Внутренний Nginx (`photodoc-frontend` контейнер):**
   - `client_max_body_size 100M;`
   - `proxy_request_buffering off;` и `proxy_buffering off;` в `location /api/`.
   - `proxy_set_header X-Forwarded-Proto $http_x_forwarded_proto;` — сохраняет и передает заголовок `https`, выставленный Edge Nginx, предотвращая сброс в `http`.
3. **FastAPI / Starlette Backend:**
   - Парсер `python-multipart` принимает поток.
   - `UploadFile` использует `SpooledTemporaryFile(max_size=1MB)`. При превышении 1 MiB данные сбрасываются во временный файл в `/tmp` контейнера, после чего транслируются в `/app/uploads/{order_id}_{uuid}.{ext}` с вычислением SHA-256 и размера.

### 3.3 Rate Limiting и платёжные маршруты
- В `infra/nginx/photodoc-https.conf` активирована директива:
  `limit_req zone=api_limit burst=20 nodelay;` в `location /api/`.
- Фактический эндпоинт генерации оплаты: **`POST /api/payment/create`** (принимает `order_token` и `amount`).
- Webhook Robokassa: **`/api/payment/robokassa/result`** вынесен в отдельный блок без `limit_req` для исключения блокировки ретраев платёжного шлюза.

---

## 4. Планы развёртывания: Staging и Production Cutover

### 4.1 План Staging (Изолированное окружение)
- **Цель:** Полная проверка всего цикла без боевых клиентов, денег и риска для данных.
- **Инфраструктура:**
  - Отдельный Docker Compose стек с префиксом `photodoc-staging`.
  - Переменные окружения: `ROBOKASSA_IS_TEST=1`, тестовые пароли `ROBOKASSA_PASSWORD1`, `ROBOKASSA_PASSWORD2`.
  - Свежая независимая база SQLite (инициализируется автоматически через `main.init_db()`).
  - Папка Яндекс.Диска: `/PhotoDoc_Staging/`.
  - Тестовый почтовый ящик для SMTP-уведомлений.
- **Порядок тестирования:** 10-шаговый сценарий (см. Раздел 5).

### 4.2 План Production Cutover (Перенос данных с `79.137.196.14`)
*Выполняется строго после отдельного явного разрешения владельца проекта.*
1. **Остановка старого бэкенда на `79.137.196.14`:**
   ```bash
   docker compose -p photodoc stop backend
   ```
2. **Снятие консистентного снапшота базы и uploads:**
   ```bash
   docker run --rm -v photodoc_backend-data:/data alpine sh -c "apk add --no-cache sqlite && sqlite3 /data/orders.db 'PRAGMA wal_checkpoint(TRUNCATE);' && sqlite3 /data/orders.db '.backup /data/orders.db.cutover'"
   tar -czf /root/photodoc_cutover.tar.gz -C /var/lib/docker/volumes/photodoc_backend-data/_data orders.db.cutover -C /opt/photodoc/backend uploads
   sha256sum /root/photodoc_cutover.tar.gz > /root/photodoc_cutover.tar.gz.sha256
   ```
3. **Безопасная передача на новый Web VPS (по SSH / scp):**
   ```bash
   scp /root/photodoc_cutover.tar.gz* user@<NEW_WEB_VPS_IP>:/var/backups/photodoc/
   ```
4. **Проверка контрольной суммы на новом сервере:**
   ```bash
   sha256sum -c /var/backups/photodoc/photodoc_cutover.tar.gz.sha256
   ```
5. **Распаковка в production volume и uploads нового сервера.**
6. **Выполнение миграции P0 → P1 на новом сервере:**
   ```bash
   python backend/migrations/reconcile_payment_schema.py --db /data/orders.db --allow-legacy-unmarked --apply
   python backend/migrations/reconcile_delivery_schema.py --db /data/orders.db --apply
   python scripts/verify_backup_integrity.py --db /data/orders.db --uploads /app/uploads
   ```
7. **Переключение DNS A-записей на IP нового Web VPS.**
8. **Запуск боевого контейнера на новом Web VPS.**
9. **План отката (Rollback):** При любом сбое до смены DNS старый бэкенд на `79.137.196.14` запускается одной командой `docker compose start backend`.

---

## 5. План сквозного тестирования (10 шагов)

1. **HTTPS и редирект:** Проверка доступности `https://<domain>/` и редиректа 301 с HTTP:80.
2. **Оформление заказа:** Заполнение всех 4 форм (документы, печать, polaroid, реставрация).
3. **Лимиты загрузки:**
   - 5 MB JPEG — 200 OK.
   - 45 MB комбинированная загрузка — 200 OK.
   - Одиночный файл > 20 MiB — 413 Payload Too Large.
   - Суммарный заказ > 80 MiB — 413 Payload Too Large.
   - Исполняемый файл (.exe под видом .jpg) — 400 Bad Request.
4. **Сохранение в SQLite:** Проверка наличия заказа, хеша `order_token_hash` и задач со статусом `pending` в `delivery_jobs`.
5. **Создание ссылки на оплату:** Вызов `POST /api/payment/create` с `order_token`, проверка ссылки на тестовый шлюз Robokassa.
6. **Тестовый callback:** Отправка синтетического запроса на `POST /api/payment/robokassa/result` с валидной подписью `ROBOKASSA_PASSWORD2`, проверка ответа `OK{InvId}` и перехода заказа в статус `paid`.
7. **Доставка на Яндекс.Диск:** Запуск воркера доставки, загрузка в папку `/PhotoDoc_Staging/{order_id}/`, проверка точного совпадения SHA-256 и размера.
8. **SMTP-уведомление:** Доставка письма с деталями заказа на тестовый адрес.
9. **Отказоустойчивость:** Имитация ошибки сети при доставке, проверка освобождения lease и успешной повторной отправки.
10. **Проверка бэкапа:** Выполнение `scripts/verify_backup_integrity.py`, подтверждение статуса `INTEGRITY_CHECK_PASSED`.

---

## 6. Уточнение инфраструктурных блокеров

| Пункт | Фактический статус | Требуемое действие владельца |
| :--- | :--- | :--- |
| **Домен `photodoc-ai.ru`** | **Не зарегистрирован** в реестре TCI/RIPN (не просто пустая DNS-зона, а отсутствие регистрации объекта) | Зарегистрировать домен у аккредитованного регистратора (Reg.ru, Beget, RU-CENTER, ~200–300 руб/год). |
| **DNS-делегирование** | Отсутствует | Настроить NS (Yandex Cloud DNS / DNS регистратора) и создать A-записи на IP нового сервера. |
| **Новый Web VPS** | Не заказан | Арендовать сервер в РФ (1–2 vCPU, 2–4 GB RAM, Ubuntu 24.04 LTS). Реальные рыночные цены: Timeweb Cloud (~290–450 ₽/мес), Beget (~250–400 ₽/мес), Selectel (~400–600 ₽/мес), FirstVDS (~300–500 ₽/мес). *Абсолютные гарантии 100% SLA провайдерами не предоставляются, ориентир 99.9%.* |
| **Robokassa** | Требуются боевые ключи | Предоставить `MerchantLogin`, `Password1`, `Password2`. В Staging используется `ROBOKASSA_IS_TEST=1` с тестовыми картами. |
| **Яндекс.Диск** | Требуется OAuth-токен | Сгенерировать токен приложения Яндекс с доступом к Диску. |
| **SMTP Яндекса** | Требуется пароль приложения | Создать пароль приложения в настройках безопасности Яндекс ID. |
| **Разрешение на перенос** | Требуется подтверждение | Перенос базы данных и переключение DNS выполняются только по отдельной команде. |
