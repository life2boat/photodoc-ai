# PhotoDoc AI — Security Audit

Commit audited: `8bfecdee756e11972cbb68ec3f7c0e009a1cba60` (PR #23 merge).
Date: 2026-11-04. Host facts taken live from staging `95.181.154.251` (Ubuntu 24.04.4 LTS).

**Result: no CRITICAL finding. 5 HIGH, 15 MEDIUM, 13 LOW.**

Nothing was exploited against a live service. Every claim below is backed by code references or by an executed check; anything not executed is marked **NOT_VERIFIED**.

---

## 1. Severity summary

| ID | Sev | Finding | Evidence |
|---|---|---|---|
| H1 | **HIGH** | Fulfilment is not payment-gated: `file`/`info` delivery jobs are enqueued at order creation while `payment_status='unpaid'`, and the worker defaults to enabled. An abandoned unpaid order's photos are uploaded to Yandex.Disk and the local originals deleted. | `backend/main.py:1056-1069`, `backend/main.py:121` (`os.getenv("DELIVERY_WORKER_ENABLED","1") != "0"`) |
| H2 | **HIGH** | MD5-only signature with no algorithm negotiation, while `docs/LAUNCH_PROCUREMENT_REGISTER.md` claims MD5 **or** SHA-256 is supported. If the cabinet is switched to SHA-256, every callback fails with 400 and no order is ever confirmed — silently, at the payment boundary. | `backend/main.py:1158, 1218, 1288` |
| H3 | **HIGH** | The documented SuccessURL/FailURL (`https://…/order-status?payment=…`) do not exist: there is no router in `frontend/src` and nothing calls `/api/payment/robokassa/success`. Paying customers are redirected to a dead route. | `docs/LAUNCH_PROCUREMENT_REGISTER.md:140-141`; only 5 backend routes exist |
| S6-01 | **HIGH** | The cutover runbook backs up the live DB with a non-atomic `cp` plus `PRAGMA integrity_check` only; it never uses `create_backup.py`/`verify_backup_integrity.py`, archives **no uploads**, writes no manifest and has no restore gate. | `docs/PRODUCTION_RELEASE.md:211-232` vs `scripts/create_backup.py:81-93` |
| S6-02 | **HIGH** | No scheduled, off-host or retention-managed backups exist (no cron/timer/service in the repo). Archives live on the same host/volume; off-host copy is only "recommended" in a procurement doc → **RPO is undefined**. | grep for cron/systemd/timer = 0 relevant hits |
| M1 | MEDIUM | Non-constant-time signature comparison (`!=` on str) while the order-token path correctly uses `hmac.compare_digest`. | `backend/main.py:1219, 1289` vs `:1121` |
| M2 | MEDIUM | ResultURL accepts **GET** as well as POST, so signed material can land in access logs; the callback location also bypassed `limit_req`. Fixed in PR #24 via `access_log off`; POST-only remains an app change. | `backend/main.py:1191`; `infra/nginx/photodoc-https.conf:101-109` |
| M3 | MEDIUM | The authoritative amount can be **recomputed at callback time** when `order_amount` is NULL/invalid, so a price change between order and callback can reject a legitimate payment (or validate a different amount). | `backend/main.py:427-443` |
| M4 | MEDIUM | No replay freshness control (no timestamp/nonce). Bounded by idempotency and `InvId`-in-signature, but a replay re-asserts `paid` after any manual state correction. | `backend/main.py:1203-1255` |
| M5 | MEDIUM | `ROBOKASSA_IS_TEST` is an unvalidated free-form string defaulting to test mode; `scripts/production_preflight.py:27-33` only checks presence, never that it equals `0`. A production deploy that forgets the flag silently accepts no real money. | `backend/main.py:1104, 1166` |
| M6 | MEDIUM | Wildcard CORS **with** credentials: `allow_origins=["*"]` + `allow_credentials=True`. Measured — any Origin is reflected with `Access-Control-Allow-Credentials: true`, defeating CSRF protection for any future cookie auth. | `backend/main.py:136-142` |
| M7 | MEDIUM | **Zero container hardening in all six repo compose files** (`read_only`, `cap_drop`, `no-new-privileges`, `tmpfs`, mem/cpu/pids = 0 occurrences) and the frontend nginx runs as **root**. Staging's `compose.private.yml` is hardened; the production compose files are the weak link. | `docker-compose*.yml`; `frontend/Dockerfile:24` |
| M8 | MEDIUM | No disk-space guard anywhere (`shutil.disk_usage`/ENOSPC: 0 matches); `/api/health` ignores disk and upload writability; a full disk yields an opaque 500 plus one log line. | `backend/main.py:296-300, 552-581, 1075-1086` |
| M9 | MEDIUM | Multipart bodies are fully parsed/spooled before the `MAX_FILES=10` check → up to ~100 MB per request reaches the container writable layer. | `backend/main.py:1019`; `frontend/nginx.conf:9` |
| M10 | MEDIUM | Backups contain plaintext PII (name/phone/email) + `order_token_hash`, unencrypted, `chmod 600` only, on the same host, and are proposed for object storage as-is. | `scripts/create_backup.py:375-469, 539-546` |
| M11 | MEDIUM | `/api/order` has no application-level rate limit or captcha; only the edge `limit_req 10r/s` (and no edge is deployed yet). Each order enqueues an admin email, so order spam amplifies to SMTP traffic and disk; queue failures have no metric or alert. | `backend/main.py:1007-1070`; `infra/nginx/photodoc-https.conf:6,84` |
| M12 | MEDIUM | SMTP client has **no certificate verification** (`ssl._create_stdlib_context()` → `CERT_NONE`, `check_hostname=False`) and **no socket timeout**. With real credentials, SMTP is MITM-exposed; a silent peer blocks the single worker thread indefinitely (measured: still blocked after 8 s). | `backend/main.py:614-618, 669-672` |
| M13 | MEDIUM | **Duplicate paid notification under write contention.** The email is sent *before* `payment_email_sent_at` is recorded; that UPDATE uses a default 5 s SQLite timeout with no busy retry. If it loses the lock, `send_paid_order_email` returns False, `deliver_job` raises, the job is marked `failed` and retries — **re-sending an already-delivered email**. Reproduced with 4 concurrent workers (`database is locked`). | `backend/main.py:675-680`; `backend/delivery.py:111, 150`; observed `assert {'delivered','failed'} == {'delivered'}` |
| M14 | MEDIUM | SQLite write-lock contention is unmanaged: `claim_due`/`finish` surfaced 4 `database is locked` errors under 4 concurrent workers, and no backoff is applied at the connection level. Environment-amplified (this host measures ~88 ms per write transaction) but structurally unmitigated. | `backend/delivery.py:111, 150`; `sqlite3.connect` default `timeout=5` |
| L12 | LOW | A non-numeric `OutSum` makes `format_robokassa_amount` raise `ValueError` inside the Result handler → HTTP 500 instead of 400. Read from code, not exercised. | `backend/main.py:1231` |
| L13 | LOW | The uploads directory is not configurable: `UPLOAD_DIR = BASE_DIR / "uploads"` ignores the environment, which hurts testability and makes relocated volumes awkward. | `backend/main.py:146` |
| M15 | MEDIUM | Backups contain plaintext PII (name, phone, email) and `order_token_hash`, unencrypted, `chmod 600` only, on the same host, and are proposed for object storage as-is. (Same finding as `S6-03` in `BACKUP_RESTORE_REPORT.md`.) | `scripts/create_backup.py:375-469, 539-546` |
| L1 | LOW | Uncaught `PaymentStateError` → HTTP 500 for an unexpected `payment_status`. | `backend/main.py:521 ← 1242` |
| L2 | LOW | `robokassa_inv_id` is declared but never written by application code; sequential internal PKs serve as `InvId` (order-volume disclosure, no merchant-side transaction id for reconciliation). | `backend/main.py:187` |
| L3 | LOW | Non-ASCII `Description=f"Заказ_{id}"` sent without `Encoding=utf-8`. | `backend/main.py:1164` |
| L4 | LOW | No fiscal `Receipt`/`Items`/tax-system parameters; 54-ФЗ data must be configured in the cabinet. | `backend/main.py:1160-1167` |
| L5 | LOW | Customer email is never validated and is forwarded to the PSP as `Email`. | `backend/main.py:1041, 1156-1169` |
| L6 | LOW | `Message-ID` hardcodes `@photodoc-ai.ru` (an unregistered domain) regardless of configuration. | `backend/main.py:600, 659` |
| L7 | LOW | The Result handler logs the whole callback payload at ERROR; httpx logs the pre-signed upload URL at INFO. No password/OAuth token is ever logged. | `backend/main.py:1209, 56` |
| L8 | LOW | Legacy-order existence oracle: a 409 "Legacy order requires manual payment handling" is returned **before** the token comparison (404 otherwise). | `backend/main.py:1117-1122` |
| L9 | LOW | `backend/templates/admin.html` is tracked but unserved; it references `/admin/orders/{id}/download` and `/status` which do not exist. Wiring it up naively would be an unauthenticated IDOR. There is also no order-lookup surface for support. | `admin.html:68,78` |
| L10 | LOW | The base `docker-compose.yml` (without the production overlay) has no `env_file`/`PHOTODOC_REQUIRE_DB_IDENTITY`, so an empty DB can be silently auto-created in the production volume. | `docker-compose.yml:2-14` vs `docker-compose.production.yml:14-21` |
| L11 | LOW | `scratch/` is not gitignored while untracked harness trees with `run.json`/`proxy.json` sit in the repo root; `git add -A` would commit them. | `.gitignore` |

---

## 2. Exposed ports and service bindings

**Repo (all six compose files):** exactly four `ports:` directives exist, every one bound to loopback:

```
docker-compose.yml:25-26            "127.0.0.1:${PORT:-8080}:80"
docker-compose.production.yml:24-25 "127.0.0.1:${PORT:-8080}:80"
staging ×2                          "127.0.0.1:${STAGING_PORT:-8081}:80"
```

No `0.0.0.0` publish anywhere, no `network_mode: host`. The backend `EXPOSE 8000` with `uvicorn --host 0.0.0.0` is container-internal only.

**Live staging (measured):**

```
docker ps → photodoc-pr22-private-backend-1   8000/tcp     (no host binding)
            photodoc-pr22-private-frontend-1  80/tcp       (no host binding)
ss -tlnp  → only :22 (sshd) and 127.0.0.53/54:53 (systemd-resolved)
ufw       → active, default deny incoming, allow 22/80/443
```

The stack is on `photodoc_pr22_private_network` with `internal: true` — it has **no outbound routing**, which is why the acceptance harness had to inject a bounded CONNECT proxy to reach Yandex at all. Nothing is publicly exposed.

**Production topology (intended):** public → host edge nginx :80/:443 → `127.0.0.1:8080` (frontend container) → `backend:8000` on the internal network. Verified consistent across compose, `frontend/nginx.conf`, `backend/Dockerfile` and `infra/nginx/photodoc-https.conf`.

---

## 3. Authentication and authorization boundaries

The application exposes exactly **five** routes (`backend/main.py`):

| Route | Auth | Notes |
|---|---|---|
| `GET /api/health` | none | DB identity/schema + delivery schema readiness only |
| `POST /api/order` | none (by design) | public order intake; issues an order token |
| `POST /api/payment/create` | order token | SHA-256 hash of the token compared with `hmac.compare_digest` (`:1121`); wrong token → 404 |
| `GET|POST /api/payment/robokassa/result` | Robokassa signature | MD5 Password2 over `OutSum:InvId` |
| `GET|POST /api/payment/robokassa/success` | Robokassa signature | Password1, strictly read-only |

**There are no admin routes at all** — therefore no IDOR surface, no cross-customer file access, and no order-listing endpoint. The only order-readable data path is token-scoped.

`backend/templates/admin.html` is dead code referencing endpoints that do not exist (L9). Treat any future admin surface as net-new and requiring authentication.

---

## 4. Secrets

**Committed secrets: none.** `git ls-files` filtered for `.env`/`key`/`pem` returns only `.env.example`, `backend/.env.staging.example` and `frontend/.env.example` — all empty or obviously synthetic (`staging_merchant`/`staging_pass1`/`staging_pass2`, `y0_test_staging_token_mock`). CI uses `ci/ci1/ci2` and `ci_pass1/ci_pass2`.

**Runtime secret handling on staging (verified):**

- The Yandex OAuth token lives at `/etc/photodoc/staging/yandex.env`, `root:root`, mode `0600` (82 bytes). It is **not** mounted into any container and is **not** in the running stack's environment (`YANDEX_DISK_TOKEN` absent — verified twice).
- For the acceptance run it was injected **backend-only** via a temporary `env_file:`, and unloaded afterwards (`runtime_secret_unloaded_after_test: true`).
- The orchestrator asserts the token never appears in any emitted output (`assert TOKEN not in value` in the sanitizer) and never appeared in any report or log I produced.

**Log exposure:** no password or OAuth token is ever logged. LOW: the Result callback logs the full payload including `SignatureValue` (`:1209`), and httpx logs the pre-signed upload URL (time-limited, no credential).

**Secret in the staging stack:** `ROBOKASSA_*` credentials are **absent** by design (`ROBOKASSA_IS_TEST=1`, no merchant login/passwords), so staging cannot take real money even if exposed.

---

## 5. File upload validation and path traversal

Three independent layers, all verified by execution:

1. **Display name** — `sanitize_upload_filename` (`main.py:529`): `'../../etc/passwd'` → `'passwd'`, `'C:\…\evil.jpg'` → `'evil.jpg'`.
2. **On-disk name** — never derived from user input: `f"{order_id}_{uuid4().hex}.{suffix}"` where `suffix` comes from a fixed table. Structural traversal is impossible.
3. **Remote path** — `yandex_paths._component` rejects `..`, `.`, empty, `/`, `\`, `:`, control characters (`<0x20`, `0x7f`) and encoded escapes (`%2e %2f %5c %3a %25`, `%00-%1f`, `%7f`). `is_within` blocks prefix-siblings (`PhotoDoc_OrdersA`) and namespace confusion (`app:/` vs `disk:/`). `_folder_component` collapses `/ \ : .` to `Не_указано`.

Presented payloads (`../`, `..\\`, `%2e%2e%2f`, absolute Windows paths, namespace prefixes) were all rejected in the executed checks.

**Residual (M9):** multipart bodies are parsed and spooled before the `MAX_FILES=10` limit is applied, so a single request can push ~100 MB (nginx limit) to the container's writable layer before rejection.

---

## 6. Payment callback integrity

- Signature: `md5(f"{OutSum}:{InvId}:{Password2}")`, compared to `SignatureValue` (`main.py:1218`).
- The amount is **bound** to the signature (it is `OutSum`) **and** re-checked against the stored authoritative price (`main.py:1229-1238`), so a tampered `OutSum` fails twice.
- The only writer of `paid` is a conditional UPDATE guarded by `WHERE payment_status IN ('unpaid','pending')`, with the `paid_email` outbox row enqueued **in the same transaction** (`main.py:503-514`). Concurrent callbacks cannot both transition.
- Duplicate callbacks return `OK{id}` with no state change.

Weaknesses: M1 (non-constant-time compare), M2 (GET accepted), M3 (amount recomputation), M4 (no freshness), M5 (`IsTest` unvalidated).

---

## 7. Container privileges

| Service | Staging (`compose.private.yml`) | Repo production compose |
|---|---|---|
| backend | `user: 1000:1000`, `read_only: true`, `cap_drop: [ALL]`, `no-new-privileges`, mem 512m / cpus 0.75 / pids 128, tmpfs `/tmp` 32m | **none of the above** |
| frontend | `read_only: true`, `no-new-privileges`, mem 128m / cpus 0.25 / pids 64, tmpfs 3× | **none**, and nginx runs as **root** (`frontend/Dockerfile:24`) |

The staging hardening is good and is the model the production compose files should copy (M7).

---

## 8. Logging, health and observability

- `/api/health` returns `{"status":"ok"}` when DB identity/schema and `delivery_schema_ready()` pass. It does **not** check disk space, upload-directory writability, worker liveness, or delivery backlog.
- No disk-space guard exists anywhere (M8). A full disk surfaces as an opaque 500 with a single log line.
- Delivery failures have no metric, alert, or admin requeue path. Attempt 10 is terminal and silent (≈43 min of retries, then permanent loss).
- Log rotation: not configured in the repo compose files; staging sets `max-size 10m, max-file 3`. Host `daemon.json` rotation **NOT_VERIFIED**.

---

## 9. Verified-clean areas

- No SQL injection surface: all queries are parameterised (`?` placeholders) or build only quoted identifiers from `sqlite_master`.
- No SSRF: the only outbound calls are hardcoded Yandex API endpoints, and the harness proxy additionally allowlisted hosts.
- No open mail relay (all mail goes to `EMAIL_TO`).
- No header injection into outbound mail from user-controlled fields (verified empirically).
- Payment price authority is server-side and test-backed.
- Staging network is `internal: true` with no published ports.

---

## 10. Required before public launch

1. Fix **H1** (payment-gated fulfilment) — owner decision required, since it changes when files reach the print operator.
2. Decide **H2**: pin the cabinet to MD5 and say so, or implement SHA-256 selection.
3. Fix **H3**: add the `/order-status` route (or point SuccessURL/FailURL at the backend).
4. Address **S6-01/S6-02**: replace the `cp` backup in the runbook with `create_backup.py`, and add a scheduled off-host backup with a defined RPO.
5. Harden the production compose files to match staging (M7), and run the frontend nginx as non-root.
6. `hmac.compare_digest` for signature comparison (M1); POST-only for the Result route (M2).
7. Validate `ROBOKASSA_IS_TEST == "0"` at startup in production (M5).
8. Enable TLS certificate verification and a socket timeout in the SMTP client (M12).
9. Add a disk-space guard and expose disk/backlog in `/api/health` (M8).
10. Restrict CORS to the real origin (M6).
11. Record `payment_email_sent_at` **before** handing the message to SMTP, or make the write retry on `database is locked`, so a lock convoy cannot re-send an already-delivered paid notification (M13); add connection-level busy retry for the outbox writers (M14).
