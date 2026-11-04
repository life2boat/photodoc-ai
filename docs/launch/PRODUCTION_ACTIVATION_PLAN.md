# PhotoDoc AI — Production Activation Plan

**Target state of this document: `READY_FOR_OWNER_APPROVAL`.**
Nothing in this plan has been executed against production. Every step below that changes public state requires explicit owner authorization.

Canonical domain: **`photo1storage.ru`** (owned, delegated to `ns1.spaceweb.ru`).
Web VPS: **`95.181.154.251`** (`photodoc-vps`). The historic VPN host `79.137.196.14` is out of scope and must not be touched — port 443 there belongs to Marzban/Xray.

---

## 0. Prerequisite matrix — check every row before starting

| # | Prerequisite | Current state | Owner action |
|---|---|---|---|
| P1 | PR #23 merged and CI green | ✅ `8bfecde`, all 4 CI jobs success | — |
| P2 | Immutable images published | ✅ backend `…59e513d4`, frontend `…e3c02f62` | — |
| P3 | Staging acceptance passed | ✅ live, 5/5 cases, `PASS` | — |
| P4 | Security audit has no CRITICAL | ✅ none | — |
| P5 | Restore rehearsal passed | ✅ logical identity verified | — |
| P6 | Apex A record → VPS IP | ❌ **NODATA** | **Create** (TTL 300) |
| P7 | `www` A record → VPS IP | ❌ **NXDOMAIN** | **Create** (TTL 300) |
| P8 | nginx config targets the owned domain | ⚠️ fixed in PR #24, **not merged** | Review + merge PR #24 |
| P9 | Robokassa merchant credentials | ❌ none provisioned | **Provide** |
| P10 | `ROBOKASSA_IS_TEST=0` decision | ❌ staging is `1` | **Decide** |
| P11 | SMTP credentials + sending domain verified | ❌ none | **Provide** |
| P12 | SPF / DKIM / DMARC on the owned domain | ⚠️ only `v=spf1 include:_spf.spaceweb.ru ~all`; no DKIM, no DMARC | **Configure** |
| P13 | HIGH finding H1 (unpaid fulfilment) resolved or accepted | ❌ open | **Decide** |
| P14 | HIGH finding H3 (`/order-status` route) resolved | ❌ open | **Approve fix** |
| P15 | Scheduled off-host backup | ❌ none | **Approve + provision** |
| P16 | Production `docker-compose` hardened like staging | ❌ no hardening | **Approve fix** |
| P17 | Owner authorization to activate | ❌ | **Grant** |

**Do not proceed past a red row.** P6/P7 are hard blockers: without an A record, no certificate can be issued and no callback can reach the site.

---

## 1. Pre-activation: release candidate

1. Merge PR #24 (domain alignment) — or apply its changes by hand.
2. Decide H1 (`docs/launch/SECURITY_AUDIT.md`) and H3. H3 is required for a working customer payment flow: without it SuccessURL/FailURL land on a route that does not exist.
3. Tag the release candidate and record the digests:
   ```bash
   git tag -a v1.0.0-rc1 -m "PhotoDoc AI release candidate 1" <commit>
   git push origin v1.0.0-rc1
   ```
4. Confirm CI published digests for that commit (`Build & Publish GHCR Images` job summary).
5. Write the digests into `docs/launch/ROLLBACK_PLAN.md` §0 as the new known-good row.

---

## 2. Production environment configuration

Create `/srv/photodoc/production/.env` — root-owned, mode `0600`, **never** in Git:

```dotenv
# --- runtime ---
PORT=8080
PHOTODOC_STAGING=0
DELIVERY_WORKER_ENABLED=1
DELIVERY_MAX_ATTEMPTS=10

# --- database identity (fail closed: never auto-create an unknown DB) ---
PHOTODOC_REQUIRE_DB_IDENTITY=1
PHOTODOC_ALLOW_FRESH_DB=0

# --- Yandex.Disk ---
# Production root. Must NOT be a staging prefix and must not be app:-prefixed
# unless you intend app-folder scope. The preflight rejects the PhotoDoc_Orders
# staging alias and any *Staging* root.
YANDEX_DISK_ROOT_DIR=PhotoDoc_Orders
YANDEX_DISK_TOKEN=<production OAuth token>

# --- Robokassa ---
ROBOKASSA_MERCHANT_LOGIN=<merchant login>
ROBOKASSA_PASSWORD1=<password 1>
ROBOKASSA_PASSWORD2=<password 2>
ROBOKASSA_IS_TEST=0

# --- SMTP ---
SMTP_SERVER=smtp.yandex.ru
SMTP_PORT=465
SMTP_USER=<mailbox on photo1storage.ru>
SMTP_PASSWORD=<app password>
EMAIL_TO=<operator inbox>
```

Rules:

- Inject secrets **only** via this file or a secret manager. Never in an image layer, never in a commit, never in chat.
- The SMTP client speaks **implicit TLS only** (port 465) and validates nothing yet — see `SECURITY_AUDIT.md` M12. Prefer a mailbox whose provider you trust until verification is enabled.
- `From:` is derived from `SMTP_USER`, so it must be on `photo1storage.ru` for DMARC alignment.

Run the preflight (must be green before any container starts):

```bash
python3 scripts/production_preflight.py
```

---

## 3. DNS activation checklist

Records to create at the `photo1storage.ru` zone (`ns1.spaceweb.ru`):

| Type | Name | Value | TTL |
|---|---|---|---|
| A | `@` | `95.181.154.251` | 300 |
| A | `www` | `95.181.154.251` | 300 |
| AAAA | `@` / `www` | *(omit unless you have working IPv6)* | — |

Do **not** change: `MX` (`mx1/mx2.spaceweb.ru`), existing TXT SPF. Do **not** add a CAA record that excludes Let's Encrypt (currently no CAA exists, which is permissive and fine).

Steps:

1. Lower TTL to 300 **before** creating records, and wait for the old TTL to expire.
2. Create both A records.
3. Verify from outside the zone:
   ```bash
   Resolve-DnsName photo1storage.ru -Type A -Server 1.1.1.1
   Resolve-DnsName www.photo1storage.ru -Type A -Server 1.1.1.1
   Resolve-DnsName photo1storage.ru -Type A -Server ns1.spaceweb.ru
   ```
4. Confirm both answer `95.181.154.251`.
5. Only then proceed to §4. The old TTL is currently 86400 — a record created without lowering it may take up to 24 h to converge for some resolvers.

**Mail records (needed before SMTP, not before HTTPS):**

| Type | Name | Value |
|---|---|---|
| TXT | `@` | `v=spf1 redirect=_spf.yandex.net` |
| TXT | `<selector>._domainkey` | *(Yandex 360 DKIM public key)* |
| TXT | `_dmarc` | `v=DMARC1; p=quarantine; rua=mailto:<inbox>` (start at `p=none`, ramp to `reject`) |

---

## 4. HTTPS activation checklist

Preconditions: §3 step 4 verified; inbound TCP/80 and TCP/443 reachable from the public internet.

1. **Provision the ACME webroot** (previously missing entirely):
   ```bash
   sudo scripts/provision_tls_webroot.sh
   ```
2. **Confirm nothing already owns :80/:443:**
   ```bash
   ss -tlnp | grep -E ':(80|443)\b'      # expect empty
   ```
3. **Firewall:** `ufw allow 80/tcp`, `ufw allow 443/tcp` (already allowed on staging), and confirm no provider-level security group blocks them.
4. **Deploy the bootstrap config** (HTTP-only, serves the ACME challenge):
   ```bash
   sudo cp infra/nginx/photodoc-http-bootstrap.conf /etc/nginx/sites-available/photodoc.conf
   sudo ln -sf /etc/nginx/sites-available/photodoc.conf /etc/nginx/sites-enabled/photodoc.conf
   sudo nginx -t && sudo systemctl reload nginx
   ```
5. **Issue the certificate** (both names in one order — all-or-nothing):
   ```bash
   sudo certbot certonly --webroot -w /var/www/certbot \
     -d photo1storage.ru -d www.photo1storage.ru
   ls /etc/letsencrypt/live/photo1storage.ru/{fullchain,privkey,chain}.pem
   ```
6. **Switch to the HTTPS config:**
   ```bash
   sudo cp infra/nginx/photodoc-https.conf /etc/nginx/sites-available/photodoc.conf
   sudo nginx -t && sudo systemctl reload nginx
   ```
7. **Enable renewal** (no renewal automation existed before PR #24):
   ```bash
   sudo cp infra/systemd/photodoc-certbot-renew.{service,timer} /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now photodoc-certbot-renew.timer
   sudo systemctl list-timers photodoc-certbot-renew.timer
   sudo systemctl start photodoc-certbot-renew.service   # dry check
   ```
8. **Verify** (see §9 smoke tests).

Expected config validation from PR #24 (already executed in an isolated container):

- `nginx -t` → `syntax is ok` on both configs.
- HTTP → `301` to `https://photo1storage.ru/...`.
- ACME path exempt from the redirect (`404` when absent, `200` when present).
- TLS 1.2/1.3 only; TLS 1.1 rejected.
- Headers present: HSTS, `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`, `Permissions-Policy`.

---

## 5. Robokassa activation checklist

1. **Cabinet configuration** (owner, in the Robokassa merchant cabinet):

| Setting | Value |
|---|---|
| ResultURL (method **POST**) | `https://photo1storage.ru/api/payment/robokassa/result` |
| SuccessURL | `https://photo1storage.ru/order-status?payment=success` **(blocked by H3)** |
| FailURL | `https://photo1storage.ru/order-status?payment=fail` **(blocked by H3)** |
| Signature algorithm | **MD5** (the code implements MD5 only — see H2) |
| Test mode | **off** |

2. **Decide H2** before touching the cabinet: either pin MD5 and record that decision, or implement algorithm selection first. If the cabinet is set to SHA-256 the code rejects every callback with 400 and no order is ever confirmed — silently.
3. **Decide H3**: implement `/order-status` (frontend route that reads `?payment=` and calls the SuccessURL handler) or point SuccessURL/FailURL at the backend. Without this, paying customers see a dead page.
4. **Confirm H1 decision**: whether unpaid orders may upload to Yandex.Disk. If not, gate the `file`/`info` jobs on `payment_status='paid'`.
5. **Set credentials** in `.env` and `ROBOKASSA_IS_TEST=0`; restart the worker; confirm startup does not raise.
6. **Configure 54-ФЗ** receipt data in the cabinet (no `Receipt` block is sent by the code).
7. **Verify with a real minimum-value payment**, then refund it (see §10).

Signature verification that must hold (already proven by 23 automated tests):

- `md5(login:OutSum:InvId:Password1)` for the invoice URL.
- `md5(OutSum:InvId:Password2)` for the ResultURL callback.
- `OutSum` must equal the stored authoritative `order_amount`.
- Duplicate callbacks are idempotent; invalid signatures are rejected; SuccessURL never marks anything paid.

---

## 6. SMTP activation checklist

1. Provision the mailbox on `photo1storage.ru`. Existing docs referencing `notifications@photodoc-ai.ru` are wrong and must be corrected — that domain is unregistered, and `main.py:600,659` hardcodes it in `Message-ID`.
2. Publish SPF, DKIM and DMARC (§3).
3. Set `SMTP_SERVER=smtp.yandex.ru`, `SMTP_PORT=465`, `SMTP_USER`, `SMTP_PASSWORD` (app password), `EMAIL_TO`.
4. **Decide whether customers receive mail.** As implemented, **both** notifications go to `EMAIL_TO`; the customer receives nothing. `docs/LAUNCH_PROCUREMENT_REGISTER.md` describes customer transactional mail, so this is a gap needing a decision.
5. Send one test order to a controlled mailbox and confirm delivery, then check the received headers for `spf=pass`, `dkim=pass`, `dmarc=pass`.
6. Validate the customer email field (currently unvalidated and forwarded to the PSP — L5).

Known limitations to accept consciously: no socket timeout (a silent SMTP peer blocks the single worker thread), no TLS certificate verification, attempt 10 is terminal with no requeue.

---

## 7. Delivery worker activation checklist

1. Confirm the production Yandex.Disk root: `YANDEX_DISK_ROOT_DIR=PhotoDoc_Orders` (must not be a staging root; the startup guard fails closed on staging prefixes and on the `disk:/PhotoDoc_Orders` alias).
2. Confirm the token is app-scoped or full-disk per your intent, and that it is a **dedicated production** credential (the staging token belongs to a real personal account — do not reuse it).
3. Verify `DELIVERY_WORKER_ENABLED=1`.
4. **Backfill check:** queued `remote_path` values written under a different root will fail closed and retain sources. Before enabling, inspect the queue:
   ```sql
   SELECT remote_path, state, attempts FROM delivery_jobs WHERE state IN ('pending','failed');
   ```
   Rows outside the new root will never succeed — resolve them before enabling the worker.
5. Enable and watch the first deliveries (§10).
6. Keep `docker logs -f` on the backend for the first hour.

---

## 8. Monitoring and incident response

**Required before launch (currently missing):**

| Item | Why |
|---|---|
| External uptime check on `https://photo1storage.ru/api/health` | Detects a dead backend before customers do |
| Alert on ResultURL 4xx rate | The only way to catch an H2-style signature misconfiguration (currently silent) |
| Alert on `/api/order` error rate and queue depth | Detects intake spam and stuck delivery |
| Alert on disk usage > 80% | No disk guard exists in the app |
| Alert on backup age > 24 h | No scheduled backup exists yet (S6-02) |
| Certificate expiry check (< 21 days) | Renewal timer is new and unproven in production |
| Log aggregation | `docker logs` is not durable |

**Incident runbook:**

| Symptom | First action |
|---|---|
| Site unreachable | `curl -fsS https://photo1storage.ru/api/health` → if nginx answers 502, check containers; if no TCP, check nginx + ufw |
| Payments confirmed but orders unpaid | Check ResultURL reachability, then the cabinet's signature algorithm (H2) and `ROBOKASSA_PASSWORD2` |
| Orders paid but no delivery | Check `delivery_jobs.last_error`; verify the token and root; worker flag |
| Delivery duplicating | Stop the worker immediately (`DELIVERY_WORKER_ENABLED=0`), inspect `delivery_jobs` leases |
| Disk full | Stop intake, prune logs; the app has no guard and will 500 |
| Suspected secret leak | Rotate the Yandex token / Robokassa passwords / SMTP app password; the token lives only in `/etc/photodoc/...` or `.env`, never in Git |

---

## 9. Production smoke tests

Run in this order after activation. All must pass.

| # | Command | Expected |
|---|---|---|
| 1 | `curl -fsS https://photo1storage.ru/api/health` | `{"status":"ok"}` |
| 2 | `curl -sSI http://photo1storage.ru/` | `301`, `Location: https://photo1storage.ru/` |
| 3 | `curl -sSI https://www.photo1storage.ru/` | `200`/`301`, valid certificate for both names |
| 4 | `curl -sSD- -o /dev/null https://photo1storage.ru/` | HSTS, `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`, `Permissions-Policy` |
| 5 | `openssl s_client -connect photo1storage.ru:443 -servername photo1storage.ru` | TLS 1.2/1.3, chain valid, no TLS 1.0/1.1 |
| 6 | `curl -sS -o /dev/null -w '%{http_code}' https://photo1storage.ru/.well-known/acme-challenge/probe` | `404` (path is served, not redirected) |
| 7 | `ss -tlnp \| grep -E ':(8000\|8080)'` | loopback only — never `0.0.0.0` |
| 8 | `docker ps --format '{{.Names}} {{.Ports}}'` | no host-published container ports |
| 9 | `docker inspect <backend> \| grep -i staging` | no staging flag in production |
| 10 | `docker exec <backend> printenv ROBOKASSA_IS_TEST` | `0` |

---

## 10. First-order verification procedure

1. Place a real order through the public site with a genuine photo, for the **cheapest** print size.
2. Confirm the order row and the server-computed `order_amount`.
3. Pay for real (minimum amount). Do not skip this — a test-mode payment proves nothing about the production signature path.
4. Confirm within 60 s:
   - `payment_status='paid'`, `paid_at` set,
   - a `paid_email` job `delivered`, and the message arrived at `EMAIL_TO`,
   - a `file` job `delivered` with `remote_path` under `PhotoDoc_Orders`,
   - the file exists in Yandex.Disk with matching size and SHA-256,
   - the local source was deleted **only after** that confirmation.
5. Verify the customer landed on a real success page (H3).
6. Issue the refund, and record the refund path — **no refund flow is implemented in the app**; it is manual in the cabinet.
7. Only after a clean first order: enable monitoring alerts and consider raising traffic.

---

## 11. Explicitly out of scope / not authorized here

- Creating public DNS records.
- Issuing a certificate.
- Merging PR #24.
- Enabling real payment collection.
- Sending real customer email.
- Enabling a permanent production delivery worker.
- Any change to `79.137.196.14`.

Each of these is an owner action. Until they are performed, the correct state is **`READY_FOR_OWNER_APPROVAL`**, not `PUBLIC_LIVE`.
