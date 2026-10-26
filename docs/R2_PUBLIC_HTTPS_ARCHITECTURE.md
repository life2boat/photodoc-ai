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

## 4. Изоляция Staging и Безопасный Регламент Production Cutover

### 4.1 Архитектура и Изоляция Staging-окружения
Для сквозного тестирования функциональности до переноса боевых данных развёртывается полностью изолированное Staging-окружение:
- **Конфигурация Docker Compose (`docker-compose.staging.yml`):**
  - Имя проекта: `photodoc-staging`.
  - Изолированные тома хранения:
    - База данных: `photodoc_staging_backend-data` (монтируется в `/data` контейнера).
    - Файлы загрузок: `photodoc_staging_uploads` (монтируется в `/app/uploads`).
  - Изолированная сеть: `photodoc_staging_network` (bridge, без связей с production).
  - Порты: frontend опубликован на `8081:80` (исключает конфликт с портами 80, 8080, 443).
  - Контейнеры: `photodoc-staging-frontend` и `photodoc-staging-backend`.
- **Конфигурация окружения (`backend/.env.staging.example`):**
  - `ROBOKASSA_IS_TEST="1"` — принудительный тестовый режим платежей.
  - `DELIVERY_WORKER_ENABLED="0"` — фоновые внешние отправки по умолчанию отключены; запуск воркера только контролируемо вручную при тестах доставки.
  - `YANDEX_DISK_ROOT_DIR="PhotoDoc_Staging"` — корневой каталог в облаке Яндекс.Диск переопределён на изолированную папку `/PhotoDoc_Staging/`.
  - Независимый тестовый OAuth-токен Яндекс.Диска и тестовый SMTP-аккаунт.
- **Гарантии безопасности изоляции:**
  - Нулевой доступ к volume боевого сервера (`photodoc_backend-data`), боевым файлам клиентов и рабочим секретам.
  - Свежая база SQLite автоматически создаётся с `PRAGMA application_id = 1346650441` (`PDAI`) и структурой P1 (`orders`, `delivery_jobs`) при первом запуске через `main.init_db()`.
  - Успешность запуска проверяется эндпоинтом `GET /api/health` (`{"status": "ok"}`).

### 4.2 Регламент резервного копирования и переносимости контрольных сумм
На основе анализа выявленных дефектов зафиксирован регламент создания и проверки бэкапа:
1. **Выделенный каталог бэкапа:**
   - Категорически запрещено создавать временные копии базы внутри действующего Docker volume `/data` (во избежание засорения и рисков повреждения).
   - Бэкапы сохраняются в выделенный защищённый каталог хоста `/var/backups/photodoc/` с правами доступа `chmod 700 /var/backups/photodoc`.
2. **Сброс SQLite WAL (Write-Ahead Log):**
   - Перед созданием снапшота базы обязательно выполняется сброс всех накопленных страниц из WAL в основной файл:
     ```sql
     PRAGMA wal_checkpoint(TRUNCATE);
     ```
   - Создание консистентного снапшота выполняется средствами SQLite API: `.backup /var/backups/photodoc/orders.db.cutover`.
3. **Единая точка восстановления (DB + Uploads):**
   - База данных и каталог клиентских фотографий упаковываются в один согласованный архив с единым детерминированным именем:
     ```bash
     ARCHIVE_NAME="photodoc_cutover.tar.gz"
     tar -czf "/var/backups/photodoc/${ARCHIVE_NAME}" \
       -C /var/backups/photodoc orders.db.cutover \
       -C /opt/photodoc/backend uploads
     ```
4. **Переносимые относительные контрольные суммы:**
   - Файл `manifest.sha256` формируется строго с относительными именами файлов (без абсолютных путей источника) с использованием того же имени архива:
     ```bash
     cd /var/backups/photodoc
     sha256sum "${ARCHIVE_NAME}" > manifest.sha256
     chmod 600 "${ARCHIVE_NAME}" manifest.sha256
     ```
   - При переносе на целевой сервер проверка выполняется локально:
     ```bash
     cd /var/backups/photodoc && sha256sum -c manifest.sha256
     ```
   - Это гарантирует успешность проверки независимо от несовпадения каталогов источника и приёмника (подтверждено тестом `test_consistent_single_archive_name_transfer_rehearsal`).
5. **Проверка ресурсов и прав:**
   - Проверка свободного места (`df -h /var/backups/photodoc` — доступно не менее 3x от объёма данных).
   - Проверка прав на файлы архивов (`chmod 600 /var/backups/photodoc/*`).
   - Исходные боевые данные на сервере не модифицируются.

### 4.3 Безопасная последовательность Production Cutover (13 шагов)
Переключение боевой системы на новый Web VPS выполняется строго по 13 шагам с явными точками авторизации:

```
[Шаг 1: Подготовка нового Web VPS и Staging]
       │
[Шаг 2: Тестирование Staging (HTTPS, API, Upload, Payment, Delivery)]
       │
   ▼ CHECKPOINT 1: Явное разрешение владельца на остановку старого VPS
       │
[Шаг 4: Заморозка писателей на 79.137.196.14 (docker compose stop backend)]
       │
[Шаг 5: Согласованный бэкап DB WAL + Uploads в /var/backups/photodoc/]
       │
[Шаг 6: Пробное восстановление (Dry-Run Restore) и проверка целостности]
       │
[Шаг 7: Перенос архива на новый VPS и сверка относительных SHA-256]
       │
[Шаг 8: Миграция P0 → P1 на новом VPS (reconcile_payment + reconcile_delivery)]
       │
[Шаг 9: Запуск боевых контейнеров на новом VPS на локальном порту]
       │
[Шаг 10: Проверка readiness, HTTPS, Edge Nginx и целостности БД]
       │
   ▼ CHECKPOINT 2: Явное разрешение владельца на переключение DNS
       │
[Шаг 11: Переключение DNS A-записей (photodoc-ai.ru → IP нового Web VPS)]
       │
[Шаг 12: Мониторинг боевого трафика, Robokassa и фоновой доставки P1]
       │
[Шаг 13: Регламент Rollback (До / После смены DNS)]
```

#### Детальный регламент каждого шага:
1. **Шаг 1. Подготовка нового Web VPS и независимого staging-окружения:**
   - Настройка ОС Ubuntu 24.04, Docker Engine, Docker Compose, системного Nginx.
   - Развёртывание `docker-compose.staging.yml` и создание тестового каталога.
2. **Шаг 2. Комплексное тестирование Staging:**
   - Выполнение 10-шагового тестового сценария (см. Раздел 5): HTTPS, API, загрузка до 80 MiB, тестовая оплата Robokassa, доставка в `/PhotoDoc_Staging/`, SMTP.
3. **Шаг 3. Авторизация владельца проекта (CHECKPOINT 1):**
   - Получение отдельного недвусмысленного подтверждения от владельца на перевод системы в регламентные работы и остановку старого бэкенда.
4. **Шаг 4. Блокировка записи на старом приложении (Freeze Writers):**
   ```bash
   # Выполняется на 79.137.196.14
   docker compose -p photodoc stop backend
   ```
   - Запрещает параллельную запись новых заказов в старую базу. Исключает состояние split-brain и рассинхронизацию двух production-БД.
5. **Шаг 5. Согласованный бэкап БД и фотографий:**
   ```bash
   # 1. Подготовка защищённого каталога бэкапа хоста
   mkdir -p /var/backups/photodoc
   chmod 700 /var/backups/photodoc

   # 2. Принудительный сброс WAL и создание .backup НАПРЯМУЮ в смонтированный каталог бэкапа
   docker run --rm \
     -v photodoc_backend-data:/data:ro \
     -v /var/backups/photodoc:/backup \
     alpine sh -c "apk add --no-cache sqlite && sqlite3 /data/orders.db 'PRAGMA wal_checkpoint(TRUNCATE);' && sqlite3 /data/orders.db '.backup /backup/orders.db.cutover'"

   chmod 600 /var/backups/photodoc/orders.db.cutover

   # 3. Упаковка единой точки восстановления (БД + uploads) под детерминированным именем
   ARCHIVE_NAME="photodoc_cutover.tar.gz"
   cd /var/backups/photodoc
   tar -czf "${ARCHIVE_NAME}" orders.db.cutover -C /opt/photodoc/backend uploads
   sha256sum "${ARCHIVE_NAME}" > manifest.sha256
   chmod 600 "${ARCHIVE_NAME}" manifest.sha256
   ```
6. **Шаг 6. Проверка восстановления из бэкапа (Restore Dry-Run):**
   - Временная тестовая распаковка на старом VPS в `/tmp/verify_restore`:
     ```bash
     mkdir -p /tmp/verify_restore && tar -xzf /var/backups/photodoc/photodoc_cutover.tar.gz -C /tmp/verify_restore
     python scripts/verify_backup_integrity.py --db /tmp/verify_restore/orders.db.cutover --uploads /tmp/verify_restore/uploads
     rm -rf /tmp/verify_restore
     ```
   - Подтверждение: статус `VERDICT: INTEGRITY_CHECK_PASSED`.
7. **Шаг 7. Перенос данных на новый Web VPS:**
   - Передача по зашифрованному SSH/SCP-каналу:
     ```bash
     scp /var/backups/photodoc/photodoc_cutover.tar.gz /var/backups/photodoc/manifest.sha256 user@<NEW_WEB_VPS_IP>:/var/backups/photodoc/
     ```
   - На новом Web VPS:
     ```bash
     cd /var/backups/photodoc && sha256sum -c manifest.sha256
     ```
8. **Шаг 8. Выполнение P0 → P1 миграции:**
   - Распаковка файлов в боевые тома нового сервера:
     ```bash
     docker volume create photodoc_backend-data
     # Распаковка orders.db.cutover в /var/lib/docker/volumes/photodoc_backend-data/_data/orders.db
     # Распаковка uploads в /opt/photodoc/backend/uploads
     ```
   - Выполнение последовательной миграции:
     ```bash
     python backend/migrations/reconcile_payment_schema.py --db /data/orders.db --allow-legacy-unmarked --apply
     python backend/migrations/reconcile_delivery_schema.py --db /data/orders.db --apply
     python scripts/verify_backup_integrity.py --db /data/orders.db --uploads /app/uploads
     ```
9. **Шаг 9. Запуск production-контейнеров на новом VPS без клиентского трафика:**
   - Запуск боевого Docker Compose на новом Web VPS:
     ```bash
     docker compose -p photodoc up -d
     ```
   - Порт приложения слушает только внутренний интерфейс `127.0.0.1:8000` и `127.0.0.1:8080`.
10. **Шаг 10. Проверка readiness, Nginx, HTTPS и сохранности данных:**
    - Проверка локального API: `curl -I http://127.0.0.1:8000/api/health`.
    - Проверка корректности конфигурации Nginx (`nginx -t`).
    - Проверка валидности TLS-сертификатов Let's Encrypt.
11. **Шаг 11. Переключение DNS (CHECKPOINT 2):**
    - Строго после получения подтверждения владельца проекта:
    - Изменение DNS A-записей домена `photodoc-ai.ru` и `www.photodoc-ai.ru` на IP-адрес нового Web VPS.
    - Время жизни TTL предварительно уменьшено до 300 секунд (5 минут).
12. **Шаг 12. Контроль боевых заказов, платежей и доставки:**
    - Мониторинг логов Edge Nginx и backend.
    - Проверка поступления реальных заказов, успешности генерации ссылок Robokassa и приёма платежей.
    - Включение рабочего процесса доставки: `DELIVERY_WORKER_ENABLED=1`.
    - Контроль очереди `delivery_jobs` (отсутствие зависших lease).
    - Старый бэкенд на `79.137.196.14` остаётся выключенным — дублирование записи заказов полностью исключено.
13. **Шаг 13. Регламент безопасного отката (Rollback Procedures):**
    - **Принципы безопасности отката:**
      - **Категорический запрет отката трафика на старый VPS `79.137.196.14` после смены DNS:**
        Старый сервер используется как VPN-нода с Xray/Marzban на порту 443 и не имеет настроенных TLS-сертификатов Let's Encrypt для домена `photodoc-ai.ru`. Попытка направить клиентский HTTPS-трафик на `79.137.196.14` приведёт к обрыву соединений, TLS-ошибкам у пользователей и нарушению работы Marzban.
      - **Предпочтительный сценарий отката:** Возврат к предыдущему стабильному релизу или контейнерному образу **на новом Web VPS** с безусловным сохранением актуальной базы данных `orders.db`, файлов `uploads/` и очереди `delivery_jobs`.
      - **Блокировка новых заказов во время восстановления:** На время восстановительных работ Edge Nginx немедленно переводится в режим обслуживания (`503 Service Unavailable`), возвращая пользователям информативную страницу и строго отклоняя маршрут оформления заказа `POST /api/order`.
      - **Сохраняемые сущности:** При любом откате гарантируется 100% сохранность:
        1. Всех принятых заказов (`orders`).
        2. Всех загруженных клиентских фотографий (`/app/uploads`).
        3. Очереди и состояний задач фоновой доставки (`delivery_jobs`).
        4. Статусов платежей и чеков Robokassa (`paid_at`, `payment_status`, `robokassa_inv_id`).
        5. Данных журнала уведомлений и отправок.
      - **Реалистичные метрики RPO/RTO:** Отказ от необоснованных обещаний мгновенного «RPO=0 за 30 секунд». Время восстановления (RTO) при контролируемом откате составляет 5–15 минут в режиме обслуживания с сохранением всех заказов, принятых новым сервером до включения режима обслуживания.

    - **Сценарий 13.A: Откат ДО переключения DNS (Шаги 4–10):**
      - Проблема обнаружена на этапе тестирования нового VPS до изменения DNS-записей.
      - Клиентский трафик ещё не направлялся на новый сервер.
      - Действие: перезапуск бэкенда на старом сервере (`docker compose -p photodoc start backend`).
      - Восстановление быстрое, данные не расходились.

    - **Сценарий 13.B: Откат ПОСЛЕ переключения DNS (Шаг 11+):**
      - Проблема обнаружена после переключения публичного DNS, когда новый сервер уже принимал боевые заказы.
      - Пошаговый алгоритм:
        1. **Включение режима обслуживания:** Активировать в Edge Nginx нового VPS блокировку создания заказов:
           ```nginx
           location /api/order {
               return 503 '{"status":"error","detail":"Service is temporarily under maintenance. Please try again shortly."}';
           }
           ```
           `nginx -s reload`. Новые заказы не принимаются, состояние базы данных зафиксировано.
        2. **Проверка совместимости схемы БД (Schema Compatibility Gate):**
           Перед переключением контейнера обязательно проверяется совместимость текущей базы данных с кодом целевого отката:
           ```bash
           docker run --rm -v photodoc_backend-data:/data:ro photodoc-backend:rollback-target \
             python -c "import main; assert main.database_readiness_ok(); assert main.delivery_schema_ready()"
           ```
           - Если проверка не пройдена (несовместимая деструктивная миграция): **откат отклоняется (`ROLLBACK_ABORTED_SCHEMA_INCOMPATIBLE`)**, система остаётся в режиме обслуживания для прямого исправления (forward-fix), предотвращая повреждение БД.
           - Если совместимость подтверждена: выполняется переход к шагу 3.

        3. **Переключение контейнера на закреплённый образ отката:**
           Пересоздание контейнера с закреплённым образом через `--force-recreate` (без удаления томов `photodoc_backend-data` и `uploads`):
           ```bash
           docker tag photodoc-backend:rollback-target photodoc-backend:latest
           docker compose -p photodoc up -d --force-recreate --no-deps backend
           ```
        4. **Верификация целостности базы и очереди:**
           ```bash
           python scripts/verify_backup_integrity.py --db /data/orders.db --uploads /app/uploads
           ```
        4. **Снятие режима обслуживания:** Возврат штатной конфигурации Nginx и возобновление приёма клиентских заказов.

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

> **Детальный реестр закупок и сервисов:** См. полный документ [LAUNCH_PROCUREMENT_REGISTER.md](LAUNCH_PROCUREMENT_REGISTER.md).

| Пункт | Фактический статус | Требуемое действие владельца |
| :--- | :--- | :--- |
| **Домен `photodoc-ai.ru`** | **Не зарегистрирован** в реестре TCI/RIPN (не просто пустая DNS-зона, а отсутствие регистрации объекта) | Зарегистрировать домен у аккредитованного регистратора (Reg.ru, Beget, RU-CENTER, ~200–300 руб/год). |
| **DNS-делегирование** | Отсутствует | Настроить NS (Yandex Cloud DNS / DNS регистратора) и создать A-записи на IP нового сервера. |
| **Новый Web VPS** | Не заказан | Арендовать сервер в РФ (1–2 vCPU, 2–4 GB RAM, Ubuntu 24.04 LTS). Реальные рыночные цены: Timeweb Cloud (~290–450 ₽/мес), Beget (~250–400 ₽/мес), Selectel (~400–600 ₽/мес), FirstVDS (~300–500 ₽/мес). *Абсолютные гарантии 100% SLA провайдерами не предоставляются, ориентир 99.9%.* |
| **Robokassa** | Требуются боевые ключи | Предоставить `MerchantLogin`, `Password1`, `Password2`. В Staging используется `ROBOKASSA_IS_TEST=1` с тестовыми картами. |
| **Яндекс.Диск** | Требуется OAuth-токен | Сгенерировать токен приложения Яндекс с доступом к Диску. |
| **SMTP Яндекса** | Требуется пароль приложения | Создать пароль приложения в настройках безопасности Яндекс ID. |
| **Разрешение на перенос** | Требуется подтверждение | Перенос базы данных и переключение DNS выполняются только по отдельной команде. |
