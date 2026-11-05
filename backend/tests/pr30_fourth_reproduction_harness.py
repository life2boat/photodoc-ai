"""PR #30 *fourth* review reproduction harness (not collected by pytest).

    python backend/tests/pr30_fourth_reproduction_harness.py [repo-root]

Expected on `5057a38`: every case shows the defect. Expected after the fixes: ``0/N``.
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


def copy_db(source: Path, target: Path) -> Path:
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(source)
    dst = sqlite3.connect(target)
    src.backup(dst)
    dst.close()
    src.close()
    return target


def run_producer(db: Path, uploads: Path, out: Path, root: Path | None = None) -> int:
    args = ["--live-db", str(db), "--uploads", str(uploads), "--output", str(out)]
    if root is not None:
        args += ["--restore-uploads-root", str(root)]
    return build_v2_set.main(args)


def verify(out: Path, db: Path, *, policy: str = "fail", root: Path | None = None):
    ok, report = backup_format.verify_set(
        out, expected_application_id=APP, legacy_p0_policy=policy,
        expected_uploads_root=str(root) if root is not None else None)
    code, _gate = evidence_gate.evaluate(
        db, out, expected_application_id=APP, max_age_hours=48,
        legacy_p0_policy=policy,
        expected_uploads_root=str(root) if root is not None else None)
    return bool(ok), code


def endpoint_fixture(root: Path):
    from backup_set_builder import build_database

    root.mkdir(parents=True, exist_ok=True)
    info = build_database(root / "orders.db", root / "uploads")
    return root / "orders.db", root / "uploads", info


# ================================================= H1: same-count wrong file job
def h1(fixture):
    src_db, uploads, info = fixture
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        db = copy_db(src_db, tmp / "db.db")
        conn = sqlite3.connect(db)
        unpaid = info["unpaid"][0]
        # order 1's real file job
        donor = conn.execute(
            "SELECT local_path, content_sha256, content_size FROM delivery_jobs "
            "WHERE order_id=? AND kind='file' LIMIT 1", (info["paid"][0],)).fetchone()
        conn.execute(
            "UPDATE delivery_jobs SET local_path=?, content_sha256=?, content_size=? "
            "WHERE order_id=? AND kind='file'", (*donor, unpaid))
        conn.commit()
        dropped = conn.execute("SELECT local_path FROM delivery_jobs WHERE order_id=? "
                               "AND kind='file'", (unpaid,)).fetchall()
        conn.close()
        out = tmp / "out"
        code = run_producer(db, uploads, out, root=uploads)
        ok, gate = verify(out, db, root=uploads) if code == 0 else (False, None)
        archived = 0
        if code == 0:
            with tarfile.open(out / "uploads.tar.gz") as tar:
                archived = sum(1 for m in tar.getmembers() if m.isfile())
        record("H1 same-count wrong file job", bool(code == 0 and ok and gate == 0),
               f"producer={code} verify={ok} gate={gate} archived={archived} target={dropped}")


# ================================================= H2: self-asserted wrong root
def h2(fixture):
    src_db, uploads, info = fixture
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        db = copy_db(src_db, tmp / "db.db")
        alt = tmp / "elsewhere" / "uploads"      # never created
        conn = sqlite3.connect(db)
        rows = conn.execute("SELECT id, local_path FROM delivery_jobs "
                            "WHERE local_path IS NOT NULL").fetchall()
        for job_id, local_path in rows:
            conn.execute("UPDATE delivery_jobs SET local_path=? WHERE id=?",
                         (str(alt / Path(local_path).name), job_id))
        conn.commit(); conn.close()
        out = tmp / "out"
        code = run_producer(db, uploads, out, root=uploads)
        ok, gate = verify(out, db, root=uploads) if code == 0 else (False, None)
        record("H2 wrong self-consistent root", bool(code == 0 and ok and gate == 0),
               f"producer={code} verify={ok} gate={gate} alt_exists={alt.exists()}")


# ============================================== M1: missing email jobs
def m1_admin(fixture):
    src_db, uploads, info = fixture
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        db = copy_db(src_db, tmp / "db.db")
        conn = sqlite3.connect(db)
        conn.execute("DELETE FROM delivery_jobs WHERE order_id=? AND kind='admin_email'",
                     (info["unpaid"][0],))
        conn.commit(); conn.close()
        out = tmp / "out"
        code = run_producer(db, uploads, out, root=uploads)
        ok, gate = verify(out, db, root=uploads) if code == 0 else (False, None)
        record("M1a missing admin_email job", bool(code == 0 and ok and gate == 0),
               f"producer={code} verify={ok} gate={gate}")


def m1_paid(fixture):
    src_db, uploads, info = fixture
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        db = copy_db(src_db, tmp / "db.db")
        conn = sqlite3.connect(db)
        conn.execute("DELETE FROM delivery_jobs WHERE order_id=? AND kind='paid_email'",
                     (info["paid"][0],))
        conn.commit(); conn.close()
        out = tmp / "out"
        code = run_producer(db, uploads, out, root=uploads)
        ok, gate = verify(out, db, root=uploads) if code == 0 else (False, None)
        record("M1b missing paid_email job", bool(code == 0 and ok and gate == 0),
               f"producer={code} verify={ok} gate={gate}")


# ============================================== M2: unknown schema version/table
def m2(fixture):
    src_db, uploads, info = fixture
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        db = copy_db(src_db, tmp / "db.db")
        extra = b"additional original bytes"
        (uploads / "999_extra.jpg").write_bytes(extra)
        conn = sqlite3.connect(db)
        conn.execute("PRAGMA user_version=99")
        conn.execute("CREATE TABLE additional_originals (path TEXT, sha256 TEXT, size INTEGER)")
        conn.execute("INSERT INTO additional_originals VALUES (?,?,?)",
                     ("/app/uploads/999_extra.jpg", hashlib.sha256(extra).hexdigest(), len(extra)))
        conn.commit(); conn.close()
        out = tmp / "out"
        code = run_producer(db, uploads, out)
        ok, gate = verify(out, db, root=uploads) if code == 0 else (False, None)
        record("M2 unknown user_version + table", bool(code == 0 and ok and gate == 0),
               f"producer={code} verify={ok} gate={gate}")


# ============================================== M3: malformed filename type
def m3():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        db = tmp / "p0.db"
        conn = sqlite3.connect(db)
        conn.execute(f"PRAGMA application_id = {APP}")
        conn.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, filename INTEGER, "
                     "payment_status TEXT)")
        conn.execute("INSERT INTO orders VALUES (1, 12345, 'paid')")
        conn.commit(); conn.close()
        try:
            recovery_artifacts.required_sources(db, legacy_p0_policy="include")
            structured = True
            detail = "returned without raising"
        except Exception as exc:  # noqa: BLE001
            structured = False
            detail = f"raised {type(exc).__name__}"
        record("M3 INTEGER filename raises", not structured, detail)


# ============================================== M4: bogus root on an empty set
def m4():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        db = tmp / "empty.db"
        conn = sqlite3.connect(db)
        conn.execute(f"PRAGMA application_id = {APP}")
        conn.executescript(
            """
            CREATE TABLE orders (id INTEGER PRIMARY KEY, filename TEXT,
                                 payment_status TEXT, order_token_hash TEXT);
            CREATE TABLE delivery_jobs (id TEXT PRIMARY KEY, order_id INTEGER, kind TEXT,
                local_path TEXT, content_sha256 TEXT, content_size INTEGER, state TEXT,
                cleanup_state TEXT, cleanup_phase TEXT, local_deleted_at TEXT,
                delivered_at TEXT);
            """)
        conn.commit(); conn.close()
        uploads = tmp / "uploads"
        uploads.mkdir()
        out = tmp / "out"
        if run_producer(db, uploads, out) != 0:
            record("M4 bogus root on empty set", False, "producer refused")
            return
        manifest = json.loads((out / "backup_manifest.json").read_text())
        manifest["restore_uploads_root"] = "attacker-controlled, not a path"
        (out / "backup_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
        lines = [f"{backup_format.sha256_file(out / n)}  {n}"
                 for n in backup_format.CHECKSUMMED]
        (out / "manifest.sha256").write_text("\n".join(lines) + "\n")
        ok, _report = backup_format.verify_set(out, expected_application_id=APP)
        record("M4 bogus root on empty set", ok, f"verify={ok}")


def main():
    with tempfile.TemporaryDirectory() as tmp:
        fixture = endpoint_fixture(Path(tmp) / "real")
        for case in (h1, h2, m1_admin, m1_paid, m2):
            try:
                case(fixture)
            except Exception as exc:  # noqa: BLE001
                record(case.__name__, True, f"raised {type(exc).__name__}: {exc}")
        for case in (m3, m4):
            try:
                case()
            except Exception as exc:  # noqa: BLE001
                record(case.__name__, True, f"raised {type(exc).__name__}: {exc}")
    bad = sum(1 for _, defect, _ in results if defect)
    print(f"\nsummary: {bad}/{len(results)} cases show the defect")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
