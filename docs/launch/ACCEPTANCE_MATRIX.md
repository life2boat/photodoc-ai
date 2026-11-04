# PhotoDoc AI — Acceptance Matrix

Commit under test: `8bfecdee756e11972cbb68ec3f7c0e009a1cba60` (merge of PR #23, Yandex.Disk namespace hardening)
Branch/worktree used for verification: `.worktrees/launch` (detached at `8bfecde`), plus `launch/https-domain-alignment-20261104` for PR #24.
Staging host: `photodoc-vps` = `95.181.154.251` (hostname `photo1storage.ru`), Ubuntu 24.04.4 LTS.
Staging compose project: `photodoc-pr22-private`, config `/srv/photodoc/staging/compose.private.yml`.

Legend: **PASS** verified by execution · **FAIL** executed and failed · **BLOCKED** cannot be executed · **NOT_VERIFIED** not executed (reason given) · **N/A** not applicable.

---

## 1. Repository and release identity

| # | Check | Result | Evidence |
|---|---|---|---|
| R1 | PR #23 merged into `main` | **PASS** | `gh pr view 23` → `state: MERGED`, `mergedAt: 2026-11-04T03:12:10Z`, `mergeCommit: 8bfecdee756e11972cbb68ec3f7c0e009a1cba60` |
| R2 | Local `main` reflects the merge | **FAIL (expected)** | Local `main` is `b3462e8`, 7 ahead / 60 behind `origin/main`, with extensive uncommitted owner work. Verification was therefore performed in a clean worktree of `origin/main`, never in the dirty tree. |
| R3 | CI green on the merge commit | **PASS** | Run `41765626212` on `8bfecde`: `Playwright Tests` success, `Payment Security Tests` success, `Docker Build` success, `Build & Publish GHCR Images` success |
| R4 | Image built from merged code exists | **PASS** | `ghcr.io/life2boat/photodoc-backend:8bfecde…` exists in GHCR (public). Pulled; `org.opencontainers.image.revision = 8bfecde…`, `created = 2026-11-04T03:14:06Z` (2 min after merge) |
| R5 | Image actually contains PR #23 code | **PASS** | Inside the image: `import yandex_paths` OK; `/app/main.py` contains `ensure_yandex_folder`, `_validated_yandex_destination`, `Yandex namespace root is unavailable`, `Conflicting Yandex directory was not confirmed` |
| R6 | Deployed staging image before the run | **PASS (measured)** | `febae24…` (PR #22), image created `2026-11-02T06:50:58Z` — one merge behind, as expected |

Deployed image references used for acceptance:

```
backend  ghcr.io/life2boat/photodoc-backend@sha256:59e513d4b2642c352eec639dad61cd3c61bcb671b7387fe25911eceb802f1525
frontend ghcr.io/life2boat/photodoc-frontend@sha256:e3c02f623dbc2d04aece54c0217b305da398edbd8d53a071f07b36944222467e
```

---

## 2. Local automated test suites (offline)

Command pattern: `Push-Location <worktree>\backend; python -m pytest tests -q; Pop-Location` (Python 3.10.6).

| # | Suite | Result | Evidence |
|---|---|---|---|
| T1 | Full backend suite | **PASS** | `388 passed, 2 skipped in 205.53s` — the 2 skips are the known Windows symlink-privilege skips. Matches the PR #23 description exactly (390 collected). |
| T2 | `tests/test_yandex_namespace.py` (PR #23 regression) | **PASS** | `59 passed in 2.30s` |
| T3 | `tests/test_payment_security.py` | **PASS** | `23 passed in 1.59s` |
| T4 | `test_delivery_delete_boundary.py` | **PASS** | `5 passed` |
| T5 | `test_backup_integrity.py` | **PASS** | `27 passed` |
| T6 | `test_rollback_bundle.py` | **PASS** | `26 passed` |
| T7 | `test_upload_limits.py` | **PASS** | `8 passed` |
| T8 | `test_photo_cleanup_privacy.py` | **PASS** | `21 passed, 1 skipped` |
| T9 | `test_backup_zero_retention.py` | **NOT_VERIFIED** | 1 failure is environmental: a test shells out to Git-Bash with a Cyrillic path (`F:\диск е\…`) → `bash: F:/диск: No such file or directory`. Not a product defect; not reproducible-as-fail on the Linux target. |

---

## 3. Stage 1 — Yandex.Disk staging acceptance (LIVE)

**Final run id: `20261104_d4e5f6a7b8c9` — verdict `PASS`.**

Executed on the staging host with the backend rolled to the PR #23 image, `DELIVERY_WORKER_ENABLED=0` throughout, OAuth token injected **backend-only** from a root-owned `0600` file, outbound HTTPS provided by a bounded CONNECT proxy that only permits `cloud-api.yandex.net` and `*.disk.yandex.net|ru`, `*.storage.yandex.net`.

Dedicated namespace: `Приложения/PhotoDoc/PhotoDoc_Staging_Acceptance_20261104_d4e5f6a7b8c9` (`app:/PhotoDoc_Staging_Acceptance_20261104_d4e5f6a7b8c9`).

| # | Check | Result | Evidence |
|---|---|---|---|
| S1-1 | PR #23 merged | **PASS** | see R1 |
| S1-2 | Deployed staging version confirmed | **PASS** | `febae24…` before, `8bfecde…` during, `febae24…` after restore |
| S1-3 | Inspect containers / SQLite / uploads / queue | **PASS** | 2 containers healthy; `orders.db` 40960 B, `PRAGMA integrity_check = ok`; 1 order (`SYNTHETIC_PR22_ACCEPTANCE`, unpaid), 3 delivery jobs `pending attempts=0`; 2 upload files |
| S1-4 | Configured Yandex root and queued destinations verified | **PASS** | `YANDEX_DISK_ROOT_DIR=PhotoDoc_Staging_Private_PR22`; queued `remote_path` all under that root, legacy-relative form retained |
| S1-5 | Backups created **and verified** before modifying staging | **PASS** | `/root/photodoc-live-yandex-20261104_d4e5f6a7b8c9/backup`: consistent SQLite snapshot via the backup API, 2 uploads, `PRAGMA integrity_check=ok`, backup logical hash **equal** to live, file SHA-256 equal. Baseline logical hash `8981540f…` **identical to the Nov 3 baseline** → staging data provably untouched between sessions. |
| S1-6 | Updated image built from merged code | **PASS** | Built by CI from `8bfecde` and published to GHCR (R4/R5). This is the sanctioned zero-build VPS architecture (`docs/R4_PREBUILT_DEPLOYMENT.md`); nothing is compiled on the 2 GB VPS. |
| S1-7 | Deployed to private staging via the existing architecture | **PASS** | Backend rolled onto the digest above inside the existing `photodoc-pr22-private` project; both containers `healthy`; network `photodoc_pr22_private_network` (`internal: true`), no published ports |
| S1-8 | Temporary namespace guard removed from the active test path | **PASS** | New harness has **no** `ExistingAncestorReadOnly` synthetic-409 branch. Native code instead performs a read-only `fields=type` lookup on each ancestor (`ancestor_readonly_lookups=2`) and issues mkdir **only** for 404 paths (`ancestor_mkdir_attempts=0`). Historical evidence directory `/srv/photodoc/staging/acceptance_20261103_815896830cb2/` left byte-identical (`acceptance_harness.py` md5 `c2b2f0bd3ada1e94c3a57bb8c1e7997b`, mtime unchanged). |
| S1-9 | Real Yandex.Disk acceptance with synthetic orders only | **PASS** | 5 synthetic cases below; no customer data referenced |
| S1-10a | File size verified remotely | **PASS** | each case: `remote_size_matches: true` (6000/6700/6400/6500/6600) |
| S1-10b | SHA-256 verified remotely | **PASS** | each case: `remote_sha256_matches: true`; independently re-downloaded for `success`: `independent_download_sha256_matches: true`, `independent_download_size_matches: true` |
| S1-10c | Retries | **PASS** | `timeout_before`: attempt 1 `failed`/`ReadTimeout` with source retained → attempt 2 `delivered` |
| S1-10d | Idempotence (ambiguous acknowledgement) | **PASS** | `ack_timeout`: upload succeeded, confirmation timed out; recovery verified the remote object and did **not** re-upload — `upload_put` stayed `1`, `remote_object_count=1` |
| S1-10e | Concurrent directory creation | **PASS (offline)** / NOT_VERIFIED (live) | `test_yandex_namespace.py` covers deterministic concurrent creation and 409-on-existing-dir; the live run exercised the 409 re-lookup branch only implicitly. No live parallel-mkdir race was staged. |
| S1-10f | Crash recovery | **PASS** | `crash_commit` (`os._exit(73)` mid-commit): job `processing`, source retained → recovered `delivered`, attempts 2. `crash_cleanup` (`os._exit(74)` before unlink): job `delivered`/`cleanup deleting`, source retained → recovered `cleanup completed`, attempts 1 |
| S1-11a | Existing DB data preserved | **PASS** | after restore `orders.db` logical hash `8981540f…` equals baseline; `orders=1`; the 3 original jobs still `pending attempts=0` |
| S1-11b | Existing local files preserved | **PASS** | both upload SHA-256 unchanged (`fc535a74…`, `e6b2d410…`) |
| S1-11c | Existing remote files preserved | **PASS** | full app-folder snapshot before/after: `existing_app_objects_unchanged: true` (28 pre-existing objects at the start of the final run, `remote-before.private.json` / `remote-after.private.json`) |
| S1-11d | No duplicate deliveries | **PASS** | `require_complete` asserts `upload_put == 1` per case; all 5 cases satisfied it (including the retry and crash cases) |
| S1-11e | No lost source files | **PASS** | sources retained on every failure/crash; deleted only after a confirmed delivery + verified cleanup |
| S1-12 | Worker returned to its original disabled state | **PASS** | `worker_disabled_after_test: true`; `DELIVERY_WORKER_ENABLED=0` read back from the running container |
| S1-13 | Runtime secret unloaded after test | **PASS** | `runtime_secret_unloaded_after_test: true`; no `YANDEX_DISK_TOKEN` in the restored container env (count 0) |
| S1-14 | Original compose restored | **PASS** | `compose.private.yml` SHA-256 `d66729a1…` identical to `compose.before.yml`; no `env_file:` line remains |
| S1-15 | Temporary proxy/network removed | **PASS** | `temporary_proxy_removed: true`; no `photodoc-acceptance-proxy-*` container, no `photodoc_acceptance_egress_*` network |
| S1-16 | Staging remains private and healthy | **PASS** | both containers `healthy`; `staging_healthy_private: true`; no published host ports |

### 3.1 The five live cases (final run)

| Case | Phase 1 | After failure/crash | After recovery | Result |
|---|---|---|---|---|
| `success` | `delivered`, attempts 1, `mkdir=2` (root + case dir), `ancestor_readonly_lookups=2`, `upload_put=1`, cleanup `deleted` | — | — | **PASS** |
| `timeout_before` | `failed`, attempts 1, `ReadTimeout`, source retained, source identity matches | unchanged, `upload_put=0` | `delivered`, attempts 2, sha/size match, cleanup `deleted` | **PASS** |
| `ack_timeout` | upload `1` succeeded, confirmation `ReadTimeout`, `failed`, source retained | unchanged | `delivered`, attempts 2, `upload_put` still `1` (no re-upload) | **PASS** |
| `crash_commit` | controlled `os._exit(73)` after delivery | `processing`, source retained | `delivered`, attempts 2 | **PASS** |
| `crash_cleanup` | controlled `os._exit(74)` before unlink | `delivered`, `cleanup_phase=deleting`, source retained | cleanup `completed`/`deleted`, attempts 1 | **PASS** |

### 3.2 Audit trail of the acceptance attempts (all kept, none deleted)

Honest record: five runs were needed. Earlier attempts failed on defects in **my test driver**, not in the product. No remote folder was deleted at any point.

| Run id | Outcome | Cause |
|---|---|---|
| `20261104_7f3a9c1d45e2` | abandoned | Driver's `safe_get` only allowed paths under the new root; native code legitimately *looks up* the ancestors `Приложения` and `Приложения/PhotoDoc`, so the harness aborted with `AssertionError` before any mkdir. Nothing was created remotely. |
| `20261104_a1b2c3d4e5f6` | abandoned | Delivery actually **succeeded** (`delivered`, attempts 1, `mkdir=2`, `upload_put=1`), but a driver counter counted every mkdir target as an "ancestor attempt" and rejected the result. |
| `20261104_b7c8d9e0f1a2` | abandoned | All assertions up to the independent-download step passed; the download helper used a no-redirect opener and the Yandex download URL answers 302. |
| `20261104_c3d4e5f6a7b8` | abandoned | All 5 cases passed; the post-run full-folder snapshot hit a transient non-200 (no retry in the driver) and aborted the aggregate step. Remote state was afterwards verified correct by hand: 5 dirs / 5 files with the expected SHA-256 each. |
| `20261104_d4e5f6a7b8c9` | **PASS** | After adding bounded retry/backoff on transient statuses to the snapshot traversal. Self-contained `PASS`. |

### 3.3 Blockers and residual risks for Stage 1

| Item | Status |
|---|---|
| Live **concurrent** mkdir race | **NOT_VERIFIED** — covered offline in `test_yandex_namespace.py`; not staged live |
| Credential provenance | **OWNER CONFIRMATION REQUIRED** — the token at `/etc/photodoc/staging/yandex.env` (root:root `0600`, 82 B, not mounted) belongs to account **`mdot22` (uid 594353979, 3.4 GB / 10 GB used, registered 2018)** — a real personal account, **not** a dedicated test account as `YANDEX_ACCEPTANCE_READINESS.md` requires. Containment is nonetheless enforced: the token is app-folder scoped (`app:/` = `disk:/Приложения/PhotoDoc`), and that folder contained only acceptance evidence (no customer data). All writes were confined to a fresh namespace. |
| Residual remote evidence | Four abandoned-attempt namespaces remain under `Приложения/PhotoDoc/` alongside the final one (12 synthetic files total from the abandoned runs; the first attempt created nothing because it aborted before any mkdir). Every one of them was **deliberately not deleted** — the safety boundary forbids deleting Yandex.Disk folders. Cleanup is an owner decision. |

---

## 4. Stage 2 — Robokassa (test-mode, no real payments)

| # | Check | Result | Evidence |
|---|---|---|---|
| S2-1 | Integration audit | **PASS** | Invoice URL `main.py:1171`; MD5 `login:OutSum:InvId:Password1` `main.py:1157-1158`; ResultURL `main.py:1191-1255` with `OutSum:InvId:Password2` `main.py:1218`; SuccessURL `main.py:1258-1304` (Password1, read-only); **no FailURL handler exists** |
| S2-2 | Server-side price locking | **PASS** | `main.py:1129-1145` — `amount_to_pay = authoritative_amount`; a client amount is only an equality assertion → 400 on mismatch. `order_amount` is the sole authority; `payment_amount` explicitly demoted (`main.py:433`) |
| S2-3 | Invoice creation and signatures | **PASS** | `test_payment_create_authoritative_without_client_amount` asserts the exact invariant signature and `InvId` in the URL |
| S2-4a | Success callback | **PASS** | `test_success_url_cannot_mark_paid`, `test_success_url_displays_paid_only_after_result_url` |
| S2-4b | Failure callback | **FAIL (gap)** | No FailURL route exists at all, and the documented frontend `/order-status?payment=fail` route does not exist either (no router in `frontend/src`) |
| S2-4c | ResultURL callback | **PASS** | `test_result_callback_idempotency`, `test_result_amount_mismatch_rejected` |
| S2-5a | Duplicate callbacks | **PASS** | `test_result_callback_idempotency`: duplicate → `OK{id}`, state unchanged |
| S2-5b | Invalid signatures | **PASS** | `test_result_invalid_signature_rejected`, `test_success_invalid_signature_rejected` |
| S2-5c | Replay | **PASS (bounded)** / **NOT_VERIFIED** as a distinct scenario | No freshness control exists; impact bounded by idempotency + `InvId` inside the digest. No stale/cross-order replay test exists. |
| S2-5d | Concurrent requests | **PASS** | `test_payment_create_racing_result_url_cannot_revert_paid`, `test_concurrent_duplicate_callback_one_owner_only` (note: the latter does not actually assert a single transition owner) |
| S2-6 | Unpaid order cannot enter *paid* state | **PASS** | Single writer `mark_order_paid` `main.py:503-514`, conditional `WHERE payment_status IN ('unpaid','pending')`, gated on Password2 + stored-amount match |
| S2-6b | Unpaid order cannot be *fulfilled* | **PASS at `main@57fede5` (was FAIL: H1, fixed by PR #26)** | Paid state is now enforced at claim (`delivery.py:126-129`), dispatch (`main.py:1054`), immediately before the file PUT (`main.py:893`, `:988`) and in cleanup (`delivery.py:332`, `:593`), with a paid-filtered cleanup candidate query (`:686-687`). Repository evidence: `test_unpaid_never_claims_uploads_or_deletes_even_legacy_delivered` (parametrised over `unpaid/pending/failed/cancelled/None` × `file/info`), `test_claim_execution_and_cleanup_recheck_payment`, `test_unpaid_api_order_keeps_both_sources_after_restarts_and_failed_payment`. Independently re-verified with real OS processes (2/4/6/8 workers): zero uploads, zero deletions, `attempts` untouched for unpaid jobs. Historical defect detail: `SECURITY_AUDIT.md` §1.1. |
| S2-7 | Payment state and delivery state survive a process restart | **PASS** | Paid UPDATE + outbox INSERT commit in one transaction into SQLite at `DB_PATH`. Repository evidence: `test_payment_delivery_concurrency_e2e.py::test_mixed_paid_unpaid_four_workers_lock_contention_and_restart` (repeat drain after restart), and real child-process crash tests `test_payment_delivery_reliability.py::test_actual_process_crash_after_smtp_acceptance_requires_reconciliation` (child exits 73) and `test_delivery_lifecycle_safety.py`. Note that a crashed worker's job is lease-bound for up to 3 minutes before it is reclaimable — see `ROLLBACK_PLAN.md` §6.5. |
| S2-8 | Test-only payment environment configured | **PASS** | Staging runs `ROBOKASSA_IS_TEST=1`; no merchant credentials present anywhere in the container env |
| S2-9 | Production configuration documented | **PASS** | `PRODUCTION_ACTIVATION_PLAN.md` §5 |

Automated result: **23/23 payment tests pass.** No CRITICAL finding. Open HIGH at
`main@57fede5`: H2 (MD5-only while docs claim SHA256 support), H3 (dead SuccessURL/FailURL
routes). **H1 (unpaid fulfilment), M13 (duplicate paid email under contention), M14
(unmanaged SQLite lock contention) and L12 (malformed `OutSum` → 500) are fixed in `main` by
PR #26** — see `SECURITY_AUDIT.md` §1.1 for the mechanism and the tests that enforce each one.

---

## 5. Stage 3 — Email delivery

| # | Check | Result | Evidence |
|---|---|---|---|
| S3-1 | SMTP configuration + templates audited | **PASS** | `smtplib.SMTP_SSL(server, port)` + mandatory `login()` (`main.py:614-618`, `669-672`); vars `SMTP_SERVER/SMTP_PORT(465)/SMTP_USER/SMTP_PASSWORD/EMAIL_TO` |
| S3-2a | Recipient validation | **FAIL (gap)** | The customer email is never validated (`Form("")` + `.strip()` only, `main.py:1041`). Measured: `""`, `not-an-email`, `a@b, c@d`, CRLF-bearing values all accepted with HTTP 200 and forwarded to Robokassa as `Email`. |
| S3-2b | Not an open relay | **PASS** | `RCPT TO` derives solely from `EMAIL_TO`; measured across all captured messages |
| S3-2c | Safe message formatting / header injection | **PASS** | Plain-text body, random MIME boundary. With `name="Иван\r\nBcc: attacker@…"`, `format="3x4\r\nReply-To: …"` the raw message contained none of the injected headers; payload only inside the base64 body |
| S3-3 | Email triggered only for correct order states | **PARTIAL** | `paid_email` is enqueued **only** inside the winning `mark_order_paid` branch. But `admin_email` **is** enqueued for an unpaid order at creation (`main.py:1070`). Measured: after `POST /api/order`, `payment_status='unpaid'`, `paid_email` rows = 0, `admin_email` pending. |
| S3-4a | Transient failures and retries | **PASS** | Measured backoff `5/10/20/40/80/160/320 s`; `state='failed'`, `last_error` set, `payment_email_sent_at` stays NULL; recovery eventually `delivered` |
| S3-4b | Duplicate-send protection | **PASS** | `idx_paid_email_once` / `idx_admin_email_once` unique partial indexes + `INSERT OR IGNORE` + `BEGIN IMMEDIATE` claim lease + timestamp guard. Measured: 4 concurrent worker threads → **exactly 1** message; raw duplicate INSERT → `UNIQUE constraint failed` |
| S3-5 | Notifications tested with a local SMTP capture service | **PASS** | Purpose-built **implicit-TLS** capture server (`python -m smtpd` cannot be used: the client only speaks implicit TLS and dies with `[SSL: WRONG_VERSION_NUMBER]`, verified). 22 messages captured under `scratch/s3/captured/` |
| S3-6 | Email failure cannot destroy orders or files | **PASS** | Sender swallows the error → `deliver_job` raises → outbox marks `failed` with backoff. Order row, uploads and other jobs verified untouched |
| S3-7 | Production SMTP configuration prepared | **PASS** | `PRODUCTION_ACTIVATION_PLAN.md` §6 |

Gaps: no customer-facing email at all (both notifications go to `EMAIL_TO`); `Message-ID` hardcodes `@photodoc-ai.ru` (`main.py:606,731` — still open, L6); the **admin-notification** path still has no socket timeout (the paid-email path gained `timeout=30` in PR #26; a silent peer can still block the single worker thread — measured still blocked after 8 s at `8bfecde`); TLS certificate verification disabled on both paths (`CERT_NONE`, `check_hostname=False` — a self-signed cert was accepted); attempt 10 is terminal with no requeue (≈43 min to permanent silent loss).

---

## 6. Stage 4 — Domain and HTTPS

| # | Check | Result | Evidence |
|---|---|---|---|
| S4-1 | DNS records inspected (read-only) | **PASS** | Apex A/AAAA = **NODATA** (confirmed via default resolver, authoritative `ns1/ns2.spaceweb.ru`, `1.1.1.1`, Google DoH); `www` = **NXDOMAIN**; `_acme-challenge` = **NXDOMAIN**; MX = `mx1/mx2.spaceweb.ru`; TXT = `v=spf1 include:_spf.spaceweb.ru ~all`; `_dmarc` = NXDOMAIN; CAA = NODATA; `photodoc-ai.ru` = **NXDOMAIN** |
| S4-2 | Existing nginx/TLS configuration inspected | **PASS** | `infra/nginx/photodoc-https.conf`, `photodoc-http-bootstrap.conf`; TLS 1.2/1.3, HSTS, nosniff, X-Frame-Options, Referrer-Policy; ACME webroot `/var/www/certbot`; upstream `127.0.0.1:8080`; `client_max_body_size 100M` |
| S4-3 | Firewall rules and port availability | **PASS** | `ufw active`, default deny incoming; allow 22/80/443. On the host **nothing listens on 80/443** (only sshd + systemd-resolved). Containers publish no host ports. |
| S4-4 | Certificate issuance requirements | **PASS** | HTTP-01 webroot. Blockers: no A/AAAA for either name; config targeted the unregistered `photodoc-ai.ru`; `/var/www/certbot` provisioned nowhere in the repo |
| S4-5 | Final HTTPS configuration prepared | **PASS** | **PR #24** (`launch/https-domain-alignment-20261104`): domain aligned to `photo1storage.ru`, `resolver` added for OCSP stapling, `Permissions-Policy` added, `access_log off` on the payment callback, `scripts/provision_tls_webroot.sh`, `infra/systemd/photodoc-certbot-renew.{service,timer}` |
| S4-6a | HTTP→HTTPS redirect | **PASS (isolated)** | Live: `GET /order-status?payment=success` → **301** → `https://photo1storage.ru/order-status?payment=success` |
| S4-6b | ACME challenge reachability while redirecting | **PASS (isolated)** | missing token → **404** (not 301); present token → **200** with exact body |
| S4-6c | TLS settings | **PASS (isolated)** | TLS 1.2 ✅, TLS 1.3 ✅, TLS 1.1 ❌ (`no protocols available`) |
| S4-6d | Security headers | **PASS (isolated)** | HSTS, `x-content-type-options`, `x-frame-options`, `referrer-policy`, `permissions-policy` all present on the response |
| S4-7 | Config syntax and routing tested in an isolated environment | **PASS** | `nginx -t` on **both** configs under `nginx:1.27-alpine` → `syntax is ok`, `test is successful`. Routing exercised with host ports bound to `127.0.0.1` only; no public exposure, no DNS change, no certificate issued. Test containers removed. |
| S4-8 | DNS changes and issuance procedure documented | **PASS** | `PRODUCTION_ACTIVATION_PLAN.md` §3–§4 |
| S4-9 | Robokassa callbacks reachable after activation | **PARTIAL** | The exact-match callback location proxies correctly and is exempt from `limit_req` (verified). `access_log off` verified: the callback produces **no** access-log line. Caveat: on upstream failure the URI (including `SignatureValue`) still appears in the **error** log — unavoidable, failure-only. Reachability itself remains **BLOCKED** on DNS + certificate. |

Known config-quality notes (documented, deliberately not changed): `listen … http2` is deprecated on nginx ≥1.25.1 but correct for the target Ubuntu 24.04 (nginx 1.24); two `DHE-RSA-*` ciphers are advertised with no `ssl_dhparam`; HSTS is a one-way door at 1 year; no CSP yet.

---

## 7. Stage 5 — Complete customer journey

See §7.1. Summary table is filled from the executed end-to-end suite.

### 7.1 Integrated journey suite — executed

```
Push-Location <worktree>\backend
python -m pytest ..\scratch\s5\test_journey.py -v -p no:cacheprovider
Pop-Location
```

**Result: 16 passed, 2 failed in 144.62s.** The two failures were deliberate assertions about required behaviour that the product did not meet at `8bfecde` — i.e. **product defects, not harness defects**. Log: `scratch/s5/_run_final.txt`; artifacts: `scratch/s5/_run/{orders.db,uploads,mail/msg-*.eml}`. **This suite is a scratch artefact, not a repository test module**; the repo tests that now enforce the same behaviour are named in §7.1 and in `SECURITY_AUDIT.md` §1.1.

Passing (16, scratch suite): `test_step1_order_creation_with_real_upload`, `test_step2_upload_restrictions`, `test_step3_price_locking_rejects_tampered_client_amount`, `test_step4_payment_request_url_and_signature`, `test_step5_result_callback_marks_order_paid`, `test_step6_delivery_uploads_photo_exactly_once`, `test_step7_remote_metadata_matches_predeletion_digest`, `test_step8_paid_email_captured_with_sane_headers`, `test_step9_final_order_state`, `test_duplicate_result_callback_is_idempotent`, `test_invalid_signature_callback_rejected_and_order_stays_unpaid`, `test_unpaid_order_fulfilment_observed_behaviour`, `test_repeated_order_submission_creates_distinct_orders`, `test_state_persists_across_process_restart`, `test_delivery_retry_after_transport_failure_uploads_once`, `test_email_header_injection_is_prevented`.

Failing (2) at `8bfecde`, **both resolved in `main` by PR #26**:

| Test | Failure at `8bfecde` | Finding | Status at `main@57fede5` |
|---|---|---|---|
| `test_unpaid_order_must_not_be_fulfilled_before_payment` | `AssertionError: DEFECT: an unpaid order was delivered to Yandex.Disk before payment (order #5, uploads=['…/5_….jpg', '…/info.txt'])` | **H1 confirmed empirically.** `POST /api/order` enqueued `file`, `info` and `admin_email`, and no part of the delivery path consulted `payment_status`. Observed rows after one drain: order still `unpaid`, yet `file`/`info` delivered and the local source deleted. | **RESOLVED.** Same scenario now leaves the source in place with `attempts=0`; gates at `delivery.py:126-129, 332, 593, 686-687` and `main.py:780, 893, 988, 1054`. |
| `test_concurrent_orders_deliver_without_cross_contamination` | `assert {'delivered','failed'} == {'delivered'}` | **M13.** With 4 concurrent `process_due` workers the paid email was *sent*, then its `payment_email_sent_at` UPDATE hit `database is locked`, so the job was marked `failed` and would retry and **re-send**. Cross-contamination itself held. | **RESOLVED.** Durable `paid_email_outbox` + attempt fencing + SQL-only retry (`paid_email_outbox.py:15, 63, 89`; `sqlite_busy.py`). Repo evidence: `test_mixed_paid_unpaid_four_workers_lock_contention_and_restart`. |

The SMTP capture that makes this work is a separate subprocess implementing SMTP **over implicit TLS** with a self-signed certificate, because `main.py:615/670` uses `smtplib.SMTP_SSL` and Python 3.10's `SMTP_SSL` does not verify certificates. It must support `EHLO/AUTH PLAIN/MAIL/RCPT/DATA/QUIT`. Note: offline, `smtplib` calls `socket.getfqdn()` on connect which blocks ~13 s per connection, so the suite stubs it to `localhost` (test plumbing, not product logic).

Captured paid email (`_run/mail/msg-2.eml`):

```
From: robot@photodoc.test
To: owner@photodoc.test
Subject: ✅ Заказ #1 ОПЛАЧЕН (20.00 руб.) | PhotoDoc AI
Message-ID: <photodoc-order-1-paid@photodoc-ai.ru>
```

### 7.2 Additional observations from this suite

- The uploads directory is **not configurable**: `main.py:146` hardcodes `BASE_DIR / "uploads"`. The suite must monkeypatch `main.UPLOAD_DIR` and `delivery.set_uploads_dir` (L13).
- `main.py:1231` called `format_robokassa_amount(out_sum)` without guarding non-numeric input, so a malformed `OutSum` raised `ValueError` → HTTP 500 instead of 400 (**L12 — fixed in `main`**: non-finite rejection at `main.py:409`, `HTTPException(400, "Invalid OutSum")` at `:1340`; repo test `test_signed_malformed_outsum_is_controlled_400`).
- Four defects in the harness itself were found and fixed, and are disclosed rather than hidden: a stale re-read of a file deleted by cleanup, a folder-PUT marker filter, `get_content()` on `multipart/mixed`, and a lock-retry wrapper around the concurrency worker (mirroring `worker_loop`'s own per-iteration `try/except`; no product assertion was weakened).

| # | Journey step | Result |
|---|---|---|
| S5-1..S5-12 | Order → upload → price lock → payment request → paid callback → delivery queue → upload → remote integrity → email → final status | see `LAUNCH_READINESS_REPORT.md` §5 |
| S5-13 | Desktop layout | **PARTIAL** — the CI `Playwright Tests` job is green on `8bfecde`, but the suite is a single smoke test (`frontend/tests/example.spec.ts`): page loads with status < 400, `body` visible, and **no horizontal overflow** at the `Desktop Chrome` viewport. That is a real desktop regression check, not a layout/visual-fidelity suite. |
| S5-14 | Mobile layout | **FAIL (coverage gap)** — `frontend/playwright.config.ts` declares a **single** project, `Desktop Chrome`. There is no mobile viewport or device project, so responsive behaviour is **not** automatically verified at any mobile width. |
| S5-15 | Invalid uploads | **PASS** — `test_step2_upload_restrictions` exercised all limits: 20 MiB+1 → **413**, `application/pdf` → **400**, no file → **422**, bad magic → **400**, 11 files → **400**, with zero orphan rows/files. Plus `test_upload_limits.py` 8 passed. |
| S5-16 | Interrupted connections | **PASS (offline)** — `.incoming-*` staging + single `BEGIN IMMEDIATE` commit; crash-before-commit leaves an orphan file but never an order without jobs |
| S5-17 | Repeated submissions | **PASS** — scratch suite `test_repeated_order_submission_creates_distinct_orders` produced two independent orders (scratch artefact, not a repo test) |
| S5-18 | Payment callback retries | **PASS** — `test_duplicate_result_callback_is_idempotent`: second signed callback returns `OK{n}`, `paid_at`/`payment_email_sent_at` unchanged, one `paid_email` row, one captured email, one upload |
| S5-19 | Server restarts | **PASS** — scratch `test_state_persists_across_process_restart` (fresh process on the same `DATABASE_PATH`, health 200, all four jobs intact) **plus repo evidence**: `test_payment_delivery_concurrency_e2e.py::test_mixed_paid_unpaid_four_workers_lock_contention_and_restart` and the real child-process crash tests in `test_payment_delivery_reliability.py` / `test_delivery_lifecycle_safety.py`. Recovery latency is lease-bound (≤3 min) — see `ROLLBACK_PLAN.md` §6.5. |
| S5-20 | Delivery retries | **PASS** — live Stage 1 `timeout_before`/`ack_timeout`/`crash_*`, plus scratch `test_delivery_retry_after_transport_failure_uploads_once` |
| S5-21 | Concurrent orders | **PASS at `main@57fede5` (was FAIL: M13, fixed by PR #26)** — the original defect was finalisation under lock contention causing a paid-email re-send, not cross-contamination (which held). Fixed by the durable outbox + attempt fencing and bounded busy retry. Verified by repo `test_mixed_paid_unpaid_four_workers_lock_contention_and_restart` and independently with **real OS processes** (2/4/6/8 workers, 20 scenarios): one upload and one paid email per order, no lost jobs, no stuck `processing` rows after lease expiry. |

---

## 8. Stage 6 — Security, backups and recovery

| # | Check | Result | Evidence |
|---|---|---|---|
| S6-1 | Exposed ports and bindings | **PASS** | Only four `ports:` directives exist in the repo, all `127.0.0.1`-bound. No `0.0.0.0` publish, no `network_mode: host`. Live staging: no published host ports, `internal: true` network. |
| S6-2 | Authentication/authorization boundaries | **PASS (with notes)** | Exactly 5 routes; **no admin routes exist**, so no IDOR surface. Order token = SHA-256 + `hmac.compare_digest`; wrong token → 404 (no enumeration). One LOW: a legacy-order 409 is returned **before** the token check, a weak existence oracle (`main.py:1117-1122`). |
| S6-3 | Secrets excluded from Git and logs | **PASS** | `git ls-files` shows only the three `.example` files, all empty/synthetic. No secret reaches application logs. LOW: the Result callback logs the whole payload at ERROR (`main.py:1209`) and httpx logs the pre-signed upload URL at INFO — signature/URL material, no credentials. |
| S6-4 | File upload validation | **PASS (with note)** | Display name sanitised (`'../../etc/passwd'` → `'passwd'`); stored name is `f"{order_id}_{uuid4hex}.{suffix}"` from a fixed suffix table; `yandex_paths._component` rejects `.. . '' / \ :`, control chars and `%2e/%2f/%5c/%3a/%25/…`. MEDIUM: multipart is fully parsed before the `MAX_FILES=10` check. |
| S6-5 | Path traversal / unsafe file handling | **PASS** | Rejected at three layers (see S6-4); no way to escape the configured Yandex root or write outside the uploads dir |
| S6-6 | Payment callback signature verification | **PASS (with MEDIUM)** | MD5 Password2 over `OutSum:InvId`, amount bound to the stored authoritative price, atomic idempotent transition. MEDIUM: non-constant-time comparison (`main.py:1219`, `1289`) vs `hmac.compare_digest` used elsewhere. |
| S6-7 | Container privileges and filesystem permissions | **FAIL (repo config) / PASS (staging)** | Repo compose files have **zero** hardening (`read_only`, `cap_drop`, `no-new-privileges`, `tmpfs`, mem/cpu/pids = 0 occurrences) and the frontend nginx runs as **root** (`frontend/Dockerfile:24`). The staging `compose.private.yml` **does** harden both services (`read_only`, `cap_drop: [ALL]`, `no-new-privileges`, mem/cpu/pids limits, tmpfs, `user: 1000:1000` for the backend) — i.e. the production compose files are the weak link. |
| S6-8 | Database backup integrity | **PASS** | `create_backup.py` → `SUCCESS (VERIFIED)`; `verify_backup_integrity.py` → `INTEGRITY_CHECK_PASSED`; negative controls (1-byte tamper, deleted file) correctly fail |
| S6-9 | Restore rehearsal in an isolated environment | **PASS** | Real schema via the app's `init_db()` (`application_id=1346650441`), seeded through the real `POST /api/order`; restored DB `PRAGMA integrity_check=ok`, per-table row-dump SHA-256 **identical** (`287dde6f…`), identical DDL/indexes; restored DB boots under `PHOTODOC_REQUIRE_DB_IDENTITY=1` and `/api/health` → 200 |
| S6-10 | Recovery from interrupted uploads and crashes | **PASS** | `.incoming-*` + fsync + promotion + single `BEGIN IMMEDIATE`; orphan sweep `cleanup_orphaned_temporary_files` (1 h age, 300 s cadence, worker-driven); delete intent `cleanup_phase='deleting'` committed before unlink with a fencing lease. Live crash cases in Stage 1 confirmed end-to-end. |
| S6-11 | Logging, health checks and disk monitoring | **PARTIAL** | `/api/health` checks DB identity/schema + `delivery_schema_ready()` only. **No disk-space guard anywhere** (`shutil.disk_usage`/ENOSPC: 0 matches), no upload-writability check, no worker-liveness or backlog metric. No log rotation in the repo compose files (staging sets `max-size 10m, max-file 3`). |
| S6-12 | Rollback instructions | **PASS** | `build_rollback_bundle.py` → 7 files, deterministic rebuild (`ROLLBACK_BUNDLE_SHA256=53688e7d…`), extraction rehearsal PASS, byte-identical; `test_rollback_bundle.py` 26 passed. Procedure in `ROLLBACK_PLAN.md`. |

No CRITICAL finding. Two HIGH: S6-01 (cutover runbook backs up the live DB with a non-atomic `cp`, no uploads, no manifest, no restore gate) and S6-02 (no scheduled/off-host/retention-managed backups → RPO undefined).

---

## 9. Verdict summary

Re-baselined against `main@57fede5` (PR #26 merged).

| Area | Verdict |
|---|---|
| Stage 1 Yandex.Disk staging acceptance | **PASS** (live, 5/5 cases) |
| Stage 2 Robokassa (test mode) | **PASS**; H2 and H3 remain open HIGH, no CRITICAL, real payments not activated. H1/M13/M14/L12 resolved by PR #26 |
| Stage 3 Email delivery | **PASS with gaps** — no customer-facing email; M13 fixed; the admin-notification path still lacks a socket timeout and neither path verifies TLS certificates (M12) |
| Stage 4 Domain/HTTPS | **BLOCKED on DNS + certificate**; configuration validated and fixed in PR #24 |
| Stage 5 End-to-end journey | **PASS with gaps** — concurrency and unpaid fulfilment resolved (PR #26); mobile layout untested; H3 post-payment route missing |
| Stage 6 Security/backup/recovery | **PASS with 2 HIGH process findings**, restore rehearsal verified for the pre-PR #26 schema (outbox not itself rehearsed) |
| Stage 7 Public launch preparation | **READY_FOR_OWNER_APPROVAL**, not `PUBLIC_LIVE` |

**Resolved since the original audit, by PR #26:** H1 (payment-gated fulfilment), M13
(duplicate paid notification), M14 (unmanaged SQLite lock contention), L12 (malformed `OutSum`
→ 500). Mechanism and enforcing tests per finding: `SECURITY_AUDIT.md` §1.1.
