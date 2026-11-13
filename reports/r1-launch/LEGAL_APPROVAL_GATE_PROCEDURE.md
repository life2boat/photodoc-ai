# Регламент процедуры юридического утверждения документов (Legal Approval Gate)

## 1. Назначение и текущее состояние

В рамках архитектуры PhotoDoc AI действует принцип **Fail-Closed Legal Gate**:
- В модуле `backend/consents.py` все юридические документы по умолчанию имеют статус `approved: False`.
- Обязательные документы (`service_terms` — Публичная оферта и `personal_data_processing` — Согласие на обработку персональных данных) имеют правовой статус:
  `legal_status: "DRAFT_PENDING_OWNER_LEGAL_FINALIZATION"`.
- Вторичные согласия (`third_party_images`, `minor_images`, `cross_border_transfer`) имеют статус `BLOCKED_PENDING_LEGAL_APPROVAL`.
- Серверный эндпоинт `POST /api/order` строго валидирует статус утверждения документов через `consents.is_document_approved()`. При отсутствии утверждения хотя бы одного обязательного документа запрос немедленно отклоняется с кодом `400 Bad Request` (`"Consent for <purpose> is blocked pending legal approval"`).
- При отклонении заказа:
  - 0 записей создается в таблице `orders`.
  - 0 записей создается в `consent_records`.
  - 0 файлов сохраняется на сервере (загружаемые файлы не сохраняются на диск).
  - 0 платежей инициируется в платёжном шлюзе.
- В продакшн-коде отсутствуют какие-либо тестовые обходы (test bypass) или автоматические переопределения `approved: False`.

---

## 2. Пошаговый регламент ввода документов в эксплуатацию

Перед открытием публичного приема заказов владелец проекта и юридический консультант обязаны выполнить следующий регламент:

### Шаг 1. Финализация текстов документов
1. Провести правовую экспертизу текстов документов:
   - `public-preview/offer.html` (Публичная оферта)
   - `public-preview/privacy.html` (Политика обработки персональных данных)
2. Внести необходимые юридические реквизиты исполнителя (ИНН, ОГРНИП, контакты оператора персональных данных).

### Шаг 2. Расчет канонических криптографических хэшей
Для предотвращения атак подмены текста документа и обеспечения юридической доказательной базы рассчитывается криптографический SHA-256 хэш файла с обязательной нормализацией переводов строк (LF normalization, UTF-8):

```bash
# Вычисление канонического SHA-256 для оферты
python -c "import consents; print('OFFER HASH:', consents.canonical_content_hash(open('public-preview/offer.html', 'rb').read()))"

# Вычисление канонического SHA-256 для согласия на обработку ПД
python -c "import consents; print('PRIVACY HASH:', consents.canonical_content_hash(open('public-preview/privacy.html', 'rb').read()))"
```

### Шаг 3. Документированное утверждение (Sign-off)
Оформить письменное или электронное решение владельца продукта об утверждении редакции документов с фиксацией:
- Идентификатора версии (например, `2026-11-15` или семантической версии `1.0.0`).
- Точных SHA-256 хэшей файлов.
- Даты и времени вступления в силу.

### Шаг 4. Обновление конфигурации в кодовой базе
В файле `backend/consents.py`:
1. Обновить константы канонических хэшей:
   ```python
   CANONICAL_OFFER_HASH = "<рассчитанный_хэш_оферты>"
   CANONICAL_PRIVACY_HASH = "<рассчитанный_хэш_политики>"
   ```
2. Для целей `PURPOSE_SERVICE` и `PURPOSE_PERSONAL_DATA` установить:
   ```python
   "version": "<утвержденная_версия>",
   "pinned_hash": CANONICAL_OFFER_HASH,  # (или CANONICAL_PRIVACY_HASH)
   "approved": True,
   "legal_status": "APPROVED",
   ```

### Шаг 5. Контрольная проверка (Preflight Readiness Check)
Запустить проверку готовности серверных шлюзов:
```bash
python -c "import consents; assert consents.mandatory_documents_approved(), 'Mandatory documents not approved!'"
python -c "import consents; assert consents.verify_document_hash(consents.PURPOSE_SERVICE, consents.CANONICAL_OFFER_HASH)"
python -c "import consents; assert consents.verify_document_hash(consents.PURPOSE_PERSONAL_DATA, consents.CANONICAL_PRIVACY_HASH)"
```

### Шаг 6. Активация на продакшн-сервере
Только после прохождения шагов 1–5:
1. Выполнить плановый деплой обновленного контейнера бэкенда.
2. Убедиться, что в `.env` продакшн-сервера выставлен флаг:
   `CONSENT_FEATURE_ENABLED=1`.
3. Запустить smoke test создания заказа с фиксацией `consent_records`.
