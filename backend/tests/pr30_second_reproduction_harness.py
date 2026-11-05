"""PR #30 *second* review reproduction harness (not collected by pytest).

    python backend/tests/pr30_second_reproduction_harness.py [repo-root]

Expected on the reviewed head ``28b3024``: every case below shows the defect.
Expected after the fixes: ``0/N``.

A case that raises is recorded as a *failure*, never swallowed: the first harness
silently skipped a case whose API did not exist on an older head, which overstated
its coverage.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sqlite3
import sys
import tarfile
import tempfile
import traceback
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "backend" / "tests"))

import backup_format  # noqa: E402
import build_v2_set  # noqa: E402
import db_evidence  # noqa: E402
import evidence_gate  # noqa: E402
import recovery_artifacts  # noqa: E402

APP = 1346650441
NOW = datetime.now(timezone.utc)
results = []


def record(name, bad, detail=""):
    results.append((name, bad, detail))
    print(f"{name:46} {'DEFECT (bad)' if bad else 'refused (good)':16} {detail}")


def new_db(path, script, rows=()):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA application_id = {APP}")
    conn.executescript(script)
    for sql in rows:
        conn.execute(sql)
    conn.commit()
    conn.close()
    return path


P1_SCHEMA = """
CREATE TABLE orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer TEXT,
    filename TEXT,
    payment_status TEXT DEFAULT 'unpaid',
    order_token_hash TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE delivery_jobs (
    id TEXT PRIMARY KEY,
    order_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    local_path TEXT,
    content_sha256 TEXT,
    content_size INTEGER,
    state TEXT NOT NULL DEFAULT 'pending',
    cleanup_state TEXT NOT NULL DEFAULT 'pending',
    cleanup_phase TEXT NOT NULL DEFAULT 'none',
    local_deleted_at TEXT,
    delivered_at TEXT
);
"""

P0_SCHEMA = """
CREATE TABLE orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer TEXT,
    filename TEXT,
    payment_status TEXT DEFAULT 'unpaid',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


def write_uploads(directory: Path, files: dict[str, bytes]) -> Path:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        (directory / name).write_bytes(data)
    return directory


def build_set(db, dest, members: dict[str, bytes]):
    """A fully checksum-bound set with the given archive member contents."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(db)
    dst = sqlite3.connect(dest / "orders.db")
    src.backup(dst)
    dst.close()
    src.close()
    archive = dest / "uploads.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        root = tarfile.TarInfo("uploads")
        root.type = tarfile.DIRTYPE
        tar.addfile(root)
        for name, data in members.items():
            member = tarfile.TarInfo(f"uploads/{name}")
            member.size = len(data)
            tar.addfile(member, io.BytesIO(data))
    conn = db_evidence.open_readonly(dest / "orders.db")
    try:
        schema = db_evidence.canonical_schema(conn)
        logical = db_evidence.logical_state_digest(conn)
    finally:
        conn.close()
    info, _ = backup_format.inspect_archive(archive)
    manifest = backup_format.build_manifest(
        artifacts={"orders.db": backup_format.sha256_file(dest / "orders.db"),
                   "uploads.tar.gz": backup_format.sha256_file(archive)},
        created_at=NOW, application_id=APP,
        schema_digest=schema["digest"], logical_digest=logical,
        database_identity={"application_id": APP, "schema_digest": schema["digest"],
                           "logical_digest": logical},
        upload_members=info["members"], upload_bytes=info["bytes"])
    backup_format.write_manifest_files(dest, manifest)
    return dest


def rehash(path: Path) -> None:
    lines = [f"{backup_format.sha256_file(path / n)}  {n}"
             for n in backup_format.CHECKSUMMED]
    (path / backup_format.MANIFEST_FILE).write_text("\n".join(lines) + "\n")


def run_gate(db, setdir):
    return evidence_gate.evaluate(Path(db), Path(setdir), expected_application_id=APP,
                                  max_age_hours=48)


# ==================================================================== HN1 (P0 omitted)
def hn1():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        db = new_db(tmp / "p0.db", P0_SCHEMA,
                    ["INSERT INTO orders (id, customer, filename, payment_status) "
                     "VALUES (1,'Ann','photo.jpg','paid')"])
        uploads = write_uploads(tmp / "uploads", {"photo.jpg": b"legacy pending original"})
        out = tmp / "backups" / "s1"
        code = build_v2_set.main(["--live-db", str(db), "--uploads", str(uploads),
                                  "--output", str(out)])
        ok, report = backup_format.verify_set(out, expected_application_id=APP)
        gate, _ = run_gate(db, out)
        archived = report["checks"]["archive"]["files"] if ok else "n/a"
        record("HN1 P0 original omitted", code == 0 and ok and gate == 0,
               f"producer={code} verify={ok} gate={gate} archived_files={archived}")


# ======================================================= HN2 (contradictory cleanup)
def hn2():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        digest = hashlib.sha256(b"missing").hexdigest()
        db = new_db(tmp / "p1.db", P1_SCHEMA, [
            "INSERT INTO orders (id, customer, payment_status, order_token_hash) "
            "VALUES (1,'Ann','paid','tok')",
            "INSERT INTO delivery_jobs (id, order_id, kind, local_path, content_sha256, "
            "content_size, state, cleanup_state, cleanup_phase, local_deleted_at) VALUES "
            f"('j1',1,'file','/app/uploads/missing.jpg','{digest}',7,'pending','completed',"
            "'deleted','2026-01-01T00:10:00+00:00')",
        ])
        setdir = build_set(db, tmp / "set", {})
        ok, report = backup_format.verify_set(setdir, expected_application_id=APP)
        gate, _ = run_gate(db, setdir)
        record("HN2 pending job exempted by cleanup", ok and gate == 0,
               f"verify={ok} gate={gate}")


# ============================================================ HN3 (nested restore path)
def hn3():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        data = b"correct recovery original"
        digest = hashlib.sha256(data).hexdigest()
        db = new_db(tmp / "p1.db", P1_SCHEMA, [
            "INSERT INTO orders (id, customer, payment_status, order_token_hash) "
            "VALUES (1,'Ann','paid','tok')",
            "INSERT INTO delivery_jobs (id, order_id, kind, local_path, content_sha256, "
            "content_size, state) VALUES "
            f"('j1',1,'file','/app/uploads/photo.jpg','{digest}',{len(data)},'pending')",
        ])
        archive = tmp / "set" / "uploads.tar.gz"
        setdir = build_set(db, tmp / "set", {"nested/photo.jpg": data})
        ok, report = backup_format.verify_set(setdir, expected_application_id=APP)
        gate, _ = run_gate(db, setdir)
        record("HN3 nested archive path accepted", ok and gate == 0,
               f"verify={ok} gate={gate} archive={archive.exists()}")


# ================================================== HN4 (real application fixture)
def hn4():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        from backup_set_builder import build_database  # noqa: WPS433
        source = tmp / "src"
        source.mkdir()
        build_database(source / "orders.db", source / "uploads")
        out = tmp / "backups" / "real"
        code = build_v2_set.main(["--live-db", str(source / "orders.db"),
                                  "--uploads", str(source / "uploads"),
                                  "--output", str(out)])
        record("HN4 real fixture refused by producer", code != 0,
               f"producer_exit={code}")


# ================================================ HN5 (temporary database copy leaked)
def hn5():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        db = new_db(tmp / "p1.db", P1_SCHEMA, [
            "INSERT INTO orders (id, customer, payment_status, order_token_hash) "
            "VALUES (1,'Ann','paid','tok')"])
        uploads = write_uploads(tmp / "uploads", {})
        created: list[Path] = []
        original = tempfile.mkdtemp

        def tracking(*args, **kwargs):
            directory = Path(original(*args, **kwargs))
            created.append(directory)
            return str(directory)

        tempfile.mkdtemp = tracking
        try:
            build_v2_set.main(["--live-db", str(db), "--uploads", str(uploads),
                               "--output", str(tmp / "out")])
        finally:
            tempfile.mkdtemp = original
        leaked = [d for d in created if (d / "snapshot.db").exists()]
        for directory in created:
            shutil.rmtree(directory, ignore_errors=True)
        record("HN5 producer leaks a database copy", bool(leaked),
               f"leaked={[str(d) for d in leaked]}")


# ================================================== M1a (set_id not validated)
def _simple_set(tmp: Path):
    db = new_db(tmp / "p1.db", P1_SCHEMA, [
        "INSERT INTO orders (id, customer, payment_status, order_token_hash) "
        "VALUES (1,'Ann','paid','tok')"])
    return db, build_set(db, tmp / "set", {})


def m1_set_id():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        db, setdir = _simple_set(tmp)
        manifest = json.loads((setdir / "backup_manifest.json").read_text())
        manifest["set_id"] = "NOT-A-UUID"
        (setdir / "backup_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
        rehash(setdir)
        ok, report = backup_format.verify_set(setdir, expected_application_id=APP)
        _gate, _ = run_gate(db, setdir)
        record("M1a non-UUID set_id accepted", ok, f"verify={ok}")


# ================================================== M1b (NULL nested digest)
def m1_null_digest():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        db, setdir = _simple_set(tmp)
        manifest = json.loads((setdir / "backup_manifest.json").read_text())
        manifest["database_identity"]["schema_digest"] = None
        (setdir / "backup_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
        rehash(setdir)
        ok, _ = backup_format.verify_set(setdir, expected_application_id=APP)
        record("M1b NULL identity schema_digest accepted", ok, f"verify={ok}")


# ================================================== M1c (unknown uploads key)
def m1_uploads_key():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        db, setdir = _simple_set(tmp)
        manifest = json.loads((setdir / "backup_manifest.json").read_text())
        manifest["uploads"]["injected"] = 1
        (setdir / "backup_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
        rehash(setdir)
        ok, _ = backup_format.verify_set(setdir, expected_application_id=APP)
        record("M1c unknown uploads key accepted", ok, f"verify={ok}")


# ============================================ M2 (extra unneeded private file)
def m2_extra_private():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        data = b"needed"
        digest = hashlib.sha256(data).hexdigest()
        db = new_db(tmp / "p1.db", P1_SCHEMA, [
            "INSERT INTO orders (id, customer, payment_status, order_token_hash) "
            "VALUES (1,'Ann','paid','tok')",
            "INSERT INTO delivery_jobs (id, order_id, kind, local_path, content_sha256, "
            "content_size, state) VALUES "
            f"('j1',1,'file','/app/uploads/needed.jpg','{digest}',{len(data)},'pending')",
        ])
        setdir = build_set(db, tmp / "set",
                           {"needed.jpg": data, "deleted-private-photo.jpg": b"private"})
        ok, _ = backup_format.verify_set(setdir, expected_application_id=APP)
        gate, _ = run_gate(db, setdir)
        record("M2 extra private file accepted", ok and gate == 0,
               f"verify={ok} gate={gate}")


# ============================================ M3 (publication after fsync failure)
def m3_fsync_failure():
    """Simulate a real I/O failure on the file durability barrier."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        db, built = _simple_set(tmp)
        staging = tmp / "staging"
        staging.mkdir()
        for name in backup_format.REQUIRED_FILES:
            (staging / name).write_bytes((built / name).read_bytes())
        barrier = getattr(backup_format, "fsync_file", None)
        if barrier is None:
            # The reviewed head had no per-file barrier at all: publication ignored it.
            record("M3 publication survives fsync failure", True,
                   "no per-file durability barrier exists")
            return

        def failing(path):
            raise backup_format.FormatError(f"simulated I/O failure flushing {path}")

        backup_format.fsync_file = failing
        try:
            try:
                backup_format.publish_set(staging, tmp / "published")
                published = (tmp / "published").is_dir()
            except Exception:  # noqa: BLE001
                published = False
        finally:
            backup_format.fsync_file = barrier
        record("M3 publication survives fsync failure", published,
               f"published={published} staging_intact={staging.is_dir()}")


# ============================================ M4 (truncated gzip trailer passes)
def m4_truncated_trailer():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        db, setdir = _simple_set(tmp)
        archive = setdir / "uploads.tar.gz"
        data = archive.read_bytes()
        archive.write_bytes(data[:-4])
        _, errors = backup_format.inspect_archive(archive)
        record("M4 truncated gzip trailer accepted", not errors, f"errors={errors[:1]}")


CASES = (hn1, hn2, hn3, hn4, hn5, m1_set_id, m1_null_digest, m1_uploads_key,
         m2_extra_private, m3_fsync_failure, m4_truncated_trailer)


def main():
    for case in CASES:
        try:
            case()
        except Exception as exc:  # noqa: BLE001
            record(case.__name__, True, f"raised {type(exc).__name__}: {exc}")
            traceback.print_exc()
    bad = sum(1 for _, defect, _ in results if defect)
    print(f"\nsummary: {bad}/{len(results)} cases show the defect")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
