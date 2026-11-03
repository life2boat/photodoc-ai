# Yandex.Disk namespace and directory creation

The pre-fix folder helper issued mkdir for every ancestor, including existing
application-folder parents. It also split `app:/...` into an invalid `app:`
component, and treated any HTTP 409 as success, even a conflict with a file.
The previous bounded live acceptance therefore used a temporary read-only
ancestor guard. Its first failing ancestor response status was not retained;
this change does not claim a specific historical HTTP error.

Native directory creation now reads `fields=type` first. An existing directory
needs no PUT. Only a 404 permits mkdir; 201 confirms creation. A concurrent 409
requires another successful lookup proving that the resource is a directory.
File conflicts, missing/malformed metadata, auth errors and service failures
fail closed. There is no in-function retry loop or destructive reconciliation;
the existing durable outbox retains sources and applies its bounded retry policy.

Paths preserve `app:/` and `disk:/` as namespace roots. Roots are lookup-only,
never mkdir targets. Legacy relative Disk paths remain supported, and the old
empty/slash-only configuration fallback is preserved. Namespace-aware joining
also prevents `app://...` in new order paths when the configured root is `app:/`.

An explicit `YANDEX_DISK_ROOT_DIR` confines destinations by namespace and whole
path components, so `app:/Stage_evil` is outside `app:/Stage`. Legacy callers
without an explicit root retain their relative-path behavior. File names must be
single raw components. Traversal, mixed/unknown namespace prefixes, control
characters, backslashes and encoded structural escapes are rejected before I/O.
Normal spaces and Unicode names remain supported. The staging production-root
guard also recognizes the `disk:/PhotoDoc_Orders` alias.

No overwrite/delete behavior was added. File uploads still request
`overwrite=false`; SHA-256/size verification, delivery leases, cleanup fencing,
price locking and Robokassa signature verification are unchanged. If an operator
changes the explicit configured root, older queued destinations outside it will
fail closed and retain sources; they are not silently rewritten or duplicated.

`backend/tests/test_yandex_namespace.py` exercises native code with synthetic HTTP:
protected existing parents, nested paths, namespace roots, deterministic concurrent
creation, 409/file conflicts, malformed metadata, API failures, root escape and
interrupted-upload retry through the real SQLite outbox and cleanup implementation.
The tests invoke the proposed native folder/upload functions directly; only HTTP
transport is synthetic. No temporary ancestor-guard wrapper is installed.
Existing transport mocks distinguish directory and file metadata.

Validation: the full backend suite passed with 388 passed and 2 Windows symlink
privilege skips (390 collected), including all 59 new namespace regressions.
Local verification blocked real HTTP transport and recorded zero attempts.
Offline staging preflight checks passed; live acceptance remained intentionally
blocked with no token. Production preflight rejected a missing DB as expected.
`git diff --check` and Python syntax checks passed. Payment-function ASTs and the
delivery/cleanup source were checked unchanged.
Docker copies the complete backend directory, including the new pure path module.

Reference: Yandex's official SDK keeps the namespace prefix separate from the
[resource path](https://github.com/yandex-disk/yandex-disk-restapi-java/blob/master/disk-restapi-sdk/src/main/java/com/yandex/disk/rest/util/ResourcePath.java).

This PR does not change staging, use OAuth credentials, call the live Disk API,
enable a worker or deploy an image. Historical acceptance harnesses remain audit
artifacts; they are not modified on the VPS. Native handling removes their
necessity in the proposed code, but the fixed image has not had a new live rollout.
There is no schema migration; before any later rollout, review the explicit root
and existing queued destinations. Rollback is reverting this code change, with
source files and durable queue records retained under the existing policy.
