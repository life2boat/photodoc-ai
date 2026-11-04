# PhotoDoc AI — Rollback Plan

Applies to the staging deployment as it exists today and to the intended production deployment.
All image references are immutable digests — never `:latest`.

---

## 0. Known-good reference points

| Environment | Role | Image | Revision label |
|---|---|---|---|
| Staging (current, restored) | known-good | `ghcr.io/life2boat/photodoc-backend@sha256:4b09d98850155459166a3aac9987c483257562e95cba782ef37d852a6141f640` | `febae24…` (PR #22) |
| Staging (PR #23, accepted) | candidate | `ghcr.io/life2boat/photodoc-backend@sha256:59e513d4b2642c352eec639dad61cd3c61bcb671b7387fe25911eceb802f1525` | `8bfecde…` (PR #23) |
| Frontend (current) | known-good | `ghcr.io/life2boat/photodoc-frontend@sha256:1f2354d3a5d5ba1e9583bf796348d1a5d287e2e05e2145d8a047b710481747bc` | — |
| Frontend (PR #23) | candidate | `ghcr.io/life2boat/photodoc-frontend@sha256:e3c02f623dbc2d04aece54c0217b305da398edbd8d53a071f07b36944222467e` | `8bfecde…` |

Staging deployment root: `/srv/photodoc/staging/`
Staging compose file: `/srv/photodoc/staging/compose.private.yml`
Staging project name: `photodoc-pr22-private`
Volumes: `photodoc_pr22_private_db` → `/data`, `photodoc_pr22_private_uploads` → `/app/uploads`
Network: `photodoc_pr22_private_network` (`internal: true`)

**A rollback never touches volumes.** `docker compose down -v` is forbidden — `-v` deletes the orders database and the customer uploads.

---

## 1. Rollback ladder (least to most invasive)

Pick the first rung that restores service. Stop as soon as `/api/health` is ok.

### Rung 1 — revert the image (≈15 s, no data risk)

```bash
cd /srv/photodoc/staging
cp compose.private.yml /root/compose.rollback.$(date -u +%Y%m%dT%H%M%SZ).yml

# restore the previous image references in compose.private.yml
sed -i \
  -e 's#ghcr.io/life2boat/photodoc-backend@sha256:59e513d4b2642c352eec639dad61cd3c61bcb671b7387fe25911eceb802f1525#ghcr.io/life2boat/photodoc-backend@sha256:4b09d98850155459166a3aac9987c483257562e95cba782ef37d852a6141f640#' \
  compose.private.yml

docker compose --env-file /dev/null -p photodoc-pr22-private \
  -f compose.private.yml up -d --no-deps backend

# verify
docker inspect photodoc-pr22-private-backend-1 \
  --format '{{index .Config.Labels "org.opencontainers.image.revision"}}'   # expect febae24…
docker exec photodoc-pr22-private-backend-1 curl -fsS http://127.0.0.1:8000/api/health
```

This exact sequence was executed during the Stage 1 acceptance: after every run the compose file was restored **byte-identically** (SHA-256 `d66729a1bd22f06d41410209a31fb8a2bbc38905854bad96d8310a5ae0498112`) and the backend was confirmed back on `febae24…`, `DELIVERY_WORKER_ENABLED=0`, with no token in the runtime environment.

### Rung 2 — restore the compose file from a verified backup

Use when the compose file itself was edited and the previous content is uncertain.

```bash
sha256sum /root/photodoc-live-yandex-<run>/compose.before.yml
cp /root/photodoc-live-yandex-<run>/compose.before.yml /srv/photodoc/staging/compose.private.yml
docker compose --env-file /dev/null -p photodoc-pr22-private \
  -f /srv/photodoc/staging/compose.private.yml up -d --no-deps backend
```

Verify the compose file is byte-identical to the backup (`sha256sum` both) before bringing anything up.

### Rung 3 — restore the database

Only if the database is corrupt or was migrated incorrectly.

```bash
# 1. STOP writes first — never copy a live SQLite file.
docker compose --env-file /dev/null -p photodoc-pr22-private \
  -f /srv/photodoc/staging/compose.private.yml stop backend

# 2. Preserve the current (bad) database for forensics — do not delete it.
TARGET=/var/lib/docker/volumes/photodoc_pr22_private_db/_data/orders.db
cp -a "$TARGET" "/root/orders.bad.$(date -u +%Y%m%dT%H%M%SZ).db"

# 3. Verify the backup BEFORE overwriting anything.
python3 /srv/photodoc/staging/verify_backup_integrity.py --backup-dir /root/photodoc-live-yandex-<run>/backup

# 4. Install the verified backup.
install -o 1000 -g 1000 -m 644 \
  /root/photodoc-live-yandex-<run>/backup/orders.db "$TARGET"

# 5. Bring the backend back and check.
docker compose --env-file /dev/null -p photodoc-pr22-private \
  -f /srv/photodoc/staging/compose.private.yml up -d --no-deps backend
docker exec photodoc-pr22-private-backend-1 curl -fsS http://127.0.0.1:8000/api/health
```

Success criterion is the **logical hash**, not the file size. Recompute it with the same routine used by the acceptance harness (SHA-256 over each table's DDL plus its sorted row tuples) and compare with `baseline.json → database_logical_sha256`. For the current staging database that value is:

```
8981540f8647d08b296908b5965a72084028ed408b4f97612c57dbb8a1f2bc8d
```

### Rung 4 — restore uploads

```bash
cp -a /root/photodoc-live-yandex-<run>/backup/uploads/. \
      /var/lib/docker/volumes/photodoc_pr22_private_uploads/_data/
chown -R 1000:1000 /var/lib/docker/volumes/photodoc_pr22_private_uploads/_data
# verify each SHA-256 against baseline.json → files[].sha256
```

### Rung 5 — full project teardown and redeploy

Last resort. **Without** `-v`, so volumes survive.

```bash
docker compose --env-file /dev/null -p photodoc-pr22-private \
  -f /srv/photodoc/staging/compose.private.yml down
docker compose --env-file /dev/null -p photodoc-pr22-private \
  -f /srv/photodoc/staging/compose.private.yml up -d
```

---

## 2. Application-level rollback (code)

If a regression is in the code rather than the image:

1. `git revert <bad_merge_commit>` on a branch, open a PR, let CI rebuild and publish new digests.
2. Deploy the new digests via Rung 1 mechanics (digest substitution).
3. Never `git reset`/force-push `main`.

For the PR #23 change specifically, the rollback is purely a code revert: there is **no schema migration**, so no database rollback is required. Sources and durable queue records are retained under the existing policy, so a reverted backend simply resumes the outbox.

> **Exception — PR #26 (payment-gated delivery and paid-email outbox).** That change *is* additive to the schema and its evidence is not readable by pre-PR code. Reverting it without the preparation in §6 can produce duplicate paid emails and unpaid deliveries. Read §6 before reverting it.

---

## 3. Feature-flag level rollback (fastest mitigation, no redeploy)

| Symptom | Immediate mitigation | Effect |
|---|---|---|
| Delivery duplicates / wrong remote paths | `DELIVERY_WORKER_ENABLED=0` then recreate the backend | Halts all delivery; queued jobs and local sources are retained |
| Email misbehaving | `DELIVERY_WORKER_ENABLED=0` | Halts all email and delivery (shared worker) |
| Payments behaving unexpectedly | `ROBOKASSA_IS_TEST=1` and/or remove `ROBOKASSA_*` credentials | Invoice creation fails closed; no money moves |
| Unexpected outbound traffic | Stop the container (network is `internal: true`; nothing egresses by default) | Containment |

These were all validated: with `DELIVERY_WORKER_ENABLED=0` the three original queued jobs stayed `pending, attempts=0` indefinitely across a multi-hour window, proving the flag genuinely prevents processing.

---

## 4. Rollback verification checklist

Run after any rung:

```bash
docker ps --format '{{.Names}}\t{{.Status}}'                     # both healthy
docker inspect <backend> --format '{{index .Config.Labels "org.opencontainers.image.revision"}}'
docker exec <backend> printenv DELIVERY_WORKER_ENABLED            # must be 0 in staging
docker inspect <backend> --format '{{range .Config.Env}}{{println .}}{{end}}' | grep -c YANDEX_DISK_TOKEN   # must be 0
docker exec <backend> curl -fsS http://127.0.0.1:8000/api/health  # {"status":"ok"}
docker ps --format '{{.Names}} {{.Ports}}'                        # no host-published ports
sha256sum /srv/photodoc/staging/compose.private.yml               # equals the known-good backup
```

Then recompute the database logical hash and compare with `baseline.json`.

---

## 5. What is proven, and what is not

**Proven (executed)**

- Image rollback: performed after each of the five acceptance runs; the backend returned to `febae24…` every time and the compose file was byte-identical to its pre-run backup.
- `DELIVERY_WORKER_ENABLED=0` genuinely stops processing (3 jobs untouched across hours).
- Runtime secret unload: no `YANDEX_DISK_TOKEN` in the restored container environment.
- Rollback **bundle** build/extract: deterministic (`ROLLBACK_BUNDLE_SHA256=53688e7d…`), byte-identical extraction, `test_rollback_bundle.py` 26 passed.
- Restore of a backed-up database to a logically identical, bootable copy (see `BACKUP_RESTORE_REPORT.md` §3).

**NOT_VERIFIED**

- Rung 3 executed against the **staging** volume (deliberately not performed — it would destroy the live staging database).
- A production rollback, because no production deployment exists yet.
- Rollback under real customer load.

---

## 6. Rolling back the paid-email outbox (PR #26)

PR #26 adds an additive `paid_email_outbox` table and makes fulfilment require a
persisted `paid` state. Reverting the code leaves the table in place — but the old
code cannot read it, and that asymmetry is the danger.

### 6.1 Why a naive revert is unsafe

| Old-code behaviour | Consequence after a revert |
|---|---|
| `send_paid_order_email` retries whenever `orders.payment_email_sent_at` is NULL, and ignores `paid_email_outbox` | Every attempt held in `sending` or `uncertain` becomes eligible for an automatic send again. A message that was **actually accepted** during an ambiguous attempt can be delivered **twice**. |
| `claim_due` does not require a paid order for `file`/`info` jobs | Unpaid orders are uploaded to Yandex.Disk again and their local source files are deleted. |

`uncertain` means the outcome could not be established — typically a disconnect or
timeout after the server accepted the DATA command. It must **never** be
automatically resent, in either direction of the rollback.

### 6.2 Safe rollback procedure

1. **Stop the workers.** Set `DELIVERY_WORKER_ENABLED=0`, recreate the backend, and
   stop any script or manual drain that could claim jobs. Reverting with a running
   old worker is not a safe rollback.
2. **Preserve the evidence.** Keep `paid_email_outbox` and the surrounding
   `delivery_jobs` rows. Do not drop the table, do not bulk-reset states, and do
   not restore an older database over newer order/payment records.
3. **Make ambiguous attempts unreachable by the old retry path**, without deleting
   anything. For each order whose outbox state is `sending` or `uncertain`, either
   set `orders.payment_email_sent_at` from verified provider evidence, or keep the
   workers stopped until that evidence exists. Never leave an `uncertain` row
   reachable by an old worker.
4. **Record the decision** privately outside the database: order id, Message-ID,
   provider evidence, operator, timestamp.
5. **Prefer a forward fix.** A patch that understands the durable evidence does not
   need any of the above and cannot duplicate mail.

### 6.3 Reconciliation without destroying evidence

```sql
-- inspect held attempts (read-only)
SELECT o.id, o.payment_email_sent_at, e.state, e.attempts, e.accepted_at, e.last_error
FROM paid_email_outbox e JOIN orders o ON o.id = e.order_id
WHERE e.state IN ('sending','uncertain')
ORDER BY o.id;
```

Then, per order, after checking the mail provider's delivery log by order id and
`Message-ID <photodoc-order-{id}-paid@photodoc-ai.ru>`:

- **Provider shows the message accepted** → record it as sent without sending again
  (`state='sent'` with `accepted_at`, and set `orders.payment_email_sent_at` to the
  same instant).
- **Provider shows no such message** → only then may the row be returned to
  `pending`/`retry` with the attempt token cleared, so a later authorised drain can
  send it once.
- **No evidence either way** → leave it `uncertain` and escalate. This is the
  correct final state; it is not a stuck record.

Never bulk-reset or delete these rows: they are the only record that an attempt
happened.

### 6.4 Verification after a rollback

```bash
docker exec <backend> printenv DELIVERY_WORKER_ENABLED          # 0 until reconciled
docker exec <backend> curl -fsS http://127.0.0.1:8000/api/health
```

and confirm that no `sending`/`uncertain` row is reachable by the running version,
and that every `delivered` `file`/`info` job of an unpaid order still has its local
source present.
