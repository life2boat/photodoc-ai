# Recovery contract (Stage 1)

What a backup set must contain for every persisted order and delivery state, and what
must be refused. This document is the authority for `scripts/recovery_artifacts.py`;
the producer and the verifier both implement it, so neither can accept a set the other
would not build.

A customer original **cannot be reconstructed** from the database. If it is required and
absent, the recovery is impossible — the set is refused. Nothing here "re-creates" a file.

## 1. Schema generations

| Generation | Detection | Status in Stage 1 |
|---|---|---|
| **P1** | `delivery_jobs` table exists **and** `orders.order_token_hash` column exists | supported |
| **P0** | no `delivery_jobs` table | **refused by default**; explicit `include` policy required |
| **mixed** | `delivery_jobs` exists but `orders.order_token_hash` is missing | **refused by default**; `include` required |
| anything else | neither | refused |

The absence of `delivery_jobs` never means "no customer files exist": a P0 database records
originals only in `orders.filename`, and treating that as "nothing to preserve" discards
the only copy.

### P0 policy

`orders.filename` may hold a comma-separated list of original names. The stored file may be
any of:

```
{order_id}_{sanitized name}   {order_id}_{raw name}   {sanitized name}   {raw name}
```

all directly inside the uploads root. With `--legacy-p0-policy include`, every listed name
must resolve to **exactly one** archived member at `uploads/<candidate>`; none resolving, or
several matching, is a refusal. The default is `fail`: P0 sets are rejected with the reason,
because the mapping is ambiguous until an operator reconciles those orders into
`delivery_jobs`. `exclude` is not offered — for a *backup* it means deliberately discarding
the only copy of a customer file.

An order whose `filename` is empty, NULL or unparseable is **refused** under `include`: an
empty list does not prove that no original ever existed. A stored layout claimed by more
than one original is refused as ambiguous.

## 2. Job kinds

| Kind | File ownership | Rule |
|---|---|---|
| `file` | upload-backed | `local_path`, `content_sha256`, `content_size` required; original must be archived while unfinished |
| `info` | upload-backed | same as `file` |
| `admin_email` | database state | `local_path` must be empty; no archive member |
| `paid_email` | database + outbox state | `local_path` must be empty; no archive member |
| unknown | — | refused: the contract cannot guess whether a file is required |

An email job with a `local_path` is contradictory and is refused rather than ignored.
Email jobs have a recognized state too: `state` must be one of the known states,
`cleanup_state` must be `not_applicable`, `cleanup_phase` must be `none`, and no
`local_deleted_at` may be set. An unknown email state is refused rather than hidden behind
"no file to check".

## 3. State × cleanup matrix (upload-backed kinds)

`state` decides whether the original is needed. Cleanup metadata may only **confirm**
that a delivered file was legitimately deleted; it may never exempt an unfinished job.

| `state` | `cleanup_state` | `cleanup_phase` | `local_deleted_at` | Original |
|---|---|---|---|---|
| `pending` / `processing` / `failed` | any | any | any | **required**; `completed`/`deleting`/`deleted` here is a contradiction → refuse |
| `delivered` | `completed` | `deleted` or `deleting` | set | may be absent (**exempt**) |
| `delivered` | `completed` | `none`/`pending`/`failed`, or no timestamp | — | **refuse**: claims deletion without evidence |
| `delivered` | `pending` / `processing` / `failed` | any | any | not required; must not be archived |
| `delivered` | `not_applicable` | any | any | **refuse** for `file`/`info`: these kinds always have files |
| `delivered` | any | `deleted`/`deleting` | missing | **refuse**: phase claims deletion without a timestamp |
| any | missing / unknown value | any | any | refused (the CHECK constraints make this corruption) |

Additional contradictions, all refusals:

* `local_deleted_at` set while `cleanup_state` is not `completed`;
* `cleanup_phase ∈ {deleted, deleting}` while `cleanup_state` is not `completed`;
* `local_deleted_at` equal to `delivered_at` (the historical "same value" shortcut that
  proves nothing);
* a `file`/`info` job with an empty `local_path`;
* a required job without a valid `content_sha256` or `content_size`.

### 3a. Order-to-job completeness (P1)

Being P1 does not by itself prove that every order still has its jobs. A partial migration
or a missing row can leave an order with an `orders.filename` but no `file` job, in which
case the original is unaddressable and must not be silently dropped. The application
creates, atomically with each order, one `file` job per attachment name and exactly one
`info` job. Both invariants are checked per order (including unpaid orders); a mismatch, or
an empty `filename`, is a refusal — not "zero originals".

### 3b. Restore-root binding

Every required original must be restorable to the **exact path the application records**.
All required `local_path` values must share one uploads root; it is recorded as
`restore_uploads_root`, and the restore rehearsal extracts members to that root and verifies
`local_path == <root>/<basename>`, not just the basename and hash. A target-root relocation
therefore needs an explicit, verified path rewrite or is refused.

## 4. Archive path contract

The archive contains exactly one directory entry named `uploads` and one entry per required
file at:

```
uploads/<basename of local_path>
```

* `local_path` must be directly inside the uploads root (`.../uploads/<name>`); a nested
  path such as `/app/uploads/sub/photo.jpg` is **unsupported** and refused, because a
  flattening restore would place the file where the database does not look for it.
* A member at any other path — `uploads/nested/photo.jpg`, `evil`, `/tmp/evil`,
  `C:/evil`, `uploads\evil` — never satisfies a required reference. Matching is by the
  **exact member path**, not by basename, so the right bytes at the wrong location are
  still a missing file.
* Duplicate names (case-insensitive), links, and non-regular members are refused.

## 5. Unexpected contents

The set of archived files must **equal** the derived required set. An extra file is
refused: a delivered-and-deleted original that reappears in the archive means the
zero-retention contract was violated, and a v1 upgrade would otherwise preserve customer
photos that were deliberately deleted. There is no "extra files are harmless" exception.

## 6. Manifest and set binding

The manifest's `schema_digest` and `logical_digest` are recomputed from the archived
database and must match; `database_identity.application_id` and `schema_digest` must be
present, non-NULL, and equal to the recomputed values; `uploads.members` and
`uploads.uncompressed_bytes` must equal the scanned archive; `set_id` must be a UUID/hex
identifier; unknown fields at the top level, under `database_identity` and under `uploads`
are refused; `trust_note` must be the exact current text.

## 7. Verification order

1. set structure, checksums, manifest fields (above);
2. gzip stream read to physical EOF — trailer CRC and length validated, so a truncated
   tail cannot pass;
3. per-member scan: path policy, limits, SHA-256 of the actual bytes;
4. required set derived from the database (generation → kinds → state × cleanup);
5. exact-path match, content match, and equality of the archived set with the required set;
6. only then a PASS.

## 8. What this contract does not cover

* Writers started after the capture (Stage 2).
* Producer authenticity: a privileged actor who rewrites artifacts, manifest and checksums
  produces a self-consistent set. An off-host signature is required to detect that.
* Virtual tables and attached databases.
* Encryption at rest, and deletion of files outside the uploads root.
