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
| retry | Provable pre-content failure, or a definite SMTP rejection; bounded job backoff |
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

A failure is retried automatically only when it is provable that no content could
have been accepted:

* connect, EHLO, TLS handshake or AUTH failed, so `send_message()` was never
  entered;
* the server replied with a definite rejection (`SMTPDataError` carries the reply
  code, which is positive evidence of non-acceptance);
* the client proves the DATA command never received 354.

The client observes the DATA/354 boundary directly (`_smtp_transport` and
`_ObservedSMTPSSL`), because `smtplib.send_message()` hides it. Without that
observation, a connection dropped between AUTH and `MAIL FROM` is
indistinguishable from one during the body — which previously recorded a
never-transmitted message as `uncertain` and suppressed it permanently.

Everything from the 354 onward — a disconnect or timeout while the body is
flowing, or a missing final reply — stays `uncertain`. No automatic resend is
allowed, even when that means an email might never have been sent. A transport
that cannot report protocol state (a test double, or a future replacement) is
treated identically: unproven outcomes stay `uncertain`. A stable Message-ID aids
investigation but is not provider-side idempotency. SMTP and a local database
cannot guarantee exactly-once recipient delivery without such a provider feature.
See the [Python SMTP contract](https://docs.python.org/3/library/smtplib.html#smtplib.SMTP.sendmail).

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
idempotent: an already-prepared DB uses a read-only fast path when no rows need
backfilling; existing outbox evidence wins. Historical jobs with an existing sent
timestamp become sent; never-attempted pending jobs become pending; previously
attempted, processing or delivered jobs without a sent timestamp become uncertain.
Those held legacy notifications need reconciliation rather than automatic retry.

Before a later authorized rollout: stop writers, take/verify a consistent backup,
review held legacy email and unpaid-delivery jobs, start the updated version, and
check the new table before considering worker activation. No migration is run on
any server by this PR task.

## Rollback safety

Reverting the application code does **not** remove the new evidence, and the old
code cannot read it. Two distinct hazards must be handled before any revert:

* Pre-PR code ignores `paid_email_outbox` entirely and retries a paid email
  whenever `orders.payment_email_sent_at` is NULL. Every row held in `sending` or
  `uncertain` therefore becomes eligible for an automatic send again, so a
  message that was actually accepted during an ambiguous attempt can be
  delivered **twice**.
* Pre-PR code also reintroduces the unpaid-delivery defect: `file`/`info` jobs of
  unpaid orders become claimable, are uploaded, and their local sources are
  deleted.

Safe procedure:

1. Stop every delivery worker, and any script or manual drain that could claim
   jobs. Reverting with a running old worker is not a safe rollback.
2. Preserve `paid_email_outbox` and the surrounding `delivery_jobs` rows. They
   are attempt evidence: do not drop the table, do not bulk-reset states, and do
   not restore an older database over newer order/payment records.
3. Before starting the reverted code, make every ambiguous attempt unreachable by
   the old retry path **without deleting evidence**: for each order whose outbox
   state is `sending` or `uncertain`, either set `orders.payment_email_sent_at`
   from verified provider evidence, or keep the workers stopped until that
   evidence is available. Never leave an `uncertain` row reachable by an old
   worker.
4. Record each reconciliation decision privately outside the database: order id,
   Message-ID, provider evidence, operator and timestamp.
5. Prefer a forward fix that understands the durable attempt evidence. No
   automatic rollback and no destructive schema down-migration is provided.

## Known limitations (independently reviewed, accepted and tracked)

None of these is a regression introduced by this change, and none is fixed here:

* **Remote directories for an order that becomes unpaid mid-flight.** The pre-PUT
  paid recheck runs after `ensure_yandex_folder()`, so empty remote directories
  can be created for a payment revoked during the request. No file content is
  uploaded and no local source is deleted.
* **Optional payment-gate parameter.** `upload_to_yandex_disk(..., order_id=None)`
  is fail-open: a caller omitting `order_id` skips the transport-level gate. The
  dispatcher gate and the pre-PUT recheck still apply, so no production path is
  currently affected, but the parameter should become required.
* **Schema readiness excludes the new table.** `delivery_schema_ready()`,
  `reconcile_delivery_schema.py` and `verify_backup_integrity.py` do not know
  `paid_email_outbox`. Startup creates it and backups copy the whole database via
  SQLite's backup API, so data is complete; but draining a queue on a database
  that never went through startup raises `no such table: paid_email_outbox`
  instead of degrading, and the integrity verifier does not check outbox
  invariants.
* **Independent retry budgets.** The outbox claim defaults to 10 attempts and
  ignores `DELIVERY_MAX_ATTEMPTS`, so the email and its delivery job can disagree
  about when retrying stops.

## Verification evidence

The full backend suite collects 448 tests: 446 pass and two are Windows symlink
privilege skips; Linux CI runs the same set. The regression modules contribute 58
cases: 40 in the original reliability/concurrency modules plus 18 in
`tests/test_smtp_protocol_stage.py`, which drives a **real implicit-TLS SMTP server
over a loopback socket** and covers every protocol stage — disconnect before
`MAIL FROM`, during `RCPT`, before `DATA`, inside the body, a definite rejection
after the body, an explicit `421` to `DATA`, AUTH rejection, TLS handshake failure,
connection refused, successful acceptance, and proof that an `uncertain` attempt is
never repeated while a `retry` one is. It also covers cleanup eligibility with 20,
100 and 1 000 older unpaid legacy jobs in front of a newer paid job, that unpaid
jobs are never even attempted, that an unpaid job becomes eligible once paid, and
that payment is rechecked at the deletion boundary. The remaining 40 cases include
a mode=ro startup regression, real process
exits before/after SMTP DATA, post-acceptance SQLite lock contention,
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
