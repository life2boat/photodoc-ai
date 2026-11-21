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

> **Stage 1 addition.** `scripts/evidence_gate.py` verifies a set against the live
> database with a canonical, bidirectional comparison (schema objects and their SQL,
> complete row identity including `WITHOUT ROWID`, shadowed rowid aliases and the
> `INTEGER PRIMARY KEY DESC` exception, type-tagged values, `sqlite_sequence`), a manifest
> whose every field is validated rather than merely checksum-bound, and a recovery-byte
> check that the archive really holds the files the database needs. See
> [BACKUP_EVIDENCE.md](BACKUP_EVIDENCE.md). It is read-only and does not replace the
> deployment gate below: it does not exclude writers.
>
> The uploads path must be the **Compose-derived mount**, not a guess: the backend
> container mounts the uploads volume at `/app/uploads` (`docker-compose.yml`,
> `docker-compose.staging*.yml`, `docker-compose.production.yml`), and `UPLOAD_DIR`
> defaults to `/app/uploads` in `backend/delivery.py`. On the host, use the bind source
> (`${PHOTODOC_UPLOADS_SOURCE:-/opt/photodoc/backend/uploads}`) or the volume's `_data`
> directory. Any other path makes the producer refuse for the wrong reason.
>
> **Version note (important).** `scripts/create_backup.py` still produces **format 1**.
> The Stage 1 gate accepts **format 2 only** and refuses a v1 set with an explicit message
> rather than reinterpreting it, because a v1 manifest cannot express the evidence digests
> or the recovery-byte contract. Build a v2 set with:
>
> ```bash
> python3 scripts/build_v2_set.py --live-db /data/orders.db \
>     --uploads /app/uploads --output "${BACKUP_ROOT}/${STAMP}"
> # or upgrade an existing legacy set (every claim is recomputed):
> python3 scripts/build_v2_set.py --from-v1 "${BACKUP_ROOT}/OLD" \
>     --output "${BACKUP_ROOT}/${STAMP}"
> ```
>
> A v2 set is not a deployment input on its own: Stage 2 must still exclude writers, and
> the deployment gate below remains the cutover decision.

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
