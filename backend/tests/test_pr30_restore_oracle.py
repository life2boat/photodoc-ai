"""Independent restore oracle (test-only).

This validator deliberately does **not** import or reuse
``recovery_artifacts.required_sources``: it re-derives the required originals from the
restored database with its own SQL, then checks each file at the exact path the
application will use. If producer and verifier shared a wrong selector, this oracle
would disagree with them.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tarfile
from pathlib import Path

import pytest

from evidence_fixtures import APPLICATION_ID

import backup_format
import build_v2_set
import db_evidence


def _sanitize(name: str) -> str:
    """The application's own rule (backend.main.sanitize_upload_filename)."""
    base = (name or "upload").replace("\\", "/").split("/")[-1]
    cleaned = "".join(c for c in base if c.isalnum() or c in "._- ")
    return cleaned.strip() or "upload"


def _norm(value: str) -> str:
    return str(value).replace("\\", "/").rstrip("/")


class OracleError(AssertionError):
    """The restored set cannot satisfy the application's own invariants."""


def oracle_required_originals(db_path: Path, *, uploads_root: str) -> dict[str, tuple[str, int]]:
    """Derive required originals from the restored DB, independently of the library.

    ``uploads_root`` is the path the **application** will use (the manifest's
    ``restore_uploads_root``), not the directory the test extracted into; those are
    different concepts and conflating them would hide exactly the root-binding defect.
    """
    conn = sqlite3.connect(f"file:{Path(db_path).resolve().as_posix()}?mode=ro", uri=True)
    try:
        orders = conn.execute("SELECT id, filename FROM orders ORDER BY id").fetchall()
        jobs = conn.execute(
            "SELECT order_id, kind, state, local_path, original_name, content_sha256, "
            "content_size, cleanup_state, cleanup_phase, local_deleted_at, delivered_at "
            "FROM delivery_jobs").fetchall()
    finally:
        conn.close()

    required: dict[str, tuple[str, int]] = {}
    for order_id, filename in orders:
        names = [n.strip() for n in (filename or "").split(",") if n.strip()]
        file_jobs = [j for j in jobs if j[0] == order_id and j[1] == "file"]
        info_jobs = [j for j in jobs if j[0] == order_id and j[1] == "info"]
        if len(info_jobs) != 1:
            raise OracleError(f"order {order_id} has {len(info_jobs)} info jobs")
        if len(file_jobs) != len(names):
            raise OracleError(
                f"order {order_id} has {len(file_jobs)} file jobs for {len(names)} names")
        originals = sorted(_sanitize(j[4] or "") for j in file_jobs)
        if originals != sorted(names):
            raise OracleError(
                f"order {order_id} file jobs describe {originals}, order says {sorted(names)}")

        for job in file_jobs + info_jobs:
            order_id_of_job, kind, state, local_path, _orig, sha, size, cleanup_state, \
                cleanup_phase, deleted_at, _delivered = job
            basename = Path((local_path or "").replace("\\", "/")).name
            if not basename.startswith(f"{order_id_of_job}_"):
                raise OracleError(
                    f"job {kind} for order {order_id_of_job} points at {basename!r}, which "
                    "does not belong to that order")
            recorded_root = _norm(str(Path(_norm(local_path)).parent))
            if recorded_root != _norm(uploads_root):
                raise OracleError(
                    f"job {kind} for order {order_id_of_job} is recorded under a different "
                    f"root than the application's {uploads_root!r}")
            if state in ("pending", "processing", "failed"):
                if not sha or not isinstance(size, int):
                    raise OracleError(f"required job {kind} has no hash/size")
                required[basename] = (sha, size)
            elif cleanup_state == "completed":
                if cleanup_phase not in ("deleted", "deleting") or not deleted_at:
                    raise OracleError("delivered job claims a deletion without evidence")
    return required


def extract_set(set_dir: Path, restore_root: Path) -> Path:
    restore_root = Path(restore_root)
    restore_root.mkdir(parents=True, exist_ok=True)
    shutil.copy2(set_dir / "orders.db", restore_root.parent / "orders.db")
    with tarfile.open(set_dir / "uploads.tar.gz", "r:gz") as tar:
        for member in tar.getmembers():
            if member.isfile():
                target = restore_root / member.name.split("/", 1)[1]
                with tar.extractfile(member) as handle:
                    target.write_bytes(handle.read())
    return restore_root.parent / "orders.db"


def validate_restored(db_path: Path, extracted: Path, expected_source: dict[str, bytes],
                      app_root: str):
    """Every required original must be restorable to its exact application path."""
    required = oracle_required_originals(db_path, uploads_root=app_root)
    if not required:
        raise OracleError("the oracle found no required originals; the fixture is empty")
    for basename, (sha, size) in required.items():
        path = Path(extracted) / basename
        if not path.is_file():
            raise OracleError(f"required original missing after restore: {path}")
        data = path.read_bytes()
        if len(data) != size:
            raise OracleError(f"{path} has {len(data)} bytes, expected {size}")
        if hashlib.sha256(data).hexdigest() != sha:
            raise OracleError(f"{path} has the wrong content")
        if basename in expected_source and data != expected_source[basename]:
            raise OracleError(f"{path} differs from the source fixture")
    conn = sqlite3.connect(f"file:{Path(db_path).resolve().as_posix()}?mode=ro", uri=True)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA application_id").fetchone()[0] == APPLICATION_ID
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()
    return required


# ---------------------------------------------------------------- the oracle tests
@pytest.fixture(scope="module")
def endpoint(tmp_path_factory):
    from backup_set_builder import build_database

    root = tmp_path_factory.mktemp("oracle")
    info = build_database(root / "orders.db", root / "uploads")
    return root / "orders.db", root / "uploads", info


def test_produce_verify_restore_matches_an_independent_oracle(endpoint, tmp_path):
    db, uploads, info = endpoint
    out = tmp_path / "backups" / "oracle"
    assert build_v2_set.main(["--live-db", str(db), "--uploads", str(uploads),
                              "--output", str(out),
                              "--restore-uploads-root", str(uploads)]) == \
        build_v2_set.EXIT_OK

    ok, report = backup_format.verify_set(out, expected_application_id=APPLICATION_ID,
                                          expected_uploads_root=str(uploads))
    assert ok, report["errors"]

    manifest = json.loads((out / "backup_manifest.json").read_text())
    app_root = manifest["restore_uploads_root"]
    restore_root = tmp_path / "restore" / "uploads"
    restored_db = extract_set(out, restore_root)
    required = validate_restored(restored_db, restore_root, info["uploads"], app_root)

    # the oracle must agree with what the producer archived
    with tarfile.open(out / "uploads.tar.gz", "r:gz") as tar:
        archived = {m.name.split("/", 1)[1] for m in tar.getmembers() if m.isfile()}
    assert archived == set(required)
    assert len(required) == 8, f"expected eight originals, derived {sorted(required)}"


def test_the_oracle_rejects_a_same_count_substitution(tmp_path, endpoint):
    """The oracle must catch what the shared selector used to miss."""
    db, uploads, info = endpoint
    copy = tmp_path / "db.db"
    src = sqlite3.connect(db)
    dst = sqlite3.connect(copy)
    src.backup(dst)
    dst.close()
    src.close()
    conn = sqlite3.connect(copy)
    unpaid = info["unpaid"][0]
    donor = conn.execute(
        "SELECT local_path, content_sha256, content_size FROM delivery_jobs "
        "WHERE order_id=? AND kind='file' LIMIT 1", (info["paid"][0],)).fetchone()
    conn.execute("UPDATE delivery_jobs SET local_path=?, content_sha256=?, content_size=? "
                 "WHERE order_id=? AND kind='file'", (*donor, unpaid))
    conn.commit()
    conn.close()

    with pytest.raises(OracleError):
        oracle_required_originals(copy, uploads_root=str(uploads))

    out = tmp_path / "out"
    assert build_v2_set.main(["--live-db", str(copy), "--uploads", str(uploads),
                              "--output", str(out),
                              "--restore-uploads-root", str(uploads)]) == \
        build_v2_set.EXIT_FAILED


def test_the_oracle_rejects_an_incompatible_uploads_root(tmp_path, endpoint):
    """A set recorded under a different root must not be treated as restorable."""
    db, uploads, info = endpoint
    copy = tmp_path / "db.db"
    src = sqlite3.connect(db)
    dst = sqlite3.connect(copy)
    src.backup(dst)
    dst.close()
    src.close()
    alt = tmp_path / "elsewhere" / "uploads"
    conn = sqlite3.connect(copy)
    for job_id, local_path in conn.execute(
            "SELECT id, local_path FROM delivery_jobs WHERE local_path IS NOT NULL").fetchall():
        conn.execute("UPDATE delivery_jobs SET local_path=? WHERE id=?",
                     (str(alt / Path(local_path).name), job_id))
    conn.commit()
    conn.close()

    with pytest.raises(OracleError):
        oracle_required_originals(copy, uploads_root=str(uploads))

    out = tmp_path / "out"
    assert build_v2_set.main(["--live-db", str(copy), "--uploads", str(uploads),
                              "--output", str(out),
                              "--restore-uploads-root", str(uploads)]) == \
        build_v2_set.EXIT_FAILED


def test_the_oracle_detects_a_missing_restored_original(tmp_path, endpoint):
    db, uploads, info = endpoint
    out = tmp_path / "backups" / "ok"
    assert build_v2_set.main(["--live-db", str(db), "--uploads", str(uploads),
                              "--output", str(out),
                              "--restore-uploads-root", str(uploads)]) == 0
    manifest = json.loads((out / "backup_manifest.json").read_text())
    restore_root = tmp_path / "restore" / "uploads"
    restored_db = extract_set(out, restore_root)
    victim = sorted(oracle_required_originals(
        restored_db, uploads_root=manifest["restore_uploads_root"]))[0]
    (restore_root / victim).unlink()
    with pytest.raises(OracleError):
        validate_restored(restored_db, restore_root, info["uploads"],
                          manifest["restore_uploads_root"])
