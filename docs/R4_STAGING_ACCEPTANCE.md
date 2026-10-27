# PhotoDoc AI — R4.1 Staging Acceptance & Safe Delivery Runbook

## 1. Overview & Verification Scope

This runbook establishes the verified operating procedure for executing the **P1 Automatic Customer Photo Cleanup & Isolated Delivery Acceptance Rehearsal**.

The required delivery and privacy lifecycle is:
$$\text{Upload} \longrightarrow \text{Persist Delivery Job} \longrightarrow \text{Upload to Staging Destination} \longrightarrow \text{Verify Remote SHA-256 \& Size} \longrightarrow \text{Commit Delivered State} \longrightarrow \text{Unlink Local File} \longrightarrow \text{Verify Cleanup State}$$

### Critical Distinction: Mocked Local Rehearsal vs. Live External Acceptance

| Dimension | Mocked Local Acceptance Rehearsal | Live External Staging Acceptance |
| :--- | :--- | :--- |
| **Execution Environment** | Local developer machine / CI runner | Dedicated Web VPS (`NOT_PURCHASED`) |
| **Yandex Disk Interaction** | Deterministic mock HTTP transport (`MockYandexDiskTransport`) | Real Yandex Disk REST API (`https://cloud-api.yandex.net`) |
| **OAuth Token Requirement** | No live token needed; simulated synthetic tokens | **Owner-provisioned dedicated staging token** required |
| **Remote Destination** | Mock in-memory directory | Dedicated remote folder `PhotoDoc_Staging_R4/` |
| **Production Impact** | **Zero** | **Zero** (Isolated root; `PhotoDoc_Orders` rejected) |
| **Current Status** | **PASS** (100% automated regression coverage across 22 acceptance scenarios) | **BLOCKED** pending owner provisioning of VPS and token |

---

## 2. Live Staging Deployment Gate & Required Owner Provisions

> [!CRITICAL]
> **FAIL-CLOSED STAGING DEPLOYMENT GATE:**
> No live staging deployment, external container startup, or network calls to external storage may occur until **ALL FIVE** owner provisions are fulfilled. If any provision is missing, live staging deployment is strictly **BLOCKED**.

### 2.1 The 5 Mandatory Owner Provisions (Deployment Gate Checklist)

| Provision | Requirement | Owner Action Needed | Current Status |
| :--- | :--- | :--- | :---: |
| **1. Separate Web VPS** | Autonomous server physically distinct from Marzban `79.137.196.14` (Economy: 1 vCPU, 2 GB RAM, 25–30 GB NVMe with 2 GB swap, or Standard: 2 vCPU, 4 GB RAM) | Purchase VPS from Timeweb Cloud or Beget according to `LAUNCH_PROCUREMENT_REGISTER.md` | `NOT_PURCHASED` **(BLOCKED)** |
| **2. SSH Access for Deployment** | Dedicated IP, SSH port (22), unprivileged deployment user (`deployer` with sudo rights), and authorized SSH public key | Provide connection endpoint and add deployment public key | `MISSING` **(BLOCKED)** |
| **3. Dedicated Staging OAuth Token** | Independent Yandex OAuth token with scopes `cloud_api:disk.write`, `cloud_api:disk.read`, `cloud_api:disk.info`. **Never use the production token.** | Create staging app in Yandex OAuth (`https://oauth.yandex.ru/client/new`) and generate token | `MISSING` **(BLOCKED)** |
| **4. Remote Directory `PhotoDoc_Staging_R4`** | Dedicated root folder created in staging Yandex Disk. Preflight guards fail closed if targeting `PhotoDoc_Orders` | Create folder `PhotoDoc_Staging_R4` in staging Yandex Disk | `MISSING` **(BLOCKED)** |
| **5. Explicit Deployment Authorization** | Formal go-ahead from project owner to deploy and execute tests on the staging target | Issue explicit confirmation to begin staging deployment | `BLOCKED` |

### 2.2 Next Actions Required from Project Owner

To unblock the live staging acceptance rehearsal:
1. **Procure Separate Web VPS:** Select either the Minimum Viable shape (1 vCPU, 2 GB RAM, 25–30 GB NVMe, ~290–350 ₽/mo) or Safer Alternative (2 vCPU, 4 GB RAM, 40 GB NVMe, ~540–590 ₽/mo) from Timeweb Cloud or Beget.
2. **Configure SSH Access:** Deploy clean Ubuntu 24.04 LTS, add non-root deployment user, and provide IP address and SSH credentials.
3. **Register Staging OAuth Token:** In `https://oauth.yandex.ru/client/new`, register a staging app, grant Disk write/read scopes, obtain OAuth token, and create folder `PhotoDoc_Staging_R4` in Yandex Disk.
4. **Issue Authorization:** Authorize the deployment operator to run the staging setup protocol.

### 2.3 Strict Privacy & Zero-Secret Disclosure Contract

- **Zero Secret Leakage:** Never print, log, commit, or request real OAuth tokens, passwords, or customer PII in GitHub repositories, pull request comments, CI artifacts, or status reports.
- **Environment Isolation:** Secrets must only be injected via local server environment variables (`YANDEX_DISK_STAGING_TOKEN`) on the staging host.

---

## 3. Operator Execution Protocol (Post-Provisioning Verification Runbook)

Once the owner provisions all 5 prerequisites, the operator must execute the following sequential verification protocol:

### Step 1: Preflight Safety Validation on Staging Host
Connect to the VPS and run the automated preflight checker:

```bash
# On the staging Web VPS:
cd /opt/photodoc
python scripts/verify_staging_delivery_preflight.py \
  --compose-staging docker-compose.staging.yml \
  --compose-override docker-compose.staging.delivery.yml \
  --check-live-token
```

The preflight tool enforces:
- `DELIVERY_WORKER_ENABLED="0"` by default in `docker-compose.staging.yml`.
- Isolated named volumes (`photodoc_staging_db`, `photodoc_staging_uploads`).
- Isolated remote root (`PhotoDoc_Staging_R4`), strictly rejecting `PhotoDoc_Orders`.
- Fail-closed validation if `YANDEX_DISK_STAGING_TOKEN` is missing, placeholder, or empty.

### Step 2: Inject Credentials & Validate Compose Configuration

```bash
# Inject owner-provisioned token into staging shell session:
export YANDEX_DISK_STAGING_TOKEN="<OWNER_PROVISIONED_STAGING_OAUTH_TOKEN>"
export YANDEX_DISK_STAGING_ROOT="PhotoDoc_Staging_R4"

# Validate compose configuration without launching containers:
docker compose -p photodoc-staging -f docker-compose.staging.yml -f docker-compose.staging.delivery.yml config
```

### Step 3: Launch Isolated Staging Stack with Delivery Enabled

```bash
docker compose -p photodoc-staging -f docker-compose.staging.yml -f docker-compose.staging.delivery.yml up -d --build
```

Verify backend health:
```bash
curl -f http://127.0.0.1:8081/api/health
# Expected output: {"status": "ok"}
```

---

## 4. Submitting a Synthetic Acceptance Photo

Generate a test image with known cryptographic payload and submit via the staging API:

```bash
# 1. Create a synthetic test JPEG with known payload
python3 -c "
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
  -F "comment=R4.1 staging rehearsal" \
  -F "files=@/tmp/test_staging_photo.jpg"
```

Expected response format:
```json
{"ok": true, "message": "Заказ успешно создан", "order_id": 1, "order_token": "...", "amount": "250.00"}
```

---

## 5. Verifying Remote Upload & Post-Delivery Local Cleanup

### Step 1: Verify Delivery Outbox State in SQLite
```bash
docker exec photodoc-staging-backend sqlite3 /data/orders.db "
SELECT id, order_id, kind, state, content_sha256, content_size, cleanup_state, cleanup_phase, local_deleted_at
FROM delivery_jobs WHERE order_id = 1;
"
```

Expected transitions:
1. Initially: `state='pending'`, `cleanup_state='pending'`, `cleanup_phase='pending'`, `local_deleted_at=NULL`.
2. After worker execution:
   - Remote verification passes.
   - `state='delivered'` committed.
   - Local file unlinked from `/app/uploads/`.
   - `cleanup_state='completed'`, `cleanup_phase='deleted'`, `local_deleted_at` populated with UTC timestamp.

### Step 2: Confirm Remote File on Yandex Disk
```bash
curl -s -H "Authorization: OAuth ${YANDEX_DISK_STAGING_TOKEN}" \
  "https://cloud-api.yandex.net/v1/disk/resources?path=PhotoDoc_Staging_R4/Заказ_1_Staging_Tester/10x15_Глянцевая_Без_полей/&fields=items.name,items.sha256,items.size,items.type"
```

Verify that the remote `sha256` and `size` match the exact values recorded in `delivery_jobs`.

### Step 3: Confirm Local Photo is Deleted
```bash
docker exec photodoc-staging-backend ls -la /app/uploads/
# The local customer photo 1_<uuid>.jpg must be absent.
# Only active in-flight temporary files (if any) or undelivered jobs may reside in /app/uploads/.
```

---

## 6. Verifying Failure Isolation and Source Preservation

To prove that unverified or failed deliveries **never delete customer photos**:

1. Submit a test order while temporarily severing external internet connectivity from the container or revoking the test token.
2. The delivery worker will encounter an HTTP connection error or authentication error.
3. Inspect SQLite:
   - `state='failed'` with exponential backoff timestamp `next_attempt_at`.
   - `cleanup_state='pending'`, `cleanup_phase='pending'`, `local_deleted_at=NULL`.
4. Inspect filesystem:
   - The original source photo **remains intact** in `/app/uploads/`.
5. Restore connectivity: the worker claims the job on retry, verifies remote upload, commits delivery, and only then safely deletes the local source.

---

## 7. Running the Backup Integrity Verifier

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

## 8. Safe Teardown

To shut down and wipe the staging environment without affecting production:

```bash
docker compose -p photodoc-staging -f docker-compose.staging.yml -f docker-compose.staging.delivery.yml down -v
```

This removes:
- `photodoc-staging-backend` and `photodoc-staging-frontend` containers.
- `photodoc_staging_db` volume.
- `photodoc_staging_uploads` volume.
- `photodoc_staging_network` bridge network.

**Production data (`photodoc_backend-data`, `orders.db`, VPS `79.137.196.14`) remains 100% isolated and untouched.**

---

## 9. Verification Matrix & Current Status

| Requirement / Check | Tool / Test Method | Result | Notes |
| :--- | :--- | :--- | :--- |
| **Separate Web VPS Provisioning** | Provider invoice / SSH check | **BLOCKED** | `VPS_PURCHASE_STATUS=NOT_PURCHASED` |
| **SSH Access Provisioning** | SSH handshake to staging host | **BLOCKED** | Awaiting VPS IP and authorized key |
| **Dedicated Staging OAuth Token** | Yandex OAuth check | **BLOCKED** | `STAGING_TOKEN_STATUS=MISSING` |
| **Remote Directory Isolation** | Yandex Disk API query | **BLOCKED** | Awaiting creation of `PhotoDoc_Staging_R4` |
| **Owner Deployment Authorization** | Formal sign-off | **BLOCKED** | Awaiting owner deployment go-ahead |
| **Live Staging Deployment Gate** | Preflight checklist | **BLOCKED** | `LIVE_STAGING_DEPLOYMENT=BLOCKED` |
| **Default Worker Disabled** | `docker-compose.staging.yml`, `verify_staging_delivery_preflight.py` | **PASS** | `DELIVERY_WORKER_ENABLED="0"` by default |
| **Opt-in Delivery Override** | `docker-compose.staging.delivery.yml` | **PASS** | Requires `${YANDEX_DISK_STAGING_TOKEN:?...}` |
| **Remote Root Isolation** | `main.py`, `verify_staging_delivery_preflight.py` | **PASS** | `PhotoDoc_Staging_R4`; `PhotoDoc_Orders` strictly rejected |
| **Volume Isolation** | Docker Compose YAML verification | **PASS** | Named volumes `photodoc_staging_*` only |
| **Deterministic Local Rehearsal** | `backend/tests/test_staging_acceptance_harness.py` | **PASS** | 22 tests passing (Scenarios A, B, C, D, E) |
| **Post-Commit Unlink Guarantee** | `test_scenario_a_successful_delivery_and_cleanup_lifecycle` | **PASS** | Unlink verified strictly after `delivered` commit |
| **Failure Preserves Local Source** | `test_scenario_b_remote_verification_failures_preserve_local_source` | **PASS** | Verified across 9 fault injection scenarios |
| **Crash Recovery & Lease Fencing** | `test_scenario_c_*` | **PASS** | Durable intent recovery & stale lease fencing verified |
| **Zero DELETE to Yandex Disk** | `test_scenario_d_no_http_delete_request_to_yandex_disk` | **PASS** | Verified 0 DELETE requests issued |
| **Zero PII / Token Leakage** | `test_scenario_d_no_pii_tokens_or_headers_leak_in_logs` | **PASS** | Logs verified free of secrets, headers, and PII |
| **Backup Verifier Compatibility** | `scripts/verify_backup_integrity.py` | **PASS** | Rehearsal accepted with 0 errors / 0 violations |
| **Live Photo Cleanup Acceptance** | External Yandex Disk REST API | **BLOCKED** | `PHOTO_CLEANUP_ACCEPTANCE=BLOCKED` (live staging); local simulation `PASS` |
| **Production Server Isolation** | Runbook & preflight guards | **PASS** | VPS `79.137.196.14` completely untouched |
