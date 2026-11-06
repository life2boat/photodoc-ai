# PR-A A1: host receipt writer

This code is **not installed or activated** by this branch. It is the isolated
host-side A1 implementation against synthetic A2 database fixtures. It does
not create application tables, change the order endpoint, start workers, or
authorize a backup/deployment PASS.

## Owner-controlled installation boundary

Provision a no-login `photodoc-receipt` UID, separate submit/audit groups,
0700 ledger directory, 0400 Ed25519 raw private key, and a pinned raw public
key outside all backend mounts. Only the two socket paths may be visible to
authorized callers. The JSON config accepted by `receipt_service.from_config`
pins the actual host app DB and uploads-root device/inode, instance/epoch IDs,
UIDs and key paths. It must be owner-rendered after inspecting the real Docker
volume source; a path string from an app DB, manifest or socket request is not
authority. `ReceiptLedger.initialize` is an explicit one-time operation. The
service never initializes, repairs corruption by reset, or activates an epoch.

A1's `activate_epoch_from_fixture` is callable only in isolated synthetic
tests. A2/Stage 3 must provide and review the real epoch migration and
activation handshake before this service may accept live submissions. The
retention/deletion events and backend cleanup integration belong to A4; this
A1 socket does not silently accept them. The
host-specific systemd drop-in must set `ReadOnlyPaths` to the inspected DB
parent and uploads mount; the supplied units are templates for runtime
validation, not deployment instructions.

The app submit socket permits only `SubmitUploadSetV1` and
`GetBySubmissionIdV1`. The separate audit socket permits only
`AuditExportV1`; the server chooses a private export filename and passes a
read-only snapshot FD back with `SCM_RIGHTS`, so the audit caller needs no
direct directory access. Both sockets
require canonical JSON in a four-byte big-endian length frame, up to 64 KiB.
The submit request passes one file descriptor per original via `SCM_RIGHTS`.
The writer checks `SO_PEERCRED`, reads a committed PREPARING reservation
from the configured DB, checks the exact PUBLISHED inventory, reopens each
name with `openat2` under its host root, verifies inode and SHA-256, then
commits a signed event and publishes HEAD. A1 tests use an explicitly
injected synthetic opener on Windows; that is **not** evidence of Linux
`openat2`, Docker mount, FD transfer or systemd behavior.

The GetBySubmissionIdV1 lookup re-verifies the surviving reservation and
returns uncertainty if the application DB rolled back after receipt acceptance.

## Integrity and uncertainty

All accepted events and reserved upload IDs are insert-only with SQL triggers
and unique indexes. WAL uses `synchronous=FULL`; HEAD is written to a
same-directory exclusive temporary file, fsynced, replaced, then the parent
directory is fsynced. A committed ledger event with lagging HEAD is recovered
only after full chain verification on restart. A HEAD ahead of the ledger,
invalid signature, missing event, or corrupt DB stops the writer. An
uncertain commit or fsync never returns success. The audit export uses SQLite
online backup, exclusive no-overwrite publication and fsync. No off-host
anchoring is activated. A coherent rollback of ledger, HEAD and app DB is
**not detectable** by this local mechanism.

## A1/A2/A3 boundary and runtime gates

The synthetic fixture implements the v1.1 schema fields read by
`ReservationVerifier`. A2 must add those tables and the signed epoch
handshake. A3 must wire the order endpoint, stable idempotency key, source
FDs, ACTIVE final transaction and 24-hour response escrow. A1 does **not**
claim that an order is active or that a backup is restorable.

Mandatory isolated Ubuntu 24.04 Docker+systemd tests before deployment:
real UID/GID and `SO_PEERCRED` mapping; both socket permissions;
`SCM_RIGHTS` with one/ten/missing/extra FDs; `openat2` rejection of
symlinks, magic links, traversal and hardlinks; host/container inode
equivalence for bind and named volumes; DB identity/WAL; and actual
systemd sandbox permissions and fsync/crash restart. A Docker image build
or mocked opener does not pass this gate. On an unavailable Linux runtime,
report **NOT_TESTED**.
