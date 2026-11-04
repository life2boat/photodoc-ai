# PhotoDoc AI — Backup and Restore

Covers S6-01 (safe, consistent backups) and S6-02 (off-host backups).

**Current state: S6-01 is implemented and tested. S6-02 is implemented but NOT
operational** — there is no destination, no credential and no schedule until the
owner supplies them. Until then **RPO is undefined**: a disk loss loses everything
since the last manual backup, and today there is no backup on the server at all.

---

## 1. What is backed up, and what is deliberately not

| Item | Included | Why |
|---|---|---|
| `orders.db` (orders, payment state, `delivery_jobs`, `paid_email_outbox`) | **Yes**, via SQLite's backup API | The authoritative state, including at-most-once email evidence |
| Uploads for **unfinished** deliveries (`pending`/`processing`/`failed`) | **Yes** | Required to resume delivery after a restore |
| Uploads for **delivered and cleaned** orders | **No** | Zero-retention privacy contract: delivered photos are deleted and must not be resurrected by a backup |
| Ambiguous legacy files with no `delivery_jobs` record | **Fails closed** | The tool refuses rather than guessing the retention status |
| `.env`, tokens, credentials | **No** | Never copied into a backup archive; they are restored from the secret store |
| Container images / compose files | **No** | Reproducible from the release bundle |

A `uploads.tar.gz` therefore contains only the recovery originals. That is a
deliberate product decision, not an omission, and the tool reports
`omitted_delivered_files_count` so the distinction stays visible.

## 2. Why a plain `cp` of `orders.db` is not a backup

SQLite in `journal_mode=delete` (the production setting) may be writing pages at the
moment of the copy. A file-level copy can capture a **torn** database: it opens, it
may even pass a superficial check, and it silently loses or corrupts recent writes.
It also copies nothing else, so a restore yields a database referencing photo files
that no longer exist.

`scripts/create_backup.py` instead:

1. opens the source **read-only** and copies through `sqlite3.Connection.backup()`,
   which takes a consistent snapshot under a read transaction;
2. verifies `PRAGMA integrity_check` on the **snapshot**, not on the source;
3. archives the recovery originals and records their SHA-256;
4. writes `manifest.sha256` and `backup_manifest.json`;
5. re-verifies the finished set with `verify_backup_integrity.py` (database
   references vs archive contents, sizes and digests) before publishing;
6. publishes **atomically** — it builds in a hidden sibling directory and renames it
   into place, so a failure cannot leave a partial set where a restore would find it,
   and cannot damage the previous valid set.

## 3. Quiescence: when an online backup is enough, and when it is not

The **database** snapshot is always consistent, even under load. The **set**
(database + uploads) is consistent only when no cleanup is deleting files while the
archive is built. Two supported modes:

| Mode | Downtime | Use when | Guarantee |
|---|---|---|---|
| **Quiescent (recommended)** | ~0 — writers are stopped, not the host | Nightly scheduled backup; before every cutover | Database and uploads are one consistent point in time |
| **Online** | 0 | Ad-hoc backup while serving | Database consistent; uploads consistent *unless* a cleanup deletes a referenced file mid-archive, in which case the run **fails closed** instead of producing a bad set |

The failure mode is the safety property: the tool re-checks every referenced file,
so a cleanup racing the archive makes the backup fail rather than succeed
incompletely.

**Quiescence procedure (the supported cutover path):**

```bash
# 1. stop writers (backup takes seconds-to-minutes; measure on your data volume)
docker compose -f docker-compose.yml -f docker-compose.production.yml stop backend
# 2. confirm nothing is in flight
python3 scripts/backup_gate.py --db /data/orders.db --require-quiescent \
        --worker-enabled no --json        # only the quiescence section matters here
# 3. take the backup (Section 4), verify it (Section 4.2)
# 4. continue the release, or restart: docker compose ... start backend
```

Required downtime is the backup duration: the database snapshot is milliseconds, the
upload archive dominates and scales with the number of unfinished deliveries (not
with total data, because delivered photos are excluded). Expect seconds for a small
VPS; measure once and record it.

## 4. Taking a backup

```bash
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP_ROOT="${BACKUP_ROOT:-/var/backups/photodoc}"

docker run --rm \
  -v "${PHOTODOC_DB_VOLUME:-photodoc_backend-data}:/data:ro" \
  -v "${PHOTODOC_UPLOADS_SOURCE:-/opt/photodoc/backend/uploads}:/app/uploads:ro" \
  -v "$PWD:/repo:ro" -v "${BACKUP_ROOT}:/backups" \
  -e PYTHONPATH=/repo/scripts \
  "${PHOTODOC_BACKEND_IMAGE}" \
  python /repo/scripts/create_backup.py \
    --db /data/orders.db --uploads /app/uploads \
    --output-dir "/backups/${STAMP}" --no-cleanup-sweep --json
```

### 4.1 Verify with the deployment gate

```bash
docker run --rm \
  -v "$PWD:/repo:ro" -v "${BACKUP_ROOT}:/backups:ro" \
  -v "${PHOTODOC_DB_VOLUME:-photodoc_backend-data}:/data:ro" \
  -e PYTHONPATH=/repo/scripts \
  "${PHOTODOC_BACKEND_IMAGE}" \
  python /repo/scripts/backup_gate.py \
    --backup-dir "/backups/${STAMP}" --require-outbox --deep --live-db /data/orders.db
```

The gate is fail-closed and returns distinct exit codes:

| Exit | Meaning |
|---|---|
| 0 | A verified, current set exists |
| 2 | No complete backup set found |
| 3 | A set exists but is not verified |
| 4 | Too old, or **older than the newest live order** |
| 5 | Writers are in flight (when `--require-quiescent`) |

`--require-outbox` is mandatory once the paid-email outbox exists: restoring a set
without it loses the evidence that decides whether an ambiguous email may be resent.

### 4.2 Retention

```bash
python scripts/backup_retention.py --root "${BACKUP_ROOT}" --require-outbox          # plan
python scripts/backup_retention.py --root "${BACKUP_ROOT}" --require-outbox --apply  # prune
```

Default plan: 7 most recent, 14 daily, 8 weekly. The newest set is never pruned, and
the tool refuses to prune if it would leave no verified set at all.

## 5. Restoring

```bash
# 1. Choose and verify the set (never restore an unverified one)
python scripts/backup_gate.py --latest-of "${BACKUP_ROOT}" --require-outbox

# 2. Refuse if the live database is newer than the backup
python scripts/backup_gate.py --backup-dir "${BACKUP_ROOT}/${STAMP}" \
        --require-outbox --live-db /data/orders.db          # exit 4 => STOP

# 3. Stop the stack
docker compose -f docker-compose.yml -f docker-compose.production.yml stop

# 4. Restore the database
install -m 0644 "${BACKUP_ROOT}/${STAMP}/orders.db" /var/lib/docker/volumes/photodoc_backend-data/_data/orders.db

# 5. Restore the recovery originals
tar xzf "${BACKUP_ROOT}/${STAMP}/uploads.tar.gz" -C "${PHOTODOC_UPLOADS_SOURCE}" --strip-components=1

# 6. Prove consistency before starting anything
python scripts/verify_backup_integrity.py --db /data/orders.db --uploads /app/uploads

# 7. Start with the worker still disabled, then enable it deliberately
```

**Hard rules**

* Never restore over a database newer than the backup (step 2 enforces it).
* Never start a **pre-PR #26** worker against a restored post-PR #26 database — it
  ignores `paid_email_outbox` and will re-send paid notifications. See
  `docs/launch/ROLLBACK_PLAN.md` §6.
* `sending`/`uncertain` outbox rows are never automatically resent after a restore;
  they require reconciliation, not a reset.

## 6. Off-host backups (S6-02) — implemented, not active

`scripts/offhost_backup.py` seals the newest **verified** local set into a single
`gpg`-encrypted archive, pushes it, optionally verifies the remote copy by checksum,
and can prune under the same retention rules. It is **dry-run by default** and
refuses to act without complete configuration:

```bash
python scripts/offhost_backup.py --env-file /etc/photodoc/backup.env            # dry run
python scripts/offhost_backup.py --env-file /etc/photodoc/backup.env --execute  # for real
```

Configuration template: `scripts/offhost_backup.env.example`.

### Activation prerequisites (owner actions — none done here)

1. **Choose a destination** reachable from the VPS without inbound access: an SSH
   account on a different provider, or S3-compatible object storage. Nothing was
   purchased or created.
2. **Create the passphrase** and store it **off** the VPS:
   `openssl rand -base64 48 > /etc/photodoc/backup.passphrase && chmod 600 …`.
   Lose it and every off-host archive is unrecoverable.
3. **Fill `/etc/photodoc/backup.env`** (mode 0600, root) from the template, including
   `OFFHOST_VERIFY_CMD` — without it the script reports "pushed but unverified" and
   the run is **not** operational.
4. **Copy `scripts/` to `/srv/photodoc/scripts`** on the host (the timer references
   that path).
5. **Install, but do not enable**, the units:
   `photodoc-backup.service` / `photodoc-backup.timer` from `infra/systemd/`.
6. **Prove one full cycle manually** — run, encrypt, push, verify, then perform a
   restore from the *off-host* copy on a different machine.
7. **Only then** `systemctl enable --now photodoc-backup.timer`.

### What "operational" requires

S6-02 is **not** operational until all seven steps are done, including a restore
proven from the off-host copy. A push that exits 0 proves nothing about restorability.

### Environment reality on the target VPS (read-only inspection)

| Tool | Present |
|---|---|
| `gpg` | yes (`/usr/bin/gpg`) |
| `openssl`, `zstd` | yes |
| `restic`, `rclone`, `age`, `sqlite3` CLI | **no** |

That is why the design uses `gpg` and a configurable push command rather than
restic/rclone; and why backups run inside the backend image (which has Python and
`sqlite3` built in) instead of relying on a host `sqlite3` binary.

## 7. Failure logging and alerting

* `scripts/run_backup.sh` logs every run to `${BACKUP_LOG:-/var/log/photodoc-backup.log}`
  and exits non-zero on: backup creation failure, gate rejection, or off-host push
  failure (a missing configuration is reported as "not configured", not a failure).
* `offhost_backup.py` writes `/var/lib/photodoc-backup/last-run.json`.
* Alert on: `systemctl is-failed photodoc-backup.service`, log lines containing
  `FAILED:`, no successful run in 26 h, and `backup_gate.py` exit ≠ 0 at cutover time.
* A failed run never deletes the previous backup, so an alerting gap degrades
  freshness, not existence.

## 8. Protection against accidental deletion

* Backups live outside the application data path (`/var/backups/photodoc`), so a
  volume mistake or `docker compose down -v` cannot remove them.
* Publication is a rename: a failed run cannot truncate an existing set, and replacing
  a set keeps the old one as `<name>.previous`.
* Retention never removes the newest set and refuses to prune the last verified one.
* Off-host replication exists precisely so that losing the VPS does not lose the
  backups; until it is active, on-host deletion **is** total loss.

## 9. Remaining risks (explicit)

| Risk | Status |
|---|---|
| No off-host copy, no schedule | **OPEN** — S6-02 awaits owner storage/credentials |
| RPO undefined | **OPEN** — until step 6 of §6 is done |
| Backup set stores plaintext PII inside the archive (before encryption) | Mitigated by `chmod 600` on host and encryption before any transfer; the local archive itself is not encrypted at rest |
| Retention/pruning is manual until the timer is enabled | **OPEN** |
| Restore rehearsal covers the current schema, not a byte-for-byte production restore | Documented; the rehearsal is synthetic by design |
