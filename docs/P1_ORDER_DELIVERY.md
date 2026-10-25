# P1 order delivery: migration and recovery

This change builds on PR #8. It does not replace the Yandex Disk OAuth or SMTP
integrations. The production database and upload directory must remain on their
existing persistent volume/bind mount.

## Before starting the P1 backend

Stop the old backend writer and make a verified backup of `orders.db` and the
uploads directory using the PR #8 release runbook. Keep the backup outside the
release tree. On the backed-up, identified PhotoDoc database, run:

```bash
python backend/migrations/reconcile_delivery_schema.py --db /data/orders.db
python backend/migrations/reconcile_delivery_schema.py --db /data/orders.db --apply
python backend/migrations/reconcile_delivery_schema.py --db /data/orders.db
```

The script requires the P0 `PDAI` application identity and complete P0 schema.
It adds `order_token_hash` and `delivery_jobs` transactionally; existing orders
and local files are not rewritten. The P1 backend refuses to start against a
database missing these additions. Run these commands in an isolated container
with the real persistent volume mounted only during an approved release. This
document is not an instruction to deploy from a draft PR.

## Recovery model

New uploads are streamed into temporary files on the persistent uploads mount.
The API assigns each attachment a random stored name, then commits the order,
its token hash, and file/info/email jobs in one SQLite transaction after the
local sources exist. A crash before commit can leave an orphan source file, but
cannot leave a committed order without delivery jobs. Orphans may be inspected
and removed only after checking the database; no automatic cleanup is performed.

The worker claims due jobs with a SQLite write transaction and a three-minute
lease. Failures retain local files and retry with bounded exponential backoff.
Expired leases are eligible again after a process crash. Delivery state is in
the same persistent database as the order. The Yandex upload uses the existing
OAuth token, `overwrite=false`, and verifies remote SHA-256 and size before
marking a job delivered. A matching remote file makes retries idempotent; a
different file at that path is an error. Local sources remain available after
delivery for manual recovery and backup; plan storage monitoring and a separate
retention policy before deleting them.

Old orders have no token hash. They remain readable in the database, and
existing signed Robokassa ResultURL callbacks can still complete initiated
payments. Creating a new payment link for an old order is blocked until an
operator has authenticated the customer through a separate, approved flow.
Old files are not renamed or requeued automatically.

SMTP jobs also persist. An SMTP failure leaves the job retryable. Delivery is
at least once: if the SMTP server accepts a message and the process crashes
before the success record is committed, a duplicate message remains possible.
Payment status itself remains terminal and is never retried as a new charge.
