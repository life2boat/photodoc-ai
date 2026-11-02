# Yandex.Disk delivery: deletion boundary fix

This change is based on current main at a5cad6283a15a70a3b44b2b7ef7c52a0d9a50f02. The initial infrastructure proposal described an older checkout with unconditional finally/unlink cleanup. Current main already has a durable SQLite outbox, verified remote metadata, retry leases and separate cleanup recovery. This PR closes the remaining deletion-boundary races without replacing that architecture or changing payment rules.

## Lifecycle and retention contract

1. The order endpoint streams bounded uploads into unique temporary files, flushes/fsyncs them, and validates signatures and limits. A SQLite write transaction inserts one order with its server-priced order_amount and hashed payment token, promotes files to immutable order/UUID paths, and inserts file, info and admin-email jobs. Acceptance follows commit. Rejected uncommitted requests can clean their temporary files; an ambiguous commit retains promoted files for recovery.
2. Delivery is independent of payment, as before. The worker claims persisted due jobs with a lease and bounded retry/backoff. A failed or interrupted delivery retains its local source. Exhausted attempts retain files and failed records for operator review; there is no age-based purge of failed order files.
3. Yandex delivery validates local identity, reconciles existing remote resources by SHA-256 and byte count, creates folders, obtains a non-overwriting upload URL, streams the PUT, then verifies remote metadata. A PUT attempt or an upload URL alone is not success. Matching remote bytes on retry avoid duplicate uploads; a conflicting remote object fails closed.
4. The worker commits delivered state only under its delivery lease. Separate cleanup uses a fenced lease and durable deleting intent. The existing privacy policy remains immediate local cleanup after verified delivery, not a new age-based retention window. A file referenced by any unfinished job remains retained.
5. Cleanup now requires a complete SHA-256/size identity. Immediately before unlink, under BEGIN IMMEDIATE, it rechecks delivery state and identity fields, unfinished references, path/symlink identity, byte count and the actual checksum. A changed source or delivery record is retained with a safe error category; a new consumer postpones cleanup. Queue writers cannot change references in the same database during this final transaction.
6. After a process crash, expired delivery leases resume from the same outbox. Durable deleting intent allows cleanup recovery before/after unlink without re-uploading verified content. Missing files without deletion evidence remain anomalies, as before. Temporary-file sweeping preserves all referenced sources and does not expire order files by age.
7. Robokassa Result signature validation, server-side price locking, payment token checks and paid-state transitions are unchanged. Duplicate signed callbacks retain one paid order and one paid-email job. Delivery retries operate on job IDs and do not insert orders or change payment state. Browser success redirects remain non-authoritative.

## Reproduced defects

Five new regression cases failed on the unmodified base:

- A same-size file replacement between initial checksum validation and unlink was deleted.
- A new pending job referencing the same local source after the initial dependency query lost its source.
- Delivery could be revoked after the initial check while cleanup still deleted the file.
- A delivered job with no stored SHA-256 could authorize deletion.
- A delivered job with no stored byte count could authorize deletion.

The final transaction previously rechecked only the cleanup lease, not these conditions. The patch adds the missing checks in backend/delivery.py. It changes no schema, payment code, HTTP API, tariff, callback or deployment configuration. Existing files and customer data are not migrated or removed by this PR.

## Automated evidence

New tests are in backend/tests/test_delivery_delete_boundary.py and backend/tests/test_delivery_lifecycle_safety.py (14 cases):

| Required scenario | Evidence |
|---|---|
| Successful delivery | Synthetic HTTP storage contains exact file/info bytes before local cleanup; repeated processing makes no extra PUT |
| Yandex unavailable | Folder API 503 leaves original bytes and failed jobs recoverable |
| Network timeout | Metadata timeout retains sources; only exception category is logged |
| Partial upload | Interrupted streaming PUT retains originals and is safely retried |
| Crash/restart | Real subprocess os._exit after remote acceptance/before SQLite finish; persisted job resumes and metadata reconciliation skips re-upload |
| Retry after failure | Same jobs/order recover after backoff; order count, unpaid status and locked amount remain unchanged |
| Duplicate callback | Repeated valid Result callbacks during delivery failure retain one paid order, unchanged paid_at/amount and one paid-email job |
| Cleanup/retention | Real process exit before and after unlink recovers durable intent; old failed/exhausted sources are never expired |
| Concurrent changes | Source replacement, new consumer and delivery-state change at pre-unlink seam all retain the source |
| Logging | Synthetic OAuth/signature/error markers do not appear in worker failure logs |

Validation commands:

```text
python -m pytest backend/tests/test_delivery_delete_boundary.py backend/tests/test_delivery_lifecycle_safety.py -q
python -m pytest backend/tests -q --tb=short
python -m compileall -q backend/main.py backend/delivery.py
python scripts/verify_staging_delivery_preflight.py
git diff --check
```

The first full Windows sandbox run had 305 passed, 24 Bash-runbook failures and 2 skipped. The Bash failures were temporary-directory permission errors, reproduced in isolation and resolved by running outside the sandbox with PYTHONUTF8=1. The full unrestricted local result and Linux CI result are recorded in the PR. Existing CI already collects backend/tests, including these new tests, and also runs payment/security, migration, privacy, Docker and frontend checks.

No live Yandex credentials or customer data are used by the new tests. The staging preflight reports its live Yandex acceptance as blocked without a dedicated test token. Real process-crash tests establish application recovery, not resilience against physical storage failure or a privileged external process rewriting files at the exact unlink syscall. Application writers use unique immutable paths; storage backup and remote retention remain operational requirements.

## Rollback and review

No database migration or configuration change is required by this patch. Before any future rollout, obtain review and green required checks, build the reviewed commit in CI, and bind validation to the immutable backend digest. This task performs no deployment or VPS changes.

If rollback is needed after a separately approved deployment, first stop the delivery worker using the existing DELIVERY_WORKER_ENABLED=0 control and preserve the database, uploads and remote objects. Reverting to code without this guard reintroduces the race, so keep cleanup paused until a reviewed safe version is available. Account for retained-file disk growth while workers are paused. Do not restore an older database over paid orders, reset job/payment states, purge files, or replay payment callbacks to perform rollback. Existing schema and outbox rows are backward compatible; use only an owner-approved image promotion/revert.
