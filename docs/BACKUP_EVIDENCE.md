# Backup evidence and the backup-set format (Stage 1)

This document describes the evidence contract implemented by
`scripts/db_evidence.py`, `scripts/recovery_artifacts.py`, `scripts/backup_format.py`,
`scripts/build_v2_set.py` and `scripts/evidence_gate.py`.

The per-state requirements are specified in
[RECOVERY_CONTRACT.md](RECOVERY_CONTRACT.md); that document is the authority for the
file-selection and restore-path rules summarised here.

It is **Stage 1** of the backup/deployment redesign: a read-only verification library, a
format-2 backup set, an explicit producer, and a command-line gate. It does **not** freeze
writers, start containers or perform any part of a cutover — that is Stage 2 (exclusion
protocol) and Stage 3 (supervisor). A Stage 1 PASS is never a deployment authorisation.

## 1. What the gate claims

A backup set represents the live database **exactly** when all of the following hold:

1. the four files are present, regular, and match `manifest.sha256`;
2. every field in `backup_manifest.json` survives validation — not merely checksum binding:
   the format and evidence versions are understood, the schema and logical digests are
   recomputed from the database, and the recorded upload counts match the archive;
3. the backup and live databases have the **identical schema**;
4. every persistent row exists in both with **identical values**, in **both directions**;
5. the archive can rebuild **every file the backup database still needs**, byte for byte.

If any check cannot be performed, the answer is a refusal. There is no "skipped" state that
still passes.

## 2. Schema comparison

`canonical_schema()` describes every `sqlite_schema` object with its stored SQL, plus
`table_list`, `table_xinfo`, `index_list`/`index_xinfo`, `foreign_key_list`,
`user_version`, `application_id`, and — per table — whether a declared `INTEGER PRIMARY
KEY` really aliases the rowid.

A changed `CHECK`, default, index, trigger, view body or declared type is a refusal:
restoring such a backup changes behaviour even where the rows happen to match.

## 3. Row identity

A row key must be **unique**, or the comparison refuses.

| Table shape | Identity |
|---|---|
| `WITHOUT ROWID` | the complete ordered primary-key tuple |
| any rowid table | the hidden `rowid` / `_rowid_` / `oid`, whichever is not shadowed |
| all three aliases shadowed | **refused** — never a non-unique fallback |

**Why the hidden rowid and not the declared key.** `INTEGER PRIMARY KEY` is normally an
alias for the rowid, but SQLite preserves a historical exception: `INTEGER PRIMARY KEY
DESC` (a column-level `DESC`) is *not* an alias — the column may be NULL while the rowid
stays distinct. Two databases whose only rows were `(rowid=1, id=NULL)` and
`(rowid=2, id=NULL)` then produced identical evidence. Keying every rowid table by the
hidden rowid needs no alias proof and is exact for both forms; the declared columns are
still compared as part of the row.

`declared_integer_pk_is_rowid_alias()` answers the alias question *semantically* rather
than by parsing SQL: a real alias creates no primary-key index, while the `DESC` form
creates an automatic `origin='pk'` index. The result is recorded in the evidence.

Two guards run before any comparison: a table whose row count differs from its count of
distinct keys is refused, and the merge is bidirectional — a row present only in the backup
(would resurrect deleted state) is reported as well as one present only in the live
database.

`sqlite_sequence` and `sqlite_stat*` are compared like any other table. Nothing is skipped
by name: the AUTOINCREMENT high-water mark is identity, not noise.

## 4. Value encoding

| SQLite value | Encoding |
|---|---|
| `NULL` | `0x00` |
| integer | `0x01` + decimal ASCII |
| real | `0x02` + IEEE-754 big-endian payload |
| blob | `0x03` + bytes |
| text | `0x04` + UTF-8 |

`1`, `1.0` and `'1'` differ; `-0.0` and `+0.0` differ; a blob containing `0x00` differs
from `NULL`. Rows stream in key order, so a large table is never materialised.

## 5. Snapshot consistency

Each side is read inside **one explicit `BEGIN` transaction held to the end of the
capture** — integrity, schema, and rows. Without it every `PRAGMA`/`SELECT` is its own
transaction, and the gate can combine rows that never coexisted: with two tables compared
either side of an atomic writer commit, each table matched the backup at the moment it was
read while the backup matched no state that ever existed.

What this does and does not give you:

* In WAL mode a writer still commits, and the held read transaction keeps returning the
  snapshot from its first read.
* In rollback-journal mode (the deployed configuration) a writer's commit is blocked for
  the duration.
* Either way, the two sides are read in *separate* transactions, and nothing stops a
  writer after the capture. **Stage 2 must exclude writers**; Stage 1 only reports what it
  observed.

The SQLite version is recorded in the evidence.

## 6. Capability checks

`PRAGMA table_list` arrived in SQLite 3.37. Below that — and for any unknown pragma —
SQLite returns an **empty result instead of raising**, which previously erased every row
from the evidence while still reporting PASS. Therefore:

* SQLite below 3.37 is refused outright;
* every table declared in `sqlite_schema` must appear in the introspection result, or the
  comparison refuses;
* an empty introspection result over a non-empty schema is a refusal, never "no tables";
* a database that is not SQLite (or is corrupt) yields a structured refusal with a reason
  rather than an escaping exception.

## 7. Recovery-byte verification

Header parsing is not evidence. A set can carry correct checksums and still hold a
substituted file whose name and size match, leaving the job unrecoverable.

`recovery_artifacts` streams every member — no extraction, so no hostile path can touch the
filesystem — and hashes it, then derives the **required recovery sources** from the backup
database itself, as specified in [RECOVERY_CONTRACT.md](RECOVERY_CONTRACT.md):

* schema generation first: P1 is supported; P0 and mixed are refused unless an operator
  opts into `--legacy-p0-policy include`, which requires the legacy candidates explicitly.
  The absence of `delivery_jobs` is **never** read as "no customer files";
* job kinds: `file`/`info` own uploads, `admin_email`/`paid_email` are database state and
  must not carry a `local_path`, unknown kinds are refused;
* **state wins over cleanup metadata**: `pending`/`processing`/`failed` always require the
  original, and a contradictory `cleanup_state='completed'` / `cleanup_phase='deleted'` /
  `local_deleted_at` on such a job is a refusal, not an exemption;
* only a delivered job with durable evidence (completed cleanup, deletion phase, timestamp
  that differs from `delivered_at`) is exempt.

Each required file must be a member at **exactly** `uploads/<basename>` with the recorded
SHA-256 and size. A nested member, or the right bytes at the wrong path, does not satisfy
the reference — a restore preserves the path, and the database looks directly in its
uploads directory. Missing, substituted, wrong-sized, truncated, ambiguous and duplicate
members are refusals.

The archived file set must **equal** the derived set: an unneeded customer file is refused,
because retaining a photo that was already deleted violates the zero-retention contract.

Limits are explicit (`Limits`): member count, per-member bytes, total uncompressed bytes,
path length.

### Path and stream policy

POSIX semantics only, independent of the host OS: absolute paths, `C:` drive prefixes,
backslashes, empty/`.`/`..` components, nesting below `uploads/`, duplicate names
(case-insensitive), links and non-regular members are rejected. The gzip stream is then
read to physical EOF so a truncated trailer (CRC and length) cannot pass.

## 8. Backup-set format (`FORMAT_VERSION = 2`)

```
orders.db              the database snapshot
uploads.tar.gz         the recovery originals
backup_manifest.json   metadata: artifact hashes, evidence digests, upload counts, set_id
manifest.sha256        checksums covering the three files above
```

Validation of every field, not just the hashes:

| Field | Check |
|---|---|
| `format_version` | exactly 2 |
| `evidence_format` | exactly the supported evidence version |
| `set_id` | a UUID or 32 hex characters (a non-empty arbitrary string is refused) |
| `artifacts` | disagree with the actual files → fatal |
| `schema_digest`, `logical_digest` | recomputed from the database and compared |
| `database_identity` | required keys present and **non-NULL**; both digests compared with recomputed values; unknown keys refused |
| `trust_note` | must be the exact current text |
| `uploads.members`, `uploads.uncompressed_bytes` | must equal the scanned archive |
| unknown top-level fields | refused |

A checksum proves the manifest was not edited; it says nothing about whether each claim is
true. Both are now checked.

### Publication

`publish_set()` refuses to overwrite an existing set — a set ID identifies one set forever —
and flushes every file before the rename. A **file** fsync failure aborts publication; only
a genuinely unsupported handle is tolerated, and `fsync_supported` records it. The parent
directory flush is best-effort and reported separately. A `.tmp-*` staging directory is
never a set and is refused by verification.

## 9. Producer and version compatibility

`scripts/create_backup.py` still writes **format 1**, which this verifier does not accept.
That is deliberate: a v1 manifest cannot express the evidence digests or the recovery-byte
contract, so reinterpreting it as v2 would be a silent downgrade. The gate detects a legacy
set and refuses it with the exact command to migrate.

`scripts/build_v2_set.py` is the explicit producer:

```bash
# fresh set from the live database and uploads
python3 scripts/build_v2_set.py --live-db /data/orders.db \
    --uploads /app/uploads --output /backups/20260101T000000Z

# re-manifest an existing legacy set (every claim is recomputed, not copied)
python3 scripts/build_v2_set.py --from-v1 /backups/OLD --output /backups/NEW
```

The producer refuses when the files on disk do not match the database (`content_sha256`,
`content_size`), when a required original is missing, when the produced set fails its own
verification, and when the output already exists. The upgrade path re-verifies the legacy
archive against its own database and earns a fresh `PASS`; it never copies the old claim.

Neither producer is wired into deployment. Producing a set is not authorising a cutover.

## 10. Command line

```bash
python3 scripts/evidence_gate.py \
  --live-db /path/to/orders.db \
  --backup-dir /backups/20260101T000000Z \
  --max-age-hours 24 --json
```

| Exit | Meaning |
|---|---|
| 0 | the set matches the live database exactly |
| 2 | a required input is missing |
| 3 | the set, the metadata, the recovery bytes or the evidence is invalid |
| 4 | the set is older than the freshness limit |
| 6 | invalid configuration (for example a non-finite `--max-age-hours`) |

`--policy pre-outbox-transition` exists for the single pre-PR#26 step: both databases must
lack `paid_email_outbox`, and nothing else is relaxed.

## 11. What Stage 1 does not establish

* **Writer exclusion.** A snapshot is an observation, not a lock. Nothing here prevents a
  writer after the capture, so no PASS means "the system is frozen".
* **Deployment safety.** No container, migration or cutover logic is included.
* **Producer authenticity.** Hashes bind a set to itself. A root actor able to rewrite every
  artifact *and* the manifest *and* the checksums can build a self-consistent replacement;
  detecting that needs an off-host signature or a separately held key.
* **Virtual tables.** Their content may live outside SQLite; the comparison refuses rather
  than guessing, and needs an explicit module adapter.
* **An already-published set is immutable by convention, not by the filesystem.** Anything
  with write access to the directory can delete it.
