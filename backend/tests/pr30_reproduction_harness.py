"""PR #30 reproduction harness (not collected by pytest).

Run against a checkout to see, case by case, whether the implementation still
returns a false PASS for each finding of the independent Stage 1 review.

    python backend/tests/pr30_reproduction_harness.py [repo-root]

Expected output on the reviewed head ``048ae7a``::

    summary: 10/10 cases still show a false pass

Expected output after the fixes::

    summary: 0/10 cases still show a false pass

The permanent, asserted form of these cases is ``test_pr30_regressions.py``; this
script exists so a reviewer can re-run the original counterexamples directly.
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
sys.path.insert(0, str(ROOT / "backend" / "tests"))

import backup_format  # noqa: E402
import db_evidence  # noqa: E402
import evidence_gate  # noqa: E402

APP = 1346650441
NOW = datetime.now(timezone.utc)
results = []


def record(name, passed_falsely, detail=""):
    results.append((name, passed_falsely, detail))
    verdict = "FALSE PASS (bad)" if passed_falsely else "refused (good)"
    print(f"{name:34} {verdict} {detail}")


def new_db(path, script, rows=()):
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA application_id = {APP}")
    conn.executescript(script)
    for sql in rows:
        conn.execute(sql)
    conn.commit()
    conn.close()
    return path


def make_set(db_path, dest, tar_members=None):
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    dst = sqlite3.connect(dest / "orders.db")
    conn.backup(dst)
    dst.close()
    conn.close()
    archive = dest / "uploads.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        info = tarfile.TarInfo("uploads")
        info.type = tarfile.DIRTYPE
        tar.addfile(info)
        for name, data in (tar_members if tar_members is not None
                           else {"photo.jpg": b"correct recovery original"}).items():
            member = tarfile.TarInfo(f"uploads/{name}")
            member.size = len(data)
            tar.addfile(member, io.BytesIO(data))
    conn = db_evidence.open_readonly(dest / "orders.db")
    try:
        schema = db_evidence.canonical_schema(conn)
        logical = db_evidence.logical_state_digest(conn)
    finally:
        conn.close()
    archive_info, _ = backup_format.inspect_archive(archive)
    manifest = backup_format.build_manifest(
        artifacts={"orders.db": backup_format.sha256_file(dest / "orders.db"),
                   "uploads.tar.gz": backup_format.sha256_file(archive)},
        created_at=NOW, application_id=APP,
        schema_digest=schema["digest"], logical_digest=logical,
        database_identity={"application_id": APP},
        upload_members=archive_info["members"], upload_bytes=archive_info["bytes"])
    backup_format.write_manifest_files(dest, manifest)
    return dest


def h1():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        a = new_db(tmp / "a.db", "CREATE TABLE t(id INTEGER PRIMARY KEY DESC, payload TEXT);",
                   ["INSERT INTO t(rowid,id,payload) VALUES (1,NULL,'same')"])
        b = new_db(tmp / "b.db", "CREATE TABLE t(id INTEGER PRIMARY KEY DESC, payload TEXT);",
                   ["INSERT INTO t(rowid,id,payload) VALUES (2,NULL,'same')"])
        res = db_evidence.compare_databases(a, b, expected_application_id=APP)
        record("H1 INTEGER PK DESC rowid", res.ok and bool(res.tables),
               f"ok={res.ok} tables={list(res.tables)}")


def h2():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        schema = "CREATE TABLE t1(v TEXT); CREATE TABLE t2(v TEXT);"
        live = new_db(tmp / "live.db", schema,
                      ["INSERT INTO t1 VALUES ('A')", "INSERT INTO t2 VALUES ('B')"])
        backup = new_db(tmp / "backup.db", schema,
                        ["INSERT INTO t1 VALUES ('A')", "INSERT INTO t2 VALUES ('D')"])
        writer = sqlite3.connect(live)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.close()

        original = db_evidence._compare_table
        state = {"committed": False}

        def hooked(*args, **kwargs):
            out = original(*args, **kwargs)
            if args[2] == "t1" and not state["committed"]:
                other = sqlite3.connect(live)
                other.executescript("BEGIN; UPDATE t1 SET v='C'; UPDATE t2 SET v='D'; COMMIT;")
                other.close()
                state["committed"] = True
            return out

        db_evidence._compare_table = hooked
        try:
            res = db_evidence.compare_databases(live, backup, expected_application_id=APP)
        finally:
            db_evidence._compare_table = original
        record("H2 mixed-time snapshot", res.ok,
               "ok=%s writer_committed=%s tables=%s"
               % (res.ok, state["committed"], list(res.tables)))


def h3():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        good = b"correct recovery original"
        db = new_db(
            tmp / "live.db",
            """CREATE TABLE orders(id INTEGER PRIMARY KEY, payment_status TEXT);
               CREATE TABLE delivery_jobs(
                   id TEXT PRIMARY KEY, order_id INTEGER, kind TEXT, state TEXT,
                   local_path TEXT, content_sha256 TEXT, content_size INTEGER,
                   cleanup_state TEXT, cleanup_phase TEXT, local_deleted_at TEXT);""",
            ["INSERT INTO orders VALUES (1,'paid')",
             "INSERT INTO delivery_jobs VALUES ('job1',1,'file','pending',"
             f"'/app/uploads/photo.jpg','{hashlib.sha256(good).hexdigest()}',{len(good)},"
             "'pending',NULL,NULL)"])
        bad = b"wrong content same-size!!"
        assert len(bad) == len(good)
        path = make_set(db, tmp / "set", tar_members={"photo.jpg": bad})
        ok, _ = backup_format.verify_set(path, expected_application_id=APP)
        code, _ = evidence_gate.evaluate(db, path, expected_application_id=APP,
                                         max_age_hours=48)
        record("H3 substituted upload bytes", bool(ok and code == 0),
               f"verify={ok} gate_exit={code}")


def h4():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        a = new_db(tmp / "a.db", "CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT);",
                   ["INSERT INTO t VALUES (1,'a')"])
        b = new_db(tmp / "b.db", "CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT);",
                   ["INSERT INTO t VALUES (1,'DIFFERENT')"])
        original = db_evidence.table_inventory
        db_evidence.table_inventory = lambda conn: {}
        try:
            res = db_evidence.compare_databases(a, b, expected_application_id=APP)
        except Exception:  # noqa: BLE001
            record("H4 table_list returns nothing", False, "raised instead of returning")
            return
        finally:
            db_evidence.table_inventory = original
        record("H4 table_list returns nothing", res.ok, f"ok={res.ok}")


def m1():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        db = new_db(tmp / "live.db", "CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT);",
                    ["INSERT INTO t VALUES (1,'a')"])
        for suffix, mutate in (
            ("format", lambda m: m.update({"evidence_format": 999})),
            ("identity", lambda m: m["database_identity"].update({"schema_digest": "f" * 64})),
            ("uploads", lambda m: m["uploads"].update({"uncompressed_bytes": 0})),
        ):
            path = make_set(db, tmp / f"set-{suffix}")
            manifest = json.loads((path / "backup_manifest.json").read_text())
            mutate(manifest)
            (path / "backup_manifest.json").write_text(
                json.dumps(manifest, indent=2, sort_keys=True))
            lines = [f"{backup_format.sha256_file(path / n)}  {n}"
                     for n in backup_format.CHECKSUMMED]
            (path / "manifest.sha256").write_text("\n".join(lines) + "\n")
            ok, _ = backup_format.verify_set(path, expected_application_id=APP)
            code, _ = evidence_gate.evaluate(db, path, expected_application_id=APP,
                                             max_age_hours=48)
            record(f"M1 {suffix} field accepted", bool(ok and code == 0),
                   f"verify={ok} gate={code}")


def m2():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        db = new_db(tmp / "live.db", "CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT);",
                    ["INSERT INTO t VALUES (1,'a')"])
        path = make_set(db, tmp / "set", tar_members={"photo.jpg": b"x"})
        archive = path / "uploads.tar.gz"
        with tarfile.open(archive, "r:gz") as tar:
            existing = [(m, tar.extractfile(m).read() if m.isreg() else None)
                        for m in tar.getmembers()]
        with tarfile.open(archive, "w:gz") as tar:
            for member, data in existing:
                tar.addfile(member, io.BytesIO(data) if data is not None else None)
            member = tarfile.TarInfo("evil")
            member.size = 1
            tar.addfile(member, io.BytesIO(b"x"))
        _, errors = backup_format.inspect_archive(archive)
        record("M2 member outside uploads/", not errors, f"errors={errors}")


def m4():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        final = tmp / "backups" / "20260101T000000Z"
        db = tmp / "src.db"
        new_db(db, "CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT);",
               ["INSERT INTO t VALUES (1,'a')"])
        staged = []
        for index in (1, 2):
            built = make_set(db, tmp / f"built{index}")
            staging = tmp / f"stage{index}"
            shutil.copytree(built, staging)
            staged.append(staging)
        backup_format.publish_set(staged[0], final)
        reused = True
        try:
            backup_format.publish_set(staged[1], final)
        except Exception:  # noqa: BLE001
            reused = False
        record("M4 same ID reused for a new set", reused, f"overwrite_allowed={reused}")


def m5():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        ok_db = new_db(tmp / "ok.db", "CREATE TABLE t(id INTEGER PRIMARY KEY);")
        junk = tmp / "junk.db"
        junk.write_bytes(b"not a sqlite database")
        try:
            res = db_evidence.compare_databases(ok_db, junk, expected_application_id=APP)
        except Exception as exc:  # noqa: BLE001
            record("M5 malformed database", True,
                   f"raised {type(exc).__name__} instead of a structured refusal")
            return
        record("M5 malformed database", res.ok,
               f"ok={res.ok} reasons={len(res.reasons)}")


def main():
    for fn in (h1, h2, h3, h4, m1, m2, m4, m5):
        try:
            fn()
        except Exception:  # noqa: BLE001
            print(f"{fn.__name__} raised:")
            traceback.print_exc()
    bad = sum(1 for _, false_pass, _ in results if false_pass)
    print(f"\nsummary: {bad}/{len(results)} cases still show a false pass")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
