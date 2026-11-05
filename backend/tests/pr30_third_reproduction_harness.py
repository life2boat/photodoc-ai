"""PR #30 *third* review reproduction harness (not collected by pytest).

    python backend/tests/pr30_third_reproduction_harness.py [repo-root]

Expected on `c17fd8f`: every case shows the defect. Expected after the fixes: ``0/N``.
"""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import sqlite3
import sys
import tarfile
import tempfile
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
    print(f"{name:52} {'DEFECT (bad)' if bad else 'refused (good)':16} {detail}")


P1_SCHEMA = """
CREATE TABLE orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer TEXT, filename TEXT, payment_status TEXT, order_token_hash TEXT
);
CREATE TABLE delivery_jobs (
    id TEXT PRIMARY KEY, order_id INTEGER NOT NULL, kind TEXT NOT NULL,
    local_path TEXT, content_sha256 TEXT, content_size INTEGER,
    state TEXT NOT NULL DEFAULT 'pending', cleanup_state TEXT NOT NULL DEFAULT 'pending',
    cleanup_phase TEXT NOT NULL DEFAULT 'none', local_deleted_at TEXT, delivered_at TEXT
);
"""


def new_db(path, script, rows=()):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA application_id = {APP}")
    conn.executescript(script)
    for sql in rows:
        conn.execute(sql)
    conn.commit(); conn.close()
    return path


def uploads(directory: Path, files: dict[str, bytes]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        (directory / name).write_bytes(data)
    return directory


def build_set(db, dest, members):
    dest = Path(dest); dest.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(db); dst = sqlite3.connect(dest / "orders.db")
    src.backup(dst); dst.close(); src.close()
    with tarfile.open(dest / "uploads.tar.gz", "w:gz") as tar:
        root = tarfile.TarInfo("uploads"); root.type = tarfile.DIRTYPE; tar.addfile(root)
        for name, data in members.items():
            m = tarfile.TarInfo(name if name.startswith("uploads/") else f"uploads/{name}")
            m.size = len(data); tar.addfile(m, io.BytesIO(data))
    conn = db_evidence.open_readonly(dest / "orders.db")
    try:
        schema = db_evidence.canonical_schema(conn)
        logical = db_evidence.logical_state_digest(conn)
    finally:
        conn.close()
    info, _ = backup_format.inspect_archive(dest / "uploads.tar.gz")
    manifest = backup_format.build_manifest(
        artifacts={"orders.db": backup_format.sha256_file(dest / "orders.db"),
                   "uploads.tar.gz": backup_format.sha256_file(dest / "uploads.tar.gz")},
        created_at=NOW, application_id=APP, schema_digest=schema["digest"],
        logical_digest=logical,
        database_identity={"application_id": APP, "schema_digest": schema["digest"],
                           "logical_digest": logical},
        upload_members=info["members"], upload_bytes=info["bytes"])
    backup_format.write_manifest_files(dest, manifest)
    return dest


def rehash(path: Path) -> None:
    lines = [f"{backup_format.sha256_file(path / n)}  {n}"
             for n in backup_format.CHECKSUMMED]
    (path / backup_format.MANIFEST_FILE).write_text("\n".join(lines) + "\n")


def file_job(order_id=1, name="/app/uploads/photo.jpg", data=b"correct recovery original",
             state="pending", kind="file", cleanup_state="pending", cleanup_phase="none"):
    return {
        "id": f"j{order_id}", "order_id": order_id, "kind": kind, "local_path": name,
        "content_sha256": hashlib.sha256(data).hexdigest(), "content_size": len(data),
        "state": state, "cleanup_state": cleanup_state, "cleanup_phase": cleanup_phase,
        "local_deleted_at": None, "delivered_at": None,
    }


def p1_with_jobs(path: Path, orders, jobs):
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA application_id = {APP}")
    conn.executescript(P1_SCHEMA)
    for order in orders:
        conn.execute("INSERT INTO orders (id, customer, filename, payment_status, "
                     "order_token_hash) VALUES (?,?,?,?,?)", order)
    for job in jobs:
        cols = ", ".join(job)
        conn.execute(f"INSERT INTO delivery_jobs ({cols}) VALUES ({','.join('?' for _ in job)})",
                     tuple(job.values()))
    conn.commit(); conn.close()
    return path


# ================================================================ H1 missing jobs
def h1_missing_jobs():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        db = p1_with_jobs(tmp / "db.db",
                          [(1, "C1", "photo.jpg", "paid", "tok1")],
                          [])   # filename present, but no file/info jobs at all
        up = uploads(tmp / "uploads", {"photo.jpg": b"correct recovery original"})
        out = tmp / "out"
        code = build_v2_set.main(["--live-db", str(db), "--uploads", str(up),
                                  "--output", str(out)])
        record("H1 missing jobs still pass", code == 0, f"producer_exit={code}")


# ================================================================ H2 wrong root
def h2_wrong_root():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        db = p1_with_jobs(tmp / "db.db",
                          [(1, "C1", "photo.jpg", "paid", "tok1")],
                          [file_job(name="/absent/uploads/photo.jpg")])
        up = uploads(tmp / "uploads", {"photo.jpg": b"correct recovery original"})
        out = tmp / "out"
        code = build_v2_set.main(["--live-db", str(db), "--uploads", str(up),
                                  "--output", str(out)])
        ok = False
        if code == 0:
            ok, _ = backup_format.verify_set(out, expected_application_id=APP)
        record("H2 unbound absolute root passes", bool(code == 0 and ok),
               f"producer_exit={code} verify={ok}")


# ================================================================ H3 blank P0 name
def h3_blank_p0():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        db = new_db(tmp / "p0.db", """
            CREATE TABLE orders (id INTEGER PRIMARY KEY, filename TEXT, payment_status TEXT);
        """, ["INSERT INTO orders VALUES (1,'','paid')"])
        up = uploads(tmp / "uploads", {})
        out = tmp / "out"
        code = build_v2_set.main(["--live-db", str(db), "--uploads", str(up),
                                  "--output", str(out), "--legacy-p0-policy", "include"])
        ok = False
        if code == 0:
            ok, _ = backup_format.verify_set(out, expected_application_id=APP,
                                             legacy_p0_policy="include")
        record("H3 blank P0 filename passes", bool(code == 0 and ok),
               f"producer_exit={code} verify={ok}")


# ================================================================ M1 email state
def m1_email_state():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        conn = sqlite3.connect(tmp / "db.db")
        conn.execute(f"PRAGMA application_id = {APP}")
        conn.executescript(P1_SCHEMA)
        conn.execute("INSERT INTO orders VALUES (1,'C1','photo.jpg','paid','tok1')")
        conn.execute("INSERT INTO delivery_jobs (id, order_id, kind, state, cleanup_state, "
                     "cleanup_phase) VALUES ('e1',1,'admin_email','alien','not_applicable','none')")
        conn.commit(); conn.close()
        up = uploads(tmp / "uploads", {})
        out = tmp / "out"
        code = build_v2_set.main(["--live-db", str(tmp / "db.db"), "--uploads", str(up),
                                  "--output", str(out)])
        ok = False
        if code == 0:
            ok, _ = backup_format.verify_set(out, expected_application_id=APP)
        record("M1 unknown email state passes", bool(code == 0 and ok),
               f"producer_exit={code} verify={ok}")


# ================================================================ M2 nested fields
def m2_nested():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        db = p1_with_jobs(tmp / "db.db", [(1, "C1", "photo.jpg", "paid", "tok1")],
                          [file_job()])
        up = uploads(tmp / "uploads", {"photo.jpg": b"correct recovery original"})
        setdir = build_set(db, tmp / "set", {"photo.jpg": b"correct recovery original"})
        manifest = json.loads((setdir / "backup_manifest.json").read_text())
        manifest["artifacts"]["unrelated"] = "x"
        manifest["database_identity"]["sqlite_version"] = "bogus"
        (setdir / "backup_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
        rehash(setdir)
        ok, _ = backup_format.verify_set(setdir, expected_application_id=APP)
        record("M2 nested artifact key + sqlite_version accepted", ok, f"verify={ok}")


# ================================================================ M3 directories
def m3_directories():
    for label, drop_root, extra_dir in (("missing-root", True, False),
                                        ("extra-dir", False, True)):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            db = p1_with_jobs(tmp / "db.db", [(1, "C1", "photo.jpg", "paid", "tok1")],
                              [file_job()])
            setdir = build_set(db, tmp / f"set-{label}", {"photo.jpg": b"correct recovery original"})
            archive = setdir / "uploads.tar.gz"
            # rebuild archive with/without the root dir / with an extra dir
            with tarfile.open(archive, "r:gz") as tar:
                members = [(m, tar.extractfile(m).read() if m.isreg() else None)
                           for m in tar.getmembers()]
            with tarfile.open(archive, "w:gz") as tar:
                for member, data in members:
                    if drop_root and member.isdir() and member.name == "uploads":
                        continue
                    tar.addfile(member, io.BytesIO(data) if data is not None else None)
                if extra_dir:
                    d = tarfile.TarInfo("uploads/extra"); d.type = tarfile.DIRTYPE
                    tar.addfile(d)
            # Rebuild the manifest so the member count matches the tampered archive;
            # otherwise the count mismatch masks the directory-set defect.
            info, _ = backup_format.inspect_archive(archive)
            conn = db_evidence.open_readonly(setdir / "orders.db")
            try:
                schema = db_evidence.canonical_schema(conn)
                logical = db_evidence.logical_state_digest(conn)
            finally:
                conn.close()
            manifest = backup_format.build_manifest(
                artifacts={"orders.db": backup_format.sha256_file(setdir / "orders.db"),
                           "uploads.tar.gz": backup_format.sha256_file(archive)},
                created_at=NOW, application_id=APP, schema_digest=schema["digest"],
                logical_digest=logical,
                database_identity={"application_id": APP, "schema_digest": schema["digest"],
                                   "logical_digest": logical},
                upload_members=info["members"], upload_bytes=info["bytes"])
            backup_format.write_manifest_files(setdir, manifest)
            ok, _ = backup_format.verify_set(setdir, expected_application_id=APP)
            record(f"M3 archive directories {label} accepted", ok, f"verify={ok}")


# ================================================================ M4 EPERM fsync
def m4_eperm():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        db = p1_with_jobs(tmp / "db.db", [(1, "C1", "photo.jpg", "paid", "tok1")],
                          [file_job()])
        setdir = build_set(db, tmp / "set", {"photo.jpg": b"correct recovery original"})
        staging = tmp / "staging"
        shutil.copytree(setdir, staging)
        import errno
        import os as _os

        original_fsync = _os.fsync

        def eperm_fsync(fd):
            raise OSError(errno.EPERM, "Operation not permitted")

        _os.fsync = eperm_fsync
        try:
            try:
                backup_format.publish_set(staging, tmp / "published")
                published = (tmp / "published").is_dir()
            except Exception:  # noqa: BLE001
                published = False
        finally:
            _os.fsync = original_fsync
        record("M4 EPERM fsync still publishes", published, f"published={published}")


CASES = (h1_missing_jobs, h2_wrong_root, h3_blank_p0, m1_email_state, m2_nested,
         m3_directories, m4_eperm)


def main():
    for case in CASES:
        try:
            case()
        except Exception as exc:  # noqa: BLE001
            record(case.__name__, True, f"raised {type(exc).__name__}: {exc}")
    bad = sum(1 for _, defect, _ in results if defect)
    print(f"\nsummary: {bad}/{len(results)} cases show the defect")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
