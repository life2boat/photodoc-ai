# PR-A A1 threat-to-test mapping

This maps A1 code to synthetic and observed local runtime evidence. The
writer cannot activate an order; A2/A3 and PR-B/Stage 2/Stage 3 remain
separate. No test is evidence of protection against coherent host rollback.

| Threat or failure | Enforcement | Test / runtime evidence |
|---|---|---|
| Invented order/submission UUID | Configured read-only app DB snapshot; exact PREPARING reservation and PUBLISHED rows | `test_adversarial_reservation_and_physical_mismatch` |
| Same-order file substitution or wrong bytes | Host reopens under pinned root; passed FD device/inode, link count, SHA-256 and size | `test_adversarial_reservation_and_physical_mismatch`, Linux openat2 test, Docker bind/named-volume tests |
| Symlink, hardlink, traversal or alternate root | `openat2` confinement, nlink=1, root FD identity | `test_linux_openat2_confinement_and_file_identity` |
| Missing/malformed/duplicate upload ID | Strict receipt fields, reserved ID table and unique indexes | Canonical parser and insert-only tests; further cross-order fixture needed with A2 |
| Duplicate submission with changed fields | Full request digest and exact original projection replay | `test_receipt_signed_chain_and_exact_replay`, `test_duplicate_submission_conflicts_and_insert_only` |
| Forged event, altered chain, corrupt HEAD | Ed25519, digest/chain replay, HEAD comparison, fail-closed startup | `test_corrupt_event_and_head_halt_without_reset`, `test_head_ahead_is_not_auto_reset` |
| Signing-key replacement/rotation | Independently pinned public registry, dual old/new rotation proof | `test_signed_dual_proof_key_rotation` |
| Ledger COMMIT/HEAD fsync uncertainty | No ACK until commit + HEAD file and directory fsync; restart repairs only lag | Five injected crash points in `test_crash_injection_never_acknowledges_uncertainty` |
| Backend direct ledger/key write | Separate host UID and no backend ledger/key mount; SQL insert-only triggers | SQL trigger test; actual mount and UID permissions require systemd deployment-mode gate |
| Socket role confusion or FD tampering | Distinct submit/audit sockets, peer UID check, exact frame/FD count | Linux socketpair FD/audit tests and no-network Docker UID 1000 bind/named-volume tests plus container-root rejection; full systemd socket activation NOT TESTED |
| Missing or replaced audit artifact | Private online SQLite snapshot, embedded HEAD, no-overwrite publication, read-only audit FD | `test_audit_export_no_overwrite`, `test_linux_audit_hardlink_publication_and_fsync`, `test_linux_audit_socket_returns_private_snapshot_fd` |
| Legacy provenance fabrication | Signed epoch fixture required before any receipt; no API to mint legacy receipts | Epoch startup/fixture tests; A2 real epoch not implemented |
| Coherent full-host rollback | **Not technically detectable locally** | Explicit limitation; do not mark PASS without independent anchor |
| App DB rolled back after accepted receipt | Lookup re-verifies the surviving PREPARING/ACTIVE reservation before returning historical receipt | test_lookup_refuses_ledger_only_receipt_after_app_rollback |

Runtime evidence: local Ubuntu WSL2 (Linux kernel 6.18.33.2), Docker Engine
29.7.1, cached `python:3.11-slim` with `--pull=never --network=none`.
The bind and named-volume tests exercise real cross-container FD transfer and
host/container inode equivalence. Systemd unit syntax was statically checked.
A transient local systemd user service with `NoNewPrivileges=yes`,
`ProtectSystem=strict` and `RestrictAddressFamilies=AF_UNIX` allowed
`AF_UNIX` and rejected `AF_INET` with errno 97. The first probe failed
because of command quoting and was rerun successfully for the intended
restriction. The actual installed A1 service, dual socket activation, UID
mapping and full ReadWritePaths/ReadOnlyPaths sandbox remain **NOT_TESTED**.
