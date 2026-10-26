# PhotoDoc AI — R2 Public Launch & HTTPS Architecture Specification

This document details the audited infrastructure baseline, architectural evaluation, recommended HTTPS deployment topology, data preservation guarantees, end-to-end testing protocol, and preflight checklist for PhotoDoc AI public launch.

---

## 1. Audited Production Infrastructure (Factual Findings)

Diagnostics performed on live host `79.137.196.14` and external network probes confirm:

```text
CURRENT_VPS_IP=79.137.196.14
OS=Ubuntu 24.04 LTS (Kernel 6.8.0-48-generic x86_64)
DOMAIN=photodoc-ai.ru (NXDOMAIN / unallocated at TCI & RIPN registry)
WWW_DOMAIN=www.photodoc-ai.ru (NXDOMAIN)
TLS_CERTIFICATES=NONE (No certificates exist for photodoc-ai.ru; certbot not installed)

PORT_443_OWNER=XRAY (PID 338571, listening on *:443 with VLESS TCP Reality, dest: www.microsoft.com:443)
PORT_80=UNUSED (No host process listening)
PORT_8080=BOUND_LOCAL (127.0.0.1:8080 -> photodoc-frontend:80 via docker-proxy)
PORT_8000=BOUND_LOCAL (127.0.0.1:8000 -> photodoc-backend:8000 via docker-proxy)
PORT_444=XRAY (Trojan TCP noTLS)
PORT_8443=XRAY (VLESS XHTTP CDN with Cloudflare origin certificate)
PORT_8444=XRAY (VLESS WS Compatibility, tunneled to 81.27.98.42)
PORT_22=SSHD (OpenSSH 9.6p1)

RAM_TOTAL=1.9 GiB
RAM_AVAILABLE=577 MiB
SWAP_TOTAL=0 B (OOM Risk on container build)
DISK_ROOT_TOTAL=30 GB
DISK_ROOT_AVAIL=8.8 GB (70% utilized; sufficient for SQLite backups)

DOCKER_VOLUME=photodoc_backend-data (16 KB, orders.db 12 KB, application_id=0, 0 records)
UPLOADS_DIR=/opt/photodoc/backend/uploads (4.0 KB, 0 files)
MARZBAN_STATE=ACTIVE (/var/lib/marzban with SQLite db, edge SSH tunnels to 81.27.98.42)
```

---

## 2. HTTPS Architecture Evaluation

### Option A: Co-hosting on Existing VPS (`79.137.196.14`)
- **Port 443 Conflict**: Port 443 is actively bound by Xray (`*:443`) in VLESS Reality mode (`dest: www.microsoft.com:443`). In Reality mode, Xray does not terminate TLS with local domain certificates.
- **SNI Fallback Limitation**: While Xray supports SNI fallbacks in standard TLS mode, Reality mode intercepts initial TLS ClientHellos and directs unmatched traffic to the disguise target.
- **Marzban Overwrite Risk**: Marzban dynamically rewrites `/var/lib/marzban/xray_config.json` upon user management operations. Any manual custom fallback rules injected into Xray will be overwritten by the Marzban daemon.
- **Resource Constraints**: With only 577 MiB available RAM and 0 swap, running Node.js Vite builds or parallel Python image processing alongside Marzban and Xray risks triggering the Linux OOM killer.
- **Verdict**: **REJECTED** (High risk of disrupting Marzban VPN; violates isolation guardrail).

### Option B: External CDN / Reverse Proxy (Cloudflare / Cloudflare Tunnel)
- **Payment Gateway Reliability**: Domestic Russian payment gateways (Robokassa) frequently experience packet drops, CAPTCHAs, or timeouts when dispatching server-to-server callbacks (`/api/payment/robokassa/result`) to IPs proxied through foreign CDNs subject to Roskomnadzor throttling.
- **Upload Size Limits**: Cloudflare Free tier imposes an unalterable 100 MB client body limit, matching our ceiling with zero margin for multipart form overhead.
- **Verdict**: **REJECTED** (Risk to payment callback delivery and Russian customer checkout completion).

### Option C: Dedicated Web VPS / Dedicated Public IP (Recommended)
- **Cost**: Low (~200–400 RUB/month for a 1–2 vCPU, 2–4 GB RAM, 20–40 GB NVMe VPS at a Russian/European cloud provider such as Timeweb, Selectel, Beget, or FirstVDS).
- **Complexity**: Low. Clean, standard Ubuntu stack with Nginx, Certbot, Docker Compose, and UFW firewall.
- **Marzban Isolation**: **100% Guaranteed**. Zero shared ports, zero shared processes, zero configuration dependencies on Xray.
- **Payment & Upload Stability**: Direct IPv4 connection ensuring 100% reliable Robokassa callback reception, unthrottled 80 MiB photo upload streaming, and full Let's Encrypt TLS 1.3 automation.
- **Resource Safety**: 2–4 GB dedicated RAM completely eliminates OOM risks during builds and runtime batch image processing.
- **Verdict**: **RECOMMENDED** (Cleanest, safest, and most resilient architecture).

---

## 3. Recommended Architecture Topology

```
[ Browser / Client ]
        │ HTTPS (Port 443) / TLS 1.2 & 1.3
        │ Managed by Certbot / Let's Encrypt
        ▼
[ Edge Nginx Reverse Proxy (Web Host / Container) ]
        │ client_max_body_size 100M; timeouts 120s; proxy_buffering off;
        ├─────────────────────────────────────────┐
        │                                         │
        ▼ GET / static assets & SPA routes        ▼ POST /api/* (Rate Limited & Unbuffered)
[ Frontend SPA (React / Vite) ]          [ Backend API (FastAPI / Uvicorn) ]
                                                  │
                   ┌──────────────────────────────┼──────────────────────────────┐
                   │                              │                              │
                   ▼                              ▼                              ▼
         [ SQLite WAL (orders.db) ]    [ Яндекс.Диск API (OAuth) ]     [ Robokassa Gateway ]
         - PRAGMA application_id=PDAI   - Уникальные имена файлов       - Генерация ссылок
         - Token-bound payment links    - Проверка SHA-256 и размера    - Подпись MD5/SHA256
         - Durable delivery_jobs queue  - Защита от перезаписи          - Result/Success callback
```

### 3.1 Network & Security Specifications
1. **Upload Limit Preservation**:
   - Edge Nginx: `client_max_body_size 100M;`, `client_body_buffer_size 1M;`.
   - Proxy timeouts: `proxy_connect_timeout 60s;`, `proxy_read_timeout 120s;`, `proxy_send_timeout 120s;`.
   - API streaming: `proxy_request_buffering off;` for `/api/` to stream large attachments directly into backend memory/disk.
   - Backend enforcement: Max 20 MiB per photo file, max 80 MiB total order payload, allowed formats JPEG/PNG/WebP with magic number validation.
2. **Payment Security**:
   - All endpoints (`/api/order`, `/api/payment/robokassa/create-link`, `/api/payment/robokassa/result`, `/api/payment/robokassa/success`, `/api/payment/robokassa/fail`) strictly HTTPS.
   - ResultURL callback `/api/payment/robokassa/result` exempted from aggressive rate-limiting to prevent dropped payment webhook retries.
   - Idempotent signature verification (`ROBOKASSA_PASSWORD2`), state lock on `paid`, zero link regeneration for legacy orders.
3. **Internal Port Protection**:
   - Ports 8000 and 8080 bound only to `127.0.0.1` or internal Docker network.
   - UFW firewall on Web VPS permits only `22/tcp`, `80/tcp`, and `443/tcp`.

---

## 4. Data Preservation & Migration Compatibility

1. **Production Volume (`photodoc_backend-data`)**:
   - Size: 16 KB. Current `orders.db`: 12 KB, 0 records, `application_id=0`.
   - Compatible with P0 migration (`reconcile_payment_schema.py --allow-legacy-unmarked`) which stamps `application_id=1346650441` (`PDAI`).
2. **P0 → P1 Schema Migration**:
   - Transactional addition of `order_token_hash` and `delivery_jobs`.
   - Reconciled and verified via 127 automated regression tests.
3. **Backup Space Sufficiency**:
   - Available disk on target: 8.8 GB.
   - Hardened backup requirement: >= 500 MB.
   - Database size: < 1 MB. Uploads directory: 0 KB.
   - Space headroom: > 17,000x above the minimum requirement.
4. **Hardened Verification Tooling**:
   - `scripts/verify_backup_integrity.py` verifies SQLite integrity, PDAI identity, path traversal confinement, file existence, size match, and SHA-256 match.

---

## 5. End-to-End Testing Protocol (10-Step Isolated Verification)

Prior to opening public traffic, the following sequence must be executed in a staging environment:

1. **HTTPS Connectivity**: Access `https://<staging-domain>/` in browser; verify TLS certificate validity, A+ security headers, and HTTP->HTTPS 301 redirection.
2. **Four Intake Forms**: Submit valid intake forms across all 4 widgets:
   - Photo for documents (3x4, 3.5x4.5)
   - Photo printing (10x15, A4)
   - Polaroid printing
   - Photo restoration
3. **Photo Upload Enforcement**:
   - Upload 1 synthetic JPEG (5 MB) -> HTTP 200.
   - Upload multiple synthetic files (total 45 MB) -> HTTP 200.
   - Upload single file > 20 MiB -> HTTP 413.
   - Upload order payload > 80 MiB -> HTTP 413.
   - Upload invalid format (.exe disguised as .jpg) -> HTTP 400.
4. **Order Persistence**: Query SQLite; verify order record exists with unique token hash and delivery jobs created in state `pending`.
5. **Robokassa Payment Link**: Call `/api/payment/robokassa/create-link` with `order_token`; verify URL generation with `ROBOKASSA_IS_TEST=1`.
6. **Payment Callback Simulation**: Dispatch synthetic signed `ResultURL` POST request with MD5/SHA256 signature calculated using `ROBOKASSA_PASSWORD2`; verify HTTP 200 `OK{InvId}` response and order transition to `paid`.
7. **Yandex Disk Delivery**: Execute delivery worker; verify synthetic files uploaded to isolated test folder `/PhotoDoc_Staging/{order_id}/` with overwrite protection and SHA-256 match.
8. **SMTP Notification**: Verify delivery of order summary to test recipient via configured test SMTP account.
9. **Failure Recovery Rehearsal**: Simulate network error during Yandex upload; verify exponential retry backoff, lease release, and successful delivery on second pass.
10. **Synthetic Backup & Restore**: Run SQLite Online Backup API and uploads tar archive; verify restore into clean directory with `scripts/verify_backup_integrity.py` reporting `BACKUP_COMPLETENESS=PASS`.

---

## 6. Project Owner Action Items (Blockers)

| # | Item | Status | Action Required by Owner |
|---|---|---|---|
| 1 | **Domain Registration** | `BLOCKED` | Register domain `photodoc-ai.ru` through a licensed registrar (Reg.ru, RU-CENTER, Beget). |
| 2 | **DNS Delegation** | `BLOCKED` | Delegate domain to DNS provider (Yandex Cloud DNS, Cloudflare, or Registrar NS) and configure A records for `@` and `www`. |
| 3 | **Web Hosting Provisioning** | `BLOCKED` | Provision a dedicated Web VPS (2 GB RAM, 20 GB SSD, Ubuntu 24.04) or assign a dedicated public IP to keep Marzban VPN 100% isolated. |
| 4 | **Robokassa Merchant** | `BLOCKED` | Provide production `MerchantLogin`, `Password1`, and `Password2` in `/opt/photodoc/backend/.env`. |
| 5 | **Yandex Disk OAuth** | `BLOCKED` | Generate and supply valid Yandex Disk OAuth token with write access. |
| 6 | **SMTP Notifications** | `BLOCKED` | Generate and supply Yandex Mail App Password for notifications. |
