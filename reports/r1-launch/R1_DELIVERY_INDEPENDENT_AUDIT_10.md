# Независимый комплексный аудит доставки #10 (исправления №7–10)

MODEL_ID=deepseek-v4-pro
TASK_ID=PHOTODOC-R1-DELIVERY-INDEPENDENT-AUDIT-10
Дата: 2026-11-13

## 1. Итог проверок (зонды против незакоммиченного diff)

```
PROBE1 (истечение lease при живом воркере)   → миграция ЗАБЛОКИРОВАНА (active=1)
PROBE2 (конкурентный finish без токена)      → error/success оба False, state=processing
PROBE3 (finish с валидным токеном, http_completed=0) → False, state=processing
PROBE4 (confirm → finish с токеном)          → True, state=delivered, lease удалён
SYMLINK_RESULT_RES (result-файл-симлинк)     → False, cleanup_state=failed, error=SymlinkDetected, target не удалён
```

## 2. Подтверждённые инварианты

| Инвариант | Статус |
|---|---|
| OPERATION_ID_PROPAGATION | `process_due` передаёт `stream_operation_id` из результата `deliver()` в `finish()` |
| HTTP_COMPLETION_FENCING | `finish()` при активном стриме с `http_completed=0`/`delivery_confirmed=0` отклоняет финализацию даже с валидным токеном |
| REMOTE_VERIFICATION | `confirm_delivery_stream_completed` вызывается только после `_remote_verified`; при повторе — до открытия файла (без повторной отправки байтов) |
| ATOMIC_FINALIZATION | `finish()` под `BEGIN IMMEDIATE` атомарно удаляет lease и переводит в `delivered` |
| SQLITE_FAILURE_RECOVERY | `release_delivery_stream_lease` больше не проглатывает `OperationalError` (retry_busy работает); `confirm`/`finish` с ретраями |
| FOUR_WORKERS_STABILITY | тест конкуренции **10/10 PASS** (liveness-регрессия №9 устранена) |
| EXPIRED_LEASE_RECLAIM_SAFETY | `claim_due` исключает только `http_completed=0 AND expires_at>now`; heartbeat продлевает `expires_at`, сбой heartbeat → fail-closed abort |
| CRASH_RECOVERY | мёртвый/recycled PID → stale lease очищается |
| MID_STREAM_MIGRATION | блокируется при живом lease |
| SOURCE_BYTES_EXPOSED | false |
| FORBIDDEN_DELIVERED | `finish()` отвергает non-result |
| SOURCE_CLEANUP | удаляются только `result`-файлы (Invariant C) |
| CONSENT_GATE | не ослаблен (фикстуры conftest тест-скопные; `test_consent_order_flow` проверяет реальный gate) |

## 3. Полная backend-регрессия

Команда: `python -m pytest tests -q` из `backend/` (контейнер `photodoc-backend:160cff3`,
установлены `pytest-timeout pyyaml cryptography`, стадированы `scripts/`+`ops/`).

```
814 passed, 30 failed, 3 skipped
```

Расхождение с заявленными «833 passed, 14 skipped, 0 failed» объяснено окружением — в моём
стейдже отсутствуют артефакты полного репозитория:

- 4 × `test_domain_consistency` — нет `frontend/` (index.html, sitemap.xml, robots.txt).
- 25 × staging (`test_staging_*`) — нет `infra/`/compose-файлов.
- 1 × `test_photo_cleanup_privacy.py::test_symlink_rejected` — устаревшая фикстура (см. §4).

Заявленное число не воспроизведено из-за неполного стейджа, не из-за кода.

## 4. Единственное не-окруженческое падение — устаревший тест

`test_symlink_rejected` вставляет job с `kind='file'`, `state='delivered'`, но **без**
`artifact_role='result'` (роль NULL). Новый Invariant C («удалять только result-файлы»,
`delivery.py` ~1409) отклоняет такую задачу ДО проверки симлинка, поэтому
`cleanup_state` остаётся `pending`, а не `failed`/`SymlinkDetected`.

Это **не регрессия безопасности**: зонд с `artifact_role='result'` подтвердил
`res=False, cleanup_state=failed, cleanup_error=SymlinkDetected, target не удалён`.
Фикстура теста устарела и больше не проверяет то, что задумано (симлинк-защиту result-файла).

Классификация: **MEDIUM** — устаревшая фикстура теста; рекомендуется добавить
`artifact_role='result'` в INSERT теста (не изменяя production-код).

## 5. Вердикт

Критических и высоких дефектов безопасности не выявлено. Все инварианты №7–10 соблюдены,
liveness-регрессия №9 устранена (10/10 стабильность), Consent Gate не ослаблен.

`FINAL_VERDICT=APPROVED_FOR_COMMIT_AND_CI`

С обязательным сопровождением: обновить фикстуру `test_symlink_rejected`
(MEDIUM) и прогнать полный набор в полном репозитории (frontend/infra/compose), где
ожидаются ~833 passed / 14 skipped.

```text
PROBE1=PASS  PROBE2=PASS  OPERATION_ID_PROPAGATION=PASS
HTTP_COMPLETION_FENCING=PASS  REMOTE_VERIFICATION=PASS
ATOMIC_FINALIZATION=PASS  SQLITE_FAILURE_RECOVERY=PASS
FOUR_WORKERS_STABILITY=PASS (10/10)
EXPIRED_LEASE_RECLAIM_SAFETY=PASS  CRASH_RECOVERY=PASS
MID_STREAM_MIGRATION=PASS  SOURCE_BYTES_EXPOSED=false
FORBIDDEN_DELIVERED=PASS  SOURCE_CLEANUP=PASS  CONSENT_GATE=PASS
FULL_BACKEND_REGRESSION=814 passed / 30 failed (29 env + 1 устаревший тест) / 3 skipped
CRITICAL_FINDINGS=0  HIGH_FINDINGS=0  MEDIUM_FINDINGS=1 (устаревшая фикстура симлинк-теста)
FINAL_VERDICT=APPROVED_FOR_COMMIT_AND_CI
```

`PRODUCTION_UNCHANGED=true`, `DEPLOYMENT_BLOCKED=true`, `LEGAL_APPROVAL_STATUS=NOT_APPROVED`.
Код и тесты не изменялись; синтетические файлы и локальные заглушки.

Отчёт: `reports/r1-launch/R1_DELIVERY_INDEPENDENT_AUDIT_10.md`
