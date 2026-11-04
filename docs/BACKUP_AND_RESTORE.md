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
   and cannot damage the previous valid set;
7. applies **durability barriers** before and after the rename (see §2.1).

### 2.1 Durability barriers, and what they do not prove

A rename is atomic in *visibility*, not in *storage*. Without an explicit flush, a
power cut can leave the new name pointing at bytes that never reached the disk, or
revert the rename while the previous entry has already been moved aside. The
publication path therefore:

1. flushes every artifact (`fsync`) **before** the rename, so the published manifest
   can never describe unflushed data;
2. flushes the staging directory;
3. flushes the parent directory after the `.previous` rotation and again after the
   rename, so the directory entries themselves are durable.

**What is supported where:**

| Platform | File contents | Directory entries |
|---|---|---|
| Linux (the deployment target) | `fsync` — supported | `fsync` on the directory — supported |
| Windows (development) | flush of a write handle only; reported as *unsupported* when it cannot be done | **not exposed** — reported as `directory_fsync_supported: false` |

The API never pretends a barrier happened: `fsync_file`/`fsync_dir` return `False`
when the platform cannot perform the flush, and the backup report carries
`{"files_flushed", "files_unsupported", "directory_fsync_supported"}`.

**This is a best-effort barrier, not a proof of power-loss survival.** It assumes
the filesystem honours `fsync` and orders the directory entry after it. On a
network filesystem, an overlay that ignores flushes, or a virtualised disk with a
volatile write cache, that assumption can be false. What has been *verified* is that
the process survives being killed at any boundary (see the crash-matrix evidence);
power-loss behaviour has not been tested on the target hardware.

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
# 1. stop every writer — the application container is a writer even with the
#    delivery worker disabled, because order intake writes to the database.
docker compose -p photodoc-staging stop backend

# 2. prove exclusion and quiescence (read-only; run this exact command)
python3 scripts/writer_barrier.py \
    --db /var/lib/docker/volumes/photodoc_staging_backend-data/_data/orders.db \
    --volume photodoc_staging_backend-data
python3 scripts/backup_gate.py --quiescence-only \
    --db /var/lib/docker/volumes/photodoc_staging_backend-data/_data/orders.db \
    --worker-enabled no --require-exclusive-lock --json

# 3. take the backup (§4) and verify it with the gate (§4.1)
# 4. run the gate again; it must still pass against the same database
# 5. migrate, cut over, and only then start the application again
```

`--quiescence-only` is a standalone mode: it needs a database, not a backup set, so it
can run before any backup exists. `writer_barrier.py` answers a different question — no
container is attached to the volume, no host process holds the file open, and an
exclusive lock is free — and both must pass.

### 3.1 The trusted boundary for writer exclusion

The barrier and the gate can prove that **no writer was detected at a known moment**, and
that an exclusive lock was obtainable. They cannot prove that nothing writes afterwards:
a SQLite read transaction does not exclude future writers, and both processes exit before
the migration runs. Continuous exclusion is therefore a *protocol*, and the trusted
boundary is:

* the staging/production **host** and its Docker daemon;
* the **enumerated writer set**: the application container (order intake, delivery
  worker), scheduled jobs, and any operator-run script that opens the database;
* the rule that **nothing in that set is started** between the barrier and the completed
  cutover.

Outside that boundary — an external process with its own filesystem access, a second
host sharing the volume, a manually started container with a different name — cannot be
excluded by this tooling and must be prevented by infrastructure. When the boundary
cannot be established (no `/proc`, no docker daemon, an unreadable volume), the
deployment **fails closed**; it never assumes silence.

The deployment script enforces the protocol mechanically: barrier → gate → operator
confirmation → re-check barrier and database identity → migration. If the database
changes between the gate and the migration (`device:inode:size:mtime` differs), the
deployment aborts and a fresh backup is required.

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
| 0 | A verified, current set exists and matches the live database |
| 2 | No complete backup set found |
| 3 | A set exists but is not verified, the archive cannot be re-verified, or the backup does not match the live rows |
| 4 | Too old, or **older than the newest live order** |
| 5 | Writers are in flight (when `--require-quiescent`) |
| 6 | Invalid configuration (bad age value, contradictory flags, missing input) |

`--require-outbox` is mandatory once the paid-email outbox exists: restoring a set
without it loses the evidence that decides whether an ambiguous email may be resent.

**`--deep` is not optional for a deployment.** `deploy_backup_gate.sh` always passes it,
in both the host and the Docker path, and the check fails closed if the verifier module
cannot be imported — a deep check that silently degrades to "not checked" is worse than
none, because the deployment would proceed believing the archive was verified. Size the
container's writable `/tmp` for the extracted archive (the wrapper uses 1 GiB).

**The gate compares rows, not counts.** With `--live-db` it reads the live database in a
single `BEGIN` transaction and compares, per row: every live order (id, `payment_status`,
`payment_amount`, `paid_at`, `payment_email_sent_at`) and every delivery job (id, kind,
state, attempts, cleanup state/phase, deletion timestamp, content digest), plus every
`paid_email_outbox` row when the table exists. Equal counts with different content are
rejected — a backup that still says `unpaid` where live says `paid`, or one that lost the
record of an ambiguous email attempt, is not a rollback point.

**Quiescence is enforced, not assumed.** `--require-quiescent` now requires `--db` or
`--live-db`; without one the gate exits `6` instead of silently skipping the check. The
worker check and the in-flight delivery/cleanup counts come from the same snapshot as the
row comparison, so a writer cannot slip between the two.

**Freshness values are validated.** `--max-age-hours` (and `BACKUP_MAX_AGE_HOURS`) must be
a finite positive number below ten years. `nan`, `inf`, `-1`, `0` and non-numeric values
are rejected: every comparison against NaN is false, so a stray `nan` would silently
disable the age check entirely.

### 4.1.0 Ordering: gate first, mutate second

In `deploy_staging_prebuilt.sh` the database-presence probe (`[7a/10]`) runs as root so a
permission error cannot masquerade as "no database"; the writer barrier and the backup
gate (`[7b/10]`) run next; then the operator confirms; only then does the schema
migration (`[7c/10]`) touch the live database. A failed gate *or a declined
confirmation* therefore leaves the database exactly as it was, and a failed migration
aborts the deployment instead of starting the application against a half-applied schema.

Immediately before the migration the script re-runs the barrier with the fingerprint
captured when the gate approved the backup. If anything wrote in between, the deployment
stops and a fresh backup is required — the gate's snapshot is a photograph, not a lock.

A cold start is only accepted when the deployment script has positively proven that no
database file exists (`DB_ABSENCE_PROVEN=1`) **and** the evidence survives an independent
re-check: the Docker daemon must answer, the volume must be reported missing with the
specific not-found error, and `LIVE_DB` must not contradict the claim. An unreachable
daemon, a permission error, an existing volume or an auto-detected mode with neither
`LIVE_DB` nor `DB_VOLUME` is an error, never a cold start.

#### Recovering from an interrupted deployment

| Interrupted at | State | Recovery |
|---|---|---|
| before the gate | nothing mutated | re-run; fix whatever the gate reported |
| after the gate, before the migration | nothing mutated; the approval is now stale | re-take the backup if the fingerprint changed, then re-gate |
| during `--apply` | schema possibly half-applied | **do not start the application**; re-run the migration (`--check` then `--apply`); it is additive and idempotent |
| after migration, before containers start | database migrated, old containers still stopped | start the stack; the migration is forward-only and the old code ignores added tables |
| after containers start | new code running | normal rollback ladder (`docs/launch/ROLLBACK_PLAN.md`) |

Never restore the backup over a migrated database without first running the gate with
`--live-db`: if the live database holds newer orders, restoring it deletes them. The
pre-outbox transition flag stays valid only until the live database gains the outbox
table, so a half-finished transition is completed by starting the new code, not by
re-running the old one.

### 4.1.1 The one-time pre-outbox transition

A database that predates PR #26 has no `paid_email_outbox` table, so **no backup of it
can contain one** — and the standard gate would therefore refuse the very deployment
that creates it. For that single step only, the outbox requirement can be replaced by
the transition check:

```bash
BACKUP_GATE_ALLOW_PRE_OUTBOX=1 sudo -E bash scripts/deploy_backup_gate.sh
# or directly:
python scripts/backup_gate.py --latest-of /var/backups/photodoc \
    --allow-pre-outbox-transition --live-db /data/orders.db --deep
```

It is **off by default** and it is not a relaxation of anything else. The gate still
requires a fresh, checksum-verified, identity-matching set with a passing deep upload
check; on top of that the transition is refused unless:

* the **live** database has no `paid_email_outbox` (once it does, the flag is an error —
  the transition is over and the normal requirement applies again);
* the **backup** has no `paid_email_outbox` (if it does, use the normal gate);
* the backup preserves **every live row**, compared field by field — each order's
  payment state, amount, `paid_at` and `payment_email_sent_at`, and each delivery job's
  kind, state, attempts, cleanup state/phase, deletion timestamp and content digest.
  An order or job that exists only in the live database is refused, and so is any
  material difference in a shared row: restoring such a backup would roll a payment back
  or erase the record that an email attempt was ambiguous.

Counts are never used as evidence. Both sides are read in one transaction each, so the
comparison describes a single moment rather than a sequence of queries.

It is independent of `SKIP_BACKUP_GATE` (which still aborts the deployment) and it
cannot be enabled together with `--require-outbox`. The run prints a bordered warning
and records `transition: true` plus the compared counts in its JSON output, so the
exception is auditable after the fact.

After the first successful cutover the table exists, and every later backup must pass
`--require-outbox`.

**Permissions model.** Backup sets are intentionally restrictive: the directory is
`root:root 0700` and the artifacts are `0600`. Verification therefore runs as root
*inside a narrowly scoped container* — `--network none`, `--read-only`, all
capabilities dropped, `no-new-privileges`, a small `tmpfs` for `--deep` extraction, and
every host mount `:ro`. With that privilege the verifier can only read the three
mounted paths; it cannot modify or delete a backup and it cannot reach the network. The
alternative — making backups group/world-readable — was rejected: it would trade a
permanent confidentiality loss for a temporary convenience.

### 4.2 Retention, `.previous` sets and abandoned attempts

```bash
python scripts/backup_retention.py --root "${BACKUP_ROOT}" --require-outbox          # plan
python scripts/backup_retention.py --root "${BACKUP_ROOT}" --require-outbox --apply  # prune

# also reclaim staging directories left by killed attempts
python scripts/backup_retention.py --root "${BACKUP_ROOT}" --apply --sweep-staging
```

Default plan: 7 most recent, 14 daily, 8 weekly. The newest set is never pruned, and
the tool **refuses to prune at all if no published set currently verifies** — pruning
must never run on the assumption that a replacement succeeded.

**`.previous` sets.** Replacing a backup keeps the displaced copy as
`<name>.previous` (and older displacements as `<name>.previous-<stamp>`). These are a
fallback, not archives: `--keep-previous` (default 2) retains the most recent ones and
prunes the rest, so disk use does not grow without bound. A `.previous` is never
treated as the primary copy, and retention refuses to prune when there is no verified
published set to fall back *from*.

**Abandoned staging (`.tmp-*`).** A killed backup leaves its staging directory behind.
`create_backup.py` sweeps them at the start of every run, and `--sweep-staging` does it
on demand. The sweep is deliberately conservative:

* only names beginning with `.tmp-` are considered, so a **published backup can never
  be removed** by it;
* symlinks are refused, never followed;
* an attempt is treated as live if its `.attempt-lock` (or the directory itself) was
  touched within 180 minutes, so a concurrent backup is never disturbed;
* anything younger than `--staging-min-age-hours` (default 24 h) is skipped;
* every skip is reported with a reason, so a run is bounded and explainable.

### 4.3 Which backup is "the latest"

Selection uses the **manifest timestamp**, not the directory modification time. A set
that was copied, restored or extracted into the backup root carries the copy's mtime,
which can make an older backup look newest. Directory mtime is only a fallback when the
manifest is unreadable — and a set whose manifest cannot be read fails verification, so
it is never selected.

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
| Power-loss survival is not proven | `fsync` barriers are in place (§2.1), but durability depends on the filesystem honouring them. Verified for process kill only. Unproven on the target hardware and on Windows, where the directory barrier does not exist. |
| Docker-mode gate execution not verified in development | The permission model is covered by static tests; the container invocation itself has not been run outside a host with Docker (`docker` is absent in the development environment). |
| Kernel/driver write caches | `fsync` cannot force data out of a drive's volatile cache; a UPS or a barrier-aware storage stack is the only further mitigation |
