# Backup evidence and the backup-set format (Stage 1)

This document describes the evidence contract implemented by
`scripts/db_evidence.py`, `scripts/backup_format.py` and `scripts/evidence_gate.py`.

It is **Stage 1** of the backup/deployment redesign. It is a read-only verification
library and command-line gate. It does **not** freeze writers, start containers or
perform any part of a cutover — that is Stage 2 (exclusion protocol) and Stage 3
(supervisor). Do not treat a Stage 1 PASS as a deployment authorisation.

## 1. What the gate claims

A backup set represents the live database **exactly** when all of the following hold:

1. the set's four files are present, regular, and match `manifest.sha256`;
2. `backup_manifest.json` is bound to those files and to the content it describes;
3. the backup database and the live database have the **identical schema**;
4. every persistent row exists in both, with **identical values**, in **both directions**.

If any check cannot be performed, the answer is a refusal. There is no "skipped" state
that still passes.

## 2. Schema comparison

`db_evidence.canonical_schema()` describes:

* every `sqlite_schema` object (`table`, `index`, `trigger`, `view`) **with its stored
  SQL**, including internal auto-indexes;
* per table: `table_list` classification (`WITHOUT ROWID`, `STRICT`, ordinary/shadow),
  `table_xinfo` columns (name, declared type, `NOT NULL`, default, `pk`, hidden/generated),
  `index_list` + `index_xinfo`, and `foreign_key_list`;
* `application_id` and `user_version`.

Comparisons are exact. A changed `CHECK`, a different default, a new index, a dropped
trigger, a modified view body or a changed declared type are all refusals — restoring
such a backup would change behaviour even where the rows happen to match.

## 3. Row identity

A row key must be **unique**, or the comparison refuses. This is where previous
implementations produced false PASSes.

| Table shape | Identity |
|---|---|
| `WITHOUT ROWID` | the complete ordered primary-key tuple |
| `INTEGER PRIMARY KEY` | that declared column |
| any other rowid table | `rowid`, `_rowid_` or `oid`, whichever is not shadowed |
| all three aliases shadowed | **refused** — never a non-unique fallback |

Two guards run before any comparison:

* a table whose row count differs from its count of distinct keys is refused, because the
  chosen identity is not unique;
* the merge is bidirectional, so a row present only in the backup is reported (restoring
  it would resurrect deleted state) as well as one present only in the live database.

`sqlite_sequence` and `sqlite_stat*` are compared like any other table. They are never
skipped by name: `sqlite_sequence` holds the AUTOINCREMENT high-water mark, and losing it
would reissue order ids after a restore.

## 4. Value encoding

Values are compared with a type tag and a length prefix:

| SQLite value | Encoding |
|---|---|
| `NULL` | `0x00` |
| integer | `0x01` + decimal ASCII |
| real | `0x02` + IEEE-754 big-endian payload |
| blob | `0x03` + bytes |
| text | `0x04` + UTF-8 |

So `1`, `1.0` and `'1'` are three different values; `-0.0` and `+0.0` differ; a blob
containing `0x00` differs from `NULL`. Rows are streamed in key order and merged, so a
large table is never materialised in memory.

## 5. Backup-set format (`FORMAT_VERSION = 2`)

```
orders.db              the database snapshot
uploads.tar.gz         the recovery originals
backup_manifest.json   metadata, including both artifact hashes and the evidence digests
manifest.sha256        checksums over the three files above
```

The manifest records the artifact hashes, `application_id`, `schema_digest`,
`logical_digest`, the upload member count, the verification result and its own format
version. `manifest.sha256` covers **all three** other files, so editing any manifest field
breaks the set. A disagreement between the manifest's recorded hash and the actual file is
fatal, never a warning.

The archive is inspected without extraction: duplicate members, symlinks, hard links,
absolute paths and `..` traversal are all refusals.

A plain hash cannot authenticate a malicious writer: storage writable by `root` remains a
trust boundary. The manifest says so in `trust_note`, and this is a statement of the
limit, not a mitigation.

## 6. Command line

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
| 3 | the set, the metadata or the evidence is invalid |
| 4 | the set is older than the freshness limit |
| 6 | invalid configuration (for example a non-finite `--max-age-hours`) |

`--policy pre-outbox-transition` exists for the single pre-PR#26 step: it requires both
databases to lack `paid_email_outbox` and changes nothing else — schemas and rows must
still be identical.

## 7. What Stage 1 does not establish

* **Writer exclusion.** The gate takes a consistent observation; it does not hold a lock
  and cannot stop a writer that starts afterwards. No PASS from this tool means "the
  system is frozen".
* **Deployment safety.** No container, migration or cutover logic is part of Stage 1.
* **Producer authentication.** Hashes bind the set to itself, not to a trusted producer.
* **Virtual tables.** A virtual table's content may live outside SQLite; the comparison
  refuses rather than guessing, and the module needs an explicit adapter before such a
  table can be covered.
