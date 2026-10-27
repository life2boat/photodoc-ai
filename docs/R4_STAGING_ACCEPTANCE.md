# PhotoDoc AI — R4.0 Staging Acceptance & Safe Delivery Runbook

## 1. Overview & Verification Scope

This runbook establishes the verified operating procedure for executing the **P1 Automatic Customer Photo Cleanup & Isolated Delivery Acceptance Rehearsal**.

The required delivery and privacy lifecycle is:
$$\text{Upload} \longrightarrow \text{Persist Delivery Job} \longrightarrow \text{Upload to Staging Destination} \longrightarrow \text{Verify Remote SHA-256 \& Size} \longrightarrow \text{Commit Delivered State} \longrightarrow \text{Unlink Local File} \longrightarrow \text{Verify Cleanup State}$$

### Critical Distinction: Mocked Local Rehearsal vs. Live External Acceptance

| Dimension | Mocked Local Acceptance Rehearsal | Live External Staging Acceptance |
| :--- | :--- | :--- |
| **Execution Environment** | Local developer machine / CI runner | Dedicated Staging VPS or local Docker stack |
| **Yandex Disk Interaction** | Deterministic mock HTTP server (`MockYandexDiskTransport`) | Real Yandex Disk REST API (`https://cloud-api.yandex.net`) |
| **OAuth Token Requirement** | No live token needed; simulated synthetic tokens | **Owner-provisioned dedicated staging token** required |
| **Remote Destination** | Mock in-memory directory | Dedicated remote folder `PhotoDoc_Staging_R4/` |
| **Production Impact** | **Zero** | **Zero** (Isolated root; `PhotoDoc_Orders` rejected) |
| **Current Status** | **PASS** (100% automated regression coverage) | **BLOCKED** pending owner token provisioning |

---

## 2. Prerequisites and Owner-Provisioned Credentials

Before running a **live** staging delivery acceptance test:
1. **Never use the production OAuth token** in staging.
2. **Never configure production root** `PhotoDoc_Orders` in staging.
3. Obtain an independent Yandex OAuth token dedicated specifically to staging.
4. Verify the following staging configuration files are in place:
   - `docker-compose.staging.yml` (Default safe configuration, worker disabled).
   - `docker-compose.staging.delivery.yml` (Opt-in delivery override, worker enabled).
   - `backend/.env.staging.example` (Reference configuration template).

### Safe Token Generation & Isolation Guidelines
- Create a dedicated Yandex account or application in Yandex OAuth (`https://oauth.yandex.ru/client/new`).
- Required scopes:
  - `cloud_api:disk.write` (to upload staging test files).
  - `cloud_api:disk.read` (to verify remote checksums and sizes).
  - `cloud_api:disk.info` (to verify directory existence).
- **Prohibited scopes:** Do not grant administrative account permissions or access beyond Disk REST API.

---

## 3. Preflight Safety Validation

Before launching containers or running delivery workers, run the automated staging delivery preflight checker:

```bash
python scripts/verify_staging_delivery_preflight.py --compose-staging docker-compose.staging.yml --compose-override docker-compose.staging.delivery.yml --check-live-token
```

### Preflight Verification Output Contract
- **Default Worker Disabled:** Confirms `DELIVERY_WORKER_ENABLED="0"` in `docker-compose.staging.yml`.
- **Volume & Root Isolation:** Confirms isolated volumes (`photodoc_staging_backend-data`, `photodoc_staging_uploads`) and staging root (`PhotoDoc_Staging`).
- **Override Configuration:** Confirms `docker-compose.staging.delivery.yml` strictly requires `${YANDEX_DISK_STAGING_TOKEN:?...}`.
- **Token Safety:** Detects and blocks known placeholders (`y0_test_staging_token_mock`, `changeme`, `placeholder`, empty values).
- **Console Privacy:** Proves zero secrets, tokens, or authorization headers are printed to stdout.

---

## 4. Starting the Isolated Staging Stack

### Mode A: Safe Default Staging (Recommended for Web UI & Health Testing)
In default mode, the background delivery worker remains strictly disabled (`DELIVERY_WORKER_ENABLED=0`):

```bash
docker compose -p photodoc-staging -f docker-compose.staging.yml config
docker compose -p photodoc-staging -f docker-compose.staging.yml up -d --build
```

Verify backend and database health:
```bash
curl -f http://127.0.0.1:8081/api/health
# Expected response: {"status": "ok"}
```

### Mode B: Opt-In Staging Delivery Acceptance (Authorized Acceptance Rehearsal ONLY)
When an owner-provisioned token is available in `YANDEX_DISK_STAGING_TOKEN`:

```bash
export YANDEX_DISK_STAGING_TOKEN="<OWNER_PROVISIONED_STAGING_OAUTH_TOKEN>"
export YANDEX_DISK_STAGING_ROOT="PhotoDoc_Staging_R4"

# Validate configuration without running:
docker compose -p photodoc-staging -f docker-compose.staging.yml -f docker-compose.staging.delivery.yml config

# Start staging stack with delivery worker enabled:
docker compose -p photodoc-staging -f docker-compose.staging.yml -f docker-compose.staging.delivery.yml up -d
```

> [!WARNING]
> If `YANDEX_DISK_STAGING_TOKEN` is unset or empty, `docker compose` will fail closed immediately with:
> `Error: YANDEX_DISK_STAGING_TOKEN is required for staging delivery acceptance`

---

## 5. Submitting a Synthetic Test Photo

Generate a test image and submit via the staging API:

```bash
# 1. Create a synthetic test JPEG with known payload
python -c "
import hashlib
data = b'\xff\xd8\xff' + b'STAGING-ACCEPTANCE-REHEARSAL' * 16 + b'\xff\xd9'
with open('/tmp/test_staging_photo.jpg', 'wb') as f:
    f.write(data)
print('SHA-256:', hashlib.sha256(data).hexdigest())
print('Size:', len(data))
"

# 2. Submit order to staging backend
curl -X POST http://127.0.0.1:8081/api/order \
  -F "name=Staging Tester" \
  -F "phone=+79998880011" \
  -F "email=staging@example.invalid" \
  -F "format=10x15" \
  -F "paper=Глянцевая" \
  -F "crop=Без полей" \
  -F "comment=R4 staging rehearsal" \
  -F "files=@/tmp/test_staging_photo.jpg"
```

Response format:
```json
{"ok": true, "message": "Заказ успешно создан", "order_id": 1, "order_token": "...", "amount": "250.00"}
```

---

## 6. Verifying Remote Upload & Post-Delivery Local Cleanup

### Step 1: Verify Delivery Outbox State in SQLite
Inspect the staging database inside the container:
```bash
docker exec photodoc-staging-backend sqlite3 /data/orders.db "
SELECT id, order_id, kind, state, content_sha256, content_size, cleanup_state, cleanup_phase, local_deleted_at
FROM delivery_jobs WHERE order_id = 1;
"
```

Expected transitions:
1. Immediately upon creation:
   - `state='pending'`, `cleanup_state='pending'`, `cleanup_phase='pending'`, `local_deleted_at=NULL`.
2. After delivery worker processes the job:
   - Remote verification passes.
   - `state='delivered'` committed.
   - Local file unlinked from `/app/uploads/`.
   - `cleanup_state='completed'`, `cleanup_phase='deleted'`, `local_deleted_at` populated with UTC ISO timestamp.

### Step 2: Confirm Remote File on Yandex Disk
Query the Yandex Disk API using the staging token:
```bash
curl -s -H "Authorization: OAuth ${YANDEX_DISK_STAGING_TOKEN}" \
  "https://cloud-api.yandex.net/v1/disk/resources?path=PhotoDoc_Staging_R4/Заказ_1_Staging_Tester/10x15_Глянцевая_Без_полей/&fields=items.name,items.sha256,items.size,items.type"
```

Verify that the remote `sha256` and `size` match the exact values recorded in `delivery_jobs`.

### Step 3: Confirm Local Photo is Deleted
Inspect the uploads volume inside the container:
```bash
docker exec photodoc-staging-backend ls -la /app/uploads/
# The local customer photo 1_<uuid>.jpg must be absent.
# Only active temporary files (if any) or undelivered jobs may reside in /app/uploads/.
```

---

## 7. Verifying Failure Isolation and Source Preservation

To prove that unverified or failed deliveries **never delete customer photos**:

1. Submit a test order while disconnecting the staging server from external network or disabling Yandex API access.
2. The delivery worker will encounter an HTTP connection error or timeout.
3. Inspect SQLite:
   - `state='failed'` with exponential backoff timestamp `next_attempt_at`.
   - `cleanup_state='pending'`, `cleanup_phase='pending'`, `local_deleted_at=NULL`.
4. Inspect filesystem:
   - The original source photo **remains intact** in `/app/uploads/`.
5. Reconnect network: the worker claims the job on retry, verifies remote upload, commits delivery, and only then cleans up.

---

## 8. Running the Backup Integrity Verifier

Run the automated backup verifier against the staging volume:

```bash
python scripts/verify_backup_integrity.py \
  --db /path/to/extracted/staging/orders.db \
  --uploads /path/to/extracted/staging/uploads \
  --json
```

Contract criteria for PASS:
- `application_id_valid: true` (Application ID: `1346650441` / `PDAI`).
- `sqlite_integrity: "ok"`.
- `p1_cleaned_files >= 1`.
- `p1_residual_uncleaned_files: []` (no forgotten customer photos left behind).
- `p1_delivered_uncleaned_anomalies: []`.
- `p1_unexplained_missing_sources: []`.
- `errors: []`.

---

## 9. Safe Teardown

To shut down and wipe the staging environment without affecting production:

```bash
docker compose -p photodoc-staging -f docker-compose.staging.yml -f docker-compose.staging.delivery.yml down -v
```

This removes:
- `photodoc-staging-backend` and `photodoc-staging-frontend` containers.
- `photodoc_staging_backend-data` volume.
- `photodoc_staging_uploads` volume.
- `photodoc_staging_network` bridge network.

**Production data (`photodoc_backend-data`, `orders.db`, VPS `79.137.196.14`) remains 100% isolated and untouched.**

---

## 10. Verification Matrix & Current Status

| Requirement / Check | Tool / Test Method | Result | Notes |
| :--- | :--- | :--- | :--- |
| **Default Worker Disabled** | `docker-compose.staging.yml`, `verify_staging_delivery_preflight.py` | **PASS** | `DELIVERY_WORKER_ENABLED="0"` by default |
| **Opt-in Delivery Override** | `docker-compose.staging.delivery.yml` | **PASS** | Requires `${YANDEX_DISK_STAGING_TOKEN:?...}` |
| **Remote Root Isolation** | `main.py`, `verify_staging_delivery_preflight.py` | **PASS** | `PhotoDoc_Staging_R4`; `PhotoDoc_Orders` strictly rejected |
| **Volume Isolation** | Docker Compose YAML verification | **PASS** | Named volumes `photodoc_staging_*` only |
| **Deterministic Local Rehearsal** | `backend/tests/test_staging_acceptance_harness.py` | **PASS** | 22 tests passing (Scenarios A, B, C, D) |
| **Post-Commit Unlink Guarantee** | `test_scenario_a_successful_delivery_and_cleanup_lifecycle` | **PASS** | Unlink verified strictly after `delivered` commit |
| **Failure Preserves Local Source** | `test_scenario_b_remote_verification_failures_preserve_local_source` | **PASS** | Verified across 9 fault injection scenarios |
| **Crash Recovery & Lease Fencing** | `test_scenario_c_*` | **PASS** | Durable intent recovery & stale lease fencing verified |
| **Zero DELETE to Yandex Disk** | `test_scenario_d_no_http_delete_request_to_yandex_disk` | **PASS** | Verified 0 DELETE requests issued |
| **Zero PII / Token Leakage** | `test_scenario_d_no_pii_tokens_or_headers_leak_in_logs` | **PASS** | Logs verified free of secrets, headers, and PII |
| **Backup Verifier Compatibility** | `scripts/verify_backup_integrity.py` | **PASS** | Rehearsal accepted with 0 errors / 0 violations |
| **Live Yandex Staging Acceptance** | External Yandex Disk REST API | **BLOCKED** | Blocked on owner staging token provisioning |
| **Production Server Isolation** | Runbook & preflight guards | **PASS** | VPS `79.137.196.14` completely untouched |
