# Payment-gated delivery and paid-email recovery

This change addresses audit H1, M13, M14 and L12. All validation uses temporary
SQLite databases, synthetic files and fake HTTP/SMTP. No live payment, customer
email, VPS, OAuth, staging data or permanent worker configuration is involved.

## File and info jobs (H1)

Order creation still durably records sources and file/info jobs in one transaction.
Jobs do not become eligible merely because they exist. Claiming file/info/paid-email
requires an existing order whose persisted payment_status is exactly `paid`.
Unpaid, pending, failed, unknown and missing orders are held without consuming
delivery attempts. Admin new-order notifications retain their existing behavior.

The file/info dispatcher rechecks paid state; the native transport rechecks it
immediately before the file PUT. Successful job finalization checks paid state in
its write transaction. Cleanup checks paid state before recovery/claim and again
inside the same BEGIN IMMEDIATE transaction that fences the final unlink. No age
policy is added for unpaid or failed sources. Only the verified Result callback
can release ordinary unpaid/pending orders; SuccessURL remains non-authoritative.
Price locking, signatures, immutable destinations, overwrite=false, SHA-256/size
verification and source identity/lease checks remain in place.

Legacy queued jobs are retained, never silently deleted or duplicated. A later
valid payment releases their existing destinations subject to the existing retry
limits/backoff; exhausted jobs remain held for operator review. Even a historical delivered
job of an unpaid order cannot authorize new local deletion. Previously deleted
files cannot be reconstructed by this fix; restore decisions require a separate
backup reconciliation. Payment status is monotonic in normal application flows;
database writes by external operators must be coordinated with workers. A check
and a network operation cannot be made one distributed atomic transaction.

## Paid-email outbox (M13)

The additive `paid_email_outbox` table has one row per order, an attempt token,
attempt counter, timestamps, and explicit states:

| State | Automatic behavior |
| --- | --- |
| pending | Eligible once the order is paid and its delivery job is due |
| retry | Safe failure before DATA, or a definite SMTP rejection; bounded job backoff |
| sending | Committed before opening SMTP; only the owning attempt can acknowledge |
| sent | SMTP accepted and SQL acknowledgment committed; retry never sends again |
| uncertain | Outcome cannot be established; operator reconciliation required |

The paid transition, delivery job and pending email row are committed together.
An atomic claim serializes SMTP attempts, including concurrent direct invocations.
No SQL write transaction spans network I/O. After SMTP accepts, the acknowledgment
transaction updates both the outbox and orders.payment_email_sent_at. On a busy
database it retries SQL only, never SMTP. If it exhausts its budget, durable
`sending` survives. An expired/null delivery lease converts it to `uncertain`,
which is excluded from automatic claims. A late acknowledgment can still commit
only with the original attempt token. A failed QUIT after accepted/recorded DATA
does not re-send the message. No exception text containing SMTP details is logged.

Timeout/disconnect during send_message, partial recipient acceptance or a process
crash after the durable claim has an unknown outcome. No automatic resend is
allowed, even when that means an email might never have been sent. A stable
Message-ID aids investigation but is not provider-side idempotency. SMTP and a
local database cannot guarantee exactly-once recipient delivery without such a
provider feature. See the [Python SMTP contract](https://docs.python.org/3/library/smtplib.html#smtplib.SMTP.sendmail).

Before resolving uncertain mail, stop its workers, inspect provider delivery
evidence by order/Message-ID, and record that evidence privately. A verified
accepted message can be reconciled to sent and its order timestamp without SMTP.
Only verified non-acceptance or an explicit owner decision permits resetting that
specific row to retry (clear attempt_token and set next_attempt_at on its existing
job). Do not bulk reset uncertain/sending rows or remove their durable records.

## SQLite contention and malformed amounts (M14/L12)

Database-only claim, finish, paid transition and email-state transactions retry
SQLITE_BUSY/SQLITE_LOCKED with bounded exponential backoff and jitter. Each
connection has a 100 ms busy timeout; the retry budget is four seconds, plus the
duration of the last SQL attempt and scheduler overhead (not a hard query deadline).
Every replay closes/rolls back the
failed transaction. Other operational errors propagate immediately. No retry
decorator wraps upload, SMTP, unlink or the cleanup deletion transaction. Existing
cleanup fencing and worker lease checks remain intact. Long contention still
fails closed; expired leases/reconciliation handle recovery after the lock clears.
See [SQLite result-code semantics](https://www.sqlite.org/rescode.html#busy).

ResultURL verifies the signature over the original OutSum text, then parses the
amount. Non-numeric, non-finite, zero and negative amounts receive HTTP 400 without
changing payment state. Existing price/amount matching remains authoritative.

## Migration and rollback

Startup first validates PhotoDoc DB identity/schema, then creates the additive
email table and imports historical paid-email jobs in a short transaction. Fresh
databases include the table in DELIVERY_SCHEMA. No existing table/column, customer
source, queue destination or payment state is removed or rewritten. Import is
idempotent: existing outbox evidence wins. Historical jobs with an existing sent
timestamp become sent; never-attempted pending jobs become pending; previously
attempted, processing or delivered jobs without a sent timestamp become uncertain.
Those held legacy notifications need reconciliation rather than automatic retry.

Before a later authorized rollout: stop writers, take/verify a consistent backup,
review held legacy email and unpaid-delivery jobs, start the updated version, and
check the new table before considering worker activation. No migration is run on
any server by this PR task.

For rollback, keep delivery workers stopped while reverting the application.
Preserve the new table and all uploaded sources; do not restore an old database
over newer payment/order records. Old code ignores the table and reintroduces the
unpaid-delivery and duplicate-email defects, so do not resume its worker. Prefer a
forward fix that understands the durable attempt evidence. No automatic rollback
or destructive schema down-migration is provided.

## Verification evidence

The full backend suite collected 429 tests: 427 passed and two Windows symlink
privilege skips. The new regression modules contribute 39 cases, including real
process exits before/after SMTP DATA, post-acceptance SQLite lock contention,
multiple workers/direct email callers, invalid signed OutSum values, unpaid
file/info retention and legacy mail migration. An independent four-worker run
also passed: eight public-API synthetic orders, half paid, duplicate callbacks,
real SQLite contention after SMTP acceptance, a second worker drain, exact remote
hash/size checks, one paid email per paid order, and both unpaid sources retained.
A verification runner disabled dotenv and blocked real HTTP: zero attempts.

Existing recovery/backup/namespace fixtures were made explicitly paid where they
exercise successful delivery; their hash, path, lease and integrity assertions
remain. Unpaid/failed/pending cases are tested separately and fail closed. No
existing test assertion was removed to permit an unpaid upload or unlink.

Security regression tests passed. AST comparison verified authoritative pricing,
payment creation, price locking, signature/payload parsing, SuccessURL, order
creation, remote checksum verification and directory creation unchanged. ResultURL
only adds controlled invalid-amount handling; the paid transition only gains SQL
busy retry and the atomic email-row enqueue. Syntax, credential-pattern scan and
git diff whitespace checks passed. Deployment/Compose/workflow/dependency files
are unchanged. Linux/container checks are supplied by PR CI; no VPS is used.
