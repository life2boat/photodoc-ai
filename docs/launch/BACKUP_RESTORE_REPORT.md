# PhotoDoc AI — Backup and Restore Report

Date: 2026-11-04. Rehearsal executed at commit `8bfecdee756e11972cbb68ec3f7c0e009a1cba60`
(pre-PR #26). **Re-baselined against `main` = `57fede5a84fc07eb24e2b6bb6defd294d6ead923`**,
which adds the `paid_email_outbox` table.

**Result: restore rehearsal PASSED (executed, not asserted) for the schema as it stood at
`8bfecde` — `orders`, `delivery_jobs`, `sqlite_sequence`. Backup tooling is sound; the
documented cutover procedure is not, and no scheduled backup exists.**

> **Scope caveat (added when re-based on `main`).** The rehearsal predates PR #26, so it did
> **not** include `paid_email_outbox`. That table is nevertheless backed up, because
> `scripts/create_backup.py` copies the **whole database** through SQLite's backup API
> (`src_conn.backup(dst_conn)`, `scripts/create_backup.py:86-90`) rather than exporting
> selected tables — a new table travels automatically and cannot be silently omitted.
> What is **not** covered is *verification* of the new table: `scripts/verify_backup_integrity.py`
> has no outbox-specific consistency checks (for example `state='sent'` ⇒ `accepted_at IS NOT
> NULL`, or orphaned outbox rows). Treat "restore rehearsal passed" as covering the three
> original tables until a rehearsal is re-run on `main`.

---

## 1. What was rehearsed, and where

Two independent backup paths were exercised:

| Path | Environment | Purpose |
|---|---|---|
| **A. Staging pre-change backup** | Live staging host, real volumes | Prove a verified backup exists before any staging modification |
| **B. Full backup → verify → restore rehearsal** | Isolated local scratch dir | Prove the backup format actually restores to an identical database |

Neither path ran against production — there is no production deployment yet, and no destructive operation was performed on the staging database.

---

## 2. Path A — staging pre-change backup (live)

Performed immediately before rolling the backend onto the PR #23 image. Source volumes (from `docker inspect`):

```
/data         <- /var/lib/docker/volumes/photodoc_pr22_private_db/_data        (orders.db)
/app/uploads  <- /var/lib/docker/volumes/photodoc_pr22_private_uploads/_data
```

Method and result (`/root/photodoc-live-yandex-20261104_d4e5f6a7b8c9/`):

```
compose.before.yml sha256: d66729a1bd22f06d41410209a31fb8a2bbc38905854bad96d8310a5ae0498112
orders.db backup size: 40960
upload backed up: 1_39574494cc5f4972b9e1fc6a2a6068bf_info.txt 197 bytes
upload backed up: 1_f55678299ff74e2d8fe4d53e59938ac0.jpg 43 bytes

BACKUP_VERIFIED=true
logical_sha256: 8981540f8647d08b296908b5965a72084028ed408b4f97612c57dbb8a1f2bc8d
integrity_check: ok   orders: 1   delivery_jobs: 3
files_verified: 2
backend_image_at_baseline: ghcr.io/life2boat/photodoc-backend@sha256:4b09d988…|febae242020710e2c2884f227ec811f3c28908d9
```

The database was copied with the **SQLite backup API** (`src.backup(dst)`) against a read-only connection — a consistent snapshot, not a file copy. Verification was explicit:

- `PRAGMA integrity_check` on the **backup** = `ok`
- The backup's **logical hash** (SHA-256 over every table's DDL and sorted row tuples) **equals the live database's**
- Each archived upload's SHA-256 **equals** the original's
- The script exits non-zero on any mismatch; it printed `BACKUP_VERIFIED=true`

Notable corroboration: `8981540f…` is **byte-identical** to the logical hash recorded by the previous session's baseline on 2026-11-03, independently proving staging data did not change between sessions.

The same procedure was repeated before each acceptance attempt (runs `…7f3a9c1d45e2`, `…a1b2c3d4e5f6`, `…b7c8d9e0f1a2`, `…c3d4e5f6a7b8`, `…d4e5f6a7b8c9`), each with `BACKUP_VERIFIED=true`. Every one of those backup roots is retained.

---

## 3. Path B — full backup → verify → restore rehearsal (isolated)

Executed in `scratch/s6/` on the local host. Every step below actually ran; commands and raw output are in `scratch/s6/S6_AUDIT_REPORT.md`.

1. **Create a realistic database.** Real schema via the app's own `init_db()` (`PRAGMA application_id = 1346650441`), then seeded through the **real** `POST /api/order` endpoint — not handcrafted INSERTs.
2. **Run the real backup tool.** `scripts/create_backup.py` → `SUCCESS (VERIFIED)`, database SHA-256 `39c9946…`, archive SHA-256 `50a230a…`. 4 recovery files archived, 3 omitted under the zero-retention policy. A legacy ambiguity was correctly **fail-closed** on the first attempt rather than silently guessed.
3. **Independently verify it.** `scripts/verify_backup_integrity.py --backup-dir <restored>` → `VERDICT: INTEGRITY_CHECK_PASSED`.
4. **Prove logical identity.** Both sides `PRAGMA integrity_check = ok`; **identical per-table row-dump SHA-256** for `orders`, `delivery_jobs`, `sqlite_sequence`, and identical DDL/indexes; aggregate `287dde6f…` == `287dde6f…` → `LOGICALLY_IDENTICAL = True`.
5. **Prove the restored DB is bootable.** Started the app against the restored database with `PHOTODOC_REQUIRE_DB_IDENTITY=1` → `ready_for_startup=true`, `/api/health` → 200. This is the step that catches "restores fine but the app refuses to start".
6. **Negative controls (prove the verifier is not vacuous).** A 1-byte change to an archived photo → checksum mismatch, exit 1. A deleted photo → missing-file, exit 1.

### 3.1 Restore rehearsal result

| Criterion | Result |
|---|---|
| Backup created by the project's own tool | **PASS** |
| Integrity verification passes | **PASS** (`INTEGRITY_CHECK_PASSED`) |
| Restored DB logically identical to source | **PASS** (per-table row-dump SHA-256 equal for `orders`, `delivery_jobs`, `sqlite_sequence`; DDL/indexes equal) |
| Restored DB passes `PRAGMA integrity_check` | **PASS** |
| Restored DB boots the application | **PASS** (`PHOTODOC_REQUIRE_DB_IDENTITY=1`, `/api/health` 200) |
| Tamper detection actually detects | **PASS** (byte flip and deletion both fail closed) |
| `paid_email_outbox` covered by this rehearsal | **NOT_VERIFIED** — the table did not exist at `8bfecde`. It is included in a whole-database backup, but is not covered by row-dump comparison or by an outbox-specific integrity check. Re-run the rehearsal on `main` before relying on it for the email outbox. |

---

## 4. Rollback bundle

`scripts/build_rollback_bundle.py` produced a **7-file** bundle; extraction rehearsal **PASS** with all extracted files byte-identical to source; a rebuild produced an identical `ROLLBACK_BUNDLE_SHA256=53688e7d…` (deterministic). Secret/PII path filtering is enforced during bundling. `test_rollback_bundle.py` → **26 passed**.

Procedure is documented in `ROLLBACK_PLAN.md`.

---

## 5. Recovery from interrupted operations

Both mechanisms were exercised — the first two live on staging, the third in tests.

| Scenario | Mechanism | Verified by |
|---|---|---|
| Interrupted **upload** | Streamed into an `.incoming-*` temp file, fsynced, promoted, and all jobs committed in a single `BEGIN IMMEDIATE`. A crash before commit leaves an orphan temp file but **never** an order without its jobs. | Stage 1 live `crash_commit` (`os._exit(73)`): job `processing`, source retained → recovered `delivered` |
| Interrupted **cleanup/delete** | Durability intent committed before the unlink (`cleanup_phase='deleting'`) plus a fencing lease, so a crash resumes the delete instead of losing the file or leaking it | Stage 1 live `crash_cleanup` (`os._exit(74)`): `delivered`/`deleting` → `completed`/`deleted` |
| Orphaned temp files | `cleanup_orphaned_temporary_files` sweeps `.incoming-*`/`.tmp*` older than 1 h on a 300 s cadence; observed `removed 1`, in-flight file preserved | `scratch/s6` demo |
| File vanished without delete intent | Flagged `MissingSourceBeforeCleanup` (non-retryable) rather than silently "completing" | `test_delivery_delete_boundary.py` (5 passed) |

---

## 6. Findings

| ID | Sev | Finding | Remediation |
|---|---|---|---|
| S6-01 | **HIGH** | The cutover runbook (`docs/PRODUCTION_RELEASE.md:211-232`) backs up the **live** database with a non-atomic `cp` and only `PRAGMA integrity_check`; it archives **no uploads**, emits no manifest, and has no restore gate — while `create_backup.py`/`verify_backup_integrity.py`, which do all of that, are documented elsewhere | Replace the runbook's backup step with `create_backup.py` + `verify_backup_integrity.py`, and make verification a **blocking** gate before any container is replaced |
| S6-02 | **HIGH** | No scheduled, off-host, or retention-managed backups. No cron/timer/service exists in the repo; archives sit on the same host and volume as the data; off-host copy is only "recommended" in a procurement doc → **RPO undefined**, RTO depends on an unverified manual restore | Add `photodoc-backup.{service,timer}` (or a cron), write to a second volume/host/object store, define a retention policy, and alert on backup failure and on age > 24 h |
| S6-03 | MEDIUM | Backups contain plaintext PII (name, phone, email) and `order_token_hash`, unencrypted, `chmod 600` only, and are proposed for object storage as-is | Encrypt at rest (age/gpg or provider SSE with a customer-managed key) and document the key custody |
| S6-04 | MEDIUM | No disk-space guard; a full disk produces an opaque 500 and one log line | Check `shutil.disk_usage` before accepting uploads; expose free space in `/api/health`; alert below a threshold |
| S6-05 | LOW | `Omitted Delivered Files` conflates "delivered therefore excluded" with "legacy therefore excluded" (observed 3 = 2 + 1) | Report the two categories separately so a zero-retention audit is unambiguous |

---

## 7. What is proven vs. not proven

**Proven by execution**

- A consistent, integrity-checked staging backup exists for every pre-change state, with logical-hash equality to the live database.
- The project's backup tool round-trips a real database to a logically identical, bootable copy.
- The verifier detects single-byte tampering and missing files.
- Both live crash scenarios recover without data loss.
- The rollback bundle is deterministic and extracts byte-identically.

**NOT_VERIFIED**

- An actual restore **into the staging volume** (deliberately not attempted — it would require destroying the live staging database).
- Restore from an **off-host** copy (none exists yet — S6-02).
- Backup of a database at production size/load; all rehearsals used small synthetic datasets.
- Host-level `daemon.json` log rotation and disk-full behaviour on the VPS.
