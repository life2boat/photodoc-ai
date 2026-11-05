"""Dedicated regressions for the PR #30 *third* review (H1-H3, M1-M4).

Each test maps one-for-one onto a finding; the third reproduction harness
(`pr30_third_reproduction_harness.py`) is the runnable before/after control.
"""

from __future__ import annotations

import errno
import hashlib
import io
import json
import os
import shutil
import sqlite3
import tarfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from evidence_fixtures import APPLICATION_ID, NOW, build_delivery_database

import backup_format
import build_v2_set
import db_evidence
import evidence_gate
import recovery_artifacts

GOOD = b"correct recovery original"

P1_SCHEMA = """
CREATE TABLE orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT, customer TEXT, filename TEXT,
    payment_status TEXT, order_token_hash TEXT
);
CREATE TABLE delivery_jobs (
    id TEXT PRIMARY KEY, order_id INTEGER NOT NULL, kind TEXT NOT NULL,
    local_path TEXT, original_name TEXT, content_sha256 TEXT, content_size INTEGER,
    state TEXT NOT NULL DEFAULT 'pending', cleanup_state TEXT NOT NULL DEFAULT 'pending',
    cleanup_phase TEXT NOT NULL DEFAULT 'none', local_deleted_at TEXT, delivered_at TEXT
);
"""


def p1(path: Path, orders, jobs):
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA application_id = {APPLICATION_ID}")
    conn.executescript(P1_SCHEMA)
    for order in orders:
        conn.execute("INSERT INTO orders (id, customer, filename, payment_status, "
                     "order_token_hash) VALUES (?,?,?,?,?)", order)
    for job in jobs:
        conn.execute(f"INSERT INTO delivery_jobs ({', '.join(job)}) "
                     f"VALUES ({', '.join('?' for _ in job)})", tuple(job.values()))
    conn.commit()
    conn.close()
    return path


def file_job(oid=1, name="/app/uploads/1_photo.jpg", data=GOOD, state="pending"):
    stored = Path(name).name
    original = stored.split("_", 1)[1] if "_" in stored else stored
    return {"id": f"f{oid}", "order_id": oid, "kind": "file", "local_path": name,
            "original_name": original,
            "content_sha256": hashlib.sha256(data).hexdigest(), "content_size": len(data),
            "state": state, "cleanup_state": "pending", "cleanup_phase": "none",
            "local_deleted_at": None, "delivered_at": None}


def info_job(oid=1):
    return {"id": f"info{oid}", "order_id": oid, "kind": "info",
            "local_path": f"/app/uploads/{oid}_info.txt", "original_name": "info.txt",
            "content_sha256": hashlib.sha256(b"info").hexdigest(), "content_size": 4,
            "state": "delivered", "cleanup_state": "completed", "cleanup_phase": "deleted",
            "local_deleted_at": "2026-01-01T00:10:00+00:00",
            "delivered_at": "2026-01-01T00:05:00+00:00"}


def complete_order(oid, name):
    return ([(oid, f"C{oid}", name, "paid", f"tok{oid}")],
            [file_job(oid, f"/app/uploads/{oid}_{name}"), info_job(oid)])


# ================================================================ H1: missing jobs
def test_missing_jobs_refuse_producer_and_verifier(tmp_path):
    orders = [(1, "C1", "photo.jpg", "paid", "tok1")]
    jobs = [file_job(1, "/app/uploads/1_photo.jpg"), info_job(1)]
    db = p1(tmp_path / "db.db", orders, jobs)
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    (uploads / "1_photo.jpg").write_bytes(GOOD)

    # remove the file job (the original is now unaddressable)
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM delivery_jobs WHERE kind='file'")
    conn.commit()
    conn.close()

    out = tmp_path / "out"
    assert build_v2_set.main(["--live-db", str(db), "--uploads", str(uploads),
                              "--output", str(out), "--restore-uploads-root", "/app/uploads"]) == build_v2_set.EXIT_FAILED
    assert not out.exists()

    required, _groups, errors, _stats = recovery_artifacts.required_sources(db)
    assert any("file job" in e for e in errors)


def test_endpoint_generated_order_with_missing_jobs_is_refused(tmp_path):
    from backup_set_builder import build_database

    root = tmp_path / "real"
    build_database(root / "orders.db", root / "uploads")
    conn = sqlite3.connect(root / "orders.db")
    order_id = conn.execute("SELECT id FROM orders WHERE payment_status!='paid' "
                            "LIMIT 1").fetchone()[0]
    conn.execute("DELETE FROM delivery_jobs WHERE order_id=?", (order_id,))
    conn.commit()
    conn.close()

    out = tmp_path / "out"
    assert build_v2_set.main(["--live-db", str(root / "orders.db"),
                              "--uploads", str(root / "uploads"),
                              "--output", str(out), "--restore-uploads-root", "/app/uploads"]) == build_v2_set.EXIT_FAILED


# ================================================================ H2: root binding
def test_multiple_recorded_roots_are_refused(tmp_path):
    orders = [(1, "C1", "a.jpg, b.jpg", "paid", "tok1")]
    a = file_job(1, "/app/uploads/a.jpg")
    b = file_job(1, "/other/uploads/b.jpg")
    b["id"] = "f1b"
    jobs = [a, b, info_job(1)]
    db = p1(tmp_path / "db.db", orders, jobs)
    _required, _groups, errors, stats = recovery_artifacts.required_sources(db)
    assert any("different uploads roots" in e for e in errors)


def test_restore_root_is_recorded_and_bound(tmp_path):
    orders, jobs = complete_order(1, "photo.jpg")
    db = p1(tmp_path / "db.db", orders, jobs)
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    (uploads / "1_photo.jpg").write_bytes(GOOD)

    out = tmp_path / "out"
    assert build_v2_set.main(["--live-db", str(db), "--uploads", str(uploads),
                              "--output", str(out), "--restore-uploads-root", "/app/uploads"]) == build_v2_set.EXIT_OK

    manifest = json.loads((out / "backup_manifest.json").read_text())
    root = manifest["restore_uploads_root"]
    assert root and Path(root.replace("\\", "/")).name == "uploads"

    # Rehearsal: extract to a chosen root and confirm the recorded path is exact.
    required, _groups, errors, stats = recovery_artifacts.required_sources(out / "orders.db")
    assert not errors
    assert stats["uploads_root"] == root
    chosen = tmp_path / "restore" / "uploads"
    chosen.mkdir(parents=True)
    with tarfile.open(out / "uploads.tar.gz", "r:gz") as tar:
        for member in tar.getmembers():
            if member.isfile():
                assert member.name.startswith("uploads/")
                target = chosen / member.name.split("/", 1)[1]
                with tar.extractfile(member) as handle:
                    target.write_bytes(handle.read())
    for member, spec in required.items():
        restored = chosen / Path(member).name
        assert restored.is_file()
        assert hashlib.sha256(restored.read_bytes()).hexdigest() == spec["sha256"]


# ============================================================ H3: blank P0 name
def test_p0_blank_filename_is_refused(tmp_path):
    conn = sqlite3.connect(tmp_path / "p0.db")
    conn.execute(f"PRAGMA application_id = {APPLICATION_ID}")
    conn.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, filename TEXT, "
                 "payment_status TEXT)")
    conn.execute("INSERT INTO orders VALUES (1,'','paid')")
    conn.commit()
    conn.close()
    _required, _groups, errors, _stats = recovery_artifacts.required_sources(
        tmp_path / "p0.db", legacy_p0_policy="include")
    assert any("empty or unparseable filename" in e for e in errors)


def test_p0_ambiguous_candidate_reuse_is_refused(tmp_path):
    conn = sqlite3.connect(tmp_path / "p0.db")
    conn.execute(f"PRAGMA application_id = {APPLICATION_ID}")
    conn.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, filename TEXT, "
                 "payment_status TEXT)")
    # two orders claiming the same stored layout
    conn.execute("INSERT INTO orders VALUES (1,'photo.jpg','paid')")
    conn.execute("INSERT INTO orders VALUES (2,'photo.jpg','paid')")
    conn.commit()
    conn.close()
    _required, _groups, errors, _stats = recovery_artifacts.required_sources(
        tmp_path / "p0.db", legacy_p0_policy="include")
    assert any("ambiguous" in e for e in errors)


# ========================================================== M1: email unknown state
def test_email_job_unknown_state_is_refused(tmp_path):
    orders, jobs = complete_order(1, "photo.jpg")
    jobs.append({"id": "e1", "order_id": 1, "kind": "admin_email", "local_path": None,
                 "content_sha256": None, "content_size": None, "state": "alien",
                 "cleanup_state": "not_applicable", "cleanup_phase": "none",
                 "local_deleted_at": None, "delivered_at": None})
    db = p1(tmp_path / "db.db", orders, jobs)
    _required, _groups, errors, _stats = recovery_artifacts.required_sources(db)
    assert any("unrecognised state 'alien'" in e for e in errors)


# ============================================================= M2: manifest fields
def test_unknown_artifact_key_is_refused(tmp_path):
    db = p1(tmp_path / "db.db", *complete_order(1, "photo.jpg"))
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    (uploads / "1_photo.jpg").write_bytes(GOOD)
    out = tmp_path / "out"
    assert build_v2_set.main(["--live-db", str(db), "--uploads", str(uploads),
                              "--output", str(out), "--restore-uploads-root", "/app/uploads"]) == 0
    manifest = json.loads((out / "backup_manifest.json").read_text())
    manifest["artifacts"]["unrelated"] = "x"
    (out / "backup_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
    lines = [f"{backup_format.sha256_file(out / n)}  {n}"
             for n in backup_format.CHECKSUMMED]
    (out / "manifest.sha256").write_text("\n".join(lines) + "\n")
    ok, report = backup_format.verify_set(out, expected_application_id=APPLICATION_ID)
    assert not ok
    assert any("unknown fields" in e for e in report["errors"])


def test_malformed_sqlite_version_is_refused(tmp_path):
    db = p1(tmp_path / "db.db", *complete_order(1, "photo.jpg"))
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    (uploads / "1_photo.jpg").write_bytes(GOOD)
    out = tmp_path / "out"
    assert build_v2_set.main(["--live-db", str(db), "--uploads", str(uploads),
                              "--output", str(out), "--restore-uploads-root", "/app/uploads"]) == 0
    manifest = json.loads((out / "backup_manifest.json").read_text())
    manifest["database_identity"]["sqlite_version"] = "bogus"
    (out / "backup_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
    lines = [f"{backup_format.sha256_file(out / n)}  {n}"
             for n in backup_format.CHECKSUMMED]
    (out / "manifest.sha256").write_text("\n".join(lines) + "\n")
    ok, report = backup_format.verify_set(out, expected_application_id=APPLICATION_ID)
    assert not ok
    assert any("sqlite_version is malformed" in e for e in report["errors"])


# ========================================================== M3: directory entries
def test_missing_uploads_directory_is_refused(tmp_path):
    archive = tmp_path / "a.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        member = tarfile.TarInfo("uploads/1_photo.jpg")
        member.size = 3
        tar.addfile(member, io.BytesIO(b"abc"))
    _members, info, errors = recovery_artifacts.scan_archive(archive)
    assert any("missing its uploads/ root directory" in e for e in errors)


def test_extra_directory_is_refused(tmp_path):
    archive = tmp_path / "a.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        root = tarfile.TarInfo("uploads")
        root.type = tarfile.DIRTYPE
        tar.addfile(root)
        member = tarfile.TarInfo("uploads/1_photo.jpg")
        member.size = 3
        tar.addfile(member, io.BytesIO(b"abc"))
        extra = tarfile.TarInfo("uploads/extra")
        extra.type = tarfile.DIRTYPE
        tar.addfile(extra)
    _members, info, errors = recovery_artifacts.scan_archive(archive)
    assert any("unexpected directory entries" in e for e in errors)


# =========================================================== M4: EPERM fsync
def test_eperm_fsync_aborts_publication(tmp_path, monkeypatch):
    db = p1(tmp_path / "db.db", *complete_order(1, "photo.jpg"))
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    (uploads / "1_photo.jpg").write_bytes(GOOD)
    out = tmp_path / "out"
    assert build_v2_set.main(["--live-db", str(db), "--uploads", str(uploads),
                              "--output", str(out), "--restore-uploads-root", "/app/uploads"]) == 0
    staging = tmp_path / "staging"
    shutil.copytree(out, staging)

    def eperm(fd):
        raise OSError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(os, "fsync", eperm)
    with pytest.raises(backup_format.FormatError):
        backup_format.publish_set(staging, tmp_path / "published")
    assert not (tmp_path / "published").exists()
    assert staging.is_dir()
