# PhotoDoc AI — Launch Readiness Report

**Program:** autonomous seven-stage launch-readiness execution
**Date:** 2026-11-04 (UTC)
**Repository:** `life2boat/photodoc-ai`
**Verified commit:** `8bfecdee756e11972cbb68ec3f7c0e009a1cba60` (merge of PR #23)
**Staging host:** `photodoc-vps` = `95.181.154.251`, Ubuntu 24.04.4 LTS
**Domain:** `photo1storage.ru`

---

## 0. Execution environment (reported at startup)

```
MODEL_ID          deepseek-flash
                  (the identifier the harness reports for this session; no DSH_* variable
                   exposes a model string and this session exposes no model-selection control,
                   so no stronger model could be selected from inside the run)
HARNESS_VERSION   DeepSeek Harness 0.2.0-rc.2  (ProductVersion 0.2.0.0, channel: nightly)
EXECUTION_MODE    autonomous single-run agent; profile "desktop"; shell pwsh;
                  file policy workspace-write at start -> danger-full-access from the
                  point the operator changed the session policy; approval prompts later
                  disabled by the operator
AVAILABLE_TOOLS   pwsh, read, write, edit, glob, grep, read_image, present,
                  todo_write, create_goal, get_goal, update_goal,
                  subagent, subagent_fork, send_message, list_agents,
                  interrupt_agent, job_output, job_list, job_kill,
                  skill, ask_user_question, exit_plan_mode,
                  web_search, web_fetch, workflow
```

### 0.1 Startup blocker that had to be cleared first

Every tool call failed at start with `SetNamedSecurityInfoW failed (Win32 5): grantWrite(F:\диск е\landing)`.
Read-only diagnostics established:

- The workspace exists, is readable, and the session user owns it (`HOME\Oleg`).
- Read, list, write and delete all succeed **once the sandbox is not provisioning a write grant** — verified by an explicit create/delete probe.
- The directory ACL contains explicit `Modify` entries plus two orphaned foreign SIDs and `HOME\CodexSandboxUsers`; there are **no** `S-1-15-2-*` AppContainer package entries (the usual cause of this failure). Drive `F:` root is owned by `NT AUTHORITY\СИСТЕМА`.
- Conclusion: the failure came from DSH's **workspace-write grant provisioning**, not from the folder being unreadable, and not from a missing workspace.

An ACL repair was attempted once and **rejected by the operator**, whose instruction was: do not change ACLs/ownership/inheritance on `F:`; diagnose read-only and propose an alternative workspace if needed. The policy was then switched to `danger-full-access`, after which no write grant is provisioned and **every operation succeeded normally**. No ACL, ownership or inheritance change was ever made. No alternative workspace was needed.

---

## 1. Task ledger

| Stage | Status | Start (UTC) | Finish (UTC) | Verdict |
|---|---|---|---|---|
| 0 — Preflight (workspace, SSH, PR, worktree) | **PASS** | 06:20 | 06:24 | ready |
| 1 — Yandex.Disk staging acceptance | **PASS** | 06:24 | 03:57 (server clock) / 06:36 | live acceptance `PASS`, 5/5 cases |
| 2 — Robokassa | **PASS (with HIGH findings)** | 06:24 | 06:30 | 23/23 tests pass, no CRITICAL, real payments untouched |
| 3 — Email delivery | **PASS (with gaps)** | 06:24 | 06:48 | capture-test verified, production checklist ready |
| 4 — Domain and HTTPS | **READY_FOR_APPROVAL** | 06:24 | 03:52 | config fixed + validated in PR #24; **BLOCKED on DNS** |
| 5 — Complete customer journey | see §5 | 06:24 | — | see §5 |
| 6 — Security, backups, recovery | **PASS (with 2 HIGH process findings)** | 06:24 | 06:46 | no CRITICAL; restore rehearsal passed |
| 7 — Public launch preparation | **READY_FOR_OWNER_APPROVAL** | 06:20 | 07:00 | six deliverables produced |

Stage 2/3/4/6 audits ran concurrently in isolated scopes; Stage 1 was executed sequentially by the primary agent because it mutates the staging host.

---

## 2. Stage 1 — Yandex.Disk staging acceptance

**Verdict: PASS.** Full evidence table in `ACCEPTANCE_MATRIX.md` §3.

- PR #23 confirmed merged (`8bfecde`), CI green, and the CI-published image for that exact commit pulled and verified by its `org.opencontainers.image.revision` label. This is the project's sanctioned zero-build VPS architecture — nothing is compiled on the 2 GB host.
- Deployed staging version before the run was `febae24` (PR #22) — one merge behind, as the mission expected.
- A verified backup was taken before every change (consistent SQLite snapshot via the backup API, integrity-checked, logical hash equal to live, upload checksums equal). Baseline logical hash `8981540f…` is **identical** to the previous session's baseline, independently proving staging data was untouched.
- The backend was rolled onto the PR #23 image inside the existing `photodoc-pr22-private` project, on the internal network, with **no published ports**, `DELIVERY_WORKER_ENABLED=0`, and the OAuth token injected **backend-only** through a bounded CONNECT proxy that only permits Yandex hosts.
- **The temporary namespace guard was removed from the active test path.** The new harness has no synthetic-409 ancestor guard. Instead the native code performs a read-only `fields=type` lookup on `Приложения` and `Приложения/PhotoDoc` (`ancestor_readonly_lookups=2`) and issues mkdir only for 404 paths (`ancestor_mkdir_attempts=0`, `mkdir=2` = the run root and the case directory). The historical Nov 3 harness directory was left byte-identical.
- All five live cases passed:

| Case | What it proves | Outcome |
|---|---|---|
| `success` | native delivery, size + SHA-256, single upload | `delivered`, attempts 1, independent re-download SHA-256 matched |
| `timeout_before` | retry with source retention | failed with source intact → retried → `delivered` |
| `ack_timeout` | idempotence after an ambiguous acknowledgement | upload stayed at **1**; no duplicate delivery |
| `crash_commit` | crash during commit | resumed → `delivered`, no lost source |
| `crash_cleanup` | crash during cleanup | cleanup resumed → `completed`, no lost source |

- Existing customer data and existing remote files preserved: DB logical hash unchanged, both uploads byte-identical, all pre-existing remote objects unchanged, and the three pre-existing queued jobs still `pending / attempts=0`.
- Worker returned to its original disabled state; runtime secret unloaded (token count 0); compose restored byte-identically; temporary proxy and network removed; staging healthy and still private.

**Honest audit trail:** five runs were required. The first four failed on defects in **my acceptance driver** (ancestor lookups wrongly rejected; a counter that counted legitimate mkdirs as ancestor attempts; a no-redirect opener for a 302 download; a snapshot pass with no retry on a transient non-200). In two of those runs the product's delivery had actually succeeded — the evidence is preserved. No remote folder was ever deleted. The final run is self-contained and reports `PASS`.

**Residual risk requiring owner confirmation:** the staging OAuth token belongs to account **`mdot22` (uid 594353979)** — a real personal account with 3.4 GB used, **not** a dedicated test account as `YANDEX_ACCEPTANCE_READINESS.md` requires. Containment is enforced by app-folder scoping (`app:/` = `disk:/Приложения/PhotoDoc`) and that folder contained no customer data, but provenance should be confirmed by the owner.

---

## 3. Stage 2 — Robokassa

**Verdict: PASS with HIGH findings; no CRITICAL; real payments not activated.** Details in `SECURITY_AUDIT.md` and `ACCEPTANCE_MATRIX.md` §4.

Core invariants hold and are test-backed (23/23 payment tests pass):

- Server-side price authority: `amount_to_pay = authoritative_amount`; a client amount is only an equality assertion. `order_amount` is the sole authority, `payment_amount` is explicitly demoted.
- Callback signature `md5(OutSum:InvId:Password2)`, with the amount bound both cryptographically and against the stored price.
- A single conditional writer for `paid`, with the paid-email outbox row enqueued in the **same transaction**; concurrent and duplicate callbacks are idempotent.
- No merchant credentials anywhere in the repo or the staging runtime (`ROBOKASSA_IS_TEST=1`); staging cannot take real money.

HIGH findings:

- **H1 — fulfilment is not payment-gated.** `file`/`info` delivery jobs are enqueued at order creation while the order is `unpaid`, and the worker defaults to enabled. An abandoned unpaid order's photos are uploaded to Yandex.Disk and the local originals deleted. **Owner decision required** — this may be intentional (files reaching the print operator early), but it should be a decision, not an accident.
- **H2 — MD5 only.** The launch register claims MD5 *or* SHA-256. If the cabinet is switched to SHA-256 every callback returns 400 and no order is ever confirmed — silently.
- **H3 — dead return URLs.** The documented SuccessURL/FailURL (`/order-status?payment=…`) do not exist: there is no frontend router and nothing calls the SuccessURL handler.

---

## 4. Stage 3 — Email delivery

**Verdict: PASS with gaps; nothing sent to real customers.** Details in `ACCEPTANCE_MATRIX.md` §5.

- Verified with a purpose-built **implicit-TLS** SMTP capture server (the standard `python -m smtpd` cannot be used: the client speaks implicit TLS only and dies with `[SSL: WRONG_VERSION_NUMBER]` — verified). 22 messages captured.
- Retry/backoff measured exactly (`5/10/20/40/80/160/320 s`); `payment_email_sent_at` stays NULL until a send truly succeeds; order rows, uploads and unrelated jobs never affected by an SMTP failure.
- Duplicate protection verified empirically: unique partial indexes + `INSERT OR IGNORE` + claim lease + timestamp guard; 4 concurrent workers produced **exactly one** message; a raw duplicate INSERT hit `UNIQUE constraint failed`.
- Header injection from user-controlled fields is **not** possible (verified with CRLF payloads in `name`, `format`, `comment`); the client is not an open relay.
- `paid_email` is gated on the paid transition. But `admin_email` **is** sent for an unpaid order at creation.

Gaps: no customer-facing email at all (both notifications go to `EMAIL_TO`); `Message-ID` hardcodes the unregistered `@photodoc-ai.ru`; no socket timeout; TLS certificate verification disabled (a self-signed certificate was accepted); attempt 10 is terminal with no requeue (≈43 min to permanent silent loss); customer email is unvalidated and forwarded to the PSP.

---

## 5. Stage 5 — Complete customer journey

Journey coverage is established from three independent evidence sources, because no single environment can exercise all of it end-to-end:

| Journey step | Evidence | Result |
|---|---|---|
| 1. Open the site | CI `Playwright Tests` green on `8bfecde`; smoke test asserts load < 400, body visible, no horizontal overflow at Desktop Chrome viewport | **PASS (desktop smoke)** |
| 2–4. Create order, upload files, size/format limits | `test_upload_limits.py` 8 passed; live order creation exercised on staging | **PASS** |
| 5. Calculate and lock price | 23/23 payment tests including underpayment/tamper rejection | **PASS** |
| 6. Create payment request | `test_payment_create_authoritative_without_client_amount` (exact invariant signature) | **PASS** |
| 7. Simulate successful payment | `test_result_callback_idempotency` etc.; no real payment attempted | **PASS** |
| 8–10. Delivery queue → Yandex upload → remote integrity | **Live** Stage 1: 5/5 cases, size and SHA-256 verified remotely, one upload per case | **PASS** |
| 11. Email notification | Stage 3 capture tests | **PASS (to operator inbox only)** |
| 12. Final order status | `payment_status`/`status` transitions asserted in payment tests; delivery state in `delivery_jobs` | **PASS** |

Additional scenarios:

| Scenario | Result |
|---|---|
| Desktop layout | **PARTIAL** — smoke test only (no visual/layout suite) |
| Mobile layout | **FAIL (coverage gap)** — `frontend/playwright.config.ts` has a single `Desktop Chrome` project; no mobile viewport is tested |
| Invalid uploads | **PASS** (8 tests) |
| Interrupted connections | **PASS** — `.incoming-*` staging + single `BEGIN IMMEDIATE`; crash-before-commit cannot produce an order without jobs |
| Repeated submissions | **PASS** — `test_repeated_order_submission_creates_distinct_orders` |
| Payment callback retries | **PASS** — duplicate signed callback: one state change, one email, one upload |
| Server restarts | **PASS** — `test_state_persists_across_process_restart`: fresh process on the same DB, paid state and all four job states intact, no re-upload |
| Delivery retries | **PASS** — live `timeout_before`, `ack_timeout`, `crash_commit`, `crash_cleanup`, plus `test_delivery_retry_after_transport_failure_uploads_once` |
| Concurrent orders | **FAIL (product defect M13)** — cross-contamination held (one upload per order, distinct paths, matching digests, one paid email per order) but finalisation failed under lock contention and the paid email will be re-sent |

Two structural notes rather than test results:

- **H1** means an unpaid order can reach delivery. Any "no unpaid order reaches delivery" guarantee is **not** met by the current design.
- **H3** means the customer's browser is redirected to a route that does not exist after paying.

Captured operator notification (real message from the local implicit-TLS capture server, decoded):

```
From: robot@photodoc.test
To: owner@photodoc.test                     <- the sole recipient; the customer never receives mail
Subject: Новый заказ #1 | PhotoDoc AI       (base64-encoded UTF-8)
Message-ID: <photodoc-order-1-new@photodoc-ai.ru>   <- hardcoded unregistered domain (L6)
Envelope: MAIL FROM:<robot@photodoc.test>  RCPT TO:<owner@photodoc.test>
Body: plain text, base64, contains the order fields and a Yandex.Disk deep link
```

**Integrated journey suite: executed — 16 passed, 2 failed.** Both failures are red assertions about required behaviour that the product does not meet, i.e. product defects, not harness defects (the four harness defects that were found are disclosed in `ACCEPTANCE_MATRIX.md` §7.2). The suite drives the real FastAPI app in-process with an isolated database and uploads directory, a fake Yandex.Disk transport, and a separate subprocess SMTP capture server implementing **implicit TLS** (required: `main.py:615/670` uses `smtplib.SMTP_SSL`).

Verified across the nine journey steps plus seven robustness cases: order creation with a real multipart upload and server-computed `20.00`; upload limits (413 / 400 / 422 / 400 / 400, zero orphans); price locking rejecting `0.01 / 1.00 / 300.00 / 19.99`; the exact invoice signature `md5(login:OutSum:InvId:password1)`; a correctly signed callback marking the order paid; exactly one photo and one `info.txt` PUT with matching size and SHA-256; remote metadata matching the pre-deletion digest (with a negative control proving the verifier is not a no-op); exactly one captured paid email with sane headers; duplicate-callback idempotence; invalid-signature rejection; repeated submission creating distinct orders; **process-restart persistence**; delivery retry after a transport failure; and header-injection prevention.

The two failures:

| Failure | What it establishes |
|---|---|
| `test_unpaid_order_must_not_be_fulfilled_before_payment` | **H1 is now empirically proven, not merely read from code.** For a never-paid order, after a single drain: the photo **and** `info.txt` were uploaded to Yandex.Disk and the local source was deleted (`cleanup_phase='deleted'`), while the order still read `payment_status='unpaid'`, `paid_at=NULL`. `POST /api/order` enqueues `file`/`info`/`admin_email` (`main.py:1056, 1067, 1070`) and no part of the delivery path consults `payment_status`. |
| `test_concurrent_orders_deliver_without_cross_contamination` | **New finding M13.** With 4 concurrent workers the paid email was *sent*, then its `payment_email_sent_at` UPDATE (`main.py:675-680`; default 5 s SQLite timeout, no busy retry) hit `database is locked`, so the job was marked `failed` and will retry and **re-send** → duplicate paid notifications. Cross-contamination itself held. Environment-amplified (this host measures ~88 ms per write transaction), so it is a contention risk rather than a certainty, but the at-least-once window is real. |

---

## 6. Stage 4 — Domain and HTTPS

**Verdict: READY_FOR_APPROVAL; activation BLOCKED on DNS.** Details in `ACCEPTANCE_MATRIX.md` §6 and `PRODUCTION_ACTIVATION_PLAN.md` §3–§4.

Two independent hard blockers were found:

1. **No A record exists.** `photo1storage.ru` A/AAAA are NODATA at the apex (confirmed via the default resolver, authoritative `ns1/ns2.spaceweb.ru`, `1.1.1.1` and Google DoH); `www` and `_acme-challenge` are NXDOMAIN. HTTP-01 validation is impossible today.
2. **The config targeted an unregistered domain.** Both nginx configs, the certificate paths and every documented `certbot -d` argument used `photodoc-ai.ru`, which is NXDOMAIN and which the repo's own procurement register lists as *not owned*. The owned domain is `photo1storage.ru`. Even with DNS fixed, a certificate would land in `…/live/photo1storage.ru/` while nginx read `…/live/photodoc-ai.ru/` → `nginx -t` fails closed.

Work completed:

- **PR #24** aligns the config to the owned domain, adds the missing `resolver` for OCSP stapling, adds `Permissions-Policy`, disables access logging on the signature-bearing payment callback, adds the previously non-existent ACME webroot provisioning script, and adds certbot renewal `systemd` units (there was no renewal automation at all).
- **Validated in an isolated container** (no DNS change, no certificate, no public exposure — host ports bound to `127.0.0.1` only):
  - `nginx -t` passes on both configs;
  - HTTP → `301` to `https://photo1storage.ru/…`;
  - the ACME challenge path is exempt from the redirect (`404` absent, `200` present with exact content);
  - HSTS / `X-Content-Type-Options` / `X-Frame-Options` / `Referrer-Policy` / `Permissions-Policy` all present;
  - TLS 1.2 and 1.3 negotiate, TLS 1.1 is rejected;
  - the Robokassa callback location proxies and is exempt from `limit_req`, and produces no access-log line.

Firewall state was inspected: `ufw` active with default deny inbound and 22/80/443 allowed; on the host **nothing currently listens on 80 or 443**.

---

## 7. Stage 6 — Security, backups and recovery

**Verdict: PASS with 2 HIGH process findings; no CRITICAL.** Full table in `SECURITY_AUDIT.md`; backup detail in `BACKUP_RESTORE_REPORT.md`.

- **Exposed ports:** only four `ports:` directives exist across all compose files, every one loopback-bound; no `0.0.0.0` publish, no `network_mode: host`; live staging publishes nothing and sits on an `internal: true` network.
- **Authorization:** exactly five routes and **no admin routes at all**, so no IDOR surface; order tokens are SHA-256 + `hmac.compare_digest`; wrong token → 404 (no enumeration).
- **Secrets:** none committed (only synthetic `.example` files); none reach the logs; the staging token sits in a root-only `0600` file that is not mounted and is absent from the running environment — verified after the acceptance run.
- **Uploads / traversal:** rejected at three independent layers (display-name sanitisation, UUID-derived on-disk names, and a namespace-aware path validator); presented traversal payloads all rejected.
- **Container hardening:** staging is well hardened (`read_only`, `cap_drop: [ALL]`, `no-new-privileges`, mem/cpu/pids limits, tmpfs, non-root backend). **The repo's production compose files have none of this and the frontend nginx runs as root** — that is the gap to fix before launch.
- **Backup integrity + restore rehearsal: executed and PASSED.** Real schema via the app's `init_db()`, seeded through the real `POST /api/order`; `create_backup.py` → `SUCCESS (VERIFIED)`; `verify_backup_integrity.py` → `INTEGRITY_CHECK_PASSED`; restored database logically identical (per-table row-dump SHA-256 equal, identical DDL/indexes), `PRAGMA integrity_check=ok`, and it **boots the application** under `PHOTODOC_REQUIRE_DB_IDENTITY=1` with `/api/health` → 200. Negative controls (1-byte tamper, deleted file) correctly fail.
- **Crash/interrupted recovery:** proven live in Stage 1 (`crash_commit`, `crash_cleanup`) and in tests.
- **Rollback bundle:** 7 files, deterministic rebuild (`53688e7d…`), byte-identical extraction, 26 tests pass.

HIGH findings, both process/architecture rather than exploitable defects:

- **S6-01** the cutover runbook backs up the live DB with a non-atomic `cp`, archives no uploads, and has no restore gate — while the good tooling exists elsewhere in the repo.
- **S6-02** there is **no scheduled, off-host or retention-managed backup at all**, so **RPO is undefined**.

Notable MEDIUM findings: no disk-space guard anywhere; wildcard CORS with credentials; unencrypted PII in backups; zero container hardening in the production compose files; no intake rate limiting or queue observability; SMTP without certificate verification or timeout.

---

## 8. Stage 7 — Public launch preparation

**State: READY_FOR_OWNER_APPROVAL (not PUBLIC_LIVE).**

Deliverables:

| File | Purpose |
|---|---|
| `docs/launch/LAUNCH_READINESS_REPORT.md` | this document |
| `docs/launch/ACCEPTANCE_MATRIX.md` | every check, result and evidence pointer |
| `docs/launch/SECURITY_AUDIT.md` | 3 HIGH / 13 MEDIUM / 11 LOW with file:line evidence |
| `docs/launch/BACKUP_RESTORE_REPORT.md` | backup and restore rehearsal evidence |
| `docs/launch/PRODUCTION_ACTIVATION_PLAN.md` | DNS, HTTPS, Robokassa, SMTP, worker, smoke-test and first-order checklists |
| `docs/launch/ROLLBACK_PLAN.md` | five-rung rollback ladder with verified commands |

Everything is prepared and validated as far as authorization allows. No public activation of any kind was performed: no DNS record was created, no certificate was issued, no payment was taken, no customer email was sent, no delivery worker was left enabled, and PR #24 was neither merged nor deployed.

---

## 9. Findings summary

| Severity | Count | IDs |
|---|---|---|
| CRITICAL | **0** | — |
| HIGH | **5** | H1 (unpaid fulfilment), H2 (MD5-only), H3 (dead return URLs), S6-01 (unsafe cutover backup), S6-02 (no scheduled backup / undefined RPO) |
| MEDIUM | 15 | M1–M15 — see `SECURITY_AUDIT.md` (includes M13 duplicate paid notification under write contention and M14 unmanaged SQLite lock contention) |
| LOW | 13 | L1–L13 — see `SECURITY_AUDIT.md` |

---

## 10. Blockers

| Blocker | Type | Consequence |
|---|---|---|
| `photo1storage.ru` has **no A/AAAA record** | external (owner) | No certificate can be issued; no callback can reach the site; nothing can go public |
| `www.photo1storage.ru` NXDOMAIN, `_acme-challenge` NXDOMAIN | external (owner) | Same; one certificate covers both names so both must resolve |
| PR #24 not merged | owner approval | Edge config still targets the unregistered domain in `main` |
| No Robokassa merchant credentials | external (owner) | Real payments cannot be validated |
| No SMTP credentials; no DKIM/DMARC on the owned domain | external (owner) | No email can be sent legitimately |
| H1/H2/H3 are product decisions | owner approval | Payment/fulfilment correctness and a working post-payment page |
| No scheduled off-host backup | owner approval | RPO undefined; not launch-blocking for a soft launch but should precede real traffic |
| Mobile layout not automatically tested | engineering | Responsive regressions would go undetected |
| Staging Yandex token belongs to a real personal account | owner confirmation | Provenance should be confirmed; containment is enforced by app-folder scope |

---

## 11. Pull requests

| PR | Title | State |
|---|---|---|
| #24 | `infra(tls): align edge config to the owned domain and close provisioning gaps` | **OPEN — not merged, not deployed** |
| #25 | `docs(launch): launch-readiness deliverables for stages 1-7` | **OPEN** (this document set) |

- https://github.com/life2boat/photodoc-ai/pull/24
- https://github.com/life2boat/photodoc-ai/pull/25

Neither PR was merged; `main` is still `8bfecdee756e11972cbb68ec3f7c0e009a1cba60` (verified after the work). No production source code was changed — #24 is edge/infra configuration and #25 is documentation only. All verification artefacts (harnesses, evidence, transcripts) live under `scratch/` in the verification worktree and are intentionally **not** committed.

---

## 12. Final verdict

```
MODEL_ID=deepseek-flash (harness-reported; no model selection available in-session)
HARNESS_VERSION=DeepSeek Harness 0.2.0-rc.2
PR23_STAGING_ACCEPTANCE=PASS
YANDEX_DISK_LIVE_TEST=PASS (5/5 cases; synthetic only; dedicated namespace; guard removed)
ROBOKASSA_TESTS=PASS (23/23; no CRITICAL; real payments NOT activated)
EMAIL_TESTS=PASS (local implicit-TLS capture; 0 real customers contacted)
HTTPS_READINESS=READY_FOR_APPROVAL (config fixed + validated in PR #24; BLOCKED on DNS A records)
END_TO_END_TESTS=PARTIAL (integrated suite 16/18 passed; the 2 failures are product defects: H1 confirmed empirically, and M13 duplicate paid email under write contention; mobile layout still untested)
SECURITY_AUDIT=PASS_NO_CRITICAL (5 HIGH, 15 MEDIUM, 13 LOW)
BACKUP_RESTORE=PASS (restore rehearsal verified: logically identical + boots)
PRODUCTION_READINESS=READY_FOR_OWNER_APPROVAL
CRITICAL_FINDINGS=0
HIGH_FINDINGS=5 (H1 unpaid fulfilment; H2 MD5-only; H3 dead SuccessURL/FailURL; S6-01 unsafe cutover backup; S6-02 no scheduled backup)
BLOCKERS=no DNS A record for photo1storage.ru or www; no Robokassa credentials; no SMTP credentials/DKIM/DMARC; DERIVED: PR #24 unmerged
PULL_REQUESTS=#24 (infra/tls, open, not merged), #25 (docs/launch, open)
OWNER_APPROVAL_REQUIRED=DNS records; certificate issuance; merge PR #24; Robokassa credentials + ROBOKASSA_IS_TEST=0; SMTP credentials + DNS mail records; delivery worker enablement; H1/H2/H3 decisions; scheduled off-host backup; accept residual synthetic Yandex namespaces
VERDICT=READY_FOR_OWNER_APPROVAL
```

`PUBLIC_LIVE` is **not** claimed, and must not be claimed until the owner separately authorizes activation and it is verified.
